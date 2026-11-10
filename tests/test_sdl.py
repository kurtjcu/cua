import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cua import sdl  # noqa: E402
from cua.model import Session, ToolCall, Turn  # noqa: E402

T0 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)


def analyse(sessions, **kw):
    """Tests run with an issue-tracker prefix configured, so the traceability
    checks are active. `test_no_ticket_prefix_skips_traceability` overrides it."""
    kw.setdefault("ticket_prefix", "PROJ")
    return sdl.analyse(sessions, **kw)


def bash(cmd, at):
    ts = T0 + timedelta(seconds=at)
    return ToolCall(id=f"b{at}", name="Bash", input={"command": cmd}, ts=ts, result_ts=ts)


def edit(path, at, content="", name="Write"):
    ts = T0 + timedelta(seconds=at)
    inp = {"file_path": path}
    if content:
        inp["content"] = content
    return ToolCall(id=f"e{at}", name=name, input=inp, ts=ts, result_ts=ts)


def sess(calls, branch="feature/PROJ-1234-thing", project="/home/dev/code/work/app", sid="s1",
         agent_types=None):
    s = Session(id=sid, source="claude_code", account="work", project=project, git_branch=branch)
    t = Turn(ts=T0, prompt="go", command="gsd-execute-phase")
    t.tool_calls = list(calls)
    s.turns.append(t)
    # subagent calls: attach to the session's sidechain so agent attribution resolves
    for c in calls:
        if c.agent_id:
            s.sidechain_tool_calls.append(c)
    if agent_types:
        s.agent_types.update(agent_types)
    return s


def checks(res):
    return {e["check"] for e in res["_events"]}


def by_check(res, name):
    return [e for e in res["_events"] if e["check"] == name]


class TestSdl(unittest.TestCase):
    def test_clean_feature_flow_has_no_findings(self):
        res = analyse([sess([
            bash("git checkout -b feature/PROJ-1234-thing develop", 0),
            bash("npm run test", 1),
            bash('git commit -m "PROJ-1234 add thing"', 2),
            bash("git push -u origin feature/PROJ-1234-thing", 3),
            bash("gh pr create --base develop --title 'PROJ-1234 add thing' --body x", 4),
        ])])
        self.assertEqual(res["_events"], [])
        self.assertEqual(res["meta"]["violations"], 0)
        self.assertEqual(res["pr_without_tests"], [])

    def test_commit_and_push_on_protected_branch(self):
        res = analyse([sess([
            bash("git checkout develop", 0),
            bash('git commit -m "PROJ-1 quick fix"', 1),
            bash("git push origin develop", 2),
        ], branch="main")])
        self.assertIn("branch.protected_commit", checks(res))
        self.assertIn("push.protected", checks(res))
        # commit happened on develop (after checkout), push target develop
        self.assertEqual(by_check(res, "branch.protected_commit")[0]["branch"], "develop")
        self.assertEqual(by_check(res, "push.protected")[0]["detail"]["targets"], ["develop"])

    def test_push_current_branch_when_protected_and_no_refspec(self):
        res = analyse([sess([bash("git push", 0)], branch="main")])
        ev = by_check(res, "push.protected")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["detail"]["target_known"], True)

    def test_push_to_release_branch_is_protected(self):
        res = analyse([sess([bash("git push origin release/1.2.3", 0)])])
        self.assertIn("push.protected", checks(res))

    def test_working_branch_missing_ticket(self):
        res = analyse([sess([bash("git switch -c feature/add-thing", 0)], branch="develop")])
        self.assertIn("branch.working_no_ticket", checks(res))
        # a properly named one does not flag
        res2 = analyse([sess([bash("git switch -c feature/PROJ-9 x", 0)], branch="develop")])
        self.assertNotIn("branch.working_no_ticket", checks(res2))

    def test_commit_traceability_from_branch_name(self):
        # subject lacks the id but branch carries it -> traceable, no finding
        res = analyse([sess([bash('git commit -m "tidy up"', 0)], branch="feature/PROJ-7-x")])
        self.assertNotIn("trace.commit_no_ticket", checks(res))
        # neither branch nor subject -> attention
        res2 = analyse([sess([bash('git commit -m "tidy up"', 0)], branch="feature/wip")])
        self.assertIn("trace.commit_no_ticket", checks(res2))

    def test_no_ticket_prefix_skips_traceability(self):
        # With no TICKET_PREFIX configured, traceability checks are off; protected-branch
        # and secret checks still fire.
        res = sdl.analyse([sess([
            bash("git switch -c feature/add-thing", 0),
            bash('git commit -m "tidy up"', 1),
            bash("git push origin main", 2),
        ], branch="develop")], ticket_prefix="")
        got = checks(res)
        self.assertNotIn("branch.working_no_ticket", got)
        self.assertNotIn("trace.commit_no_ticket", got)
        self.assertIn("push.protected", got)

    def test_checkout_dash_dash_is_not_a_branch_move(self):
        # `git checkout -- file` must not change the replayed branch
        res = analyse([sess([
            bash("git checkout -- src/app.py", 0),
            bash('git commit -m "PROJ-1 x"', 1),
        ], branch="main")])
        self.assertIn("branch.protected_commit", checks(res))

    def test_self_merge_and_bypass(self):
        res = analyse([sess([bash("gh pr merge 42 --squash --admin", 0)])])
        self.assertIn("pr.self_merge", checks(res))
        self.assertIn("pr.bypass", checks(res))
        self.assertTrue(by_check(res, "pr.self_merge")[0]["detail"]["admin_or_auto"])

    def test_self_approve(self):
        res = analyse([sess([bash("gh pr review 42 --approve", 0)])])
        self.assertIn("pr.self_approve", checks(res))

    def test_pr_base_and_ticket(self):
        # targeting another feature branch (stacking) flags; no issue id in title/body flags
        res = analyse([sess([bash("gh pr create --base feature/PROJ-1-base --title nope --body y", 0)])])
        self.assertIn("pr.base_not_release", checks(res))
        self.assertIn("pr.no_ticket", checks(res))
        # develop and the develop->main promotion are both compliant bases
        for base in ("develop", "main", "release/1.2.3"):
            r = analyse([sess([bash(f"gh pr create --base {base} --title 'PROJ-2 x' --body y", 0)])])
            self.assertNotIn("pr.base_not_release", checks(r), base)

    def test_new_dependency_vs_restore(self):
        res = analyse([sess([bash("npm install left-pad", 0)])])
        self.assertIn("deps.new_dependency", checks(res))
        self.assertEqual(by_check(res, "deps.new_dependency")[0]["detail"]["packages"], ["left-pad"])
        # lockfile restore does not flag
        for restore in ("npm ci", "npm install", "uv sync", "pip install -r requirements.txt"):
            r = analyse([sess([bash(restore, 0)])])
            self.assertNotIn("deps.new_dependency", checks(r), restore)

    def test_secret_path_and_content_and_add(self):
        res = analyse([sess([
            edit("/home/dev/code/work/app/.env", 0, content="DB_PASSWORD=hunter2hunter"),
            bash("git add .env", 1),
        ])])
        self.assertIn("secrets.secret_path_write", checks(res))
        self.assertIn("secrets.credential_assignment", checks(res))  # .env is not a test/doc path
        self.assertIn("secrets.git_add_secret", checks(res))
        # the secret value never leaves in the head or detail
        for e in res["_events"]:
            self.assertNotIn("hunter2hunter", e["head"])
            self.assertNotIn("hunter2hunter", str(e["detail"]))

    def test_sk_prefix_in_a_word_does_not_flag(self):
        # a task slug ".../task-261014-es9/PLAN.md" must not read as an sk- API key
        res = analyse([sess([
            edit("/home/dev/code/work/app/x.py", 0, content="path = '.planning/quick/task-261014-es9-abcd/PLAN.md'"),
        ])])
        self.assertNotIn("secrets.key_or_token", checks(res))

    def test_test_fixture_credential_not_flagged(self):
        res = analyse([sess([
            edit("/home/dev/code/work/app/tests/conftest.py", 0, content='PASSWORD = "localdev-pw-123"'),
        ])])
        self.assertNotIn("secrets.credential_assignment", checks(res))

    def test_secret_content_prose_and_docs_do_not_flag(self):
        # prose assignment in a plan doc: not a finding
        res = analyse([sess([
            edit("/home/dev/code/work/app/.planning/PLAN.md", 0, content="The password: required before deploy."),
            edit("/home/dev/code/work/app/README.md", 1, content="Set API_KEY=your-key-here in the env."),
            edit("/home/dev/code/work/app/config.py", 2, content='TIMEOUT = "default-value"'),
        ])])
        self.assertNotIn("secrets.credential_assignment", checks(res))

    def test_real_key_flags_even_in_a_doc(self):
        res = analyse([sess([
            edit("/home/dev/code/work/app/notes.md", 0, content="token ghp_abcdefghijklmnopqrstuvwxyz0123"),
        ])])
        self.assertIn("secrets.key_or_token", checks(res))

    def test_pr_without_tests_flagged(self):
        res = analyse([sess([
            bash("git commit -m 'PROJ-1 x'", 0),
            bash("gh pr create --base develop --title 'PROJ-1' --body y", 1),
        ])])
        self.assertEqual(res["pr_without_tests"], ["s1"])

    def test_agent_attribution(self):
        c = bash("git push origin main", 0)
        c.agent_id = "agent-9"
        s = sess([c], agent_types={"agent-9": "gsd-executor"})
        res = analyse([s])
        self.assertEqual(by_check(res, "push.protected")[0]["agent"], "gsd-executor")
        self.assertIn("gsd-executor", res["by_agent"])

    def test_rollups_and_catalogue(self):
        res = analyse([sess([bash("git push origin main", 0)], branch="main")])
        self.assertEqual(res["meta"]["violations"], 1)
        self.assertIn("push.protected", res["by_check"])
        self.assertIn("push.protected", res["meta"]["checks"])
        self.assertTrue(res["meta"]["not_judgeable_from_transcripts"])

    def test_claude_ai_sessions_skipped(self):
        s = Session(id="x", source="claude_ai", account="work", project="/x")
        self.assertEqual(analyse([s])["meta"]["sessions_total"], 0)


if __name__ == "__main__":
    unittest.main()
