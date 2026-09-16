import asyncio
import io
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

import MIMI_dashboard
import MIMI
from MIMI import _await_with_activity


class DashboardRuntimeStateTests(unittest.TestCase):
    def setUp(self):
        MIMI_dashboard.reset_web_state("Starting test run")

    def tearDown(self):
        MIMI_dashboard.update_web_state(running=False, phase="idle", message="Idle")

    def test_subtask_badge_tracks_live_phase_and_attempt(self):
        payload = {
            "tasks": [
                {
                    "task_number": 1,
                    "task_id": "task_1",
                    "instructions": "Build the calculation core.",
                    "is_Coding_Team_required": True,
                }
            ]
        }
        MIMI_dashboard.update_web_state(
            task_breaker={
                "raw": "",
                "subtasks": MIMI_dashboard.task_breaker_payload_to_subtasks(payload),
            }
        )

        changed = MIMI_dashboard.update_subtask_status(
            "task_1",
            "coding",
            attempt_count=1,
            max_attempts=4,
            detail="Codex is editing the workspace.",
        )

        self.assertTrue(changed)
        subtask = MIMI_dashboard.snapshot_web_state()["task_breaker"]["subtasks"][0]
        self.assertEqual(subtask["status"], "coding")
        self.assertEqual(subtask["attempt_count"], 1)
        self.assertEqual(subtask["max_attempts"], 4)
        self.assertIn("editing", subtask["detail"])

    def test_activity_is_visible_in_status_snapshot(self):
        terminal = io.StringIO()
        with redirect_stdout(terminal):
            MIMI_dashboard.add_activity(
                "Coder (Codex)",
                "Started implementing the task.",
                status="running",
                task_id="task_2",
                attempt=1,
            )

        activity = MIMI_dashboard.snapshot_web_state()["activity"]
        self.assertEqual(activity[-1]["agent"], "Coder (Codex)")
        self.assertEqual(activity[-1]["task_id"], "task_2")
        self.assertEqual(activity[-1]["attempt"], 1)
        self.assertIn(
            "Coder (Codex) [task_2] attempt 1: Started implementing",
            terminal.getvalue(),
        )

    def test_dashboard_has_live_terminal_and_agent_type_labels(self):
        project_root = Path(MIMI_dashboard.__file__).resolve().parent
        html = (project_root / "web" / "index.html").read_text(encoding="utf-8")
        javascript = (project_root / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="activity-output"', html)
        self.assertIn("Coder (Codex)", html)
        self.assertIn("Planner (API agent)", html)
        self.assertIn("renderActivity(state)", javascript)

    def test_dashboard_exposes_review_and_recovery_limits(self):
        project_root = Path(MIMI_dashboard.__file__).resolve().parent
        html = (project_root / "web" / "index.html").read_text(encoding="utf-8")
        javascript = (project_root / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="max-subtask-attempts"', html)
        self.assertIn('id="max-plan-revisions"', html)
        self.assertIn("maxSubtaskAttempts:", javascript)
        self.assertIn("maxPlanRevisions:", javascript)


class AgentHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_agent_call_emits_periodic_terminal_heartbeat(self):
        async def slow_result():
            await asyncio.sleep(0.035)
            return "finished"

        terminal = io.StringIO()
        with redirect_stdout(terminal):
            result = await _await_with_activity(
                slow_result(),
                agent="Verifier (API agent)",
                message="Verifier is still running",
                task_id="task_3",
                interval_seconds=0.01,
            )

        self.assertEqual(result, "finished")
        self.assertIn(
            "Verifier (API agent) [task_3]: Verifier is still running",
            terminal.getvalue(),
        )


class BrowserShutdownTests(unittest.TestCase):
    def test_active_manifest_is_marked_aborted_during_forced_shutdown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            run_root = Path(temp_dir)
            manifest_path = run_root / "run_manifest.json"
            manifest_path.write_text(
                '{"status": "running", "tasks": []}',
                encoding="utf-8",
            )
            previous_root = MIMI.ACTIVE_RUN_ROOT
            try:
                MIMI.ACTIVE_RUN_ROOT = run_root
                result = MIMI.mark_active_run_terminated(
                    "aborted",
                    "Browser closed.",
                )
            finally:
                MIMI.ACTIVE_RUN_ROOT = previous_root

            manifest = MIMI.read_json(manifest_path, {})
            self.assertEqual(result, run_root)
            self.assertEqual(manifest["status"], "aborted")
            self.assertEqual(manifest["termination_reason"], "Browser closed.")
            self.assertIn("finished_at", manifest)

    def test_launcher_exit_does_not_wait_for_non_daemon_worker(self):
        project_root = Path(MIMI_dashboard.__file__).resolve().parent
        script = (
            "import threading, time; "
            "from launch_MIMI import exit_without_lingering_workers; "
            "threading.Thread(target=time.sleep, args=(30,), daemon=False).start(); "
            "exit_without_lingering_workers(0)"
        )

        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=project_root,
            timeout=3,
            check=False,
        )

        self.assertEqual(completed.returncode, 0)

    def test_closing_last_browser_aborts_run_and_releases_server(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        aborted = threading.Event()
        control_token = "shutdown-test-token"

        def abort_run():
            aborted.set()
            MIMI_dashboard.update_web_state(
                running=False,
                phase="aborted",
                message="Run aborted by browser close.",
            )
            return True

        MIMI_dashboard.update_web_state(
            running=True,
            phase="coder",
            message="Coder is running.",
        )
        server_thread = threading.Thread(
            target=MIMI_dashboard.serve_web,
            args=("127.0.0.1", port, lambda bundle: None),
            kwargs={
                "abort_run": abort_run,
                "auto_shutdown": True,
                "disconnect_grace_seconds": 0.05,
                "heartbeat_timeout_seconds": 0.5,
                "run_shutdown_grace_seconds": 0.5,
                "control_token": control_token,
            },
            daemon=True,
        )
        server_thread.start()

        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.01)
            else:
                self.fail("MIMI test server did not start.")

            heartbeat = urllib.request.Request(
                f"http://127.0.0.1:{port}/heartbeat",
                method="POST",
                data=b"",
            )
            with urllib.request.urlopen(heartbeat, timeout=1) as response:
                self.assertEqual(response.status, 204)

            disconnect = urllib.request.Request(
                f"http://127.0.0.1:{port}/disconnect",
                method="POST",
                data=b"closed",
            )
            with urllib.request.urlopen(disconnect, timeout=1) as response:
                self.assertEqual(response.status, 204)

            self.assertTrue(aborted.wait(2), "Browser close did not abort the active run.")
            server_thread.join(2)
            self.assertFalse(server_thread.is_alive(), "MIMI server remained stuck after tab close.")
            self.assertTrue(MIMI_dashboard.MIMIThreadingHTTPServer.daemon_threads)
            self.assertFalse(MIMI_dashboard.MIMIThreadingHTTPServer.block_on_close)
        finally:
            if server_thread.is_alive():
                shutdown = urllib.request.Request(
                    f"http://127.0.0.1:{port}/shutdown",
                    method="POST",
                    headers={"X-MIMI-Control-Token": control_token},
                    data=b"",
                )
                try:
                    urllib.request.urlopen(shutdown, timeout=1).close()
                except OSError:
                    pass
                server_thread.join(2)
            MIMI_dashboard.update_web_state(running=False, phase="idle", message="Idle")


if __name__ == "__main__":
    unittest.main()
