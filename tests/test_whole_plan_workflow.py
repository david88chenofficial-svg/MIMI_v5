import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import MIMI
from MIMI_agents import VerificationResult
from MIMI_codex import CodexPlanChange, CodexTaskResult
from MIMI_inputs import MIMIInputBundle


def _verdict(value: str) -> VerificationResult:
    failed = value != "pass"
    return VerificationResult(
        verdict=value,
        key_numbers=[],
        key_equations=[],
        insights=[],
        predicted_properties=[],
        feedback=["repair stage two"] if failed else [],
        failure_modes=["stage two failed"] if failed else [],
        relevant_files=[],
    )


def _tasks() -> list[dict]:
    return [
        {
            "task_id": f"TASK_{index:03d}",
            "task_number": index,
            "title": f"Stage {index}",
            "instructions": f"Implement and validate stage {index}.",
            "is_Coding_Team_required": True,
            "insights_from_overview": "",
            "sub_filename": f"task_{index:03d}.txt",
        }
        for index in range(1, 4)
    ]


class _CodexContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _WholePlanSession:
    repair_scopes: list[list[str]] = []
    change_upstream = False

    def __init__(self, codex, workspace, **kwargs):
        self.workspace = Path(workspace)
        self.thread_id = "one-whole-plan-thread"

    def _result(self, task: dict) -> CodexTaskResult:
        task_id = str(task["task_id"])
        filename = f"{task_id.lower()}.py"
        (self.workspace / filename).write_text("VALUE = 1\n", encoding="utf-8")
        return CodexTaskResult(
            task_id=task_id,
            summary=f"implemented {task_id}",
            entrypoint=filename,
            files=[filename],
            tests_run=[],
            notes=[],
        )

    async def implement_plan(self, *, plan, tasks, code_index):
        results = [self._result(task) for task in tasks]
        return CodexPlanChange(
            summary="initial whole-plan build",
            tasks=results,
            notes=[],
            changed_files=[item.entrypoint for item in results],
            final_response="{}",
            thread_id=self.thread_id,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
        )

    async def repair_from_task(self, *, remaining_tasks, **kwargs):
        task_ids = [str(task["task_id"]) for task in remaining_tasks]
        self.repair_scopes.append(task_ids)
        results = [self._result(task) for task in remaining_tasks]
        changed = [item.entrypoint for item in results]
        if self.change_upstream:
            upstream = self.workspace / "task_001.py"
            upstream.write_text("VALUE = 2\n", encoding="utf-8")
            changed.append("task_001.py")
        return CodexPlanChange(
            summary="suffix repair",
            tasks=results,
            notes=[],
            changed_files=changed,
            final_response="{}",
            thread_id=self.thread_id,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
        )


class _RevisedPlanResult:
    final_output = "Revised continuation for stages two and three."
    usage = None


class _ReplacementOutput:
    def model_dump(self):
        return {
            "tasks": [
                {
                    "title": "Replacement stage two",
                    "instructions": "Repair and validate replacement stage two.",
                    "is_Coding_Team_required": True,
                    "insights_from_overview": "",
                },
                {
                    "title": "Replacement stage three",
                    "instructions": "Regenerate and validate replacement stage three.",
                    "is_Coding_Team_required": True,
                    "insights_from_overview": "",
                },
            ]
        }


class _ReplacementResult:
    final_output = _ReplacementOutput()
    usage = None


class WholePlanWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self,
        *,
        change_upstream: bool,
        max_attempts: int = 3,
        max_plan_revisions: int = 0,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "spec.md"
            spec.write_text("Build three ordered stages.\n", encoding="utf-8")
            tasks = _tasks()
            verify_order: list[str] = []
            documentation_order: list[str] = []
            task_two_checks = 0

            async def fake_verify(task, runtime, *, attempt_number, accepted_context):
                nonlocal task_two_checks
                task_id = str(task["task_id"])
                verify_order.append(task_id)
                if task_id == "TASK_002":
                    task_two_checks += 1
                    if task_two_checks == 1:
                        return _verdict("fail"), 3
                return _verdict("pass"), 3

            async def fake_document(paths, task, changed_files):
                documentation_order.append(str(task["task_id"]))
                return 1

            def fake_run_stage(paths, task, result, *, attempt_number):
                return {
                    "result": result,
                    "execution": None,
                    "artifacts": None,
                    "deterministic_failure": "",
                }

            _WholePlanSession.repair_scopes = []
            _WholePlanSession.change_upstream = change_upstream
            bundle = MIMIInputBundle(
                spec_path=spec,
                background_path=None,
                image_paths=[],
                max_subtask_attempts=max_attempts,
                max_plan_revisions=max_plan_revisions,
            )

            with (
                patch.object(MIMI, "AGENTS_OUTPUT_ROOT", root / "runs"),
                patch.object(
                    MIMI,
                    "_prepare_tasks",
                    AsyncMock(return_value=(tasks, "complete three-stage plan", 0)),
                ),
                patch.object(MIMI, "AsyncCodex", return_value=_CodexContext()),
                patch.object(MIMI, "CodexPlanSession", _WholePlanSession),
                patch.object(MIMI, "_run_task_stage", side_effect=fake_run_stage),
                patch.object(MIMI, "_verify_task_stage", side_effect=fake_verify),
                patch.object(MIMI, "_document_accepted_change", side_effect=fake_document),
                patch.object(
                    MIMI,
                    "revise_plan_continuation",
                    AsyncMock(return_value=_RevisedPlanResult()),
                ),
                patch.object(
                    MIMI,
                    "run_task_breaker_on_plan",
                    AsyncMock(return_value=_ReplacementResult()),
                ),
            ):
                output = await MIMI.run_pipeline(bundle)

            manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
            return manifest, verify_order, documentation_order, list(_WholePlanSession.repair_scopes)

    async def test_verifies_forward_and_repairs_only_failed_suffix(self):
        manifest, verify_order, documentation_order, repair_scopes = await self._run(
            change_upstream=False
        )

        self.assertEqual(verify_order, ["TASK_001", "TASK_002", "TASK_002", "TASK_003"])
        self.assertEqual(documentation_order, ["TASK_001", "TASK_002", "TASK_003"])
        self.assertEqual(repair_scopes, [["TASK_002", "TASK_003"]])
        self.assertEqual([len(task["attempts"]) for task in manifest["tasks"]], [1, 2, 2])
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(len(manifest["codex_builds"]), 2)
        self.assertEqual(
            {build["thread_id"] for build in manifest["codex_builds"]},
            {"one-whole-plan-thread"},
        )

    async def test_reverifies_upstream_stage_if_repair_changes_accepted_source(self):
        manifest, verify_order, documentation_order, _ = await self._run(change_upstream=True)

        self.assertEqual(
            verify_order,
            ["TASK_001", "TASK_002", "TASK_001", "TASK_002", "TASK_003"],
        )
        self.assertEqual(documentation_order, ["TASK_001", "TASK_001", "TASK_002", "TASK_003"])
        self.assertEqual([len(task["attempts"]) for task in manifest["tasks"]], [2, 2, 2])

    async def test_keeps_plan_recovery_after_stage_uses_its_failure_budget(self):
        manifest, verify_order, documentation_order, repair_scopes = await self._run(
            change_upstream=False,
            max_attempts=1,
            max_plan_revisions=1,
        )

        self.assertEqual(
            verify_order,
            ["TASK_001", "TASK_002", "revision_1_002", "revision_1_003"],
        )
        self.assertEqual(
            documentation_order,
            ["TASK_001", "revision_1_002", "revision_1_003"],
        )
        self.assertEqual(repair_scopes, [["revision_1_002", "revision_1_003"]])
        statuses = {record["task_id"]: record["status"] for record in manifest["tasks"]}
        self.assertEqual(statuses["TASK_002"], "superseded")
        self.assertEqual(statuses["TASK_003"], "superseded")
        self.assertEqual(statuses["revision_1_002"], "accepted")
        self.assertEqual(statuses["revision_1_003"], "accepted")
        self.assertEqual(manifest["status"], "complete")


if __name__ == "__main__":
    unittest.main()
