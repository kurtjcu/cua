import builtins
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cua import lint  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class TestHelpers(unittest.TestCase):
    def test_frontmatter(self):
        fm = lint.frontmatter("---\nname: x\ndescription: >\n  Does a thing.\n  Use when y.\ntools:\nmodel: 'sonnet'\n---\nbody")
        self.assertEqual(fm["name"], "x")
        self.assertEqual(fm["description"], "Does a thing. Use when y.")
        self.assertEqual(fm["tools"], "")
        self.assertEqual(fm["model"], "sonnet")
        self.assertEqual(lint.frontmatter("no frontmatter"), {})

    def test_subsumes(self):
        s = lint.subsumes
        self.assertTrue(s("Skill(gsd-:*)", "Skill(gsd-secure-phase)"))
        self.assertTrue(s("Bash(git log *)", "Bash(git log --oneline)"))
        self.assertTrue(s("Bash(git -C:*)", "Bash(git -C /repo add a b)"))
        self.assertTrue(s("Read(.planning/**)", "Read(.planning/*)"))
        self.assertTrue(s("Bash", "Bash(ls)"))
        self.assertFalse(s("Bash(python:*)", "Bash(python3:*)"))  # word boundary
        self.assertFalse(s("Bash(git log *)", "Bash(git log)"))  # bare form: not judged
        self.assertFalse(s("Bash(ls *)", "Bash(ls a; rm b)"))  # compound: not judged
        self.assertFalse(s("Read(src/*)", "Read(src/a/b)"))
        self.assertFalse(s("Bash(git log *)", "Bash(git log *)"))

    def test_hook_scripts_resolve_project_paths(self):
        cfg = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": 'bash "$CLAUDE_PROJECT_DIR/.claude-local/hooks/gate.sh"'},
            {"type": "command", "command": "node .claude/hooks/guard.js"},
            {"type": "command", "command": '"/usr/bin/node" "/home/u/.claude/hooks/a.js"'},
            {"type": "command", "command": "$OTHER/x.sh"}]}]},
            "statusLine": {"type": "command", "command": "node ~/.claude/hooks/sl.js"}}
        got = [p for _, _, p in lint.hook_scripts(cfg, "/p")]
        self.assertEqual(got[:3], ["/p/.claude-local/hooks/gate.sh", "/p/.claude/hooks/guard.js", "/home/u/.claude/hooks/a.js"])
        self.assertEqual(len(got), 4)
        self.assertTrue(got[3].endswith("/.claude/hooks/sl.js"))
        self.assertEqual(len(lint.hook_scripts({"hooks": {"X": [{"hooks": [{"command": "node rel.js"}]}]}})), 0)

    def test_memory_dir_name(self):
        self.assertEqual(lint.memory_dir_name("/home/dev/code/app/x.y"), "-home-dev-code-app-x-y")


class TestLintTree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.g = os.path.join(t, "claude")
        self.root = os.path.join(t, "code")
        self.proj = os.path.join(self.root, "me", "app")
        g = self.g
        write(f"{g}/CLAUDE.md", "# Global\n" + "- Never commit directly to main or develop under any circumstances at all.\n")
        write(f"{g}/settings.json", json.dumps({
            "permissions": {"allow": ["Bash(git log *)", "Bash(git log --oneline)"], "deny": ["Read(./.env)"]},
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": f"node {g}/hooks/guard.js"},
                {"type": "command", "command": f"node {g}/hooks/gone.js"}]}]}}))
        write(f"{g}/hooks/guard.js", "if (bad) process.exit(2)\n")
        write(f"{g}/hooks/stray.js", "console.log(1)\n")
        write(f"{g}/hooks/guard.js.bak-1", "old\n")
        write(f"{g}/skills/good/SKILL.md", "---\nname: good\ndescription: Formats things. Use when asked to format.\n---\nbody\n")
        write(f"{g}/skills/vague/SKILL.md", "---\nname: vague\ndescription: Formats things.\n---\nRun !`date` first\n")
        write(f"{g}/agents/helper.md", "---\nname: helper\ndescription: helps\ntools:\n---\nuse mcp__ghost__thing\n")
        # project
        p = self.proj
        write(f"{p}/CLAUDE.md", "\n".join(["# App"] + [f"rule {i}" for i in range(250)]
                                          + ["- Never commit directly to main or develop under any circumstances at all.",
                                             "See @docs/extra.md and @missing.md"]) + "\n")
        write(f"{p}/docs/extra.md", "extra\n")
        write(f"{p}/.claude/agents/helper.md", "---\nname: helper\ndescription: helps, but older\n---\n")
        write(f"{p}/.claude/settings.local.json", json.dumps({"permissions": {"defaultMode": "bypassPermissions"}}))
        with open(f"{p}/CLAUDE.md") as f:
            write(os.path.join(self.root, "me", "app-wt", "CLAUDE.md"), f.read())
        # memory + a transcript that must never be opened
        mem = os.path.join(g, "projects", lint.memory_dir_name(p), "memory")
        write(f"{mem}/MEMORY.md", "- [A](a.md) — x\n- [B](b.md) — y\n")
        write(f"{mem}/a.md", "a\n")
        write(f"{mem}/c.md", "c\n")
        write(os.path.join(g, "projects", "-other", "memory", "note.md"), "n\n")
        write(os.path.join(g, "projects", lint.memory_dir_name(p), "s1.jsonl"), '{"secret": 1}\n')

    def tearDown(self):
        self.tmp.cleanup()

    def run_lint(self):
        opened = []
        real_open = builtins.open

        def spy(path, *a, **k):
            opened.append(str(path))
            return real_open(path, *a, **k)

        with mock.patch("builtins.open", spy):
            res = lint.lint(self.g, [self.root], [("personal", self.root + "/me/*")], claude_json=None)
        return res, opened

    def test_end_to_end(self):
        res, opened = self.run_lint()
        self.assertFalse([p for p in opened if p.endswith(".jsonl")], "lint opened a transcript")
        checks = {}
        for f in res["findings"]:
            checks.setdefault(f["check"], []).append(f)
        self.assertIn("instructions.size", checks)
        self.assertIn("instructions.identical_files", checks)
        bi = {f["path"].split("/me/")[1]: f["detail"]["imports"] for f in checks["instructions.broken_import"]}
        self.assertEqual(bi["app/CLAUDE.md"], ["missing.md"])
        self.assertEqual(bi["app-wt/CLAUDE.md"], ["docs/extra.md", "missing.md"])  # copy without docs/
        self.assertIn("instructions.repeats_global", checks)
        self.assertIn("skill.no_when_clause", checks)
        self.assertEqual([os.path.basename(os.path.dirname(f["path"])) for f in checks["skill.no_when_clause"]], ["vague"])
        self.assertIn("skill.runs_shell", checks)
        self.assertIn("agent.empty_tools", checks)
        self.assertEqual(checks["agent.shadowed"][0]["severity"], "warn")
        self.assertIn("agent.no_model", checks)
        self.assertNotIn("agent.unknown_mcp", checks)  # claude_json not given: unknown, not reported
        self.assertTrue(any("gone.js" in f["message"] for f in checks["hooks.missing_script"]))
        self.assertTrue(any(f["path"].endswith("stray.js") for f in checks["hooks.unwired"]))
        self.assertIn("hooks.backup_file", checks)
        self.assertTrue(any(f["path"].endswith("guard.js") for f in checks["hooks.decides_without_log"]))
        self.assertIn("settings.redundant_allow", checks)
        self.assertIn("settings.bypass_mode", checks)
        self.assertEqual(checks["memory.dangling_link"][0]["detail"]["links"], ["b.md"])
        self.assertEqual(checks["memory.orphan"][0]["detail"]["files"], ["c.md"])
        self.assertIn("memory.no_index", checks)
        row = next(r for r in res["budget"] if r["project"].endswith("/me/app"))
        self.assertEqual(row["account"], "personal")
        self.assertGreater(row["memory_tokens"], 0)
        self.assertTrue(any(f.endswith("docs/extra.md") for f in row["instruction_files"]))
        self.assertEqual(row["agents"], 1)  # project helper replaces global helper
        md = lint.render(res)
        self.assertIn("## Findings", md)

    def test_unknown_mcp_with_claude_json(self):
        cj = os.path.join(self.tmp.name, "claude.json")
        write(cj, json.dumps({"mcpServers": {"real": {}}}))
        res = lint.lint(self.g, [self.root], claude_json=cj)
        hits = [f for f in res["findings"] if f["check"] == "agent.unknown_mcp"]
        self.assertTrue(hits and "ghost" in hits[0]["message"])
        self.assertEqual(res["meta"]["mcp_global"], ["real"])


class TestRetirement(unittest.TestCase):
    def test_verdicts(self):
        A = lint.Artifact
        with tempfile.TemporaryDirectory() as t:
            def sk(name, body=""):
                p = os.path.join(t, "skills", name, "SKILL.md")
                write(p, f"---\nname: {name}\ndescription: d\n---\n{body}")
                write(os.path.join(t, "skills", name, "references", "x.md"), f"see {name}")  # own tree
                return A("skill", name, p, "global", "d", 3, {"name": name})
            arts = [sk("gsd-plan-phase"), sk("gsd-sketch"), sk("gsd-ns-docs"), sk("gsd-next"), sk("resume")]
            wf = os.path.join(t, "wf.md")
            write(wf, "then run /gsd-sketch to mock it up")
            corpus = [(wf, "then run /gsd-sketch to mock it up")] + \
                [(os.path.join(t, "skills", n, "references", "x.md"), f"see {n}") for n in ("gsd-ns-docs",)]
            usage = {"gsd-plan-phase": {"typed": 3}, "resume": {"history_typed": 9}}
            clusters = {"gsd-next": ["core_loop"], "gsd-plan-phase": ["core_loop"], "gsd-sketch": ["research_ideate"]}
            rows = {r["name"]: r for r in lint.retirement(arts, usage, clusters, corpus)}
        self.assertEqual(rows["gsd-plan-phase"]["verdict"], "keep")
        self.assertEqual(rows["gsd-sketch"]["verdict"], "unused, but referenced: check before retiring")
        self.assertEqual(rows["gsd-ns-docs"]["verdict"], "retire candidate")  # own references/ don't count
        self.assertEqual(rows["gsd-next"]["verdict"], "keep (core loop)")
        self.assertTrue(rows["resume"]["verdict"].startswith("unclear"))
        cs = {c["cluster"]: c for c in lint.cluster_summary(list(rows.values()), clusters)}
        self.assertEqual(cs["core_loop"]["used"], ["gsd-plan-phase"])


class TestSkillUsage(unittest.TestCase):
    def test_counts_typed_model_subagent_history(self):
        from datetime import datetime, timezone
        from cua.analyze import skill_usage
        from cua.model import Session, ToolCall, Turn
        t0 = datetime(2026, 10, 20, tzinfo=timezone.utc)
        t1 = datetime(2026, 10, 25, tzinfo=timezone.utc)
        s = Session(id="s1", source="claude_code", account="t", project="/x/app", turns=[
            Turn(ts=t0, prompt="", kind="command", command="gsd-plan-phase"),
            Turn(ts=t1, prompt="do it", tool_calls=[
                ToolCall(id="1", name="Skill", input={"skill": "uat-run"}, ts=t1),
                ToolCall(id="2", name="Skill", input={"skill": "gsd-plan-phase", "args": "3"}, ts=t1)])],
            sidechain_tool_calls=[ToolCall(id="3", name="Skill", input={"command": "/gsd-code-review"}, ts=t1)])
        hist = [{"display": "/gsd-plan-phase 2", "timestamp": 1756684800000, "project": "/x/app"},
                {"display": "/gsd-ui-phase", "timestamp": 1756771200000, "project": "/x/web"},
                {"display": "plain prompt", "timestamp": 1756771200000}]
        res = skill_usage([s], hist, "basename")
        by = res["by_name"]
        self.assertEqual((by["gsd-plan-phase"]["typed"], by["gsd-plan-phase"]["model"],
                          by["gsd-plan-phase"]["history_typed"]), (1, 1, 1))
        self.assertEqual(by["uat-run"]["model"], 1)
        self.assertEqual(by["gsd-code-review"]["subagent"], 1)
        self.assertEqual(by["gsd-ui-phase"]["history_typed"], 1)
        self.assertNotIn("plain", by)
        self.assertEqual(by["gsd-plan-phase"]["last"], t1.isoformat())
        self.assertTrue(res["history_since"].startswith("2025-09-01"))


if __name__ == "__main__":
    unittest.main()
