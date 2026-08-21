import json
import tempfile
import unittest
from pathlib import Path

from MIMI_codex import CodexTaskSession
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


class CodexAndModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_repair_reuses_the_same_codex_thread(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            codex = _FakeCodex(workspace)
            session = CodexTaskSession(
                codex,
                workspace,
                model="gpt-5.6-sol",
                effort="medium",
            )
            first = await session.implement("create product", {"files": []})
            second = await session.repair(
                task="create product",
                failure="value was wrong",
                verifier_feedback="expected two",
            )
            self.assertEqual(codex.start_count, 1)
            self.assertEqual(first.thread_id, second.thread_id)
            self.assertEqual(second.total_tokens, 15)
            self.assertEqual(second.changed_files, ["product.py"])

    async def test_legacy_documentation_model_name_migrates(self):
        config = AgentModelConfig(
            models={
                "coder_secretary": "gpt-5.6-luna",
                "supervisor": "gpt-5.6-terra",
            }
        )
        config.validate()
        self.assertEqual(config.models, {"documentation": "gpt-5.6-luna"})
        self.assertEqual(selected_model(config, "coder"), "gpt-5.6-sol")

    async def test_dashboard_separates_api_and_codex_model_options(self):
        codex_models = {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
        api_models = codex_models | {
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

    async def test_agent_defaults_use_role_appropriate_gpt_5_6_models(self):
        self.assertEqual(MODEL_DEFAULTS["planner"], "gpt-5.6-sol")
        self.assertEqual(MODEL_DEFAULTS["task_breaker"], "gpt-5.6-terra")
        self.assertEqual(MODEL_DEFAULTS["coder"], "gpt-5.6-sol")
        self.assertEqual(MODEL_DEFAULTS["verifier"], "gpt-5.6-sol")
        self.assertEqual(MODEL_DEFAULTS["documentation"], "gpt-5.6-luna")


if __name__ == "__main__":
    unittest.main()
