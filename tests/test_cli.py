import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from thunopro_agent import core, cli


class CliTests(unittest.TestCase):
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
            for index, role in enumerate(r.name for r in core.ROLES):
                db.execute("INSERT INTO panes(pane_id,session_name,role,updated_at) VALUES (?,?,?,0)",
                           (f"%{index + 1}", "thunopro-v1", role))
            db.execute("INSERT INTO panes(pane_id,session_name,role,updated_at) VALUES ('%7','other-session','builder',0)")
            db.execute("INSERT INTO meta(key,value) VALUES ('active_session','thunopro-v1')")

    def tearDown(self):
        self.models_patch.stop()
        self.db_patch.stop()
        self.state_patch.stop()
        self.temp.cleanup()

    def _run(self, *argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = 0
        with patch("sys.argv", ["thuno", *argv]), patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            try:
                cli.main()
            except SystemExit as exc:
                code = 0 if exc.code is None else int(exc.code)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_init_creates_state_and_reports_active_session(self):
        code, out, err = self._run("init")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        data = json.loads(out)
        self.assertEqual(data["initialized"], str(self.db_path))
        self.assertEqual(data["active_tmux_session"], "thunopro-v1")
        self.assertEqual(data["quota_config"], str(self.state_dir / "models.json"))
        self.assertTrue(self.db_path.exists())
        self.assertTrue((self.state_dir / "models.json").exists())

    def test_plan_previews_without_live_model_call(self):
        code, out, err = self._run("plan", "Implement parser", "--kind", "coding")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["kind"], "coding")
        self.assertEqual(data["agent"], "builder")
        self.assertEqual(data["model"], "test/high-quota")
        self.assertIn("score", data)
        self.assertIn("reason", data)

    def test_plan_infers_kind_from_request(self):
        code, out, err = self._run("plan", "Please test this patch")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["kind"], "testing")
        self.assertEqual(data["agent"], "tester")

    def test_plan_rejects_unknown_model_override(self):
        code, out, err = self._run("plan", "x", "--model", "missing/model")
        self.assertEqual(code, 2)
        self.assertIn("Model 'missing/model' is not in the available model catalog", err)

    def test_task_add_uses_active_session(self):
        code, out, err = self._run("task", "add", "--title", "Implement change",
                                   "--description", "Write code", "--agent", "builder")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["agent"], "builder")
        self.assertEqual(data["kind"], "coding")
        self.assertEqual(data["tmux_session"], "thunopro-v1")
        with core.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (data["id"],)).fetchone()
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["tmux_session"], "thunopro-v1")
        self.assertEqual(json.loads(row["depends_on"]), [])

    def test_task_add_explicit_session_and_dependency(self):
        code, out, err = self._run("task", "add", "--title", "Base", "--description", "Write code",
                                   "--agent", "builder", "--session", "other-session")
        self.assertEqual(code, 0)
        base_id = json.loads(out)["id"]
        code, out, err = self._run("task", "add", "--title", "Follow up", "--description", "Write code",
                                   "--agent", "builder", "--session", "other-session",
                                   "--depends-on", base_id)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["tmux_session"], "other-session")
        with core.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (data["id"],)).fetchone()
        self.assertEqual(row["tmux_session"], "other-session")
        self.assertEqual(json.loads(row["depends_on"]), [base_id])

    def test_task_list_filters_by_session(self):
        self._run("task", "add", "--title", "V1 task", "--description", "Write code",
                  "--agent", "builder")
        self._run("task", "add", "--title", "Other task", "--description", "Write code",
                  "--agent", "builder", "--session", "other-session")
        code, out, err = self._run("task", "list")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)), 2)
        code, out, err = self._run("task", "list", "--session", "other-session")
        self.assertEqual(code, 0)
        filtered = json.loads(out)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["tmux_session"], "other-session")

    def test_task_add_errors_for_unbound_session_and_agent(self):
        with core.connect() as db:
            db.execute("DELETE FROM meta WHERE key='active_session'")
        code, out, err = self._run("task", "add", "--title", "No session", "--description", "Write code",
                                   "--agent", "builder")
        self.assertEqual(code, 2)
        self.assertIn("Bind exactly one target tmux session first", err)

        code, out, err = self._run("task", "add", "--title", "Unbound agent", "--description", "Write code",
                                   "--agent", "reviewer", "--session", "other-session")
        self.assertEqual(code, 2)
        self.assertIn("Agent reviewer is not bound in tmux session other-session", err)

    def test_task_add_rejects_unknown_model(self):
        code, out, err = self._run("task", "add", "--title", "Bad model", "--description", "Write code",
                                   "--agent", "builder", "--model", "nope/model")
        self.assertEqual(code, 2)
        self.assertIn("Model 'nope/model' is not in the available model catalog", err)

    def test_task_add_argparse_requires_fields_and_valid_choices(self):
        code, out, err = self._run("task", "add", "--title", "Missing description")
        self.assertEqual(code, 2)
        code, out, err = self._run("task", "add", "--title", "Bad agent", "--description", "Write code",
                                   "--agent", "not-a-role")
        self.assertEqual(code, 2)

    def test_mailbox_send_inbox_and_mark_read(self):
        code, out, err = self._run("message", "send", "--from", "builder", "--to", "reviewer",
                                   "--body", "Please inspect", "--kind", "request")
        self.assertEqual(code, 0)
        message_id = json.loads(out)["message_id"]
        with core.connect() as db:
            row = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        self.assertEqual(row["sender"], "builder")
        self.assertEqual(row["recipient"], "reviewer")
        self.assertEqual(row["body"], "Please inspect")
        self.assertEqual(row["kind"], "request")
        self.assertEqual(row["status"], "new")
        self.assertEqual(row["thread_id"], message_id)
        self.assertIsNone(row["reply_to"])

        code, out, err = self._run("message", "inbox", "--agent", "reviewer")
        self.assertEqual(code, 0)
        messages = json.loads(out)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], message_id)
        self.assertEqual(messages[0]["status"], "new")

        code, out, err = self._run("message", "inbox", "--agent", "builder")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [])

        code, out, err = self._run("message", "inbox", "--agent", "reviewer", "--mark-read")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)), 1)
        with core.connect() as db:
            row = db.execute("SELECT status FROM messages WHERE id=?", (message_id,)).fetchone()
        self.assertEqual(row["status"], "read")

        code, out, err = self._run("message", "inbox", "--agent", "reviewer")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [])

    def test_mailbox_threading_and_unknown_reply(self):
        code, out, err = self._run("message", "send", "--from", "builder", "--to", "reviewer",
                                   "--body", "Parent")
        self.assertEqual(code, 0)
        parent_id = json.loads(out)["message_id"]
        with core.connect() as db:
            parent = db.execute("SELECT thread_id FROM messages WHERE id=?", (parent_id,)).fetchone()

        code, out, err = self._run("message", "send", "--from", "reviewer", "--to", "builder",
                                   "--body", "Reply", "--reply-to", parent_id)
        self.assertEqual(code, 0)
        reply_id = json.loads(out)["message_id"]
        with core.connect() as db:
            reply = db.execute("SELECT thread_id, reply_to, kind FROM messages WHERE id=?", (reply_id,)).fetchone()
        self.assertEqual(reply["thread_id"], parent["thread_id"])
        self.assertEqual(reply["reply_to"], parent_id)
        self.assertEqual(reply["kind"], "reply")

        code, out, err = self._run("message", "send", "--from", "reviewer", "--to", "builder",
                                   "--body", "Orphan", "--reply-to", "missing-id")
        self.assertEqual(code, 2)
        self.assertIn("Cannot reply to unknown message", err)

    def test_message_errors_and_argparse(self):
        code, out, err = self._run("message", "send", "--from", "builder", "--to", "reviewer",
                                   "--body", "hi", "--kind", "invalid-kind")
        self.assertEqual(code, 2)
        code, out, err = self._run("message", "inbox", "--agent", "not-a-role")
        self.assertEqual(code, 2)

    def test_delegate_creates_session_scoped_tasks_without_live_calls(self):
        def fake_run(command, **kwargs):
            if command and command[0] == "opencode" and len(command) > 1 and command[1] == "run":
                payload = {"tasks": [
                    {"title": "Research", "description": "Inspect repository", "kind": "research",
                     "agent": "scout", "acceptance": "Notes provided", "depends_on": []},
                    {"title": "Implement", "description": "Write code", "kind": "coding",
                     "agent": "builder", "acceptance": "Tests pass", "depends_on": [0]},
                ]}
                return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
            raise AssertionError(f"unexpected subprocess call: {command}")

        with patch("subprocess.run", side_effect=fake_run), \
                patch("thunopro_agent.context.snapshot", return_value="ctx"):
            code, out, err = self._run("delegate", "Build feature", "--session", "thunopro-v1")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["tmux_session"], "thunopro-v1")
        self.assertEqual(len(data["tasks"]), 2)
        with core.connect() as db:
            rows = db.execute("SELECT * FROM tasks WHERE tmux_session='thunopro-v1' ORDER BY created_at").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["assigned_agent"], "scout")
        self.assertEqual(rows[1]["assigned_agent"], "builder")
        self.assertEqual(json.loads(rows[1]["depends_on"]), [rows[0]["id"]])
        self.assertEqual(rows[0]["acceptance"], "Notes provided")

    def test_plan_objective_rejects_non_json_commander_output(self):
        with patch("subprocess.run", side_effect=lambda command, **kwargs:
                   SimpleNamespace(returncode=0, stdout="not json", stderr="")):
            with self.assertRaises(ValueError):
                cli._plan_objective("objective", None)


if __name__ == "__main__":
    unittest.main()
