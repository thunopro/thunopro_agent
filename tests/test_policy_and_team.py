import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from thunopro_agent import agents, context, core, orchestrator, policy, scorecard


class PolicyCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name) / ".thunopro"
        self.state_dir.mkdir()
        self.patches = [
            patch.object(core, "STATE_DIR", self.state_dir),
            patch.object(core, "DB_PATH", self.state_dir / "state.sqlite3"),
            patch.object(core.shutil, "which", return_value="/usr/bin/opencode"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def _catalog(self, config, listing):
        (self.state_dir / "models.json").write_text(json.dumps(config), encoding="utf-8")
        stdout = "\n".join(listing) + "\n"
        return patch.object(core.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=stdout, stderr=""))

    def test_only_allowed_provider_and_verified_unlimited_models_run_workers(self):
        listing = ["opencode/beam-free", "opencode-go/beam-free", "opencode-go/gpt-6-luna",
                   "opencode-go/iceman-contributor-free"]
        config = {
            "routing": {"worker_mode": "unlimited_only", "allowed_providers": ["opencode-go"]},
            "models": {
                "opencode-go/beam-free": {"verified": True, "quota_tier": 4},
                "opencode-go/iceman-contributor-free": {"verified": True, "quota_tier": 4,
                                                        "trains_on_request_data": True},
                "opencode-go/gpt-6-luna": {"verified": False, "quota_tier": 4},
            },
        }
        with self._catalog(config, listing):
            models = core.available_models()
            ids = [m.id for m in models]
            self.assertEqual(ids, ["opencode-go/beam-free", "opencode-go/gpt-6-luna"])
            self.assertEqual(core.unlimited_models()[0].id, "opencode-go/beam-free")

    def test_training_models_are_excluded_unless_explicitly_allowed(self):
        listing = ["opencode-go/iceman-contributor-free", "opencode-go/gpt-6-luna"]
        config = {"models": {"opencode-go/iceman-contributor-free": {"verified": True, "quota_tier": 4}}}
        with self._catalog(config, listing):
            with self.assertRaisesRegex(RuntimeError, "No verified unlimited model"):
                core.unlimited_models()
        config["routing"] = {"allow_models_trained_on_request_data": True}
        with self._catalog(config, listing):
            self.assertEqual([m.id for m in core.unlimited_models()], ["opencode-go/iceman-contributor-free"])

    def test_unverified_quota_tier_is_never_treated_as_unlimited(self):
        listing = ["opencode-go/beam-free"]
        config = {"models": {"opencode-go/beam-free": {"verified": False, "quota_tier": 4}}}
        with self._catalog(config, listing):
            with self.assertRaises(RuntimeError):
                core.unlimited_models()

    def test_policy_rejects_metered_worker_mode(self):
        with self.assertRaises(ValueError):
            policy.RoutingPolicy.from_config({"worker_mode": "quota_first"})

    def test_documented_training_models_are_flagged_by_default(self):
        self.assertTrue(policy.trains_on_request_data("opencode-go/muse-spark-1.3-contributor"))
        self.assertFalse(policy.trains_on_request_data("opencode-go/beam-free"))
        self.assertEqual(policy.request_estimates("opencode-go/beam-free"), (None, None, None))


class ParsingTests(unittest.TestCase):
    def test_extract_json_object_ignores_tool_logs_and_fences(self):
        noisy = ("> build · beam-free\n$ ls {not json}\n```json\n"
                 '{"verdict": "accept", "notes": "evidence present"}\n```\ndone')
        data = core.extract_json_object(noisy, "verdict")
        self.assertEqual(data["verdict"], "accept")

    def test_extract_json_object_returns_none_without_key(self):
        self.assertIsNone(core.extract_json_object('{"other": 1}', "verdict"))


class ScorecardAndAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.patches = [
            patch.object(core, "STATE_DIR", base / ".thunopro"),
            patch.object(core, "DB_PATH", base / ".thunopro" / "state.sqlite3"),
            patch.object(core, "available_models", return_value=[
                core.Model("opencode-go/beam-free", ("coding", "reasoning", "review", "testing", "research"),
                           3, 3, 3, 4, 4, "verified")]),
            patch.object(scorecard, "_unlimited_ids", return_value={"opencode-go/beam-free"}),
        ]
        for item in self.patches:
            item.start()
        with core.connect() as db:
            db.execute("INSERT INTO panes(pane_id,session_name,role,updated_at) VALUES('%1','s1','builder',0)")

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def test_scorecard_rewards_first_pass_completion_and_compliance(self):
        clean = core.add_task("Implement A", "Write code", "builder", "coding", session="s1")
        core.task_claim(clean["id"], "builder")
        core.task_complete(clean["id"], "builder", "EVIDENCE: pytest passed")
        core.task_accept(clean["id"], "accept")
        with core.connect() as db:
            db.execute("INSERT INTO attempts(id,task_id,model,status,started_at,finished_at) VALUES('a1',?,?,?,0,1)",
                       (clean["id"], "opencode-go/beam-free", "done"))
        result = scorecard.compute("s1")
        self.assertEqual(result["tasks"], 1)
        self.assertEqual(result["components"]["completion"], 1.0)
        self.assertEqual(result["components"]["first_pass"], 1.0)
        self.assertEqual(result["components"]["compliance"], 1.0)
        self.assertGreaterEqual(result["score"], 85)

    def test_scorecard_penalises_metered_attempts(self):
        task = core.add_task("Implement B", "Write code", "builder", "coding", session="s1")
        with core.connect() as db:
            db.execute("INSERT INTO attempts(id,task_id,model,status,started_at) VALUES('m1',?,?,?,0)",
                       (task["id"], "opencode-go/gpt-6-luna", "done"))
        self.assertEqual(scorecard.compute("s1")["components"]["compliance"], 0.0)

    def test_scorecard_without_data_is_explicit(self):
        self.assertEqual(scorecard.compute("empty-session")["rating"], "no data")

    def test_sync_agent_files_pins_subagent_and_limits_worker_task_tool(self):
        target = Path(self.temp.name) / "agent"
        with patch.object(core, "unlimited_models", return_value=[core.Model(
                "opencode-go/beam-free", ("coding", "reasoning", "review", "testing", "research"), 3, 3, 3, 4, 4)]):
            result = agents.sync_agent_files(directory=target)
        self.assertEqual(result["subagent_model"], "opencode-go/beam-free")
        sub = (target / "thuno-sub.md").read_text()
        self.assertIn("model: opencode-go/beam-free", sub)
        self.assertIn("edit: deny", sub)
        worker = (target / "thuno-worker.md").read_text()
        self.assertIn('"thuno-sub": allow', worker)
        self.assertIn('"*": deny', worker)

    def test_context_snapshot_is_bounded(self):
        text = context.snapshot("s1", max_chars=800)
        self.assertLessEqual(len(text), 800 + len("\n...[snapshot truncated]"))
        self.assertIn("## TMUX TEAM", text)


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.patches = [
            patch.object(core, "STATE_DIR", base / ".thunopro"),
            patch.object(core, "DB_PATH", base / ".thunopro" / "state.sqlite3"),
            patch.object(core, "available_models", return_value=[
                core.Model("opencode-go/beam-free", ("coding", "reasoning", "research", "review", "testing", "fast"),
                           3, 3, 3, 4, 4, "verified")]),
            patch.object(orchestrator.time, "sleep", lambda seconds: None),
            patch.object(orchestrator.context, "snapshot", return_value="ctx"),
        ]
        for item in self.patches:
            item.start()
        with core.connect() as db:
            for role in ("scout", "architect", "builder", "reviewer", "tester", "commander"):
                db.execute("INSERT INTO panes(pane_id,session_name,role,updated_at) VALUES(?,?,?,0)",
                           (f"%{role}", "team", role))

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def test_run_objective_respects_dependencies_and_accepts_after_review(self):
        plan = {"tasks": [
            {"title": "Research api", "description": "Read docs", "acceptance": "notes", "kind": "research",
             "agent": "scout", "depends_on": []},
            {"title": "Implement api", "description": "Write code", "acceptance": "tests pass", "kind": "coding",
             "agent": "builder", "depends_on": [0]},
        ]}
        dispatch_order = []

        def fake_leader(prompt, purpose, model_id, reference="", timeout=0):
            if purpose == "plan":
                return 0, json.dumps(plan)
            return 0, json.dumps({"verdict": "accept", "notes": "evidence present"})

        def fake_dispatch(task_id):
            with core.connect() as db:
                row = db.execute("SELECT assigned_agent, depends_on FROM tasks WHERE id=?", (task_id,)).fetchone()
            deps = json.loads(row["depends_on"])
            for dep in deps:
                with core.connect() as db:
                    status = db.execute("SELECT status FROM tasks WHERE id=?", (dep,)).fetchone()["status"]
                assert status == "done", "dependency must be accepted before dispatch"
            dispatch_order.append(row["assigned_agent"])
            core.task_claim(task_id, row["assigned_agent"])
            core.task_complete(task_id, row["assigned_agent"], "CHANGES: x\nEVIDENCE: tests passed")
            return f"%{row['assigned_agent']}"

        with patch.object(orchestrator.core, "run_leader", side_effect=fake_leader), \
                patch.object(orchestrator.core, "dispatch_task", side_effect=fake_dispatch), \
                patch.object(orchestrator.agents, "sync_agent_files", return_value={"subagent_model": "x"}):
            summary = orchestrator.run_objective("Build API", "team", log=lambda message: None)

        self.assertEqual(dispatch_order, ["scout", "builder"])
        statuses = [task["status"] for task in summary["tasks"]]
        self.assertEqual(statuses, ["done", "done"])
        self.assertEqual(summary["needs_human"], [])
        self.assertFalse(summary["timed_out"])

    def test_failed_dependency_blocks_dependents_instead_of_dispatching(self):
        plan = {"tasks": [
            {"title": "Scout", "description": "Read", "acceptance": "a", "kind": "research",
             "agent": "scout", "depends_on": []},
            {"title": "Build", "description": "Write code", "acceptance": "b", "kind": "coding",
             "agent": "builder", "depends_on": [0]},
        ]}

        def fake_leader(prompt, purpose, model_id, reference="", timeout=0):
            return 0, json.dumps(plan)

        def fake_dispatch(task_id):
            with core.connect() as db:
                agent = db.execute("SELECT assigned_agent FROM tasks WHERE id=?", (task_id,)).fetchone()["assigned_agent"]
            core.task_claim(task_id, agent)
            core.task_complete(task_id, agent, "could not finish", failed=True)
            return f"%{agent}"

        with patch.object(orchestrator.core, "run_leader", side_effect=fake_leader), \
                patch.object(orchestrator.core, "dispatch_task", side_effect=fake_dispatch), \
                patch.object(orchestrator.agents, "sync_agent_files", return_value={"subagent_model": "x"}):
            summary = orchestrator.run_objective("Build", "team", log=lambda message: None)
        self.assertEqual([task["status"] for task in summary["tasks"]], ["failed", "failed"])
        self.assertIn("Blocked", summary["tasks"][1]["title"] and
                      core.connect().execute("SELECT result FROM tasks WHERE title='Build'").fetchone()[0])

    def test_plan_rejects_forward_dependencies(self):
        with self.assertRaisesRegex(ValueError, "earlier task indexes"):
            orchestrator._validate_tasks([
                {"title": "A", "description": "a", "agent": "builder", "depends_on": [1]},
                {"title": "B", "description": "b", "agent": "tester", "depends_on": []},
            ])

    def test_require_team_reports_missing_roles(self):
        with self.assertRaisesRegex(ValueError, "not a bound six-role team"):
            orchestrator._require_team("nobody")


if __name__ == "__main__":
    unittest.main()
