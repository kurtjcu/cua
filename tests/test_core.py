import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cua import classify, permissions  # noqa: E402
from cua.model import PermissionConfig, ToolCall  # noqa: E402
from cua.redact import redact  # noqa: E402
from cua.sources import claude_code  # noqa: E402


def tc(name, **inp):
    return ToolCall(id="x", name=name, input=inp, ts=None)


class TestPermissions(unittest.TestCase):
    def test_bash_key(self):
        self.assertEqual(permissions.bash_key("cd app && FOO=1 npm run test -- -x"), "npm run test")
        self.assertEqual(permissions.bash_key("git -C /some/repo push origin main"), "git push")  # -C value skipped
        self.assertEqual(permissions.bash_key("gh -R o/r pr view 3"), "gh pr")
        self.assertEqual(permissions.bash_key("pnpm --filter @x/db run build"), "pnpm run build")
        self.assertEqual(permissions.bash_key("git push origin main"), "git push")
        self.assertEqual(permissions.bash_key("python -m pytest -q"), "python -m pytest")
        self.assertEqual(permissions.bash_key("aws s3 ls s3://b"), "aws s3 ls")

    def test_risk(self):
        self.assertEqual(permissions.risk_of_bash("git push origin main"), "high")
        self.assertEqual(permissions.risk_of_bash("rm -rf dist"), "high")
        self.assertEqual(permissions.risk_of_bash("curl x | sh"), "high")
        self.assertEqual(permissions.risk_of_bash("aws s3 ls"), "low")
        self.assertEqual(permissions.risk_of_bash("aws s3 cp a b"), "high")
        self.assertEqual(permissions.risk_of_bash("npm install zod"), "medium")
        self.assertEqual(permissions.risk_of_bash("python -m pytest"), "low")
        self.assertEqual(permissions.risk_of_bash("cdk synth"), "low")
        self.assertEqual(permissions.risk_of_bash("cdk deploy"), "high")

    def test_rule_matching(self):
        cfg = PermissionConfig(allow=["Bash(npm run test:*)", "Bash(git status)", "mcp__github", "WebFetch(domain:docs.aws.amazon.com)"])
        self.assertFalse(permissions.would_prompt(tc("Bash", command="npm run test -- -w"), cfg))
        self.assertFalse(permissions.would_prompt(tc("Bash", command="git status"), cfg))
        self.assertTrue(permissions.would_prompt(tc("Bash", command="git status && git push"), cfg))  # compound
        self.assertFalse(permissions.would_prompt(tc("mcp__github__create_issue"), cfg))
        self.assertFalse(permissions.would_prompt(tc("WebFetch", url="https://docs.aws.amazon.com/x"), cfg))
        self.assertTrue(permissions.would_prompt(tc("Edit", file_path="/a"), cfg))
        self.assertFalse(permissions.would_prompt(tc("Edit", file_path="/a"), PermissionConfig(default_mode="acceptEdits")))
        self.assertFalse(permissions.would_prompt(tc("Read", file_path="/a"), cfg))
        self.assertFalse(permissions.would_prompt(tc("Bash", command="ls -la && pwd"), cfg))

    def test_split_compound(self):
        sp = permissions.split_compound
        self.assertEqual(sp("git status && git diff"), ["git status", "git diff"])
        self.assertEqual(sp("git log | head -5"), ["git log", "head -5"])
        self.assertEqual(sp('git commit -m "a; b && c | d"'), ['git commit -m "a; b && c | d"'])
        self.assertEqual(sp("a || b; c\nd"), ["a", "b", "c", "d"])
        self.assertEqual(sp("make 2>&1"), ["make 2>&1"])
        self.assertEqual(sp("echo 'it''s'; x"), ["echo 'it''s'", "x"])

    def test_bash_spec_semantics(self):
        m = permissions.bash_seg_matches
        # mid-pattern wildcards (the bug: treated as a literal prefix)
        self.assertTrue(m("distrobox enter * -- *", "distrobox enter dev -- uv run pytest"))
        self.assertFalse(m("distrobox enter * -- *", "distrobox create --name x"))
        self.assertTrue(m("git -C * commit*", "git -C /r commit -m x"))
        # trailing " *" is a word boundary and also matches the bare command
        self.assertTrue(m("ls *", "ls -la"))
        self.assertTrue(m("ls *", "ls"))
        self.assertFalse(m("ls *", "lsof"))
        # legacy ":*" prefix form, with a word boundary
        self.assertTrue(m("npm run test:*", "npm run test -- -w"))
        self.assertTrue(m("npm run test:*", "npm run test"))
        self.assertFalse(m("npm run test:*", "npm run testx"))
        # exact
        self.assertTrue(m("git status", "git status"))
        self.assertFalse(m("git status", "git status -s"))

    def test_each_subcommand_checked_separately(self):
        cfg = PermissionConfig(allow=["Bash(git status)", "Bash(git diff *)", "Bash(cd *)"])
        wp = permissions.would_prompt
        self.assertFalse(wp(tc("Bash", command="git status && git diff --stat"), cfg))  # two rules cover it
        self.assertTrue(wp(tc("Bash", command="git status && git push"), cfg))
        self.assertTrue(wp(tc("Bash", command="git diff | xargs rm"), cfg))  # piped part needs a rule too
        self.assertTrue(permissions._rule_matches("Bash(cd *)", tc("Bash", command="cd /x && git status")))  # cd gets hits
        deny = PermissionConfig(allow=["Bash(git *)"], deny=["Bash(git push *)"])
        self.assertFalse(wp(tc("Bash", command="git status && git push origin x"), deny))  # denied, not prompted
        ask = PermissionConfig(allow=["Bash(git *)"], ask=["Bash(git commit *)"])
        self.assertTrue(wp(tc("Bash", command="git commit -m x"), ask))  # ask beats allow

    def test_path_rules(self):
        home = os.path.expanduser("~")
        self.assertFalse(permissions._rule_matches("Read(~/.claude/skills/**)", tc("Edit", file_path=home + "/.claude/skills/a/SKILL.md")))  # tool mismatch
        r = lambda rule, p: permissions._rule_matches(rule, tc("Edit", file_path=p))
        self.assertTrue(r("Edit(~/.claude/skills/**)", home + "/.claude/skills/a/SKILL.md"))
        self.assertFalse(r("Edit(~/.claude/skills/**)", "/elsewhere/.claude/skills/a/SKILL.md"))  # ~ is anchored now
        self.assertTrue(r("Edit(//tmp/x/**)", "/tmp/x/y.txt"))
        self.assertFalse(r("Edit(//tmp/x/**)", "/var/tmp/x/y.txt"))
        self.assertTrue(r("Edit(./out/**)", "/repo/out/a.md"))  # relative: suffix match (stated over-match)

    def test_webfetch_domain(self):
        r = lambda rule, url: permissions._rule_matches(rule, tc("WebFetch", url=url))
        self.assertTrue(r("WebFetch(domain:docs.aws.amazon.com)", "https://docs.aws.amazon.com/x"))
        self.assertFalse(r("WebFetch(domain:aws.amazon.com)", "https://evil.com/?aws.amazon.com"))  # substring bug
        self.assertTrue(r("WebFetch(domain:*.github.com)", "https://api.github.com/x"))


class TestDenials(unittest.TestCase):
    def test_kinds(self):
        c = claude_code.classify_denial
        self.assertEqual(c("The user doesn't want to proceed with this tool use. The tool use was rejected", True), "user_rejected")
        self.assertEqual(c("Permission to use Bash with command rm -rf x has been denied.", True), "rule_denied")
        self.assertEqual(c("Claude requested permissions to use Write, but you haven't granted it yet.", True), "not_granted")
        self.assertEqual(c("Permission for this action was denied by the Claude Code auto mode classifier.", True), "classifier_denied")
        self.assertEqual(c("PreToolUse:Bash hook error: blocked by policy", True), "hook_blocked")
        self.assertEqual(c("rm: cannot remove 'x': Operation not permitted", True), "sandbox_blocked")
        self.assertIsNone(c("Operation not permitted", False))
        self.assertIsNone(c("all good", False))
        # successful output that merely talks about hooks (e.g. Read of a GSD workflow doc)
        self.assertIsNone(c("Step 3: the PreToolUse hook blocks writes outside the worktree.", False))
        self.assertIsNone(c("Permission to use Bash with command x has been denied. (quoted in docs)", False))


class TestParser(unittest.TestCase):
    def test_streamed_usage_counted_once_and_feedback(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "projects", "-x")
        os.makedirs(p)
        u = {"input_tokens": 10, "output_tokens": 100, "cache_read_input_tokens": 1000}
        lines = [
            {"type": "user", "sessionId": "s", "cwd": "/x", "timestamp": "2026-07-01T00:00:00Z", "message": {"role": "user", "content": "fix the bug"}},
            {"type": "assistant", "sessionId": "s", "timestamp": "2026-07-01T00:00:01Z", "message": {"id": "m1", "model": "m", "usage": u, "content": [{"type": "text", "text": "hi"}]}},
            {"type": "assistant", "sessionId": "s", "timestamp": "2026-07-01T00:00:01Z", "message": {"id": "m1", "model": "m", "usage": u, "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "git push"}}]}},
            {"type": "user", "sessionId": "s", "timestamp": "2026-07-01T00:00:02Z", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
             "content": "The user doesn't want to proceed with this tool use. To tell you how to proceed, the user said:\nopen a PR instead"}]}},
            {"type": "user", "sessionId": "s", "timestamp": "2026-07-01T00:00:03Z", "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user for tool use]"}]}},
            {"type": "user", "sessionId": "s", "timestamp": "2026-07-01T00:01:00Z", "message": {"role": "user", "content": "<command-name>/compact</command-name>"}},
        ]
        with open(os.path.join(p, "s.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(x) for x in lines))
        ss = claude_code.load_sessions(d, "t")
        self.assertEqual(len(ss), 1)
        s = ss[0]
        self.assertEqual(s.usage.output, 100)
        self.assertEqual(len(s.turns), 2)
        self.assertTrue(s.turns[0].interrupted)
        self.assertEqual(s.turns[0].tool_calls[0].denial, "user_rejected")
        self.assertEqual(s.turns[0].tool_calls[0].user_feedback, "open a PR instead")
        self.assertEqual(s.turns[1].command, "compact")
        self.assertEqual(s.compactions, 1)

    def test_since_applies_to_subagent_only_transcripts(self):
        """Subagent files have no main-thread turns, so Session.start is None. They must still
        be windowed, and must not survive as orphan sessions when their parent is out of window."""
        from datetime import datetime, timezone
        d = tempfile.mkdtemp()
        p = os.path.join(d, "projects", "-x")
        u = {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 1000}

        def write(path, sid, ts, user_text=None):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            lines = []
            if user_text:
                lines.append({"type": "user", "sessionId": sid, "cwd": "/x", "timestamp": ts, "message": {"role": "user", "content": user_text}})
            lines.append({"type": "assistant", "sessionId": sid, "cwd": "/x", "timestamp": ts, "isSidechain": not user_text,
                          "message": {"id": sid + ts, "model": "m", "usage": u,
                                      "content": [{"type": "tool_use", "id": "t" + ts, "name": "Read", "input": {}}]}})
            with open(path, "w") as f:
                f.write("\n".join(json.dumps(x) for x in lines))

        old, new = "2026-08-01T00:00:00Z", "2026-10-20T00:00:00Z"
        write(os.path.join(p, "old.jsonl"), "old", old, "old work")             # parent out of window
        write(os.path.join(p, "old", "subagents", "agent-a.jsonl"), "old", old)  # its subagent
        write(os.path.join(p, "orphan", "subagents", "agent-b.jsonl"), "orphan", old)  # old, no parent
        write(os.path.join(p, "new.jsonl"), "new", new, "new work")
        write(os.path.join(p, "new", "subagents", "agent-c.jsonl"), "new", new)
        since = datetime(2026, 10, 1, tzinfo=timezone.utc)
        ss = claude_code.load_sessions(d, "t", since)
        self.assertEqual(sorted(s.id for s in ss), ["new"])
        self.assertEqual(len(ss[0].sidechain_tool_calls), 1)  # subagent merged into its parent
        self.assertEqual(len(claude_code.load_sessions(d, "t")), 3)  # no window: old, orphan, new

    def test_subagent_models_are_attributed_to_their_type(self):
        """Main-thread `models` ignores subagents; subagent_models must report what they ran on,
        per subagent_type, linked through the parent's Agent tool_result (toolUseResult.agentId)."""
        from cua import analyze
        d = tempfile.mkdtemp()
        p = os.path.join(d, "projects", "-x")
        os.makedirs(os.path.join(p, "s1", "subagents"))
        u = {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 98}

        def asst(model, mid, sidechain, agent=None, content=None):
            r = {"type": "assistant", "sessionId": "s1", "cwd": "/x", "timestamp": "2026-10-20T00:00:00Z",
                 "isSidechain": sidechain, "message": {"id": mid, "model": model, "usage": u, "content": content or []}}
            if agent:
                r["agentId"] = agent
            return r

        parent = [
            {"type": "user", "sessionId": "s1", "cwd": "/x", "timestamp": "2026-10-20T00:00:00Z",
             "message": {"role": "user", "content": "go"}},
            asst("opus", "m1", False, content=[{"type": "tool_use", "id": "toolu_A", "name": "Agent",
                                                "input": {"subagent_type": "gsd-executor"}}]),
            {"type": "user", "sessionId": "s1", "cwd": "/x", "timestamp": "2026-10-20T00:01:00Z",
             "toolUseResult": {"agentId": "abc", "status": "completed"},
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_A", "content": "done"}]}},
        ]
        with open(os.path.join(p, "s1.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(x) for x in parent))
        with open(os.path.join(p, "s1", "subagents", "agent-abc.jsonl"), "w") as f:  # id from the record
            f.write("\n".join(json.dumps(asst("sonnet", i, True, agent="abc")) for i in ("a1", "a2", "a2")))  # a2 streamed twice
        with open(os.path.join(p, "s1", "subagents", "agent-zzz.jsonl"), "w") as f:  # id from the filename, no link
            f.write(json.dumps(asst("opus", "z1", True)))
        s = claude_code.load_sessions(d, "t")[0]
        self.assertEqual(s.agent_types, {"abc": "gsd-executor"})
        r = analyze.subagent_models([s])
        self.assertEqual(r["subagent_models_by_type"]["gsd-executor"], {"sonnet": {"msgs": 2, "tokens": 200}})
        self.assertEqual(r["subagent_models_by_type"]["(unlinked)"], {"opus": {"msgs": 1, "tokens": 100}})
        self.assertEqual(r["subagent_models"]["sonnet"]["msgs"], 2)


class TestMisc(unittest.TestCase):
    def test_redact(self):
        r = redact("key AKIAABCDEFGHIJKLMNOP mail a@b.com in /home/alice/x token=abc12345", 500)
        self.assertNotIn("AKIA", r)
        self.assertNotIn("a@b.com", r)
        self.assertNotIn("alice", r)
        self.assertNotIn("abc12345", r)

    def test_phase(self):
        self.assertEqual(classify.phase_of("Traceback (most recent call last):\n  File x"), "debugging")
        self.assertEqual(classify.phase_of("write unit tests for parser"), "testing")
        self.assertEqual(classify.phase_of("deploy to staging with cdk"), "deployment_ops")
        self.assertTrue(classify.is_correction("no, keep the old signature"))
        self.assertTrue(classify.is_bare_continue("yes"))

    def test_cluster(self):
        cl = classify.cluster_prompts([("fix this error in a.py: KeyError 3", {}), ("fix this error in b.ts: KeyError 9", {}), ("plan the roadmap", {})])
        self.assertEqual(cl[0]["size"], 2)


class TestEffectivePermissions(unittest.TestCase):
    def _settings(self, d, name, perms):
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w") as f:
            json.dump({"permissions": perms}, f)

    def test_project_rules_and_mode_stay_in_their_project(self):
        t = tempfile.mkdtemp()
        user, a, b = os.path.join(t, "user"), os.path.join(t, "a"), os.path.join(t, "b")
        self._settings(user, "settings.json", {"allow": ["Bash(git status)"]})
        self._settings(os.path.join(a, ".claude"), "settings.local.json", {"allow": ["Bash(make *)"], "defaultMode": "bypassPermissions"})
        os.makedirs(b)
        ca = claude_code.effective_permission_config(user, a)
        cb = claude_code.effective_permission_config(user, b)
        self.assertEqual(ca.default_mode, "bypassPermissions")
        self.assertIsNone(cb.default_mode)  # the bug: a's bypass leaked into every session
        self.assertFalse(permissions.would_prompt(tc("Bash", command="make all"), ca))
        self.assertTrue(permissions.would_prompt(tc("Bash", command="make all"), cb))
        self.assertFalse(permissions.would_prompt(tc("Bash", command="git status"), cb))

    def test_recorded_mode_parsed(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "projects", "-x")
        os.makedirs(p)
        lines = [{"type": "user", "sessionId": "s", "cwd": "/x", "permissionMode": "default", "timestamp": "2026-10-10T00:00:00Z",
                  "message": {"role": "user", "content": "hi"}},
                 {"type": "user", "sessionId": "s", "cwd": "/x", "permissionMode": "acceptEdits", "timestamp": "2026-10-10T00:01:00Z",
                  "message": {"role": "user", "content": "go on"}}]
        with open(os.path.join(p, "s.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(x) for x in lines))
        self.assertEqual(claude_code.load_sessions(d, "t")[0].permission_mode, "acceptEdits")


class TestBlockOutcomes(unittest.TestCase):
    def test_signature_groups_variable_parts(self):
        from cua.analyze import block_signature
        a = block_signature("<tool_use_error>Secret read guard: Bash would read '/a/.env', which matches</tool_use_error>")
        b = block_signature("Secret read guard: Bash would read '/b/c/.env.local', which matches")
        self.assertEqual(a, b)
        self.assertEqual(block_signature("Write blocked: /x/y outside worktree 12"),
                         block_signature("Write blocked: /p/q outside worktree 7"))

    def test_outcomes(self):
        from cua.analyze import block_outcome
        b = ToolCall(id="1", name="Bash", input={"command": "cd x && ls"}, ts=None, result_error=True, denial="hook_blocked")
        self.assertEqual(block_outcome(b, None), "nothing_after")
        self.assertEqual(block_outcome(b, ToolCall(id="2", name="Bash", input={"command": "cd x && ls"}, ts=None)), "reissued")
        self.assertEqual(block_outcome(b, ToolCall(id="2", name="Bash", input={"command": "ls x"}, ts=None)), "recovered")
        self.assertEqual(block_outcome(b, ToolCall(id="2", name="Bash", input={"command": "ls y"}, ts=None, result_error=True)), "errored")
        self.assertEqual(block_outcome(b, ToolCall(id="2", name="Bash", input={"command": "cd y && ls"}, ts=None,
                                                   result_error=True, denial="hook_blocked")), "blocked_again")

    def test_rejection_runs_count_as_one_event_and_questions_are_separate(self):
        from cua import analyze
        from cua.model import Session, Turn
        rej = "The user doesn't want to proceed with this tool use. The tool use was rejected"
        burst = [ToolCall(id=str(i), name="Read", input={"file_path": f"/x/{i}"}, ts=None, result_error=True,
                          result_text=rej, denial="user_rejected") for i in range(4)]
        later = ToolCall(id="9", name="Edit", input={"file_path": "/x/e"}, ts=None, result_error=True,
                         result_text=rej, denial="user_rejected")
        s = Session(id="s1", source="claude_code", account="t", project="/x",
                    turns=[Turn(ts=None, prompt="go", tool_calls=burst + [ToolCall(id="8", name="Bash", input={}, ts=None), later])])
        summary, _ = analyze.analyze([s], "t")
        self.assertEqual(summary["blockers"]["denials_by_kind"]["user_rejected"], 5)
        self.assertEqual(summary["blockers"]["denial_events_by_kind"]["user_rejected"], 2)

    def test_dismissed_question_is_not_a_rejection(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "projects", "-x")
        os.makedirs(p)
        lines = [
            {"type": "user", "sessionId": "s", "cwd": "/x", "timestamp": "2026-10-10T00:00:00Z", "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "sessionId": "s", "timestamp": "2026-10-10T00:00:01Z", "message": {"id": "m", "model": "m", "content": [
                {"type": "tool_use", "id": "q", "name": "AskUserQuestion", "input": {"questions": []}}]}},
            {"type": "user", "sessionId": "s", "timestamp": "2026-10-10T00:00:02Z", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "q", "is_error": True, "content": "The user doesn't want to proceed with this tool use."}]}},
        ]
        with open(os.path.join(p, "s.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(x) for x in lines))
        c = claude_code.load_sessions(d, "t")[0].turns[0].tool_calls[0]
        self.assertEqual(c.denial, "question_dismissed")
        self.assertFalse(permissions.would_prompt(c, PermissionConfig()))

    def test_end_to_end_row(self):
        from cua import analyze
        from cua.model import Session, Turn
        blocked = ToolCall(id="1", name="Bash", input={"command": "cd /x && ls"}, ts=None, result_error=True,
                           result_text="This Bash call would make the user approve it only because it starts with `cd <dir>`",
                           denial="hook_blocked")
        fixed = ToolCall(id="2", name="Bash", input={"command": "ls /x"}, ts=None)
        s = Session(id="s1", source="claude_code", account="t", project="/x",
                    turns=[Turn(ts=None, prompt="go", tool_calls=[blocked, fixed])])
        summary, _ = analyze.analyze([s], "t")
        row = summary["blockers"]["block_outcomes"][0]
        self.assertEqual((row["kind"], row["count"], row["outcomes"], row["where"]),
                         ("hook_blocked", 1, {"recovered": 1}, {"main": 1}))


class TestReport(unittest.TestCase):
    def test_compare_formats_are_explicit(self):
        from cua import report
        fmt = {label: fmt for label, _, fmt in report.COMPARE_METRICS}
        self.assertEqual(fmt["est. prompts/session"](0.3), "0.30")  # was "30.0%"
        self.assertEqual(fmt["prompts/session median"](1.0), "1.0")  # was "100.0%"
        self.assertEqual(fmt["subagent share"](0.373), "37.3%")


class TestScanFilters(unittest.TestCase):
    def test_project_selected(self):
        from cua.cli import project_selected
        work = ["/mnt/x/code/work/*"]
        self.assertTrue(project_selected("/mnt/x/code/work/repo", work, None))
        self.assertTrue(project_selected("/mnt/x/code/work/repo/sub", work, None))
        self.assertFalse(project_selected("/mnt/x/code/personal/repo", work, None))
        self.assertFalse(project_selected(None, work, None))
        self.assertTrue(project_selected(None, None, work))
        self.assertFalse(project_selected("/mnt/x/code/work/repo", None, work))
        self.assertTrue(project_selected("/anything", None, None))

    def test_project_selected_follows_symlink(self):
        from cua.cli import project_selected
        with tempfile.TemporaryDirectory() as t:
            real = os.path.join(t, "real", "hc", "repo")
            os.makedirs(real)
            os.symlink(os.path.join(t, "real"), os.path.join(t, "link"))
            via_link = os.path.join(t, "link", "hc", "repo")
            self.assertTrue(project_selected(via_link, [os.path.join(os.path.realpath(t), "real", "hc", "*")], None))

    def test_history_window(self):
        from datetime import datetime, timezone
        from cua.cli import _history_in_window
        since = datetime(2026, 10, 1, tzinfo=timezone.utc)
        ms = lambda d: d.timestamp() * 1000
        self.assertTrue(_history_in_window({"timestamp": ms(datetime(2026, 10, 2, tzinfo=timezone.utc))}, since))
        self.assertFalse(_history_in_window({"timestamp": ms(datetime(2026, 9, 2, tzinfo=timezone.utc))}, since))
        self.assertFalse(_history_in_window({}, since))
        self.assertTrue(_history_in_window({}, None))

    def test_overlap_warning(self):
        from cua.cli import overlap_warnings
        m = lambda acct, src, inc=(), exc=(): {"meta": {"account": acct, "claude_projects_sources": src,
                                                       "project_filter": {"include": list(inc), "exclude": list(exc)}}}
        self.assertEqual(len(overlap_warnings([m("p", ["/a"]), m("w", ["/a"])])), 1)
        self.assertEqual(overlap_warnings([m("p", ["/a"]), m("w", ["/b"])]), [])
        self.assertEqual(overlap_warnings([m("p", ["/a"], exc=["x"]), m("w", ["/a"], inc=["x"])]), [])
        self.assertEqual(overlap_warnings([{"meta": {"account": "old"}}, m("w", ["/a"])]), [])


if __name__ == "__main__":
    unittest.main()
