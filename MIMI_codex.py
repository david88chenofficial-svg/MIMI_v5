"""Codex SDK adapter for MIMI's whole-plan implementation stage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai_codex import ApprovalMode, AsyncCodex, AsyncThread, Sandbox
from openai_codex.types import ReasoningEffort

from MIMI_workspace import (
    changed_workspace_files,
    render_code_index,
    workspace_snapshot,
)


PLAN_CODING_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "summary": {"type": "string"},
                    "entrypoint": {"type": "string"},
                    "files": {"type": "array", "items": {"type": "string"}},
                    "tests_run": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "task_id",
                    "summary",
                    "entrypoint",
                    "files",
                    "tests_run",
                    "notes",
                ],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "tasks", "notes"],
    "additionalProperties": False,
}

CODEX_DEVELOPER_INSTRUCTIONS = """You are MIMI's engineering implementation specialist.
Work directly in the supplied workspace and obey its AGENTS.md.
Use the supplied code index as the normal navigation layer. Do not scan or read the whole
workspace by default. Open only files needed to implement the supplied plan.
Implement the ordered coding tasks as one coherent product, in one continuous pass.
Each coding task needs a small runnable validation entrypoint. Entrypoints may import shared
product modules; do not duplicate the product implementation between task runners.
Prefer clear runner names such as validation/subtask_01_attempt.py,
validation/subtask_02_attempt.py, and so on.
Each entrypoint must create relative artifacts and exactly one result.json when its process
working directory is an empty task-attempt artifact directory. It must produce at least one
text artifact and any plot required by that task's acceptance criteria.
You may inspect relevant implementation files freely when repairing a failed stage.
Edit the actual product files, not a code block or temporary duplicate.
Create or update tests when practical. Never access credentials or paths outside the workspace.
"""


@dataclass(slots=True)
class CodexTaskResult:
    task_id: str
    summary: str
    entrypoint: str
    files: list[str]
    tests_run: list[str]
    notes: list[str]


@dataclass(slots=True)
class CodexPlanChange:
    summary: str
    tasks: list[CodexTaskResult]
    notes: list[str]
    changed_files: list[str]
    final_response: str
    thread_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int

    def result_for(self, task_id: str) -> CodexTaskResult:
        for result in self.tasks:
            if result.task_id == task_id:
                return result
        raise KeyError(f"Codex returned no result for {task_id}.")


def _parse_object(text: str | None, label: str) -> dict[str, Any]:
    if not text:
        raise RuntimeError(f"Codex returned no {label} response.")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Codex returned invalid {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Codex {label} response must be a JSON object.")
    return value


def _usage(result) -> tuple[int, int, int]:
    usage = getattr(result, "usage", None)
    last = getattr(usage, "last", None)
    if last is None:
        return 0, 0, 0
    return (
        int(getattr(last, "input_tokens", 0) or 0),
        int(getattr(last, "output_tokens", 0) or 0),
        int(getattr(last, "total_tokens", 0) or 0),
    )


class CodexPlanSession:
    def __init__(
        self,
        codex: AsyncCodex,
        workspace: Path,
        *,
        model: str,
        effort: str | None = None,
    ) -> None:
        self.codex = codex
        self.workspace = Path(workspace).resolve()
        self.model = model
        self.effort = effort
        self.thread: AsyncThread | None = None

    async def _ensure_thread(self) -> AsyncThread:
        if self.thread is None:
            self.thread = await self.codex.thread_start(
                approval_mode=ApprovalMode.deny_all,
                cwd=str(self.workspace),
                developer_instructions=CODEX_DEVELOPER_INSTRUCTIONS,
                model=self.model,
                sandbox=Sandbox.workspace_write,
            )
        return self.thread

    async def implement_plan(
        self,
        *,
        plan: str,
        tasks: list[dict[str, Any]],
        code_index: dict[str, Any],
    ) -> CodexPlanChange:
        """Implement every coding task in one persistent Codex turn."""

        coding_tasks = [task for task in tasks if task.get("is_Coding_Team_required") is True]
        prompt = (
            "Implement the complete ordered engineering plan in the current workspace in one "
            "continuous pass. Build one coherent product, then create a small validation "
            "entrypoint for every coding task. Each entrypoint must independently generate that "
            "task's result.json and required intermediate artifacts when MIMI runs it later. "
            "Run focused tests while building. Return one structured task result for every coding "
            "task, in the supplied order. The files list for each task must name the shared product "
            "and validation files that establish that stage.\n\n"
            "COMPLETE PLAN:\n"
            + plan
            + "\n\nORDERED TASK CONTRACTS:\n"
            + json.dumps(tasks, indent=2, ensure_ascii=False)
            + "\n\nCODE INDEX:\n"
            + render_code_index(code_index)
        )
        return await self._run_plan_change(
            prompt,
            expected_task_ids=[str(task["task_id"]) for task in coding_tasks],
            allowed_task_ids=[str(task["task_id"]) for task in tasks],
        )

    async def repair_from_task(
        self,
        *,
        plan: str,
        remaining_tasks: list[dict[str, Any]],
        failure: str,
        verifier_feedback: str,
        accepted_context: str,
        code_index: dict[str, Any],
    ) -> CodexPlanChange:
        """Repair the failed task and regenerate every downstream task in the same thread."""

        coding_tasks = [
            task for task in remaining_tasks if task.get("is_Coding_Team_required") is True
        ]
        prompt = (
            "Repair the current product beginning at the first supplied remaining task. Preserve "
            "accepted upstream behavior and interfaces. Diagnose the verifier evidence, fix the "
            "failed stage, and then update every downstream stage whose results may depend on it. "
            "Regenerate or update each remaining validation entrypoint so MIMI can rerun the "
            "remaining stages. Return one structured task result for every supplied coding task.\n\n"
            "COMPLETE PLAN:\n"
            + plan
            + "\n\nACCEPTED UPSTREAM DOCUMENTATION:\n"
            + (accepted_context or "No upstream coding stage has been accepted yet.")
            + "\n\nFAILURE:\n"
            + failure
            + "\n\nVERIFIER FEEDBACK:\n"
            + verifier_feedback
            + "\n\nREMAINING TASK CONTRACTS:\n"
            + json.dumps(remaining_tasks, indent=2, ensure_ascii=False)
            + "\n\nCURRENT CODE INDEX:\n"
            + render_code_index(code_index)
        )
        return await self._run_plan_change(
            prompt,
            expected_task_ids=[str(task["task_id"]) for task in coding_tasks],
            allowed_task_ids=[str(task["task_id"]) for task in remaining_tasks],
        )

    async def _run_plan_change(
        self,
        prompt: str,
        *,
        expected_task_ids: list[str],
        allowed_task_ids: list[str],
    ) -> CodexPlanChange:
        thread = await self._ensure_thread()
        before = workspace_snapshot(self.workspace)
        kwargs: dict[str, Any] = {
            "approval_mode": ApprovalMode.deny_all,
            "cwd": str(self.workspace),
            "model": self.model,
            "output_schema": PLAN_CODING_OUTPUT_SCHEMA,
            "sandbox": Sandbox.workspace_write,
        }
        if self.effort:
            kwargs["effort"] = ReasoningEffort(self.effort)
        result = await thread.run(prompt, **kwargs)
        after = workspace_snapshot(self.workspace)
        payload = _parse_object(result.final_response, "whole-plan coding")
        task_results = [
            CodexTaskResult(
                task_id=str(item.get("task_id") or ""),
                summary=str(item.get("summary") or ""),
                entrypoint=str(item.get("entrypoint") or ""),
                files=[str(path) for path in item.get("files", [])],
                tests_run=[str(test) for test in item.get("tests_run", [])],
                notes=[str(note) for note in item.get("notes", [])],
            )
            for item in payload.get("tasks", [])
            if isinstance(item, dict)
        ]
        returned_ids = [item.task_id for item in task_results]
        duplicate_ids = sorted(
            {task_id for task_id in returned_ids if returned_ids.count(task_id) > 1}
        )
        if duplicate_ids:
            raise RuntimeError(
                "Codex whole-plan response contains duplicate task IDs: "
                + ", ".join(duplicate_ids)
            )
        unexpected_ids = [task_id for task_id in returned_ids if task_id not in allowed_task_ids]
        if unexpected_ids:
            raise RuntimeError(
                "Codex whole-plan response contains unknown task IDs: "
                + ", ".join(unexpected_ids)
            )

        # Codex may describe a declared context-only stage even though MIMI does not
        # execute it. Ignore those harmless extras, but keep strict validation for
        # every requested coding stage.
        task_results = [item for item in task_results if item.task_id in expected_task_ids]
        coding_result_ids = [item.task_id for item in task_results]
        if coding_result_ids != expected_task_ids:
            raise RuntimeError(
                "Codex whole-plan response task IDs must exactly match the requested coding "
                f"tasks in order. Expected {expected_task_ids}; received {coding_result_ids}."
            )
        if any(not item.entrypoint for item in task_results):
            raise RuntimeError("Every Codex task result must name a validation entrypoint.")
        input_tokens, output_tokens, total_tokens = _usage(result)
        return CodexPlanChange(
            summary=str(payload.get("summary") or ""),
            tasks=task_results,
            notes=[str(note) for note in payload.get("notes", [])],
            changed_files=changed_workspace_files(before, after),
            final_response=result.final_response or "",
            thread_id=thread.id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )


# Transitional name for callers that imported the v5 class before the workflow
# became plan-wide. The class now exposes only whole-plan operations.
CodexTaskSession = CodexPlanSession
