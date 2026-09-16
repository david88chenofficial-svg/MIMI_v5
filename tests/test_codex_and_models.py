import json
import tempfile
import unittest
from pathlib import Path

from MIMI_codex import CodexPlanSession, PLAN_CODING_OUTPUT_SCHEMA
from MIMI_models import (
    MODEL_CATALOG,
    MODEL_DEFAULTS,
    AgentModelConfig,
    model_options_for_agent,
    selected_model,
)


class _LastUsage:
    input_tokens = 10
    output_tokens = 5
    total_tokens = 15


class _Usage:
    last = _LastUsage()


class _Result:
    usage = _Usage()

    def __init__(self, final_response: str):
        self.final_response = final_response


class _FakeThread:
    id = "persistent-task-thread"

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.run_count = 0

    async def run(self, prompt, **kwargs):
        self.run_count += 1
        (self.workspace / "product.py").write_text(
            f"VALUE = {self.run_count}\n", encoding="utf-8"
        )
        return _Result(
            json.dumps(
                {
                    "summary": f"change {self.run_count}",
                    "entrypoint": "product.py",
                    "tests_run": [],
                    "notes": [],
                }
            )
        )


class _FakeCodex:
    def __init__(self, workspace: Path):
        self.thread = _FakeThread(workspace)
        self.start_count = 0

    async def thread_start(self, **kwargs):
        self.start_count += 1
        return self.thread


class _WholePlanThread(_FakeThread):
    async def run(self, prompt, **kwargs):
        self.run_count += 1
        if not hasattr(self, "schemas"):
            self.schemas = []
        self.schemas.append(kwargs.get("output_schema"))
        task_ids = ["TASK_001", "TASK_002"] if self.run_count == 1 else ["TASK_002"]
        for task_id in task_ids:
            filename = f"{task_id.lower()}.py"
            (self.workspace / filename).write_text("VALUE = 1\n", encoding="utf-8")
        return _Result(
            json.dumps(
                {
                    "summary": f"plan change {self.run_count}",
                    "tasks": [
                        {
                            "task_id": task_id,
                            "summary": f"implemented {task_id}",
                            "entrypoint": f"{task_id.lower()}.py",
                            "files": [f"{task_id.lower()}.py"],
                            "tests_run": [],
                            "notes": [],
                        }
                        for task_id in task_ids
                    ],
                    "notes": [],
                }
            )
        )


class _WholePlanCodex(_FakeCodex):
    def __init__(self, workspace: Path):
        self.thread = _WholePlanThread(workspace)
        self.start_count = 0


class CodexAndModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_whole_plan_and_suffix_repair_share_one_thread(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            codex = _WholePlanCodex(workspace)
            session = CodexPlanSession(codex, workspace, model="gpt-5.6-sol")
            tasks = [
                {
                    "task_id": "TASK_001",
                    "instructions": "context only",
                    "is_Coding_Team_required": False,
                },
                {
                    "task_id": "TASK_002",
                    "instructions": "second",
                    "is_Coding_Team_required": True,
                },
            ]

            initial = await session.implement_plan(plan="complete plan", tasks=tasks, code_index={})
            repair = await session.repair_from_task(
                plan="complete plan",
                remaining_tasks=tasks[1:],
                failure="second stage failed",
                verifier_feedback="wrong sign",
                accepted_context="first stage accepted",
                code_index={},
            )

            self.assertEqual(codex.start_count, 1)
            self.assertEqual(initial.thread_id, repair.thread_id)
            self.assertEqual([item.task_id for item in initial.tasks], ["TASK_002"])
            self.assertEqual([item.task_id for item in repair.tasks], ["TASK_002"])
            self.assertTrue(
                all(schema is PLAN_CODING_OUTPUT_SCHEMA for schema in codex.thread.schemas)
            )

    async def test_legacy_documentation_model_name_migrates(self):
        config = AgentModelConfig(
            models={
                "coder_secretary": "gpt-5.6-luna",
                "supervisor": "gpt-5.6-terra",
            }
        )
        config.validate()
        self.assertEqual(config.models, {"documentation": "gpt-5.6-luna"})
        self.assertEqual(selected_model(config, "coder"), "gpt-5.6-luna")

    async def test_dashboard_separates_api_and_codex_model_options(self):
        codex_models = {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
        api_models = codex_models | {
            "gpt-5-mini",
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.4-nano",
        }
        self.assertEqual(set(MODEL_CATALOG), api_models)
        self.assertEqual(set(model_options_for_agent("coder")), codex_models)
        for agent_key in ("planner", "task_breaker", "verifier", "documentation"):
            self.assertEqual(set(model_options_for_agent(agent_key)), api_models)

    async def test_api_only_model_is_rejected_for_codex_but_allowed_for_documentation(self):
        AgentModelConfig(models={"documentation": "gpt-5.4-mini"}).validate()
        with self.assertRaisesRegex(ValueError, "codex runtime"):
            AgentModelConfig(models={"coder": "gpt-5.4-mini"}).validate()

    async def test_agent_defaults_use_mini_and_luna(self):
        self.assertEqual(MODEL_DEFAULTS["planner"], "gpt-5-mini")
        self.assertEqual(MODEL_DEFAULTS["task_breaker"], "gpt-5-mini")
        self.assertEqual(MODEL_DEFAULTS["coder"], "gpt-5.6-luna")
        self.assertEqual(MODEL_DEFAULTS["verifier"], "gpt-5-mini")
        self.assertEqual(MODEL_DEFAULTS["documentation"], "gpt-5-mini")


if __name__ == "__main__":
    unittest.main()
