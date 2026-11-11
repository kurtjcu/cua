import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cua import waits  # noqa: E402
from cua.model import PermissionConfig, Session, ToolCall, Turn  # noqa: E402

T0 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)


def call(name, secs, at=0, **inp):
    ts = T0 + timedelta(seconds=at)
    return ToolCall(id=f"{name}{at}", name=name, input=inp, ts=ts, result_ts=ts + timedelta(seconds=secs))


def session(calls, project="/home/dev/code/work/proj"):
    s = Session(id="s1", source="claude_code", account="work", project=project)
    t = Turn(ts=T0, prompt="go", end_ts=T0 + timedelta(seconds=60))
    t.tool_calls = calls
    s.turns.append(t)
    return s


def write_log(rows, mode="acceptEdits"):
    """rows: (epoch, kind, tool[, tool_use_id[, session_id]]). Without an id/session the row has 5 columns."""
    f = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
    for epoch, kind, tool, *rest in rows:
        iso = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        cols = [iso, kind, mode, "work/proj", f"{tool}: x"]
        if rest:
            cols += [rest[0], rest[1] if len(rest) > 1 else ""]
        f.write("\t".join(cols) + "\n")
    f.close()
    return f.name


def row(epoch, tool="Bash", mode="acceptEdits", id="", session="", project="work/proj", hash=""):
    return {"epoch": epoch, "mode": mode, "project": project, "tool": tool, "id": id, "session": session, "hash": hash}


def dcall(tool="Bash", at=0, lat=10, session="s1", **kw):
    c = {"id": f"{tool}{at}", "tool": tool, "epoch": T0.timestamp() + at, "lat": lat, "tail": "work/proj",
         "session": session, "prompted": False, "mode": None, "match": None, "async": False}
    c.update(kw)
    return c


class TestWaits(unittest.TestCase):
    def test_label(self):
        self.assertEqual(waits.call_label("Bash", {"command": "git -C /x status"}), "Bash: git status")
        self.assertEqual(waits.call_label("Bash", {"command": "until x; do sleep 5; done"}), "Bash: sleep")
        self.assertEqual(waits.call_label("Agent", {"subagent_type": "gsd-executor"}), "Agent: gsd-executor")
        self.assertEqual(waits.call_label("Edit", {}), "Edit")

    def test_stats_cap_and_away(self):
        st = waits._stats([10, 100, 5000])
        self.assertEqual(st["n"], 3)
        self.assertEqual(st["away_gt_30m"], 1)
        self.assertEqual(st["total_min"], round((10 + 100 + 1800) / 60, 1))

    def test_match_is_one_to_one_and_tool_specific(self):
        calls = [dcall("Bash", 0), dcall("Bash", 1), dcall("Edit", 0)]
        n = waits.match_prompts(calls, [row(T0.timestamp())])
        self.assertEqual([c["prompted"] for c in calls], [True, False, False])
        self.assertEqual(n, {"id": 0, "hash": 0, "session": 0, "guess": 1, "unmatched_rows": 0})
        self.assertEqual(calls[0]["match"], "guess")

    def test_match_window(self):
        c = dcall("Bash", 60)
        waits.match_prompts([c], [row(T0.timestamp())])
        self.assertFalse(c["prompted"])

    def test_match_by_id_beats_nearer_call(self):
        near, far = dcall("Bash", 0), dcall("Bash", 8)
        n = waits.match_prompts([near, far], [row(T0.timestamp() + 1, id=far["id"], session="s1")])
        self.assertEqual((near["prompted"], far["prompted"], far["match"]), (False, True, "id"))
        self.assertEqual(n["id"], 1)

    def test_row_with_id_never_falls_back_to_guess(self):
        c = dcall("Bash", 0)
        n = waits.match_prompts([c], [row(T0.timestamp(), id="toolu_other", session="s1")])
        self.assertFalse(c["prompted"])
        self.assertEqual(n["unmatched_rows"], 1)

    def test_match_by_session_requires_same_session(self):
        a, b = dcall("Bash", 0, session="sA"), dcall("Bash", 1, session="sB")
        n = waits.match_prompts([a, b], [row(T0.timestamp(), session="sB")])
        self.assertEqual((a["prompted"], b["prompted"], b["match"]), (False, True, "session"))
        self.assertEqual(n["session"], 1)

    def test_call_hash_matches_the_hook(self):
        # value produced by the permission-prompt hook's callHash for this exact payload
        inp = {"description": "naïve ünï", "command": "ls -la /tmp", "timeout": 5000,
               "nested": {"b": [1, 2, {"z": 1, "a": None}], "a": True}}
        self.assertEqual(waits.call_hash("Bash", inp), "0fc42cbdff13")

    def test_match_by_hash_picks_the_identical_call_not_the_nearest(self):
        e = T0.timestamp()
        near = dcall("Bash", 0, hash=waits.call_hash("Bash", {"command": "ls"}))
        want = dcall("Bash", 6, hash=waits.call_hash("Bash", {"command": "make"}))
        n = waits.match_prompts([near, want], [row(e + 1, session="s1", hash=want["hash"])])
        self.assertEqual((near["prompted"], want["prompted"], want["match"], n["hash"]), (False, True, "hash", 1))

    def test_hash_miss_falls_back_to_session(self):
        c = dcall("Bash", 0, hash="aaaaaaaaaaaa")
        n = waits.match_prompts([c], [row(T0.timestamp(), session="s1", hash="bbbbbbbbbbbb")])
        self.assertEqual((c["match"], n["hash"], n["session"]), ("session", 0, 1))

    def test_log_only_real_prompts_and_columns(self):
        e = T0.timestamp()
        p = write_log([(e, "prompt", "Bash"), (e, "coached:cd", "Bash"), (e, "prompt:reissued", "Bash", "toolu_1", "s9")])
        try:
            rows = waits.load_prompt_log(p)
        finally:
            os.unlink(p)
        self.assertEqual([(r["id"], r["session"]) for r in rows], [("", ""), ("toolu_1", "s9")])

    def test_union_min_merges_overlap_and_caps(self):
        cs = [dcall(at=0, lat=60), dcall(at=30, lat=60), dcall(at=1000, lat=60), dcall(at=0, lat=60, session="s2")]
        self.assertEqual(waits.union_min(cs), round((90 + 60 + 60) / 60, 1))
        self.assertEqual(waits.union_min([dcall(lat=99999)]), 30.0)

    def test_classify_groups(self):
        cs = [dcall("Bash", 0, 1), dcall("Bash", 10, 3), dcall("Bash", 20, 40), dcall("AskUserQuestion", 30, 40),
              dcall("Monitor", 40, 99999, **{"async": True})]
        for c in cs:
            c["prompted"] = True
        waits.classify_dialogs(cs)
        self.assertEqual([c["group"] for c in cs], ["lt2s", "mid", "human", "question", "async"])

    def test_burst_is_not_human(self):
        cs = [dcall("Read", i, 8) for i in range(25)] + [dcall("Read", 3600, 8)]
        for c in cs:
            c["prompted"] = True
        waits.classify_dialogs(cs)
        self.assertEqual([c["group"] for c in cs], ["burst"] * 25 + ["human"])

    def test_subagent_calls_get_workflow_and_agent_type(self):
        s = Session(id="s1", source="claude_code", account="work", project="/home/dev/code/work/proj")
        s.turns.append(Turn(ts=T0, prompt="/gsd-plan-phase 1", kind="command", command="gsd-plan-phase"))
        s.turns.append(Turn(ts=T0 + timedelta(seconds=500), prompt="/gsd-execute-phase 1", kind="command", command="gsd-execute-phase"))
        s.turns.append(Turn(ts=T0 + timedelta(seconds=900), prompt="just go", kind="prompt"))
        mk = lambda at, aid: ToolCall(id=f"t{at}", name="Bash", input={"command": "make"}, ts=T0 + timedelta(seconds=at),
                                      result_ts=T0 + timedelta(seconds=at + 40), agent_id=aid)
        s.sidechain_tool_calls = [mk(100, "a"), mk(600, "a"), mk(1000, "zz"), mk(1100, "")]
        s.agent_types = {"a": "gsd-executor"}
        rows = waits.analyse([s], "/nonexistent.log")["_calls"]
        self.assertEqual([(r["turn_cmd"], r["agent_type"]) for r in rows],
                         [("gsd-plan-phase", "gsd-executor"), ("gsd-execute-phase", "gsd-executor"),
                          (None, "(unlinked)"), (None, "(main)")])  # a free-text turn has no command of its own
        # ...but the workflow in force carries on through it
        self.assertEqual([r["workflow"] for r in rows],
                         ["gsd-plan-phase", "gsd-execute-phase", "gsd-execute-phase", "gsd-execute-phase"])

    def test_question_shape_never_keeps_text(self):
        def q(text, *labels, multi=False):
            return {"question": text, "options": [{"label": l} for l in labels], "multiSelect": multi}
        confirm = waits.question_extra({"questions": [q("Shall I proceed with the plan?", "Yes", "No")]})
        self.assertEqual((confirm["q_n"], confirm["q_opts_max"], confirm["q_confirm"]), (1, 2, True))
        bare = waits.question_extra({"questions": [q("Next?", "Yes (Recommended)", "No")]})
        self.assertTrue(bare["q_confirm"])
        real = waits.question_extra({"questions": [q("Which database should we use?", "Postgres", "SQLite", "DuckDB", multi=True)]})
        self.assertEqual((real["q_confirm"], real["q_multi"], real["q_opts_max"]), (False, True, 3))
        mixed = waits.question_extra({"questions": [q("Ready to ship?", "Yes", "No"), q("Which region?", "us", "eu")]})
        self.assertFalse(mixed["q_confirm"])  # only counts as confirm if every question is
        self.assertEqual(waits.question_extra({}), {"q_n": 0})
        self.assertNotIn("question", str(real))
        self.assertEqual(waits.web_extra("AskUserQuestion", {"questions": [q("Proceed?", "Yes")]})["q_n"], 1)

    def test_async_and_web_helpers(self):
        self.assertTrue(waits.is_async("Monitor", {}))
        self.assertTrue(waits.is_async("Bash", {"run_in_background": True}))
        self.assertFalse(waits.is_async("Bash", {"command": "ls"}))
        self.assertEqual(waits.web_extra("WebFetch", {"url": "https://docs.example.com/a/b?q=secret"}), {"host": "docs.example.com"})
        self.assertEqual(waits.web_extra("WebSearch", {"query": "hello"}), {"query_len": 5})
        self.assertEqual(waits.web_extra("Bash", {}), {})

    def test_analyse_separates_prompts_from_slow_calls(self):
        s = session([call("Bash", 40, at=0, command="podman run x"), call("Bash", 40, at=100, command="pnpm test"),
                     call("AskUserQuestion", 90, at=200)])
        p = write_log([(T0.timestamp(), "prompt", "Bash"), (T0.timestamp() + 200, "prompt", "AskUserQuestion")])
        try:
            res = waits.analyse([s], p)
        finally:
            os.unlink(p)
        self.assertEqual(res["meta"]["log_prompts_matched"], 2)
        self.assertEqual(res["prompt_wait"]["dialogs"], 1)  # the question is not a permission dialog
        self.assertEqual(res["prompt_wait"]["human"]["n"], 1)
        self.assertEqual(res["by_mode"]["acceptEdits"]["human_n"], 1)
        labels = [r["label"] for r in res["slow_tools_not_prompts"]]
        self.assertIn("Bash: pnpm test", labels)
        self.assertNotIn("Bash: podman run", labels)
        self.assertNotIn("AskUserQuestion", labels)
        self.assertEqual(res["question_wait"]["n"], 1)  # reported separately, matched or not

    def test_async_excluded_from_latency_and_split_by_time(self):
        s = session([call("Monitor", 300000, at=0), call("Bash", 60, at=10, command="make", run_in_background=True),
                     call("Bash", 50, at=700, command="pnpm test")])
        res = waits.analyse([s], "/nonexistent.log")
        self.assertEqual([r["label"] for r in res["slow_tools_not_prompts"]], ["Bash: pnpm test"])
        self.assertEqual(res["async_calls"]["Monitor"]["n"], 1)
        self.assertEqual(res["before_after_mode_split"]["before"]["calls"], 3)  # T0 is before the Auto split
        self.assertEqual(res["before_after_mode_split"]["after"]["calls"], 0)

    def test_why_prompted_names_unmatched_segment(self):
        cfg = PermissionConfig(allow=["Bash(git status *)"])
        why = waits.why_prompted(call("Bash", 5, command="git -C /x status && npm test"), cfg)
        self.assertTrue(why["est_prompt"])
        self.assertTrue(any("npm test" in s for s in why["unmatched_segs"]))
        self.assertEqual(waits.why_prompted(call("Bash", 5, command="ls"), None), {})

    def test_approved_prompts_rollup(self):
        # two approved dialogs of one shape, one rejected of another: only the approved shape
        # appears in approved_prompts, with the real count, a suggested rule and a risk tier.
        cs = [call("Bash", 40, at=0, command="pnpm test"),
              call("Bash", 40, at=5, command="pnpm test"),
              call("Bash", 40, at=10, command="git push")]
        cs[2].denial = "user_rejected"
        s = session(cs)
        p = write_log([(T0.timestamp(), "prompt", "Bash"),
                       (T0.timestamp() + 5, "prompt", "Bash"),
                       (T0.timestamp() + 10, "prompt", "Bash")])
        try:
            res = waits.analyse([s], p)
        finally:
            os.unlink(p)
        ap = res["approved_prompts"]
        self.assertEqual(ap["total_approved"], 2)
        self.assertEqual(ap["total_rejected"], 1)
        top = ap["by_label"][0]
        self.assertEqual((top["label"], top["approved"], top["rule"]), ("Bash: pnpm test", 2, "Bash(pnpm test:*)"))
        self.assertEqual(top["sessions"], 1)
        self.assertNotIn("Bash: git push", [r["label"] for r in ap["by_label"]])

    def test_approved_prompts_empty_without_log(self):
        res = waits.analyse([session([call("Bash", 40, at=0, command="pnpm test")])], "/nonexistent.log")
        self.assertEqual(res["approved_prompts"], {"total_approved": 0, "total_rejected": 0,
                                                   "note": res["approved_prompts"]["note"], "by_label": []})

    def test_missing_result_ts_is_skipped(self):
        c = ToolCall(id="a", name="Bash", input={"command": "ls"}, ts=T0)
        res = waits.analyse([session([c])], "/nonexistent.log")
        self.assertEqual(res["meta"]["calls_with_latency"], 0)


if __name__ == "__main__":
    unittest.main()
