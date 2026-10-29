import tempfile
import unittest
from contextlib import redirect_stdout
import io
from pathlib import Path
from types import SimpleNamespace
from types import SimpleNamespace
from unittest.mock import patch

from thunopro_agent import core


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.state_dir = base / ".thunopro"
        self.db_path = self.state_dir / "state.sqlite3"
        self.state_patch = patch.object(core, "STATE_DIR", self.state_dir)
        self.db_patch = patch.object(core, "DB_PATH", self.db_path)
        self.high_quota = core.Model("test/high-quota", ("coding", "reasoning", "research", "review", "testing"),
                                     3, 3, 3, 3, 4, "verified high allowance")
        self.strong = core.Model("test/strong", ("coding", "reasoning", "research", "review", "testing"),
                                 5, 5, 5, 3, 0, "unverified")
        self.models_patch = patch.object(core, "available_models", return_value=[self.high_quota, self.strong])
        self.state_patch.start()
        self.db_patch.start()
        self.models_patch.start()
        with core.connect() as db:
            db.execute("INSERT INTO panes(pane_id,session_name,role,updated_at) VALUES('%%1','thunopro-v1','builder',0)")
            db.execute("INSERT INTO panes(pane_id,session_name,role,updated_at) VALUES('%%2','other-session','builder',0)")
            db.execute("INSERT INTO meta(key,value) VALUES('active_session','thunopro-v1')")

    def tearDown(self):
        self.models_patch.stop()
        self.db_patch.stop()
        self.state_patch.stop()
        self.temp.cleanup()

    def test_router_prioritizes_verified_quota_after_capability_gate(self):
        winner, _, rationale = core.choose_model("coding")
        self.assertEqual(winner.id, "test/high-quota")
        self.assertIn("quota_tier=4", rationale)

    def test_task_is_bound_to_selected_tmux_session(self):
        task = core.add_task("Implement change", "Write code", "builder", "coding")
        self.assertEqual(task["tmux_session"], "thunopro-v1")
        other = core.add_task("Scoped change", "Write code", "builder", "coding", session="other-session")
        self.assertEqual(other["tmux_session"], "other-session")

    def test_dispatch_targets_only_pane_bound_to_task_session(self):
        task = core.add_task("Scoped change", "Write code", "builder", "coding", session="other-session")
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if command[1] == "display-message":
                return SimpleNamespace(returncode=0, stdout="bash\t" + str(core.ROOT))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(core.subprocess, "run", side_effect=fake_run):
            pane = core.dispatch_task(task["id"])
        self.assertEqual(pane, "%%2")
        sends = [call for call in calls if call[1] == "send-keys"]
        self.assertTrue(sends)
        self.assertTrue(all(call[call.index("-t") + 1] == "%%2" for call in sends))

    def test_claim_checks_assignment_and_dependencies(self):
        first = core.add_task("First", "Implement", "builder", "coding")
        second = core.add_task("Second", "Implement", "builder", "coding", depends_on=[first["id"]])
        with self.assertRaisesRegex(ValueError, "Unfinished dependencies"):
            core.task_claim(second["id"], "builder")
        core.task_claim(first["id"], "builder")
        core.task_complete(first["id"], "builder", "Tests pass")
        with self.assertRaisesRegex(ValueError, "Only the commander"):
            core.task_accept(first["id"], "accept", agent="tester")
        core.task_accept(first["id"], "accept")
        core.task_claim(second["id"], "builder")
        with self.assertRaisesRegex(ValueError, "assigned to builder"):
            core.task_complete(second["id"], "reviewer", "wrong role")

    def test_worker_falls_back_after_provider_failure_and_waits_for_acceptance(self):
        task = core.add_task("Implement bounded feature", "Write code", "builder", "coding")
        core.task_claim(task["id"], "builder")
        calls = []
        second = core.Model("test/second-unlimited", ("reasoning", "coding", "research", "review", "testing"),
                            3, 3, 3, 3, 4, "verified unlimited")

        def fake_stream(command, log_path, timeout):
            calls.append(command)
            if len(calls) == 1:
                return 1, "Upstream request failed: region restricted"
            return 0, "Implemented; tests passed"

        with patch.object(core, "available_models", return_value=[self.high_quota, second, self.strong]), \
                patch.object(core, "_run_streaming", side_effect=fake_stream), redirect_stdout(io.StringIO()):
            code = core.run_worker_task(task["id"], max_attempts=2)
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][calls[0].index("--model") + 1], "test/high-quota")
        # Workers never fall back to unverified/metered models: only the second unlimited model is eligible.
        self.assertEqual(calls[1][calls[1].index("--model") + 1], "test/second-unlimited")
        with core.connect() as db:
            row = db.execute("SELECT status FROM tasks WHERE id=?", (task["id"],)).fetchone()
            attempts = db.execute("SELECT status FROM attempts WHERE task_id=? ORDER BY started_at",
                                  (task["id"],)).fetchall()
        self.assertEqual(row["status"], "awaiting_review")
        self.assertEqual([attempt["status"] for attempt in attempts], ["failed", "done"])
        with self.assertRaisesRegex(ValueError, "Only the commander"):
            core.task_accept(task["id"], "accept", agent="reviewer")
        core.task_accept(task["id"], "accept", notes="Independent review passed")
        with core.connect() as db:
            row = db.execute("SELECT status FROM tasks WHERE id=?", (task["id"],)).fetchone()
        self.assertEqual(row["status"], "done")

    def test_mailbox_is_addressed_and_durable(self):
        message = core.send_message("builder", "reviewer", "Review task xyz", "request")
        reply = core.send_message("reviewer", "builder", "Will inspect", reply_to=message)
        with core.connect() as db:
            row = db.execute("SELECT * FROM messages WHERE id=?", (message,)).fetchone()
            reply_row = db.execute("SELECT * FROM messages WHERE id=?", (reply,)).fetchone()
        self.assertEqual(row["recipient"], "reviewer")
        self.assertEqual(row["status"], "new")
        self.assertEqual(reply_row["thread_id"], row["thread_id"])
        self.assertEqual(reply_row["kind"], "reply")
        with self.assertRaisesRegex(ValueError, "unknown message"):
            core.send_message("reviewer", "builder", "orphan", reply_to="missing")

    def test_infer_kind_and_reject_unknown(self):
        self.assertEqual(core.infer_kind("Please test this patch"), "testing")
        with self.assertRaises(ValueError):
            core.infer_kind("something", "invalid")

    def test_quota_cannot_override_minimum_quality_gate(self):
        weak = core.Model("test/weak-unlimited", ("coding",), 2, 2, 2, 5, 4,
                          "verified unlimited")
        with self.assertRaisesRegex(RuntimeError, "minimum quality floor"):
            core.choose_model("coding", [weak])

    def test_free_model_wins_close_quality_tie_but_not_verified_quota(self):
        paid = core.Model("test/paid", ("coding",), 4, 4, 4, 3, 0)
        free = core.Model("test/model-free", ("coding",), 4, 4, 4, 3, 0)
        self.assertEqual(core.choose_model("coding", [paid, free])[0].id, "test/model-free")
        unlimited = core.Model("test/unlimited", ("coding",), 3, 3, 3, 3, 4)
        self.assertEqual(core.choose_model("coding", [free, unlimited])[0].id, "test/unlimited")


if __name__ == "__main__":
    unittest.main()
