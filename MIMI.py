"""MIMI orchestration entry point.

The harness owns paths and state.  Agents define plans, structured tasks,
documentation, and scientific verdicts; Codex edits the real product workspace.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import mimetypes
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from agents import Runner
from openai_codex import AsyncCodex, CodexConfig

from MIMI_agents import Documentation_agent, Planner_agent, VerificationResult, Verifier_agent
from MIMI_codex import CodexTaskSession, review_root_cause
from MIMI_credentials import configure_openai_api_key
from MIMI_dashboard import (
    add_activity,
    add_coder_run,
    append_planner_output,
    artifact_snapshot,
    reset_web_state,
    serve_web,
    set_progress,
    task_breaker_payload_to_subtasks,
    update_subtask_status,
    update_web_state,
)
from MIMI_inputs import MIMIInputBundle, build_prompt_with_reference_image, default_input_bundle
from MIMI_models import (
    AgentModelConfig,
    apply_agent_models,
    model_options_for_agent,
    restore_agent_models,
    selected_model,
    selected_settings,
)
from MIMI_revision import (
    build_failure_report,
    coding_task_count,
    revise_plan_continuation,
    run_task_breaker_on_plan,
    stream_text_response,
)
from MIMI_workspace import (
    RunPaths,
    WorkspaceContractError,
    build_code_index,
    collect_artifacts,
    copy_input,
    initialize_workspace,
    materialize_tasks,
    read_json,
    render_code_index,
    resolve_entrypoint,
    run_workspace_entrypoint,
    sha256_file,
    source_bundle,
    write_json,
)


PROJECT_ROOT = Path(__file__).resolve().parent
AGENTS_OUTPUT_ROOT = PROJECT_ROOT / "agents_output"
RUN_CONTROL_LOCK = threading.Lock()
RUN_MANIFEST_LOCK = threading.Lock()
ACTIVE_RUN_LOOP = None
ACTIVE_RUN_TASK = None
ACTIVE_RUN_ROOT: Path | None = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def write_run_manifest(path: Path, manifest: dict[str, Any]) -> None:
    with RUN_MANIFEST_LOCK:
        write_json(path, manifest)


def resolve_bundle_paths(bundle: MIMIInputBundle, base_dir: Path) -> MIMIInputBundle:
    def resolve(path: Path | None) -> Path | None:
        if path is None:
            return None
        expanded = Path(path).expanduser()
        return expanded.resolve() if expanded.is_absolute() else (base_dir / expanded).resolve()

    bundle.spec_path = resolve(bundle.spec_path)
    bundle.background_path = resolve(bundle.background_path)
    bundle.image_path = resolve(bundle.image_path)
    bundle.image_paths = [resolve(path) for path in (bundle.image_paths or [])]
    bundle.resume_plan_path = resolve(bundle.resume_plan_path)
    bundle.resume_task_breaker_path = resolve(bundle.resume_task_breaker_path)
    return bundle


def _copy_run_inputs(bundle: MIMIInputBundle, paths: RunPaths) -> None:
    legacy_task_files: list[Path] = []
    if bundle.resume_task_breaker_path:
        manifest_source = Path(bundle.resume_task_breaker_path).resolve()
        try:
            raw_manifest = json.loads(manifest_source.read_text(encoding="utf-8"))
            payload = raw_manifest[0] if isinstance(raw_manifest, list) and raw_manifest else raw_manifest
            if isinstance(payload, dict):
                for task in payload.get("tasks", []):
                    if not isinstance(task, dict) or task.get("instructions"):
                        continue
                    name = Path(str(task.get("sub_filename") or "")).name
                    candidate = manifest_source.parent / name
                    if name not in {"", ".", ".."} and candidate.is_file():
                        legacy_task_files.append(candidate)
        except (OSError, ValueError, json.JSONDecodeError):
            # _prepare_tasks emits the user-facing manifest validation error.
            pass

    bundle.spec_path = copy_input(bundle.spec_path, paths.inputs, "spec")
    bundle.background_path = copy_input(bundle.background_path, paths.inputs, "background")
    copied_images = [
        copy_input(image_path, paths.inputs, f"reference_{index}")
        for index, image_path in enumerate(bundle.image_paths or [], start=1)
    ]
    bundle.image_paths = [path for path in copied_images if path is not None]
    bundle.image_path = bundle.image_paths[0] if bundle.image_paths else None
    bundle.resume_plan_path = copy_input(bundle.resume_plan_path, paths.inputs, "resume_plan")
    bundle.resume_task_breaker_path = copy_input(
        bundle.resume_task_breaker_path, paths.inputs, "resume_tasks"
    )
    for task_file in legacy_task_files:
        destination = paths.inputs / task_file.name
        destination.write_bytes(task_file.read_bytes())


def _agent_tokens(result: Any) -> int:
    total = 0
    for response in getattr(result, "raw_responses", []) or []:
        usage = getattr(response, "usage", None)
        total += int(getattr(usage, "total_tokens", 0) or 0)
    return total


async def _await_with_activity(
    awaitable,
    *,
    agent: str,
    message: str,
    task_id: str | None = None,
    attempt: int | None = None,
    interval_seconds: float = 30.0,
):
    """Await a long agent call while proving liveness in the GUI and terminal."""

    future = asyncio.ensure_future(awaitable)
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        while True:
            done, _ = await asyncio.wait({future}, timeout=interval_seconds)
            if future in done:
                return await future
            elapsed = max(1, int(loop.time() - started))
            add_activity(
                agent,
                f"{message} ({elapsed}s elapsed)",
                status="running",
                task_id=task_id,
                attempt=attempt,
            )
    except BaseException:
        if not future.done():
            future.cancel()
        await asyncio.gather(future, return_exceptions=True)
        raise


def _task_text(task: dict[str, Any]) -> str:
    return str(task.get("instructions") or "").strip()


def _verifier_json(result: VerificationResult) -> str:
    return result.model_dump_json(indent=2)


def _synthetic_failure(message: str, relevant_files: list[str] | None = None) -> VerificationResult:
    return VerificationResult(
        verdict="fail",
        key_numbers=[],
        key_equations=[],
        insights=["The deterministic execution or artifact contract failed before physics validation."],
        predicted_properties=[],
        feedback=[message],
        failure_modes=[message],
        relevant_files=relevant_files or [],
    )


def _verification_prompt(task: str, artifacts) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": (
                "Verify these artifacts against the task contract. The harness already checked "
                "that the manifest and paths are valid.\n\nTASK CONTRACT:\n" + task
            ),
        }
    ]
    for path, description in zip(artifacts.plot_paths, artifacts.plot_descriptions):
        image_path = Path(path)
        mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        content.append({"type": "input_text", "text": f"Plot: {description}"})
        content.append(
            {
                "type": "input_image",
                "detail": "auto",
                "image_url": f"data:{mime};base64,{encoded}",
            }
        )
    for path, description in zip(artifacts.text_paths, artifacts.text_descriptions):
        text = Path(path).read_text(encoding="utf-8", errors="replace")[:12_000]
        content.append(
            {
                "type": "input_text",
                "text": f"Text artifact: {description}\n--- BEGIN ---\n{text}\n--- END ---",
            }
        )
    return [{"role": "user", "content": content}]


def _load_annotations(paths: RunPaths) -> dict[str, dict[str, Any]]:
    value = read_json(paths.reports / "documentation_annotations.json", {})
    return value if isinstance(value, dict) else {}


def _refresh_deterministic_index(paths: RunPaths) -> dict[str, Any]:
    index = build_code_index(paths.workspace, annotations=_load_annotations(paths))
    write_json(paths.code_index, index)
    return index


async def _document_accepted_change(
    paths: RunPaths,
    task: dict[str, Any],
    changed_files: list[str],
) -> int:
    """Enrich the deterministic code index after, and only after, acceptance."""

    source_paths = [
        relative
        for relative in sorted(set(changed_files))
        if relative != "AGENTS.md"
        and (paths.workspace / relative).is_file()
        and (paths.workspace / relative).suffix.lower()
        in {".py", ".json", ".md", ".toml", ".yaml", ".yml"}
    ]
    if not source_paths:
        _refresh_deterministic_index(paths)
        return 0

    deterministic_index = build_code_index(paths.workspace)
    prompt = (
        "TASK CONTRACT:\n"
        + _task_text(task)
        + "\n\nDETERMINISTIC INDEX:\n"
        + render_code_index(deterministic_index)
        + "\n\nACCEPTED CHANGED SOURCES:\n"
        + source_bundle(paths.workspace, source_paths)
    )
    result = await Runner.run(Documentation_agent, prompt)
    allowed = set(source_paths)
    annotations = _load_annotations(paths)
    for relative in allowed:
        annotations.pop(relative, None)
    for record in result.final_output.files:
        relative = Path(record.path).as_posix().lstrip("/")
        source_path = (paths.workspace / relative).resolve()
        if relative not in allowed or not source_path.is_file():
            continue
        annotations[relative] = {
            "sha256": sha256_file(source_path),
            "purpose": record.purpose,
            "public_interfaces": [item.model_dump() for item in record.public_interfaces],
            "dependencies": record.dependencies,
            "artifacts": record.artifacts,
            "caveats": record.caveats,
        }
    write_json(paths.reports / "documentation_annotations.json", annotations)
    _refresh_deterministic_index(paths)
    return _agent_tokens(result)


async def _prepare_tasks(bundle: MIMIInputBundle, paths: RunPaths) -> tuple[list[dict], str, int]:
    token_used = 0
    spec = bundle.load_spec()
    knowledge = bundle.load_background()

    if bundle.resume_task_breaker_path:
        update_web_state(phase="resume", message="Loading the supplied structured task manifest.")
        add_activity(
            "MIMI",
            f"Loading Level 3 task manifest {bundle.resume_task_breaker_path.name}.",
            status="running",
        )
        raw = json.loads(bundle.resume_task_breaker_path.read_text(encoding="utf-8"))
        payload = raw[0] if isinstance(raw, list) and raw else raw
        if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
            raise ValueError("Resume Task Breaker JSON must contain a top-level tasks list.")
        tasks = materialize_tasks(
            payload["tasks"],
            paths.tasks,
            source_dirs=[bundle.resume_task_breaker_path.parent],
            prefix="resume_task",
        )
        active_plan = spec
        update_web_state(
            planner_output="Resume mode: Planner and Task Breaker were skipped.",
            message=f"Loaded {len(tasks)} structured tasks.",
        )
        add_activity(
            "MIMI",
            f"Loaded {len(tasks)} tasks; Planner and Task Breaker were skipped.",
            status="complete",
        )
    else:
        if bundle.resume_plan_path:
            active_plan = bundle.resume_plan_path.read_text(encoding="utf-8", errors="replace")
            update_web_state(
                phase="plan_resume",
                planner_output=active_plan,
                message="Planner skipped. Task Breaker is shaping the supplied plan.",
            )
            add_activity(
                "MIMI",
                f"Loaded Level 2 plan {bundle.resume_plan_path.name}; Planner was skipped.",
                status="complete",
            )
        else:
            prompt = "TASK SPECIFICATION:\n" + spec
            if knowledge:
                prompt += "\n\nBACKGROUND KNOWLEDGE:\n" + knowledge
            update_web_state(phase="planner", message="Planner working.")
            add_activity("Planner (API agent)", "Started building the implementation plan.", status="running")
            planning = Runner.run_streamed(
                starting_agent=Planner_agent,
                input=build_prompt_with_reference_image(prompt, bundle),
            )
            await _await_with_activity(
                stream_text_response(planning),
                agent="Planner (API agent)",
                message="Planner is still running",
            )
            active_plan = str(planning.final_output)
            token_used += _agent_tokens(planning)
            (paths.reports / "planner_output.md").write_text(
                active_plan + "\n", encoding="utf-8", newline="\n"
            )
            update_web_state(
                planner_output=active_plan,
                message="Planner finished. Task Breaker is shaping structured work.",
            )
            add_activity("Planner (API agent)", "Finished the implementation plan.", status="complete")

        update_web_state(phase="task_breaker", message="Task Breaker working.")
        add_activity("Task Breaker (API agent)", "Started creating structured subtasks.", status="running")
        task_breaker = await _await_with_activity(
            run_task_breaker_on_plan(active_plan),
            agent="Task Breaker (API agent)",
            message="Task Breaker is still running",
        )
        token_used += _agent_tokens(task_breaker)
        payload = task_breaker.final_output.model_dump()
        tasks = materialize_tasks(payload.get("tasks", []), paths.tasks, prefix="task")
        add_activity(
            "Task Breaker (API agent)",
            f"Finished with {len(tasks)} structured tasks.",
            status="complete",
        )

    write_json(paths.tasks / "task_manifest.json", {"tasks": tasks})
    if not tasks:
        raise RuntimeError("Task Breaker returned no tasks.")
    dashboard_payload = {"tasks": tasks}
    update_web_state(
        task_breaker={
            "raw": json.dumps(dashboard_payload, indent=2, ensure_ascii=False),
            "subtasks": task_breaker_payload_to_subtasks(dashboard_payload),
        },
        message=f"Prepared {len(tasks)} tasks in deterministic task paths.",
    )
    add_activity("MIMI", f"Queued {coding_task_count(tasks)} coding tasks for execution.")
    return tasks, active_plan, token_used


async def main(bundle: MIMIInputBundle | None = None):
    configure_openai_api_key(required=True)
    bundle = resolve_bundle_paths(bundle or default_input_bundle(), Path.cwd())
    original_models = apply_agent_models(bundle.model_config)
    try:
        return await run_pipeline(bundle)
    finally:
        restore_agent_models(original_models)


async def run_pipeline(bundle: MIMIInputBundle) -> Path:
    global ACTIVE_RUN_ROOT
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    paths = RunPaths.create(AGENTS_OUTPUT_ROOT, timestamp)
    with RUN_CONTROL_LOCK:
        ACTIVE_RUN_ROOT = paths.root
    initialize_workspace(paths)
    _copy_run_inputs(bundle, paths)
    update_web_state(output_dir=str(paths.root))

    if bundle.start_subtask_number > 1:
        raise ValueError(
            "Starting after task 1 requires a prior product workspace, which this input route "
            "does not supply. Resume from task 1 so MIMI can reconstruct owned code."
        )

    manifest: dict[str, Any] = {
        "schema_version": 2,
        "run_id": paths.root.name,
        "status": "running",
        "paths": {
            "inputs": str(paths.inputs),
            "workspace": str(paths.workspace),
            "tasks": str(paths.tasks),
            "artifacts": str(paths.artifacts),
            "reports": str(paths.reports),
            "code_index": str(paths.code_index),
        },
        "models": {
            key: selected_model(bundle.model_config, key)
            for key in ("planner", "task_breaker", "coder", "verifier", "documentation")
        },
        "tasks": [],
        "token_usage": {
            "total": 0,
            "planning": 0,
            "codex": 0,
            "verifier": 0,
            "root_cause": 0,
            "documentation": 0,
        },
    }
    write_run_manifest(paths.manifest, manifest)
    print(f"MIMI run directory: {paths.root}")
    add_activity("MIMI", f"Created run directory {paths.root}.")

    tasks, active_plan, token_used = await _prepare_tasks(bundle, paths)
    manifest["token_usage"]["planning"] = token_used
    manifest["token_usage"]["total"] = token_used
    write_run_manifest(paths.manifest, manifest)
    _refresh_deterministic_index(paths)
    total_coding_tasks = coding_task_count(tasks)
    set_progress(0, total_coding_tasks, f"0 of {total_coding_tasks} coding tasks complete")

    coder_model = selected_model(bundle.model_config, "coder")
    coder_settings = selected_settings(bundle.model_config, "coder")
    coder_effort = coder_settings.get("reasoning_effort")
    if coder_effort in {None, "default", "none", "minimal", "max"}:
        coder_effort = None

    completed: list[dict[str, Any]] = []
    completed_coding = 0
    task_index = 0
    revision_count = 0

    async with AsyncCodex(CodexConfig(cwd=str(paths.workspace))) as codex:
        while task_index < len(tasks):
            task = tasks[task_index]
            if task.get("is_Coding_Team_required") is not True:
                add_activity(
                    "MIMI",
                    "Recorded this task as context only; no coding run is required.",
                    status="complete",
                    task_id=str(task["task_id"]),
                )
                completed.append(task)
                manifest["tasks"].append(
                    {"task_id": task["task_id"], "status": "context_only", "attempts": []}
                )
                task_index += 1
                write_run_manifest(paths.manifest, manifest)
                continue

            task_number = int(task["task_number"])
            task_id = str(task["task_id"])
            task_text = _task_text(task)
            set_progress(
                completed_coding,
                total_coding_tasks,
                f"Working on coding task {completed_coding + 1} of {total_coding_tasks}",
            )
            update_web_state(phase="coder", message=f"Codex is implementing {task_id}.")
            update_subtask_status(
                task_id,
                "starting",
                attempt_count=0,
                max_attempts=bundle.max_subtask_attempts,
            )

            session = CodexTaskSession(
                codex,
                paths.workspace,
                model=coder_model,
                effort=coder_effort,
            )
            attempts: list[dict[str, Any]] = []
            task_changed: set[str] = set()
            prior_failure = ""
            verifier_feedback = ""
            root_cause: dict[str, Any] | None = None
            accepted = False

            for attempt_number in range(1, bundle.max_subtask_attempts + 1):
                code_index = _refresh_deterministic_index(paths)
                coding_status = "coding" if attempt_number == 1 else "repairing"
                task["status"] = coding_status
                task["attempt_count"] = attempt_number
                task["max_attempts"] = bundle.max_subtask_attempts
                update_subtask_status(
                    task_id,
                    coding_status,
                    attempt_count=attempt_number,
                    max_attempts=bundle.max_subtask_attempts,
                    detail="Codex is editing the product workspace.",
                )
                add_activity(
                    "Coder (Codex)",
                    (
                        "Started implementing the task."
                        if attempt_number == 1
                        else "Started a repair using the previous failure evidence."
                    ),
                    status="running",
                    task_id=task_id,
                    attempt=attempt_number,
                )
                if attempt_number == 1:
                    change = await _await_with_activity(
                        session.implement(task_text, code_index),
                        agent="Coder (Codex)",
                        message="Coder is still running",
                        task_id=task_id,
                        attempt=attempt_number,
                    )
                else:
                    # Same persistent Codex thread: it can inspect and modify its current code.
                    change = await _await_with_activity(
                        session.repair(
                            task=task_text,
                            failure=prior_failure,
                            verifier_feedback=verifier_feedback,
                            root_cause=root_cause,
                        ),
                        agent="Coder (Codex)",
                        message="Coder repair is still running",
                        task_id=task_id,
                        attempt=attempt_number,
                    )
                token_used += change.total_tokens
                manifest["token_usage"]["codex"] += change.total_tokens
                task_changed.update(change.changed_files)
                attempt_dir = paths.attempt_dir(task_id, attempt_number)
                update_subtask_status(
                    task_id,
                    "executing",
                    attempt_count=attempt_number,
                    max_attempts=bundle.max_subtask_attempts,
                    detail="Running the generated entrypoint and collecting artifacts.",
                )
                add_activity(
                    "Coder (Codex)",
                    f"Finished editing {len(change.changed_files)} file(s); entrypoint is {change.entrypoint}.",
                    status="complete",
                    task_id=task_id,
                    attempt=attempt_number,
                )
                add_activity(
                    "Execution",
                    f"Running {change.entrypoint} and collecting its outputs.",
                    status="running",
                    task_id=task_id,
                    attempt=attempt_number,
                )

                execution = None
                artifacts = None
                deterministic_failure = ""
                try:
                    entrypoint = resolve_entrypoint(
                        paths.workspace, change.entrypoint, sorted(task_changed)
                    )
                    execution = run_workspace_entrypoint(
                        paths.workspace, entrypoint, attempt_dir
                    )
                    if execution.returncode != 0:
                        deterministic_failure = (
                            f"Entrypoint {execution.entrypoint} exited with code "
                            f"{execution.returncode}. stderr:\n{execution.stderr[-8000:]}"
                        )
                    else:
                        artifacts = collect_artifacts(attempt_dir)
                except (OSError, ValueError, json.JSONDecodeError, WorkspaceContractError) as exc:
                    deterministic_failure = f"{type(exc).__name__}: {exc}"

                execution_status = (
                    f"failed: {deterministic_failure.splitlines()[0]}"
                    if deterministic_failure
                    else f"finished with return code {execution.returncode}"
                )
                add_activity(
                    "Execution",
                    execution_status,
                    status="failed" if deterministic_failure else "complete",
                    task_id=task_id,
                    attempt=attempt_number,
                )

                if deterministic_failure:
                    verdict = _synthetic_failure(deterministic_failure, change.changed_files)
                    verifier_tokens = 0
                else:
                    update_web_state(phase="verifier", message=f"Verifier checking {task_id}.")
                    update_subtask_status(
                        task_id,
                        "verifying",
                        attempt_count=attempt_number,
                        max_attempts=bundle.max_subtask_attempts,
                        detail="The API verifier is checking the produced artifacts.",
                    )
                    add_activity(
                        "Verifier (API agent)",
                        "Started checking the implementation and artifacts.",
                        status="running",
                        task_id=task_id,
                        attempt=attempt_number,
                    )
                    verification = await _await_with_activity(
                        Runner.run(Verifier_agent, _verification_prompt(task_text, artifacts)),
                        agent="Verifier (API agent)",
                        message="Verifier is still running",
                        task_id=task_id,
                        attempt=attempt_number,
                    )
                    verdict = verification.final_output
                    verifier_tokens = _agent_tokens(verification)
                    token_used += verifier_tokens
                    manifest["token_usage"]["verifier"] += verifier_tokens
                    add_activity(
                        "Verifier (API agent)",
                        f"Finished with verdict: {verdict.verdict}.",
                        status="complete" if verdict.verdict == "pass" else "failed",
                        task_id=task_id,
                        attempt=attempt_number,
                    )

                verifier_feedback = _verifier_json(verdict)
                prior_failure = deterministic_failure or "\n".join(verdict.failure_modes + verdict.feedback)
                root_cause = None
                review_tokens = 0
                if verdict.verdict != "pass":
                    # A distinct read-only Codex thread sees the implementation only once a
                    # concrete failure mode exists.
                    update_web_state(
                        phase="root_cause", message=f"Reviewing the failure mode in {task_id}."
                    )
                    update_subtask_status(
                        task_id,
                        "diagnosing",
                        attempt_count=attempt_number,
                        max_attempts=bundle.max_subtask_attempts,
                        detail="A separate read-only Codex reviewer is diagnosing the failure.",
                    )
                    add_activity(
                        "Root Cause (Codex, read-only)",
                        "Started diagnosing the failed attempt.",
                        status="running",
                        task_id=task_id,
                        attempt=attempt_number,
                    )
                    root_cause, review_tokens = await _await_with_activity(
                        review_root_cause(
                            codex,
                            paths.workspace,
                            model=coder_model,
                            effort=coder_effort,
                            task=task_text,
                            failure=prior_failure,
                            verifier_feedback=verifier_feedback,
                            changed_files=sorted(task_changed),
                            code_index=_refresh_deterministic_index(paths),
                        ),
                        agent="Root Cause (Codex, read-only)",
                        message="Root-cause review is still running",
                        task_id=task_id,
                        attempt=attempt_number,
                    )
                    token_used += review_tokens
                    manifest["token_usage"]["root_cause"] += review_tokens
                    add_activity(
                        "Root Cause (Codex, read-only)",
                        "Finished diagnosing the failed attempt.",
                        status="complete",
                        task_id=task_id,
                        attempt=attempt_number,
                    )

                artifact_view = (
                    artifact_snapshot(
                        artifacts.plot_paths,
                        artifacts.plot_descriptions,
                        artifacts.text_paths,
                        artifacts.text_descriptions,
                    )
                    if artifacts
                    else {"images": [], "texts": []}
                )
                attempt_record = {
                    "attempt": attempt_number,
                    "thread_id": change.thread_id,
                    "changed_files": change.changed_files,
                    "summary": change.summary,
                    "entrypoint": change.entrypoint,
                    "tests_run": change.tests_run,
                    "return_code": execution.returncode if execution else None,
                    "stdout": (execution.stdout if execution else "")[-20_000:],
                    "stderr": (
                        execution.stderr if execution else deterministic_failure
                    )[-20_000:],
                    "verdict": verdict.model_dump(),
                    "root_cause": root_cause,
                    "artifacts": (
                        {
                            "manifest_path": artifacts.manifest_path,
                            "summary": artifacts.summary,
                            "plots": artifacts.plot_paths,
                            "texts": artifacts.text_paths,
                        }
                        if artifacts
                        else None
                    ),
                    "token_usage": {
                        "codex": change.total_tokens,
                        "verifier": verifier_tokens,
                        "root_cause": review_tokens,
                    },
                }
                attempts.append(attempt_record)
                add_coder_run(
                    {
                        "subtask_number": task_number,
                        "attempt": attempt_number,
                        "subtask": task_text,
                        "code_path": change.entrypoint,
                        "coder_output": change.summary,
                        "verifier_output": verifier_feedback,
                        "verifier_verdict": verdict.verdict,
                        "return_code": attempt_record["return_code"],
                        "stdout": attempt_record["stdout"],
                        "stderr": attempt_record["stderr"],
                        "artifacts": artifact_view,
                        "root_cause": root_cause,
                    }
                )

                if verdict.verdict == "pass":
                    accepted = True
                    break

            if accepted:
                update_web_state(
                    phase="documentation",
                    message=f"Documenting accepted changes for {task_id}.",
                )
                update_subtask_status(
                    task_id,
                    "documenting",
                    attempt_count=len(attempts),
                    max_attempts=bundle.max_subtask_attempts,
                    detail="The API documentation agent is recording accepted interfaces.",
                )
                add_activity(
                    "Documentation (API agent)",
                    "Started documenting the accepted changes.",
                    status="running",
                    task_id=task_id,
                )
                documentation_tokens = await _await_with_activity(
                    _document_accepted_change(paths, task, sorted(task_changed)),
                    agent="Documentation (API agent)",
                    message="Documentation is still running",
                    task_id=task_id,
                )
                token_used += documentation_tokens
                manifest["token_usage"]["documentation"] += documentation_tokens
                task["status"] = "accepted"
                update_subtask_status(
                    task_id,
                    "accepted",
                    attempt_count=len(attempts),
                    max_attempts=bundle.max_subtask_attempts,
                    detail="Implementation, execution, verification, and documentation passed.",
                )
                add_activity(
                    "Documentation (API agent)",
                    "Finished documentation; task accepted.",
                    status="complete",
                    task_id=task_id,
                )
                completed.append(task)
                completed_coding += 1
                manifest["tasks"].append(
                    {
                        "task_id": task_id,
                        "status": "accepted",
                        "thread_id": attempts[-1]["thread_id"],
                        "changed_files": sorted(task_changed),
                        "documentation_tokens": documentation_tokens,
                        "attempts": attempts,
                    }
                )
                manifest["token_usage"]["total"] = token_used
                write_run_manifest(paths.manifest, manifest)
                task_index += 1
                set_progress(
                    completed_coding,
                    total_coding_tasks,
                    f"{completed_coding} of {total_coding_tasks} coding tasks complete",
                )
                continue

            if revision_count >= bundle.max_plan_revisions:
                task["status"] = "failed"
                update_subtask_status(
                    task_id,
                    "failed",
                    attempt_count=len(attempts),
                    max_attempts=bundle.max_subtask_attempts,
                    detail="The task exhausted its attempts and plan revisions.",
                )
                add_activity(
                    "MIMI",
                    "Task failed after exhausting its attempts and plan revisions.",
                    status="failed",
                    task_id=task_id,
                )
                manifest["status"] = "failed"
                manifest["token_usage"]["total"] = token_used
                manifest["tasks"].append(
                    {"task_id": task_id, "status": "failed", "attempts": attempts}
                )
                write_run_manifest(paths.manifest, manifest)
                raise RuntimeError(
                    f"{task_id} failed after {bundle.max_subtask_attempts} attempts and "
                    f"{bundle.max_plan_revisions} plan revisions."
                )

            revision_count += 1
            task["status"] = "replanning"
            update_subtask_status(
                task_id,
                "replanning",
                attempt_count=len(attempts),
                max_attempts=bundle.max_subtask_attempts,
                detail="The API planner is revising this task and the remaining plan.",
            )
            add_activity(
                "Planner (API agent)",
                f"Started plan revision {revision_count} after the failed task.",
                status="running",
                task_id=task_id,
            )
            manifest["tasks"].append(
                {
                    "task_id": task_id,
                    "status": "superseded_after_failure",
                    "revision": revision_count,
                    "attempts": attempts,
                }
            )
            manifest["token_usage"]["total"] = token_used
            write_run_manifest(paths.manifest, manifest)
            failure_report = build_failure_report(
                subtask_number=task_number,
                subtask_text=task_text,
                attempts=attempts,
                completed_tasks=completed,
                remaining_tasks=tasks[task_index:],
            )
            (paths.reports / f"failure_revision_{revision_count}.json").write_text(
                failure_report, encoding="utf-8", newline="\n"
            )
            revision = await _await_with_activity(
                revise_plan_continuation(
                    bundle=bundle,
                    original_plan=active_plan,
                    failure_report=failure_report,
                    failed_subtask_number=task_number,
                    revision_number=revision_count,
                ),
                agent="Planner (API agent)",
                message="Planner recovery is still running",
                task_id=task_id,
            )
            token_used += _agent_tokens(revision)
            manifest["token_usage"]["planning"] += _agent_tokens(revision)
            revised_plan = str(revision.final_output)
            active_plan += f"\n\n--- REVISION {revision_count} ---\n{revised_plan}"
            append_planner_output(f"Revision {revision_count}", revised_plan)
            add_activity(
                "Planner (API agent)",
                f"Finished plan revision {revision_count}.",
                status="complete",
                task_id=task_id,
            )

            add_activity(
                "Task Breaker (API agent)",
                "Started creating replacement subtasks for the revised plan.",
                status="running",
                task_id=task_id,
            )
            replacement = await _await_with_activity(
                run_task_breaker_on_plan(
                    "Create only replacement tasks for this revised continuation.\n\n"
                    + revised_plan
                    + "\n\nFAILURE EVIDENCE:\n"
                    + failure_report
                ),
                agent="Task Breaker (API agent)",
                message="Task Breaker recovery is still running",
                task_id=task_id,
            )
            token_used += _agent_tokens(replacement)
            manifest["token_usage"]["planning"] += _agent_tokens(replacement)
            raw_replacements = replacement.final_output.model_dump().get("tasks", [])
            if not raw_replacements:
                raise RuntimeError("Task Breaker returned no replacement tasks.")
            replacement_tasks = materialize_tasks(
                raw_replacements,
                paths.tasks,
                prefix=f"revision_{revision_count}",
                start_number=task_number,
            )
            add_activity(
                "Task Breaker (API agent)",
                f"Created {len(replacement_tasks)} replacement tasks.",
                status="complete",
                task_id=task_id,
            )
            tasks = tasks[:task_index] + replacement_tasks
            total_coding_tasks = completed_coding + coding_task_count(tasks[task_index:])
            write_json(paths.tasks / "task_manifest.json", {"tasks": tasks})
            update_web_state(
                task_breaker={
                    "raw": json.dumps({"tasks": tasks}, indent=2, ensure_ascii=False),
                    "subtasks": task_breaker_payload_to_subtasks({"tasks": tasks}),
                },
                message=f"Replaced {task_id} and downstream tasks after revision.",
            )

    manifest["status"] = "complete"
    manifest["token_usage"]["total"] = token_used
    write_run_manifest(paths.manifest, manifest)
    update_web_state(
        phase="complete",
        message=f"MIMI completed {completed_coding} coding tasks.",
        output_dir=str(paths.root),
    )
    add_activity("MIMI", f"Completed {completed_coding} coding tasks.", status="complete")
    return paths.root


def request_abort() -> bool:
    """Cancel the active asynchronous run from the browser UI."""

    global ACTIVE_RUN_LOOP, ACTIVE_RUN_TASK
    with RUN_CONTROL_LOCK:
        loop = ACTIVE_RUN_LOOP
        task = ACTIVE_RUN_TASK
        if loop is None or task is None or task.done():
            return False
        try:
            loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:
            return False
    update_web_state(phase="stopping", message="Stopping the active MIMI run.")
    add_activity("MIMI", "Abort requested; stopping the active run.", status="running")
    return True


def mark_active_run_terminated(status: str, reason: str) -> Path | None:
    """Persist a terminal status before the launcher exits forcibly."""

    with RUN_CONTROL_LOCK:
        run_root = ACTIVE_RUN_ROOT
    if run_root is None:
        return None

    manifest_path = run_root / "run_manifest.json"
    with RUN_MANIFEST_LOCK:
        manifest = read_json(manifest_path, {})
        if not isinstance(manifest, dict) or manifest.get("status") != "running":
            return run_root

        manifest["status"] = status
        manifest["termination_reason"] = reason
        manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
        write_json(manifest_path, manifest)
    return run_root


def run_bundle_in_background(bundle: MIMIInputBundle) -> None:
    global ACTIVE_RUN_LOOP, ACTIVE_RUN_TASK, ACTIVE_RUN_ROOT
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(main(bundle))
    with RUN_CONTROL_LOCK:
        ACTIVE_RUN_LOOP = loop
        ACTIVE_RUN_TASK = task
    try:
        reset_web_state(f"Agents running with inputs from {bundle.spec_path.parent}")
        output_dir = loop.run_until_complete(task)
        update_web_state(
            running=False,
            phase="complete",
            message=f"Agent run finished. Files saved in {output_dir}.",
            output_dir=str(output_dir),
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
    except asyncio.CancelledError:
        mark_active_run_terminated("aborted", "The active run was cancelled.")
        update_web_state(
            running=False,
            phase="aborted",
            message="Run aborted.",
            finished_at=datetime.now().isoformat(timespec="seconds"),
            error=None,
        )
        add_activity("MIMI", "Run aborted.", status="failed")
    except Exception as exc:
        mark_active_run_terminated("failed", str(exc))
        update_web_state(
            running=False,
            phase="error",
            message=f"Agent run failed: {exc}",
            finished_at=datetime.now().isoformat(timespec="seconds"),
            error=str(exc),
        )
        add_activity("MIMI", f"Agent run failed: {exc}", status="failed")
        print(f"MIMI run failed: {exc}", file=sys.stderr)
    finally:
        with RUN_CONTROL_LOCK:
            if ACTIVE_RUN_TASK is task:
                ACTIVE_RUN_LOOP = None
                ACTIVE_RUN_TASK = None
                ACTIVE_RUN_ROOT = None
        pending = [item for item in asyncio.all_tasks(loop) if not item.done()]
        for item in pending:
            item.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        asyncio.set_event_loop(None)


def parse_args():
    parser = argparse.ArgumentParser(description="Run MIMI with Codex-owned product workspaces.")
    parser.add_argument("--web", action="store_true", help="Start the HTML interface.")
    parser.add_argument("--host", default="127.0.0.1", help="Host for --web mode.")
    parser.add_argument("--port", type=int, default=8080, help="Port for --web mode.")
    parser.add_argument("--spec", type=Path, default=Path("seal_analysis_tool_spec_v6.md"))
    parser.add_argument(
        "--background",
        type=Path,
        default=Path("seal_tool_background_knowledge_short.md"),
        help="Optional Markdown, text, or structured JSON background-knowledge file.",
    )
    parser.add_argument("--image", type=Path, default=None)
    parser.add_argument("--planner-output", type=Path, default=None)
    parser.add_argument("--task-breaker-json", type=Path, default=None)
    parser.add_argument("--start-subtask", type=int, default=1)
    parser.add_argument("--max-subtask-attempts", type=int, default=3)
    parser.add_argument("--max-plan-revisions", type=int, default=3)
    parser.add_argument(
        "--planner-model", choices=model_options_for_agent("planner"), default=None
    )
    parser.add_argument(
        "--task-breaker-model", choices=model_options_for_agent("task_breaker"), default=None
    )
    parser.add_argument("--coder-model", choices=model_options_for_agent("coder"), default=None)
    parser.add_argument(
        "--verifier-model", choices=model_options_for_agent("verifier"), default=None
    )
    parser.add_argument(
        "--documentation-model",
        choices=model_options_for_agent("documentation"),
        default=None,
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.web:
        serve_web(
            args.host,
            args.port,
            run_bundle_in_background,
            abort_run=request_abort,
            auto_shutdown=False,
        )
    else:
        background = args.background if args.background and args.background.exists() else None
        image = args.image if args.image and args.image.exists() else None
        model_config = AgentModelConfig(
            models={
                "planner": args.planner_model,
                "task_breaker": args.task_breaker_model,
                "coder": args.coder_model,
                "verifier": args.verifier_model,
                "documentation": args.documentation_model,
            }
        )
        asyncio.run(
            main(
                MIMIInputBundle(
                    spec_path=args.spec,
                    background_path=background,
                    image_path=image,
                    image_paths=[image] if image else [],
                    resume_plan_path=(
                        args.planner_output
                        if args.planner_output and args.planner_output.exists()
                        else None
                    ),
                    resume_task_breaker_path=(
                        args.task_breaker_json
                        if args.task_breaker_json and args.task_breaker_json.exists()
                        else None
                    ),
                    start_subtask_number=max(1, args.start_subtask),
                    max_subtask_attempts=args.max_subtask_attempts,
                    max_plan_revisions=args.max_plan_revisions,
                    model_config=model_config,
                )
            )
        )
