from MIMI_agents import (
    Coder_agent,
    Verifier_agent,
    ConversationSupervisor_agent,
    Planner_agent,
    Coder_secretary,
)
import asyncio
import argparse
from contextlib import contextmanager
import os
import shutil
import sys
import threading
from agents import Runner
from datetime import datetime
import time
import json
from pathlib import Path
import MIMI_functions as mf
from MIMI_credentials import configure_openai_api_key
from MIMI_dashboard import (
    add_coder_run,
    append_planner_output,
    artifact_snapshot,
    reset_web_state,
    serve_web,
    set_progress,
    task_breaker_payload_to_subtasks,
    update_web_state,
)
from MIMI_inputs import (
    MIMIInputBundle,
    build_prompt_with_reference_image,
    default_input_bundle,
)
from MIMI_models import (
    OPENAI_MODEL_OPTIONS,
    AgentModelConfig,
    apply_agent_models,
    restore_agent_models,
)
from MIMI_revision import (
    build_failure_report,
    coding_task_count,
    revise_plan_continuation,
    run_task_breaker_on_plan,
    stream_text_response,
)


PROJECT_ROOT = Path(__file__).resolve().parent
AGENTS_OUTPUT_ROOT = PROJECT_ROOT / "agents_output"
RUN_CONTROL_LOCK = threading.Lock()
ACTIVE_RUN_LOOP = None
ACTIVE_RUN_TASK = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def safe_output_name(value: str | None, fallback: str) -> str:
    name = Path(str(value or "").strip()).name
    return fallback if name in {"", ".", ".."} else name


def unique_python_output_path(raw_filename: str, subtask_number: int, attempt_number: int) -> Path:
    filename = safe_output_name(raw_filename, f"subtask_{subtask_number}.py")
    candidate = Path.cwd() / filename
    if candidate.suffix.lower() != ".py":
        candidate = candidate.with_suffix(".py")

    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    parent = candidate.parent
    attempt_candidate = parent / f"{stem}_subtask{subtask_number}_attempt{attempt_number}{suffix}"
    if not attempt_candidate.exists():
        return attempt_candidate

    index = 2
    while True:
        numbered_candidate = parent / f"{stem}_subtask{subtask_number}_attempt{attempt_number}_{index}{suffix}"
        if not numbered_candidate.exists():
            return numbered_candidate
        index += 1


def unique_manifest_filename(filename: str, used_filenames: set[str], prefix: str) -> str:
    path = Path(safe_output_name(filename, f"{prefix}.txt"))
    if not path.suffix:
        path = path.with_suffix(".txt")
    candidate = path
    index = 2
    while candidate.name in used_filenames:
        candidate = path.with_name(f"{path.stem}_{prefix}_{index}{path.suffix}")
        index += 1
    used_filenames.add(candidate.name)
    return candidate.name


def find_task_source(filename: str, source_dirs: list[Path]) -> Path | None:
    safe_name = safe_output_name(filename, "subtask.txt")
    candidates = [Path.cwd() / safe_name]
    candidates.extend(source_dir / safe_name for source_dir in source_dirs)

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    if AGENTS_OUTPUT_ROOT.is_dir():
        archived_matches = sorted(
            AGENTS_OUTPUT_ROOT.glob(f"*/{safe_name}"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if archived_matches:
            return archived_matches[0].resolve()
    return None


def ensure_unique_task_filenames(
    tasks: list[dict],
    used_filenames: set[str],
    prefix: str,
    source_dirs: list[Path] | None = None,
) -> list[dict]:
    source_dirs = [Path(path).resolve() for path in (source_dirs or [])]
    normalized_tasks = []
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict):
            normalized_tasks.append(task)
            continue
        task = dict(task)
        original_filename = safe_output_name(
            task.get("sub_filename"),
            f"{prefix}_{index}.txt",
        )
        unique_filename = unique_manifest_filename(
            original_filename,
            used_filenames,
            f"{prefix}_{index}",
        )
        source_path = find_task_source(original_filename, source_dirs)
        destination_path = Path.cwd() / unique_filename
        if source_path and source_path != destination_path.resolve() and not destination_path.exists():
            shutil.copy2(source_path, destination_path)
        task["sub_filename"] = unique_filename
        normalized_tasks.append(task)
    return normalized_tasks


def resolve_bundle_paths(bundle: MIMIInputBundle, base_dir: Path) -> MIMIInputBundle:
    def resolve(path: Path | None) -> Path | None:
        if path is None:
            return None
        path = Path(path).expanduser()
        return path.resolve() if path.is_absolute() else (base_dir / path).resolve()

    bundle.spec_path = resolve(bundle.spec_path)
    bundle.background_path = resolve(bundle.background_path)
    bundle.image_path = resolve(bundle.image_path)
    bundle.image_paths = [resolve(path) for path in (bundle.image_paths or [])]
    bundle.resume_plan_path = resolve(bundle.resume_plan_path)
    bundle.resume_task_breaker_path = resolve(bundle.resume_task_breaker_path)
    return bundle


def create_run_output_directory(timestamp: str) -> Path:
    AGENTS_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    output_dir = AGENTS_OUTPUT_ROOT / timestamp
    suffix = 2
    while output_dir.exists():
        output_dir = AGENTS_OUTPUT_ROOT / f"{timestamp}_{suffix}"
        suffix += 1
    output_dir.mkdir()
    return output_dir.resolve()


@contextmanager
def working_directory(path: Path):
    previous_directory = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous_directory)


async def main(bundle: MIMIInputBundle | None = None):
    configure_openai_api_key(required=True)
    bundle = bundle or default_input_bundle()
    original_models = apply_agent_models(bundle.model_config)
    try:
        return await run_pipeline(bundle)
    finally:
        restore_agent_models(original_models)


async def run_pipeline(bundle: MIMIInputBundle):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = create_run_output_directory(timestamp)
    bundle = resolve_bundle_paths(bundle, Path.cwd())
    print(f"Agent output directory: {output_dir}")

    with working_directory(output_dir):
        await run_pipeline_in_output_directory(bundle, timestamp)

    return output_dir


async def run_pipeline_in_output_directory(bundle: MIMIInputBundle, timestamp: str):
    # All relative files created below are contained in this run's output directory.
    log_filename = f"Seal_{timestamp}.json"
    sub_task_number = max(1, bundle.start_subtask_number)

    # # Initialize token usage
    token_used = 0

    # User inputs
    spec = bundle.load_spec()
    knowledge = bundle.load_background()
    background_section = ""
    if knowledge:
        background_section = "\n\nAnd this is the background knowledge that can be helpful" + knowledge
    image_section = ""
    if bundle.image_paths:
        image_section = "\n\nReference figure attached. The plan should be consistent to the layout in the picture."
    user_prompt = build_prompt_with_reference_image(
        "This is the task specification"
        + spec
        + background_section
        + image_section,
        bundle,
    )

    if bundle.resume_task_breaker_path:
        print("Resume mode: loading Task Breaker JSON...")
        update_web_state(
            phase="resume",
            message=f"Resume mode. Loading {bundle.resume_task_breaker_path.name}.",
            planner_output="Resume mode: Planner was skipped because a Task Breaker JSON file was provided.",
        )
        with bundle.resume_task_breaker_path.open("r", encoding="utf-8") as task_breaker_json:
            data = json.load(task_breaker_json)
        payload = data[0] if isinstance(data, list) and data else data
        if not isinstance(payload, dict) or "tasks" not in payload:
            raise ValueError("Resume Task Breaker JSON must contain a top-level tasks list or a list with one tasks object.")
        payload["tasks"] = ensure_unique_task_filenames(
            payload["tasks"],
            set(),
            "resume_subtask",
            source_dirs=[bundle.resume_task_breaker_path.parent, PROJECT_ROOT],
        )
        mf.log_sub_tasks(f"task_breaker_{timestamp}_resume.json", content=payload)
        task_breaker_subtasks = task_breaker_payload_to_subtasks(payload)
        update_web_state(
            task_breaker={
                "raw": json.dumps(payload, indent=2, ensure_ascii=False),
                "subtasks": task_breaker_subtasks,
            },
            message=f"Loaded {len(task_breaker_subtasks)} subtasks. Starting at subtask {sub_task_number}.",
        )
        active_plan_context = spec
    else:
        if bundle.resume_plan_path:
            print("Plan resume mode: loading Planner output...")
            update_web_state(
                phase="plan_resume",
                message=f"Plan resume mode. Loading {bundle.resume_plan_path.name}.",
            )
            the_plan = bundle.resume_plan_path.read_text(encoding="utf-8", errors="replace")
            mf.save_text_to_txt(the_plan, f"Planner_output_{timestamp}_resume.txt")
            update_web_state(
                planner_output=the_plan,
                message="Planner skipped. Task Breaker starting from uploaded plan...",
            )
            active_plan_context = the_plan
        else:
            # Run Planner to generate overall plan
            print("Planner working...")
            update_web_state(phase="planner", message="Planner working...")
            Planner_message = Runner.run_streamed(
                starting_agent=Planner_agent,
                input=user_prompt
            )

            await stream_text_response(Planner_message)

            # Log the Planner message
            # Because this can be a very large text, we save it to a separate .txt file
            mf.save_text_to_txt(Planner_message.final_output, f"Planner_output_{timestamp}.txt")
            update_web_state(
                planner_output=Planner_message.final_output,
                message="Planner finished. Task Breaker starting...",
            )

            # Update token usage
            token_used = Planner_message.raw_responses[0].usage.total_tokens + token_used
            print()
            print("token just used: ", Planner_message.raw_responses[0].usage.total_tokens)
            print("total token used: ", token_used)
            txt_path = Path(f"Planner_output_{timestamp}.txt")
            with txt_path.open("r", encoding="utf-8") as f:
                the_plan = f.read()
            active_plan_context = the_plan

        # Run Task Breaker
        # Task Breaker should break the overall plan into sub tasks
        # and each task into a .txt file
        # and a JSON file that contains the information of each sub task
        # File saved using the tool given to Task Breaker
        print("Task Breaker working...")
        update_web_state(phase="task_breaker", message="Task Breaker working...")
        TaskBreaker_message = await run_task_breaker_on_plan(the_plan)

        # Update token usage
        token_used = TaskBreaker_message.raw_responses[0].usage.total_tokens + token_used
        print()
        print("token just used: ", TaskBreaker_message.raw_responses[0].usage.total_tokens)
        print("total token used: ", token_used)

        # Log the Task Breaker JSON message and information of sub tasks
        payload = TaskBreaker_message.final_output.model_dump()
        payload["tasks"] = ensure_unique_task_filenames(
            payload.get("tasks", []),
            set(),
            "subtask",
        )
        mf.log_sub_tasks(f"task_breaker_{timestamp}.json", content=payload)
        task_breaker_subtasks = task_breaker_payload_to_subtasks(payload)
        update_web_state(
            task_breaker={
                "raw": json.dumps(payload, indent=2, ensure_ascii=False),
                "subtasks": task_breaker_subtasks,
            },
            message=f"Task Breaker finished with {len(task_breaker_subtasks)} subtasks.",
        )
    
    # Give it a break
    print(1)
    time.sleep(5)
    print(5)
    time.sleep(5)
    print(10)

###########################################################################################################################
    # timestamp = "20260512_194222"
    # token_used = 0
    log_filename = f"Seal_{timestamp}.json"
    start_subtask_number = max(1, bundle.start_subtask_number)
###########################################################################################################################

    tasks = payload["tasks"]
    total_coding_tasks = coding_task_count(tasks)
    set_progress(0, total_coding_tasks, f"0 of {total_coding_tasks} coding subtasks complete")
    completed_task_records = []
    task_index = 0
    plan_revision_count = 0
    max_plan_revisions = 2

    # Search for every sub tasks in task_breaker.json
    while task_index < len(tasks):
        file_data = tasks[task_index]

        if file_data.get("is_Coding_Team_required") != True:
            completed_task_records.append(file_data)
            task_index += 1
            continue
        if sub_task_number < start_subtask_number:
            sub_task_number += 1
            completed_task_records.append(file_data)
            task_index += 1
            continue

        sub_file_name = Path(file_data["sub_filename"]).name

        # Initialize iteration breaker
        max_conversation_iteration = 3
        is_code_correct = False
        n = 0

        # Initialize the wrong code
        wrong_code = "wrong code"
        err = "error"
        attempt_records = []

        print("Running sub task ", sub_task_number, " with iteration of ", n)
        # Load the sub task file
        sub_task_path = Path(sub_file_name)
        if not sub_task_path.is_file():
            raise FileNotFoundError(
                f"Subtask file '{sub_file_name}' was not found in {Path.cwd()}. "
                "Resume runs need the task files beside the Task Breaker JSON or in a previous agents_output run."
            )
        sub_task = sub_task_path.read_text(encoding="utf-8")
        set_progress(
            sub_task_number,
            total_coding_tasks,
            f"Working on subtask {sub_task_number} of {total_coding_tasks}",
        )
        update_web_state(
            phase="coder",
            message=f"Coder working on subtask {sub_task_number} of {total_coding_tasks}",
        )

        # Give it a break
        print(1)
        time.sleep(5)
        print(5)
        time.sleep(5)
        print(10)

        # To carry out the coding process requires three critirias:
        # 1. The code should be indicated as wrong.
        #     This is used to avoid if the code is correct but continue solving the same task.
        # 2. The interation is under the limit.
        #     This can make sure the agents are not solving one single task over and over again.
        # 3. Code is required for the sub task.
        #     Sometimes the sub task is simply a introduction.
        while is_code_correct == False and n != max_conversation_iteration and file_data["is_Coding_Team_required"] == True:
            attempt_number = n + 1
            update_web_state(
                message=(
                    f"Coder working on subtask {sub_task_number} of {total_coding_tasks} "
                    f"(attempt {attempt_number})"
                )
            )

            # Create the prompt for Coder
            # The prompt consist of the last code for last sub task and the instructions for the current sub task

            # Load the sub task instructions
            CodingTeam_prompt = (
                "You will code in Python. You may use Python equivalent function to Matlab. "
                "The task you will be doing is\n"
                + sub_task
            )

            # The variable of "sub_task_number" was set to be 1 initially.
            # If the Coder is doing the first task, "sub_task_number" will be 1.
            # If it is not 1, then we can load the documentation for previous code.
            if sub_task_number != 1:
                doc_text = mf.load_documentation("documentation.json")
                CodingTeam_prompt = (
                    CodingTeam_prompt
                    + "\nThis is the documentation of the code that you may find useful\n"
                    + doc_text
                )

            # Add Coder's last trial and Verifier's feedback if it failed before
            if n != 0:
                CodingTeam_prompt = (
                    CodingTeam_prompt
                    + "\nYou did the task once and here is what you did\n"
                    + wrong_code
                )
                CodingTeam_prompt = CodingTeam_prompt + "\nThe error is\n" + err
                CodingTeam_prompt = (
                    CodingTeam_prompt
                    + "\nVerifier's feedback on your last trial on the task\n"
                    + Verifier_message.final_output
                )

            CodingTeam_prompt = build_prompt_with_reference_image(CodingTeam_prompt, bundle)

            # Run the Coder
            print("Coder working...")
            coder_message = Runner.run_streamed(
                starting_agent=Coder_agent,
                input=CodingTeam_prompt
            )

            await stream_text_response(coder_message)

            # Save the code to a variable so that it can be reviewed by the Coder if it is wrong
            wrong_code = coder_message.final_output

            # Update token usage and log the outputs
            token_used = coder_message.raw_responses[0].usage.total_tokens + token_used
            print()
            print("token just used: ", coder_message.raw_responses[0].usage.total_tokens)
            print("total token used: ", token_used)
            mf.log_result(
                filename=log_filename,
                role="Coder",
                content=coder_message.final_output,
            )
            mf.log_result(
                filename=log_filename,
                role="Token just used",
                content=coder_message.raw_responses[0].usage.total_tokens
            )

            # Get the filename of the python file
            requested_py_file_title = mf.first_line_value(coder_message.final_output)
            py_file_path = unique_python_output_path(
                requested_py_file_title,
                sub_task_number,
                attempt_number,
            )
            py_file_title = str(py_file_path)
            # Save the solution to a python file with consistent file names
            py_file_path.parent.mkdir(parents=True, exist_ok=True)
            with py_file_path.open("w", encoding="utf-8", newline="\n") as solution_file:
                solution_file.write(coder_message.final_output)

            # Run generated code in this run directory so all relative artifacts stay together.
            Path("result.json").unlink(missing_ok=True)
            rc, out, err, path = mf.run_code_string_locally(
                coder_message.final_output,
                f"{sub_task_number}_attempt{attempt_number}",
                workdir=Path.cwd(),
            )

            # Give it a break
            print(1)
            time.sleep(5)
            print(5)
            time.sleep(5)
            print(10)

            # Show user what are the output
            print("Saved to:", path)
            print("Return code:", rc)
            print("STDOUT:\n", out)
            print("STDERR:\n", err)

            # Give it time for user to see what is the output of the code
            print(1)
            time.sleep(5)
            print(5)
            time.sleep(5)
            print(10)
            time.sleep(5)
            print(15)

            # Keep execution and artifact-contract failures inside the existing
            # Verifier/Coder repair loop instead of failing the whole MIMI run.
            verification_error = ""
            if rc != 0:
                verification_error = err or f"Program exited with return code {rc} without a traceback."
            if verification_error:
                Verifier_prompt = (
                    "The generated program did not run successfully.\n"
                    "Task instructions:\n"
                    + sub_task
                    + "\n\nExecution error:\n"
                    + verification_error
                    + "\n\nCode:\n"
                    + coder_message.final_output
                )
                run_artifacts = {"images": [], "texts": []}
            else:
                try:
                    plot_paths, plot_description, text_path, text_description = mf.extract_artifact_lists(
                        r"result.json"
                    )
                    missing_artifacts = [
                        artifact_path
                        for artifact_path in plot_paths + text_path
                        if not Path(artifact_path).is_file()
                    ]
                    if missing_artifacts:
                        raise FileNotFoundError(
                            "result.json references missing artifacts: "
                            + ", ".join(missing_artifacts)
                        )

                    for image_index, image_path in enumerate(bundle.image_paths or [], start=1):
                        plot_paths.append(str(image_path))
                        plot_description.append(f"This is uploaded reference image {image_index}.")
                    run_artifacts = artifact_snapshot(
                        plot_paths,
                        plot_description,
                        text_path,
                        text_description,
                    )
                    b64_images = [mf.image_to_base64(plot_path) for plot_path in plot_paths]
                    Verifier_prompt = mf.load_artifacts_to_prompt(
                        b64_images,
                        plot_description,
                        text_path,
                        text_description,
                        sub_task,
                        image_mime=bundle.image_mime(),
                    )
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as artifact_exc:
                    verification_error = (
                        "The program exited successfully but violated the artifact contract: "
                        f"{artifact_exc}"
                    )
                    err = verification_error
                    run_artifacts = {"images": [], "texts": []}
                    Verifier_prompt = (
                        "The generated program could not be verified because its artifacts "
                        "or result.json were invalid.\nTask instructions:\n"
                        + sub_task
                        + "\n\nArtifact error:\n"
                        + verification_error
                        + "\n\nCode:\n"
                        + coder_message.final_output
                    )

            # Run the Verifier
            print("Verifier working...")
            update_web_state(
                phase="verifier",
                message=f"Verifier checking subtask {sub_task_number} of {total_coding_tasks}",
            )
            Verifier_message = await Runner.run(
                starting_agent=Verifier_agent,
                input=Verifier_prompt
            )
            print(Verifier_message.final_output)

            # Update token usage and log the outputs
            token_used = Verifier_message.raw_responses[0].usage.total_tokens + token_used
            print()
            print("token just used: ", Verifier_message.raw_responses[0].usage.total_tokens)
            print("total token used: ", token_used)
            mf.log_result(
                filename=log_filename,
                role="Verifier",
                content=Verifier_message.final_output,
            )
            mf.log_result(
                filename=log_filename,
                role="Token just used",
                content=Verifier_message.raw_responses[0].usage.total_tokens
            )

            # Check the verdict
            print("Conversation Supervisor working...")
            update_web_state(
                phase="supervisor",
                message=f"Supervisor reviewing subtask {sub_task_number} of {total_coding_tasks}",
            )
            ConversationSupervisor_message = await Runner.run(
                starting_agent=ConversationSupervisor_agent,
                input=Verifier_message.final_output
            )
            token_used = ConversationSupervisor_message.raw_responses[0].usage.total_tokens + token_used
            print()
            print("token just used: ", ConversationSupervisor_message.raw_responses[0].usage.total_tokens)
            print("total token used: ", token_used)
            print(ConversationSupervisor_message.final_output)
            mf.log_result(
                filename=log_filename,
                role="Conversation Supervisor",
                content=ConversationSupervisor_message.final_output
            )
            mf.log_result(
                filename=log_filename,
                role="Token just used",
                content=ConversationSupervisor_message.raw_responses[0].usage.total_tokens
            )

            if ConversationSupervisor_message.final_output == 'correct':
                is_code_correct = True
            else:
                n = n + 1

            run_record = {
                "subtask_number": sub_task_number,
                "attempt": attempt_number,
                "subtask": sub_task,
                "code_path": str(py_file_title),
                "temp_script_path": str(path),
                "return_code": rc,
                "stdout": out,
                "stderr": err,
                "coder_output": coder_message.final_output,
                "verifier_output": Verifier_message.final_output,
                "supervisor_output": ConversationSupervisor_message.final_output,
                "artifacts": run_artifacts,
            }
            attempt_records.append({
                "attempt": attempt_number,
                "code_path": str(py_file_title),
                "temp_script_path": str(path),
                "return_code": rc,
                "stdout": out,
                "stderr": err,
                "coder_output": coder_message.final_output,
                "verifier_output": Verifier_message.final_output,
                "supervisor_output": ConversationSupervisor_message.final_output,
            })
            add_coder_run(run_record)

        if is_code_correct:
            # Save solution to a .py file only after the verifier/supervisor loop accepts it.
            accepted_py_file_path = Path(py_file_title)
            accepted_py_file_path.parent.mkdir(parents=True, exist_ok=True)
            with accepted_py_file_path.open("w", encoding="utf-8", newline="\n") as solution_file:
                solution_file.write(coder_message.final_output)
            completed_task_records.append(file_data)
            sub_task_number = sub_task_number + 1
            task_index += 1
            set_progress(
                min(sub_task_number - 1, total_coding_tasks),
                total_coding_tasks,
                f"{min(sub_task_number - 1, total_coding_tasks)} of {total_coding_tasks} coding subtasks complete",
            )

            # Update the documentation
            print("Updating code documentation")
            CoderSecretary_prompt = "This is the instructions\n" + sub_task
            CoderSecretary_prompt = CoderSecretary_prompt + "\nThis is the code\n" + coder_message.final_output
            CoderSecretary_message = await Runner.run(
                starting_agent=Coder_secretary,
                input=CoderSecretary_prompt
            )
            mf.update_documentation(filename="documentation.json", content=CoderSecretary_message.final_output)
            continue

        if plan_revision_count >= max_plan_revisions:
            raise RuntimeError(
                f"Subtask {sub_task_number} failed after {max_conversation_iteration} attempts "
                f"and {max_plan_revisions} plan revisions."
            )

        plan_revision_count += 1
        failure_report = build_failure_report(
            subtask_number=sub_task_number,
            subtask_text=sub_task,
            attempts=attempt_records,
            completed_tasks=completed_task_records,
            remaining_tasks=tasks[task_index:],
        )
        revision_report_path = Path(f"plan_revision_failure_{timestamp}_r{plan_revision_count}.json")
        revision_report_path.write_text(failure_report, encoding="utf-8")
        mf.log_result(
            filename=log_filename,
            role="Plan Revision Failure Report",
            content=failure_report,
        )

        Planner_revision_message = await revise_plan_continuation(
            bundle=bundle,
            original_plan=active_plan_context,
            failure_report=failure_report,
            failed_subtask_number=sub_task_number,
            revision_number=plan_revision_count,
        )
        token_used = Planner_revision_message.raw_responses[0].usage.total_tokens + token_used
        revised_plan = Planner_revision_message.final_output
        active_plan_context = (
            active_plan_context
            + "\n\n--- Scoped Planner Revision "
            + str(plan_revision_count)
            + " from subtask "
            + str(sub_task_number)
            + " ---\n"
            + revised_plan
        )
        revised_plan_path = Path(f"Planner_revision_{timestamp}_r{plan_revision_count}.txt")
        mf.save_text_to_txt(revised_plan, revised_plan_path.name)
        append_planner_output(
            f"Planner Revision {plan_revision_count} from subtask {sub_task_number}",
            revised_plan,
        )
        update_web_state(
            message=(
                f"Planner revision {plan_revision_count} finished. "
                "Task Breaker regenerating downstream subtasks."
            ),
        )

        print("Task Breaker revising downstream tasks...")
        update_web_state(
            phase="task_breaker_revision",
            message=f"Task Breaker regenerating from subtask {sub_task_number}",
        )
        TaskBreaker_revision_message = await run_task_breaker_on_plan(
            "Break this revised continuation plan into replacement subtasks. "
            f"Start numbering filenames at revised_{sub_task_number}_ and only include the failed subtask "
            "and downstream remaining work. "
            "Do not reuse any completed or existing sub_filename values from this failure report.\n\n"
            "Failure report with filenames to preserve and avoid:\n"
            + failure_report
            + "\n\nRevised continuation plan:\n"
            + revised_plan
        )
        token_used = TaskBreaker_revision_message.raw_responses[0].usage.total_tokens + token_used
        revised_payload = TaskBreaker_revision_message.final_output.model_dump()
        replacement_tasks = revised_payload.get("tasks", [])
        if not replacement_tasks:
            raise RuntimeError("Task Breaker returned no replacement tasks for the revised continuation plan.")
        used_subtask_filenames = {
            Path(task.get("sub_filename", "")).name
            for task in tasks[:task_index]
            if isinstance(task, dict) and task.get("sub_filename")
        }
        replacement_tasks = ensure_unique_task_filenames(
            replacement_tasks,
            used_subtask_filenames,
            f"revised_{sub_task_number}",
        )

        tasks = tasks[:task_index] + replacement_tasks
        data = [{"tasks": tasks}]
        Path(f"task_breaker_{timestamp}_revision_{plan_revision_count}.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        total_coding_tasks = (sub_task_number - 1) + coding_task_count(tasks[task_index:])
        set_progress(
            sub_task_number - 1,
            total_coding_tasks,
            f"Plan revised. Retrying subtask {sub_task_number} of {total_coding_tasks}",
        )
        update_web_state(
            task_breaker={
                "raw": json.dumps({"tasks": tasks}, indent=2, ensure_ascii=False),
                "subtasks": task_breaker_payload_to_subtasks({"tasks": tasks}),
            },
            message=f"Replaced subtask {sub_task_number} and downstream tasks after planner revision.",
        )

    mf.log_result(filename=log_filename, role="Total Token Used", content=token_used)


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
    return True


def run_bundle_in_background(bundle: MIMIInputBundle) -> None:
    global ACTIVE_RUN_LOOP, ACTIVE_RUN_TASK

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
        update_web_state(
            running=False,
            phase="aborted",
            message="Run aborted.",
            finished_at=datetime.now().isoformat(timespec="seconds"),
            error=None,
        )
    except Exception as exc:
        update_web_state(
            running=False,
            phase="error",
            message=f"Agent run failed: {exc}",
            finished_at=datetime.now().isoformat(timespec="seconds"),
            error=str(exc),
        )
        print(f"MIMI run failed: {exc}", file=sys.stderr)
    finally:
        with RUN_CONTROL_LOCK:
            if ACTIVE_RUN_TASK is task:
                ACTIVE_RUN_LOOP = None
                ACTIVE_RUN_TASK = None

        pending = [item for item in asyncio.all_tasks(loop) if not item.done()]
        for item in pending:
            item.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        asyncio.set_event_loop(None)


def parse_args():
    parser = argparse.ArgumentParser(description="Run MIMI with configurable markdown and image inputs.")
    parser.add_argument("--web", action="store_true", help="Start the drag-and-drop HTML interface.")
    parser.add_argument("--host", default="127.0.0.1", help="Host for --web mode.")
    parser.add_argument("--port", type=int, default=8080, help="Port for --web mode.")
    parser.add_argument("--spec", type=Path, default=Path("seal_analysis_tool_spec_v6.md"), help="Task specification markdown path.")
    parser.add_argument("--background", type=Path, default=Path("seal_tool_background_knowledge_short.md"), help="Optional background markdown path.")
    parser.add_argument("--image", type=Path, default=None, help="Optional reference image path.")
    parser.add_argument("--planner-output", type=Path, default=None, help="Optional Planner output path. Skips Planner and starts at Task Breaker.")
    parser.add_argument("--task-breaker-json", type=Path, default=None, help="Optional Task Breaker JSON path for resume mode.")
    parser.add_argument("--start-subtask", type=int, default=1, help="Subtask number to start from in resume mode.")
    parser.add_argument("--planner-model", choices=OPENAI_MODEL_OPTIONS, default=None)
    parser.add_argument("--task-breaker-model", choices=OPENAI_MODEL_OPTIONS, default=None)
    parser.add_argument("--coder-model", choices=OPENAI_MODEL_OPTIONS, default=None)
    parser.add_argument("--verifier-model", choices=OPENAI_MODEL_OPTIONS, default=None)
    parser.add_argument("--supervisor-model", choices=OPENAI_MODEL_OPTIONS, default=None)
    parser.add_argument("--coder-secretary-model", choices=OPENAI_MODEL_OPTIONS, default=None)
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
        model_config = AgentModelConfig({
            "planner": args.planner_model,
            "task_breaker": args.task_breaker_model,
            "coder": args.coder_model,
            "verifier": args.verifier_model,
            "supervisor": args.supervisor_model,
            "coder_secretary": args.coder_secretary_model,
        })
        image = args.image if args.image and args.image.exists() else None
        task_breaker_json = args.task_breaker_json if args.task_breaker_json and args.task_breaker_json.exists() else None
        asyncio.run(main(MIMIInputBundle(
            spec_path=args.spec,
            background_path=background,
            image_path=image,
            image_paths=[image] if image else [],
            resume_plan_path=args.planner_output if args.planner_output and args.planner_output.exists() else None,
            resume_task_breaker_path=task_breaker_json,
            start_subtask_number=max(1, args.start_subtask) if task_breaker_json else 1,
            model_config=model_config,
        )))
