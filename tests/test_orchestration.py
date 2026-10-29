import io
import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from thunopro_agent import cli, core


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.state_dir = base / ".thunopro"
        self.db_path = self.state_dir / "state.sqlite3"
        self.state_patch = patch.object(core, "STATE_DIR", self.state_dir)
        self.db_patch = patch.object(core, "DB_PATH", self.db_path)
        self.models_patch = patch.object(core, "available_models", return_value=[
            core.Model("test/high-quota", ("reasoning", "coding", "research", "review",
                                           "testing", "long_context", "fast"), 3, 3, 3, 3, 4,
                       "verified high allowance"),
            core.Model("test/strong", ("reasoning", "coding", "research", "review",
                                       "testing", "long_context", "fast"), 5, 5, 5, 3, 0,
                       "unverified"),
        ])
        self.state_patch.start()
        self.db_patch.start()
        self.models_patch.start()
        with core.connect() as db:
            for index, role in enumerate(role.name for role in core.ROLES):
                db.execute(
                    "INSERT INTO panes(pane_id,session_name,role,updated_at) VALUES (?,?,?,0)",
                    (f"%{index + 1}", "thunopro-v1", role),
                )
            db.execute("INSERT INTO meta(key,value) VALUES('active_session','thunopro-v1')")

    def tearDown(self):
        self.models_patch.stop()
        self.db_patch.stop()
        self.state_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def _successful_tmux(command, **kwargs):
        if command[1] == "display-message":
            return SimpleNamespace(returncode=0, stdout=f"bash\t{core.ROOT}", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def _run_cli(self, *argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = 0
        with patch("sys.argv", ["thuno", *argv]), patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            try:
                cli.main()
            except SystemExit as exc:
                code = 0 if exc.code is None else int(exc.code)
        return code, stdout.getvalue(), stderr.getvalue()

    def _status(self, task_id):
        with core.connect() as db:
            row = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        return row["status"]

    def test_dispatch_ready_allows_reviewer_after_awaiting_review_but_blocks_builder(self):
        builder = core.add_task("Build base", "Implement the base change", "builder", "coding")
        reviewer = core.add_task(
            "Review base", "Review the base change", "reviewer", "review",
            depends_on=[builder["id"]],
        )
        dependent_builder = core.add_task(
            "Extend base", "Implement a follow-up change", "builder", "coding",
            depends_on=[builder["id"]],
        )
        core.task_claim(builder["id"], "builder")
        core.task_complete(builder["id"], "builder", "Base implemented")

        with patch.object(core.subprocess, "run", side_effect=self._successful_tmux):
            dispatched = core.dispatch_ready("thunopro-v1")

        self.assertEqual(len(dispatched), 1)
        self.assertEqual(self._status(builder["id"]), "awaiting_review")
        self.assertEqual(self._status(reviewer["id"]), "running")
        self.assertEqual(self._status(dependent_builder["id"]), "queued")

    def test_dispatch_task_send_keys_failure_rolls_back_and_emits_event(self):
        task = core.add_task("Build feature", "Implement a feature", "builder", "coding")

        def failing_tmux(command, **kwargs):
            if command[1] == "send-keys":
                raise subprocess.CalledProcessError(1, command, stderr="send failed")
            return self._successful_tmux(command, **kwargs)

        with patch.object(core.subprocess, "run", side_effect=failing_tmux):
            with self.assertRaises(subprocess.CalledProcessError):
                core.dispatch_task(task["id"])

        self.assertEqual(self._status(task["id"]), "queued")
        with core.connect() as db:
            events = db.execute(
                "SELECT event FROM events WHERE task_id=? AND event='task.dispatch_failed'",
                (task["id"],),
            ).fetchall()
        self.assertEqual(len(events), 1)

    def test_dispatch_ready_does_not_claim_second_task_for_busy_role(self):
        running = core.add_task("First build", "Implement the first change", "builder", "coding")
        queued = core.add_task("Second build", "Implement the second change", "builder", "coding")
        core.task_claim(running["id"], "builder")

        with patch.object(core.subprocess, "run", side_effect=self._successful_tmux) as tmux:
            dispatched = core.dispatch_ready("thunopro-v1")

        self.assertEqual(dispatched, [])
        self.assertEqual(self._status(running["id"]), "running")
        self.assertEqual(self._status(queued["id"]), "queued")
        tmux.assert_not_called()

    def test_delegate_run_dispatches_ready_tasks_in_requested_session(self):
        planned = {
            "title": "Build feature",
            "description": "Implement a bounded feature",
            "acceptance": "Tests pass",
            "kind": "coding",
            "agent": "builder",
            "depends_on": [],
        }
        created = {"id": "task-1", "title": planned["title"], "agent": "builder"}
        with patch.object(cli, "_plan_objective", return_value=([planned], "test/high-quota")), \
                patch.object(core, "add_task", return_value=created), \
                patch.object(core, "dispatch_ready", return_value=[{"task_id": "task-1"}]) as dispatch_ready:
            code, out, err = self._run_cli(
                "delegate", "Build feature", "--session", "thunopro-v1", "--run",
            )

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["tmux_session"], "thunopro-v1")
        dispatch_ready.assert_called_once_with("thunopro-v1")

    def test_dispatch_ready_uses_descending_priority(self):
        low = core.add_task("Design", "Design a component", "architect", "reasoning", priority=1)
        high = core.add_task("Research", "Research the requirements", "scout", "research", priority=5)
        medium = core.add_task("Test", "Validate the behavior", "tester", "testing", priority=3)

        with patch.object(core.subprocess, "run", side_effect=self._successful_tmux) as tmux:
            core.dispatch_ready("thunopro-v1")

        sent_task_ids = []
        for call in tmux.call_args_list:
            command = call.args[0]
            if command[1] == "send-keys" and "-l" in command:
                sent_task_ids.append(shlex.split(command[-1])[-1])
        self.assertEqual(sent_task_ids, [high["id"], medium["id"], low["id"]])


if __name__ == "__main__":
    unittest.main()
