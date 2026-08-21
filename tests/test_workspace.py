import json
import os
import tempfile
import unittest
from pathlib import Path

from MIMI_workspace import (
    RunPaths,
    WorkspaceContractError,
    build_code_index,
    collect_artifacts,
    initialize_workspace,
    materialize_tasks,
    render_code_index,
    resolve_entrypoint,
    sanitized_subprocess_env,
    sha256_file,
)


class WorkspaceContractTests(unittest.TestCase):
    def test_run_layout_and_inline_task_materialization(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = RunPaths.create(Path(temporary), "run")
            initialize_workspace(paths)
            tasks = materialize_tasks(
                [
                    {
                        "title": "Build solver",
                        "instructions": "Implement and validate the solver.",
                        "is_Coding_Team_required": True,
                        "insights_from_overview": "",
                    }
                ],
                paths.tasks,
            )
            self.assertEqual(tasks[0]["task_id"], "task_001")
            instruction_path = Path(tasks[0]["instruction_path"])
            self.assertTrue(instruction_path.is_relative_to(paths.tasks))
            self.assertEqual(
                instruction_path.read_text(encoding="utf-8").strip(),
                "Implement and validate the solver.",
            )
            self.assertTrue((paths.workspace / "AGENTS.md").is_file())

    def test_legacy_task_lookup_is_limited_to_exact_source_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "01_task.txt").write_text("Exact legacy instructions", encoding="utf-8")
            tasks = materialize_tasks(
                [
                    {
                        "sub_filename": "01_task.txt",
                        "is_Coding_Team_required": True,
                    }
                ],
                root / "tasks",
                source_dirs=[source],
            )
            self.assertEqual(tasks[0]["instructions"], "Exact legacy instructions")
            with self.assertRaises(WorkspaceContractError):
                materialize_tasks(
                    [
                        {
                            "sub_filename": "01_task.txt",
                            "is_Coding_Team_required": True,
                        }
                    ],
                    root / "other_tasks",
                    source_dirs=[root / "unrelated"],
                )

    def test_artifact_manifest_cannot_escape_attempt_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary) / "attempt"
            attempt.mkdir()
            outside = Path(temporary) / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            (attempt / "result.json").write_text(
                json.dumps(
                    {
                        "summary": "bad",
                        "artifacts": {
                            "plots": {},
                            "texts": {
                                "escape": {
                                    "path": "../outside.txt",
                                    "description": "outside",
                                }
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(WorkspaceContractError):
                collect_artifacts(attempt)

    def test_result_manifest_must_be_at_attempt_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary) / "attempt"
            nested = attempt / "artifacts"
            nested.mkdir(parents=True)
            (nested / "numbers.txt").write_text("value=2", encoding="utf-8")
            (nested / "result.json").write_text(
                json.dumps(
                    {
                        "summary": "nested",
                        "artifacts": {
                            "plots": {},
                            "texts": {
                                "numbers": {
                                    "path": "numbers.txt",
                                    "description": "numeric evidence",
                                }
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(WorkspaceContractError, "directly"):
                collect_artifacts(attempt)

    def test_valid_artifacts_and_hash_bound_documentation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            source = workspace / "solver.py"
            source.write_text(
                '"""Small solver."""\n\ndef solve(x: float) -> float:\n    return 2 * x\n',
                encoding="utf-8",
            )
            annotations = {
                "solver.py": {
                    "sha256": sha256_file(source),
                    "purpose": "Double a scalar.",
                }
            }
            record = build_code_index(workspace, annotations=annotations)["files"][0]
            self.assertEqual(record["agent_documentation"]["purpose"], "Double a scalar.")
            source.write_text(source.read_text(encoding="utf-8") + "\nVALUE = 2\n", encoding="utf-8")
            changed = build_code_index(workspace, annotations=annotations)["files"][0]
            self.assertNotIn("agent_documentation", changed)

            attempt = root / "attempt"
            attempt.mkdir()
            (attempt / "numbers.txt").write_text("value=2", encoding="utf-8")
            (attempt / "result.json").write_text(
                json.dumps(
                    {
                        "summary": "ok",
                        "artifacts": {
                            "plots": {},
                            "texts": {
                                "numbers": {
                                    "path": "numbers.txt",
                                    "description": "numeric evidence",
                                }
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            artifacts = collect_artifacts(attempt)
            self.assertEqual(artifacts.summary, "ok")
            self.assertEqual(artifacts.text_descriptions, ["numeric evidence"])

    def test_compact_index_stays_valid_json_and_entrypoint_stays_in_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            for index in range(20):
                (workspace / f"module_{index}.py").write_text(
                    f'"""Module {index} with a deliberately long description."""\n'
                    f"def function_{index}(value: float) -> float:\n    return value\n",
                    encoding="utf-8",
                )
            rendered = render_code_index(build_code_index(workspace), max_chars=800)
            parsed = json.loads(rendered)
            self.assertTrue(parsed["truncated"])
            self.assertLessEqual(len(rendered), 800)

            runnable = workspace / "main.py"
            runnable.write_text("if __name__ == '__main__':\n    print('ok')\n", encoding="utf-8")
            self.assertEqual(
                resolve_entrypoint(workspace, "main.py", []), runnable.resolve()
            )
            with self.assertRaises(WorkspaceContractError):
                resolve_entrypoint(workspace, "../outside.py", [])

    def test_subprocess_environment_removes_secret_variables(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            previous = os.environ.get("MIMI_TEST_API_KEY")
            os.environ["MIMI_TEST_API_KEY"] = "must-not-leak"
            try:
                environment = sanitized_subprocess_env(root, root / "artifacts")
            finally:
                if previous is None:
                    os.environ.pop("MIMI_TEST_API_KEY", None)
                else:
                    os.environ["MIMI_TEST_API_KEY"] = previous
            self.assertNotIn("MIMI_TEST_API_KEY", environment)
            self.assertEqual(environment["MIMI_ARTIFACT_DIR"], str(root / "artifacts"))


if __name__ == "__main__":
    unittest.main()
