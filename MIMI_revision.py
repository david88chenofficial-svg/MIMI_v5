import json
import sys

from agents import Runner
from openai.types.responses import ResponseTextDeltaEvent

from MIMI_agents import Planner_agent, TaskBreaker_agent
from MIMI_dashboard import task_breaker_payload_to_subtasks, update_web_state
from MIMI_inputs import MIMIInputBundle, build_prompt_with_reference_image


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


async def stream_text_response(streamed_message):
    async for event in streamed_message.stream_events():
        if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
            try:
                print(event.data.delta, end="", flush=True)
            except UnicodeEncodeError:
                safe_delta = event.data.delta.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
                print(safe_delta, end="", flush=True)


async def run_task_breaker_on_plan(plan_text: str):
    task_breaker_message = Runner.run_streamed(
        starting_agent=TaskBreaker_agent,
        input=plan_text,
    )
    await stream_text_response(task_breaker_message)
    return task_breaker_message


def coding_task_count(tasks: list[dict]) -> int:
    return sum(1 for task in tasks if task.get("is_Coding_Team_required") == True)


def build_failure_report(
    *,
    subtask_number: int,
    subtask_text: str,
    attempts: list[dict],
    completed_tasks: list[dict],
    remaining_tasks: list[dict],
) -> str:
    completed_summary = task_breaker_payload_to_subtasks({"tasks": completed_tasks})
    remaining_summary = task_breaker_payload_to_subtasks({"tasks": remaining_tasks})
    report = {
        "failed_subtask_number": subtask_number,
        "failed_subtask_text": subtask_text,
        "completed_subtasks_to_preserve": completed_summary,
        "remaining_subtasks_before_revision": remaining_summary,
        "attempts": attempts,
    }
    return json.dumps(report, indent=2, ensure_ascii=False)


async def revise_plan_continuation(
    *,
    bundle: MIMIInputBundle,
    original_plan: str,
    failure_report: str,
    failed_subtask_number: int,
    revision_number: int,
):
    prompt = (
        "A downstream subtask failed after the maximum coding attempts. "
        "Revise only the failed subtask and the downstream remaining work. "
        "Do not rewrite completed subtasks. Keep all symbols, assumptions, units, filenames, and interfaces "
        "consistent with the completed work.\n\n"
        "Return a revised continuation plan only. The continuation must start at the failed subtask and include "
        "all downstream work that still depends on it. It must be detailed enough for Task Breaker to create "
        "replacement subtasks. Do not include code.\n\n"
        f"Failed coding subtask number: {failed_subtask_number}\n"
        f"Revision cycle: {revision_number}\n\n"
        "Original planner output:\n"
        f"{original_plan}\n\n"
        "Structured downstream failure report:\n"
        f"{failure_report}\n\n"
        "Required output sections:\n"
        "1) Diagnosis of what was wrong or under-specified in the failed part of the plan.\n"
        "2) Consistency constraints that must be preserved from completed subtasks.\n"
        "3) Revised continuation plan starting at the failed subtask.\n"
        "4) Which downstream subtasks must be regenerated.\n"
    )
    update_web_state(
        phase="planner_revision",
        message=f"Planner revising from subtask {failed_subtask_number}",
    )
    revision_message = Runner.run_streamed(
        starting_agent=Planner_agent,
        input=build_prompt_with_reference_image(prompt, bundle),
    )
    await stream_text_response(revision_message)
    return revision_message
