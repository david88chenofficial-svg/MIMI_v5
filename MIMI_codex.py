"""Codex SDK adapter used by MIMI's coding and diagnosis stages."""

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


CODING_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "entrypoint": {"type": "string"},
        "tests_run": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "entrypoint", "tests_run", "notes"],
    "additionalProperties": False,
}

ROOT_CAUSE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "root_causes": {"type": "array", "items": {"type": "string"}},
        "relevant_files": {"type": "array", "items": {"type": "string"}},
        "recommended_actions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "root_causes", "relevant_files", "recommended_actions"],
    "additionalProperties": False,
}


CODEX_DEVELOPER_INSTRUCTIONS = """You are MIMI's coding specialist.
Work directly in the supplied workspace and obey its AGENTS.md.
Use the supplied code index as the normal navigation layer. Do not scan or read the whole
workspace by default. Open only files needed to implement the current task.
You may inspect the relevant implementation files freely when repairing a failed attempt.
Edit the actual product files, not a code block or temporary duplicate.
Create or update tests when practical. Never access credentials or paths outside the workspace.
The runnable entrypoint must create relative artifacts and exactly one result.json when executed
with its process working directory set to an attempt artifact directory.
"""


@dataclass(slots=True)
class CodexChange:
    summary: str
    entrypoint: str
    tests_run: list[str]
    notes: list[str]
    changed_files: list[str]
    final_response: str
    thread_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int


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


class CodexTaskSession:
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

    async def implement(self, task: str, code_index: dict[str, Any]) -> CodexChange:
        prompt = (
            "Implement this subtask in the current workspace. Begin with the compact code index; "
            "inspect only files necessary for this subtask. You may edit multiple product and test "
            "files. Run focused checks. Return the requested structured result.\n\n"
            "SUBTASK:\n"
            + task
            + "\n\nCODE INDEX:\n"
            + render_code_index(code_index)
        )
        return await self._run_change(prompt)

    async def repair(
        self,
        *,
        task: str,
        failure: str,
        verifier_feedback: str,
        root_cause: dict[str, Any] | None = None,
    ) -> CodexChange:
        prompt = (
            "Repair the current implementation for the same subtask. You are explicitly allowed "
            "to inspect the relevant code, tests, and current diff before modifying them. Preserve "
            "working behavior. Run focused checks and return the structured result.\n\n"
            f"SUBTASK:\n{task}\n\nDETERMINISTIC FAILURE:\n{failure}\n\n"
            f"VERIFIER FEEDBACK:\n{verifier_feedback}\n"
        )
        if root_cause:
            prompt += "\nREAD-ONLY ROOT-CAUSE REVIEW:\n" + json.dumps(
                root_cause, indent=2, ensure_ascii=False
            )
        return await self._run_change(prompt)

    async def _run_change(self, prompt: str) -> CodexChange:
        thread = await self._ensure_thread()
        before = workspace_snapshot(self.workspace)
        kwargs: dict[str, Any] = {
            "approval_mode": ApprovalMode.deny_all,
            "cwd": str(self.workspace),
            "model": self.model,
            "output_schema": CODING_OUTPUT_SCHEMA,
            "sandbox": Sandbox.workspace_write,
        }
        if self.effort:
            kwargs["effort"] = ReasoningEffort(self.effort)
        result = await thread.run(prompt, **kwargs)
        after = workspace_snapshot(self.workspace)
        payload = _parse_object(result.final_response, "coding")
        input_tokens, output_tokens, total_tokens = _usage(result)
        return CodexChange(
            summary=str(payload.get("summary") or ""),
            entrypoint=str(payload.get("entrypoint") or ""),
            tests_run=[str(item) for item in payload.get("tests_run", [])],
            notes=[str(item) for item in payload.get("notes", [])],
            changed_files=changed_workspace_files(before, after),
            final_response=result.final_response or "",
            thread_id=thread.id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )


async def review_root_cause(
    codex: AsyncCodex,
    workspace: Path,
    *,
    model: str,
    effort: str | None,
    task: str,
    failure: str,
    verifier_feedback: str,
    changed_files: list[str],
    code_index: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """Perform a separate read-only code review only after a failure mode exists."""

    workspace = Path(workspace).resolve()
    thread = await codex.thread_start(
        approval_mode=ApprovalMode.deny_all,
        cwd=str(workspace),
        developer_instructions=(
            "You are MIMI's root-cause reviewer. Work read-only. Inspect the reported changed "
            "files and their dependencies, then identify concrete causes. Do not edit anything."
        ),
        model=model,
        sandbox=Sandbox.read_only,
    )
    prompt = (
        f"SUBTASK:\n{task}\n\nFAILURE:\n{failure}\n\nVERIFIER FEEDBACK:\n"
        f"{verifier_feedback}\n\nCHANGED FILES:\n{json.dumps(changed_files)}\n\n"
        "CODE INDEX:\n"
        + render_code_index(code_index)
    )
    kwargs: dict[str, Any] = {
        "approval_mode": ApprovalMode.deny_all,
        "cwd": str(workspace),
        "model": model,
        "output_schema": ROOT_CAUSE_OUTPUT_SCHEMA,
        "sandbox": Sandbox.read_only,
    }
    if effort:
        kwargs["effort"] = ReasoningEffort(effort)
    result = await thread.run(prompt, **kwargs)
    payload = _parse_object(result.final_response, "root-cause")
    return payload, _usage(result)[2]
