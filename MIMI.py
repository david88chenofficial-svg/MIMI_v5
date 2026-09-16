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
from MIMI_codex import CodexPlanChange, CodexPlanSession, CodexTaskResult
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


def _verification_prompt(
    task: str,
    artifacts,
    accepted_context: str = "",
) -> list[dict[str, Any]]:
    upstream = (
        "\n\nHASH-VALID DOCUMENTATION FROM ACCEPTED UPSTREAM STAGES:\n" + accepted_context
        if accepted_context
        else ""
    )
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": (
                "Verify these artifacts against the task contract. The harness already checked "
                "that the manifest and paths are valid. Treat upstream documentation as context, "
                "not as a substitute for evidence from this stage.\n\nTASK CONTRACT:\n"
                + task
                + upstream
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


def _coding_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [task for task in tasks if task.get("is_Coding_Team_required") is True]


def _task_record(manifest: dict[str, Any], task_id: str) -> dict[str, Any]:
    for record in manifest["tasks"]:
        if str(record.get("task_id")) == str(task_id):
            return record
    raise KeyError(f"Run manifest has no task record for {task_id}.")


def _safe_task_files(paths: RunPaths, result: CodexTaskResult) -> list[str]:
    """Keep only workspace files that actually exist after a Codex build."""

    index = build_code_index(paths.workspace)
    available = {str(item.get("path")) for item in index.get("files", [])}
    candidates = [str(path).replace("\\", "/") for path in result.files]
    candidates.append(str(result.entrypoint).replace("\\", "/"))
    return sorted({path for path in candidates if path in available})


def _accepted_context(paths: RunPaths) -> str:
    """Return compact, hash-valid documentation for an in-place Codex repair."""

    return render_code_index(_refresh_deterministic_index(paths), max_chars=40_000)


def _run_task_stage(
    paths: RunPaths,
    task: dict[str, Any],
    result: CodexTaskResult,
    *,
    attempt_number: int,
) -> dict[str, Any]:
    """Execute one task's validation entrypoint and collect deterministic evidence."""

    task_id = str(task["task_id"])
    attempt_dir = paths.attempt_dir(task_id, attempt_number)
    update_subtask_status(
        task_id,
        "executing",
        attempt_count=attempt_number,
        detail="Running this stage's validation entrypoint and collecting artifacts.",
    )
    add_activity(
        "Execution",
        f"Running {result.entrypoint} and collecting intermediate outputs.",
        status="running",
        task_id=task_id,
        attempt=attempt_number,
    )

    execution = None
    artifacts = None
    deterministic_failure = ""
    try:
        entrypoint = resolve_entrypoint(paths.workspace, result.entrypoint, result.files)
        execution = run_workspace_entrypoint(paths.workspace, entrypoint, attempt_dir)
        if execution.returncode != 0:
            deterministic_failure = (
                f"Entrypoint {execution.entrypoint} exited with code {execution.returncode}. "
                f"stderr:\n{execution.stderr[-8000:]}"
            )
        else:
            artifacts = collect_artifacts(attempt_dir)
    except (OSError, ValueError, json.JSONDecodeError, WorkspaceContractError) as exc:
        deterministic_failure = f"{type(exc).__name__}: {exc}"

    add_activity(
        "Execution",
        (
            f"failed: {deterministic_failure.splitlines()[0]}"
            if deterministic_failure
            else f"finished with return code {execution.returncode}"
        ),
        status="failed" if deterministic_failure else "complete",
        task_id=task_id,
        attempt=attempt_number,
    )
    return {
        "result": result,
        "execution": execution,
        "artifacts": artifacts,
        "deterministic_failure": deterministic_failure,
    }


async def _verify_task_stage(
    task: dict[str, Any],
    runtime: dict[str, Any],
    *,
    attempt_number: int,
    accepted_context: str,
) -> tuple[VerificationResult, int]:
    """Verify one stage, using a deterministic failure when execution never qualified."""

    task_id = str(task["task_id"])
    deterministic_failure = str(runtime.get("deterministic_failure") or "")
    result: CodexTaskResult = runtime["result"]
    if deterministic_failure:
        return _synthetic_failure(deterministic_failure, result.files), 0

    update_web_state(phase="verifier", message=f"Verifier checking {task_id}.")
    update_subtask_status(
        task_id,
        "verifying",
        attempt_count=attempt_number,
        detail="The verifier is checking this stage before MIMI proceeds downstream.",
    )
    add_activity(
        "Verifier (API agent)",
        "Started checking this stage's intermediate artifacts.",
        status="running",
        task_id=task_id,
        attempt=attempt_number,
    )
    verification = await _await_with_activity(
        Runner.run(
            Verifier_agent,
            _verification_prompt(
                _task_text(task),
                runtime["artifacts"],
                accepted_context,
            ),
        ),
        agent="Verifier (API agent)",
        message="Verifier is still running",
        task_id=task_id,
        attempt=attempt_number,
    )
    verdict = verification.final_output
    tokens = _agent_tokens(verification)
    add_activity(
        "Verifier (API agent)",
        f"Finished with verdict: {verdict.verdict}.",
        status="complete" if verdict.verdict == "pass" else "failed",
        task_id=task_id,
        attempt=attempt_number,
    )
    return verdict, tokens


def _attempt_record(
    runtime: dict[str, Any],
    *,
    attempt_number: int,
    thread_id: str,
    task_files: list[str],
) -> dict[str, Any]:
    execution = runtime.get("execution")
    artifacts = runtime.get("artifacts")
    result: CodexTaskResult = runtime["result"]
    return {
        "attempt": attempt_number,
        "thread_id": thread_id,
        "changed_files": task_files,
        "summary": result.summary,
        "entrypoint": result.entrypoint,
        "tests_run": result.tests_run,
        "return_code": execution.returncode if execution else None,
        "stdout": (execution.stdout if execution else "")[-20_000:],
        "stderr": (
            execution.stderr if execution else runtime.get("deterministic_failure", "")
        )[-20_000:],
        "verdict": None,
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
        "token_usage": {"verifier": 0},
    }


def _dashboard_attempt(
    task: dict[str, Any],
    runtime: dict[str, Any],
    attempt: dict[str, Any],
    verdict: VerificationResult,
) -> None:
    artifacts = runtime.get("artifacts")
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
    add_coder_run(
        {
            "subtask_number": int(task["task_number"]),
            "attempt": attempt["attempt"],
            "subtask": _task_text(task),
            "code_path": attempt["entrypoint"],
            "coder_output": attempt["summary"],
            "verifier_output": _verifier_json(verdict),
            "verifier_verdict": verdict.verdict,
            "return_code": attempt["return_code"],
            "stdout": attempt["stdout"],
            "stderr": attempt["stderr"],
            "artifacts": artifact_view,
        }
    )


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
        "schema_version": 3,
        "run_id": paths.root.name,
        "status": "running",
        "workflow": "whole_plan_codex_forward_verification",
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
        "codex_builds": [],
        "token_usage": {
            "total": 0,
            "planning": 0,
            "codex": 0,
            "verifier": 0,
            "documentation": 0,
        },
    }
    write_run_manifest(paths.manifest, manifest)
    print(f"MIMI run directory: {paths.root}")
    add_activity("MIMI", f"Created run directory {paths.root}.")

    tasks, active_plan, token_used = await _prepare_tasks(bundle, paths)
    manifest["token_usage"]["planning"] = token_used
    coding_tasks = _coding_tasks(tasks)
    for task in tasks:
        context_only = task.get("is_Coding_Team_required") is not True
        manifest["tasks"].append(
            {
                "task_id": str(task["task_id"]),
                "task_number": int(task["task_number"]),
                "status": "context_only" if context_only else "pending",
                "accepted_files": [],
                "documentation_tokens": 0,
                "attempts": [],
            }
        )
        if context_only:
            add_activity(
                "MIMI",
                "Recorded this task as context only; no validation entrypoint is required.",
                status="complete",
                task_id=str(task["task_id"]),
            )
    manifest["token_usage"]["total"] = token_used
    write_run_manifest(paths.manifest, manifest)
    _refresh_deterministic_index(paths)

    total_coding_tasks = len(coding_tasks)
    set_progress(0, total_coding_tasks, f"0 of {total_coding_tasks} coding stages accepted")
    if not coding_tasks:
        manifest["status"] = "complete"
        write_run_manifest(paths.manifest, manifest)
        update_web_state(phase="complete", message="MIMI completed the context-only plan.")
        return paths.root

    coder_model = selected_model(bundle.model_config, "coder")
    coder_settings = selected_settings(bundle.model_config, "coder")
    coder_effort = coder_settings.get("reasoning_effort")
    if coder_effort in {None, "default", "none", "minimal", "max"}:
        coder_effort = None

    task_results: dict[str, CodexTaskResult] = {}
    task_files: dict[str, list[str]] = {}
    latest_runtime: dict[str, dict[str, Any]] = {}
    failure_counts: dict[str, int] = {}
    revision_count = 0

    def apply_build(change: CodexPlanChange, start_index: int, kind: str) -> int:
        """Persist one Codex build and execute every affected validation stage."""

        for result in change.tasks:
            task_results[result.task_id] = result
            task_files[result.task_id] = _safe_task_files(paths, result)

        effective_start = start_index
        changed = set(change.changed_files)
        for index, earlier in enumerate(coding_tasks[:start_index]):
            record = _task_record(manifest, str(earlier["task_id"]))
            if record["status"] == "accepted" and changed.intersection(record["accepted_files"]):
                effective_start = min(effective_start, index)

        if effective_start < start_index:
            affected_id = str(coding_tasks[effective_start]["task_id"])
            add_activity(
                "MIMI",
                f"Repair changed accepted source; reverification will restart at {affected_id}.",
                status="info",
                task_id=affected_id,
            )

        manifest["codex_builds"].append(
            {
                "build": len(manifest["codex_builds"]) + 1,
                "kind": kind,
                "thread_id": change.thread_id,
                "summary": change.summary,
                "task_ids": [item.task_id for item in change.tasks],
                "changed_files": change.changed_files,
                "notes": change.notes,
                "tokens": change.total_tokens,
            }
        )

        for index in range(effective_start, len(coding_tasks)):
            task = coding_tasks[index]
            task_id = str(task["task_id"])
            if task_id not in task_results:
                raise RuntimeError(f"No Codex validation result is available for {task_id}.")
            record = _task_record(manifest, task_id)
            if record["status"] == "accepted":
                record["status"] = "pending_reverification"
                record["documentation_tokens"] = 0
            attempt_number = len(record["attempts"]) + 1
            task["status"] = "executing"
            task["attempt_count"] = attempt_number
            task["max_attempts"] = max(bundle.max_subtask_attempts, attempt_number)
            runtime = _run_task_stage(
                paths,
                task,
                task_results[task_id],
                attempt_number=attempt_number,
            )
            latest_runtime[task_id] = runtime
            record["status"] = "awaiting_verification"
            record["attempts"].append(
                _attempt_record(
                    runtime,
                    attempt_number=attempt_number,
                    thread_id=change.thread_id,
                    task_files=task_files.get(task_id, []),
                )
            )
            update_subtask_status(
                task_id,
                "awaiting verification",
                attempt_count=attempt_number,
                max_attempts=max(bundle.max_subtask_attempts, attempt_number),
                detail="Intermediate artifacts are ready for forward verification.",
            )

        manifest["token_usage"]["total"] = token_used
        write_run_manifest(paths.manifest, manifest)
        return effective_start

    async with AsyncCodex(CodexConfig(cwd=str(paths.workspace))) as codex:
        session = CodexPlanSession(
            codex,
            paths.workspace,
            model=coder_model,
            effort=coder_effort,
        )
        update_web_state(
            phase="coder",
            message="Codex is implementing the complete ordered plan in one continuous run.",
        )
        for task in coding_tasks:
            update_subtask_status(
                str(task["task_id"]),
                "building",
                attempt_count=0,
                max_attempts=bundle.max_subtask_attempts,
                detail="Included in the whole-plan Codex build.",
            )
        add_activity(
            "Coder (Codex)",
            f"Started one whole-plan build covering {len(coding_tasks)} coding stages.",
            status="running",
        )
        initial_change = await _await_with_activity(
            session.implement_plan(
                plan=active_plan,
                tasks=tasks,
                code_index=_refresh_deterministic_index(paths),
            ),
            agent="Coder (Codex)",
            message="Whole-plan Codex build is still running",
        )
        token_used += initial_change.total_tokens
        manifest["token_usage"]["codex"] += initial_change.total_tokens
        add_activity(
            "Coder (Codex)",
            f"Finished the whole-plan build and changed {len(initial_change.changed_files)} files.",
            status="complete",
        )
        verification_index = apply_build(initial_change, 0, "initial_whole_plan")

        while verification_index < len(coding_tasks):
            task = coding_tasks[verification_index]
            task_id = str(task["task_id"])
            record = _task_record(manifest, task_id)
            attempt = record["attempts"][-1]
            attempt_number = int(attempt["attempt"])
            set_progress(
                sum(
                    _task_record(manifest, str(item["task_id"]))["status"] == "accepted"
                    for item in coding_tasks
                ),
                len(coding_tasks),
                f"Verifying stage {verification_index + 1} of {len(coding_tasks)}",
            )
            verdict, verifier_tokens = await _verify_task_stage(
                task,
                latest_runtime[task_id],
                attempt_number=attempt_number,
                accepted_context=_accepted_context(paths),
            )
            token_used += verifier_tokens
            manifest["token_usage"]["verifier"] += verifier_tokens
            attempt["verdict"] = verdict.model_dump()
            attempt["token_usage"]["verifier"] = verifier_tokens
            _dashboard_attempt(task, latest_runtime[task_id], attempt, verdict)

            if verdict.verdict == "pass":
                update_web_state(
                    phase="documentation",
                    message=f"Documenting accepted stage {task_id}.",
                )
                update_subtask_status(
                    task_id,
                    "documenting",
                    attempt_count=attempt_number,
                    detail="Recording hash-bound interfaces before verifying the next stage.",
                )
                add_activity(
                    "Documentation (API agent)",
                    "Started documenting this accepted stage.",
                    status="running",
                    task_id=task_id,
                )
                documentation_tokens = await _await_with_activity(
                    _document_accepted_change(paths, task, task_files.get(task_id, [])),
                    agent="Documentation (API agent)",
                    message="Documentation is still running",
                    task_id=task_id,
                )
                token_used += documentation_tokens
                manifest["token_usage"]["documentation"] += documentation_tokens
                record["status"] = "accepted"
                record["accepted_files"] = task_files.get(task_id, [])
                record["documentation_tokens"] = documentation_tokens
                task["status"] = "accepted"
                update_subtask_status(
                    task_id,
                    "accepted",
                    attempt_count=attempt_number,
                    max_attempts=max(bundle.max_subtask_attempts, attempt_number),
                    detail="Execution, forward verification, and documentation passed.",
                )
                add_activity(
                    "Documentation (API agent)",
                    "Finished documentation; verifier is moving to the next stage.",
                    status="complete",
                    task_id=task_id,
                )
                verification_index += 1
                accepted_count = sum(
                    _task_record(manifest, str(item["task_id"]))["status"] == "accepted"
                    for item in coding_tasks
                )
                set_progress(
                    accepted_count,
                    len(coding_tasks),
                    f"{accepted_count} of {len(coding_tasks)} coding stages accepted",
                )
                manifest["token_usage"]["total"] = token_used
                write_run_manifest(paths.manifest, manifest)
                continue

            failure_counts[task_id] = failure_counts.get(task_id, 0) + 1
            record["status"] = "flagged"
            task["status"] = "flagged"
            failure = str(latest_runtime[task_id].get("deterministic_failure") or "").strip()
            if not failure:
                failure = "\n".join(verdict.failure_modes + verdict.feedback)
            verifier_feedback = _verifier_json(verdict)
            update_subtask_status(
                task_id,
                "flagged",
                attempt_count=attempt_number,
                max_attempts=max(bundle.max_subtask_attempts, attempt_number),
                detail="Verification stopped here; Codex will repair this stage and downstream work.",
            )

            if failure_counts[task_id] < bundle.max_subtask_attempts:
                remaining = coding_tasks[verification_index:]
                for remaining_task in remaining:
                    update_subtask_status(
                        str(remaining_task["task_id"]),
                        "repairing",
                        detail=f"Regenerating from failed stage {task_id}.",
                    )
                update_web_state(
                    phase="coder",
                    message=f"Codex is repairing {task_id} and all downstream stages.",
                )
                add_activity(
                    "Coder (Codex)",
                    f"Started suffix repair from {task_id}; accepted upstream stages are preserved.",
                    status="running",
                    task_id=task_id,
                    attempt=attempt_number + 1,
                )
                repair = await _await_with_activity(
                    session.repair_from_task(
                        plan=active_plan,
                        remaining_tasks=remaining,
                        failure=failure,
                        verifier_feedback=verifier_feedback,
                        accepted_context=_accepted_context(paths),
                        code_index=_refresh_deterministic_index(paths),
                    ),
                    agent="Coder (Codex)",
                    message="Codex suffix repair is still running",
                    task_id=task_id,
                    attempt=attempt_number + 1,
                )
                token_used += repair.total_tokens
                manifest["token_usage"]["codex"] += repair.total_tokens
                add_activity(
                    "Coder (Codex)",
                    "Finished the suffix repair; affected intermediate stages will run again.",
                    status="complete",
                    task_id=task_id,
                    attempt=attempt_number + 1,
                )
                verification_index = apply_build(repair, verification_index, "suffix_repair")
                continue

            if revision_count >= bundle.max_plan_revisions:
                record["status"] = "failed"
                manifest["status"] = "failed"
                manifest["token_usage"]["total"] = token_used
                write_run_manifest(paths.manifest, manifest)
                update_subtask_status(
                    task_id,
                    "failed",
                    attempt_count=attempt_number,
                    detail="The stage exhausted repair attempts and plan revisions.",
                )
                raise RuntimeError(
                    f"{task_id} failed after {bundle.max_subtask_attempts} verification failures "
                    f"and {bundle.max_plan_revisions} plan revisions."
                )

            revision_count += 1
            task_number = int(task["task_number"])
            full_task_index = next(
                index for index, item in enumerate(tasks) if str(item["task_id"]) == task_id
            )
            failure_report = build_failure_report(
                subtask_number=task_number,
                subtask_text=_task_text(task),
                attempts=record["attempts"],
                completed_tasks=tasks[:full_task_index],
                remaining_tasks=tasks[full_task_index:],
            )
            (paths.reports / f"failure_revision_{revision_count}.json").write_text(
                failure_report, encoding="utf-8", newline="\n"
            )
            update_subtask_status(
                task_id,
                "replanning",
                detail="The planner is revising this stage and the downstream continuation.",
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
            revision_tokens = _agent_tokens(revision)
            token_used += revision_tokens
            manifest["token_usage"]["planning"] += revision_tokens
            revised_plan = str(revision.final_output)
            active_plan += f"\n\n--- REVISION {revision_count} ---\n{revised_plan}"
            append_planner_output(f"Revision {revision_count}", revised_plan)

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
            replacement_tokens = _agent_tokens(replacement)
            token_used += replacement_tokens
            manifest["token_usage"]["planning"] += replacement_tokens
            raw_replacements = replacement.final_output.model_dump().get("tasks", [])
            if not raw_replacements:
                raise RuntimeError("Task Breaker returned no replacement tasks.")
            replacement_tasks = materialize_tasks(
                raw_replacements,
                paths.tasks,
                prefix=f"revision_{revision_count}",
                start_number=task_number,
            )
            replacement_coding = _coding_tasks(replacement_tasks)
            if not replacement_coding:
                raise RuntimeError("The revised continuation contains no coding task.")

            for superseded in coding_tasks[verification_index:]:
                _task_record(manifest, str(superseded["task_id"]))["status"] = "superseded"
            tasks = tasks[:full_task_index] + replacement_tasks
            coding_tasks = coding_tasks[:verification_index] + replacement_coding
            for replacement_task in replacement_tasks:
                manifest["tasks"].append(
                    {
                        "task_id": str(replacement_task["task_id"]),
                        "task_number": int(replacement_task["task_number"]),
                        "status": (
                            "pending"
                            if replacement_task.get("is_Coding_Team_required") is True
                            else "context_only"
                        ),
                        "accepted_files": [],
                        "documentation_tokens": 0,
                        "attempts": [],
                    }
                )
            write_json(paths.tasks / "task_manifest.json", {"tasks": tasks})
            update_web_state(
                task_breaker={
                    "raw": json.dumps({"tasks": tasks}, indent=2, ensure_ascii=False),
                    "subtasks": task_breaker_payload_to_subtasks({"tasks": tasks}),
                },
                message=f"Replaced {task_id} and its downstream stages after plan revision.",
            )
            failure_counts = {
                key: value
                for key, value in failure_counts.items()
                if key in {str(item["task_id"]) for item in coding_tasks[:verification_index]}
            }
            revised_change = await _await_with_activity(
                session.repair_from_task(
                    plan=active_plan,
                    remaining_tasks=replacement_tasks,
                    failure=failure,
                    verifier_feedback=verifier_feedback,
                    accepted_context=_accepted_context(paths),
                    code_index=_refresh_deterministic_index(paths),
                ),
                agent="Coder (Codex)",
                message="Codex revised-continuation build is still running",
                task_id=str(replacement_coding[0]["task_id"]),
            )
            token_used += revised_change.total_tokens
            manifest["token_usage"]["codex"] += revised_change.total_tokens
            verification_index = apply_build(
                revised_change, verification_index, "revised_continuation"
            )

    accepted_count = sum(
        _task_record(manifest, str(task["task_id"]))["status"] == "accepted"
        for task in coding_tasks
    )
    manifest["status"] = "complete"
    manifest["token_usage"]["total"] = token_used
    write_run_manifest(paths.manifest, manifest)
    update_web_state(
        phase="complete",
        message=f"MIMI completed and verified {accepted_count} coding stages.",
        output_dir=str(paths.root),
    )
    add_activity(
        "MIMI", f"Completed and verified {accepted_count} coding stages.", status="complete"
    )
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
