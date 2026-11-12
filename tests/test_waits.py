import contextlib
import difflib
import json
import os
import sys
import tempfile
import unicodedata
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cua import waits  # noqa: E402
from cua.model import PermissionConfig, Session, ToolCall, Turn  # noqa: E402

T0 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 11, 5, 3, 0, 0, tzinfo=timezone.utc)  # after waits.MODE_SPLIT (2026-11-04 02:00Z)
GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "waits_analyse_golden.json")


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
        # a tool_use_id without a session id still makes a 7-column row (session column "")
        f.write(hook_row(epoch, tool=tool, mode=mode, kind=kind, id=rest[0] if rest else "",
                         session=(rest[1] if len(rest) > 1 else "") if rest else None) + "\n")
    f.close()
    return f.name


def hook_row(epoch, tool="Bash", shape=None, mode="default", project="~/code/work/proj",
             id="", session=None, hash=None, kind="prompt"):
    """One log line exactly as hooks/prompt-logger.js writes it. session=None -> 5 columns,
    hash=None -> 7 columns, both given -> 8 (current format). id may be '' (PermissionRequest has none)."""
    iso = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % int((epoch % 1) * 1000)
    cols = [iso, kind, mode, project, shape or f"{tool}: x"]
    if session is not None:
        cols += [id, session]
        if hash is not None:
            cols.append(hash)
    return "\t".join(cols)


def log_file(test, lines):
    """Write hook-shape lines to a temp prompts.log that is removed when the test ends; return its path."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    path = os.path.join(tmp.name, "prompts.log")
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
    return path


def row(epoch, tool="Bash", mode="acceptEdits", id="", session="", project="work/proj", hash=""):
    return {"epoch": epoch, "mode": mode, "project": project, "tool": tool, "id": id, "session": session, "hash": hash}


def dcall(tool="Bash", at=0, lat=10, session="s1", **kw):
    c = {"id": f"{tool}{at}", "tool": tool, "epoch": T0.timestamp() + at, "lat": lat, "tail": "work/proj",
         "session": session, "prompted": False, "mode": None, "match": None, "async": False}
    c.update(kw)
    return c


def tcall(id, name, base, at, lat, agent_id="", denial=None, err=False, **inp):
    """A ToolCall `at` seconds after `base` that waited `lat` seconds (None: no result_ts)."""
    ts = base + timedelta(seconds=at)
    return ToolCall(id=id, name=name, input=inp, ts=ts, result_ts=None if lat is None else ts + timedelta(seconds=lat),
                    result_error=err, denial=denial, agent_id=agent_id)


def rich_fixture(tmpdir):
    """(sessions, log_path, kwargs) for waits.analyse: deterministic, synthetic /home/dev data only, built to
    touch every section of waits.json. Fixed epochs, no wall clock, no randomness; nothing real is read."""
    e0, e1 = T0.timestamp(), T1.timestamp()
    pnpm = waits.call_hash("Bash", {"command": "pnpm test"})
    proj, other = "~/code/work/proj", "~/code/work/other"

    # --- session A: project "proj", before the mode split, multi-turn ---
    a = [tcall("a1", "Bash", T0, 0, 40, command="pnpm test"),
         tcall("a2", "Bash", T0, 100, 20, denial="user_rejected", command="git push origin main"),
         tcall("a3", "Bash", T0, 200, 60, command="pnpm test"),
         tcall("a4", "Read", T0, 300, 1, file_path="/home/dev/code/work/proj/README.md"),
         tcall("a5", "Read", T0, 310, 3, file_path="/home/dev/code/work/proj/notes.txt"),
         tcall("a6", "AskUserQuestion", T0, 320, 90, questions=[
             {"question": "Shall I proceed with the plan?", "options": [{"label": "Yes"}, {"label": "No"}]}]),
         tcall("a7", "Monitor", T0, 330, 500, command="tail -f build.log"),
         tcall("a8", "Bash", T0, 340, 60, command="make build", run_in_background=True),
         tcall("a9", "Bash", T0, 400, 45, command="cargo build"),
         tcall("a10", "Bash", T0, 450, 35, command="café"),                  # non-ASCII label
         tcall("a11", "Bash", T0, 500, None, command="ls"),                  # no result_ts: dropped
         tcall("a12", "Bash", T0, 510, 0.5, err=True, command="ls /missing"),
         tcall("a13", "Bash", T0, 520, 1, command="git status")]
    sa = Session(id="sA", source="claude_code", account="work", project="/home/dev/code/work/proj")
    sa.turns = [
        Turn(ts=T0, prompt="build it", end_ts=T0 + timedelta(seconds=600), tool_calls=a),
        Turn(ts=T0 + timedelta(seconds=620), prompt="next", end_ts=T0 + timedelta(seconds=3000), interrupted=True,
             tool_calls=[tcall("a14", "Bash", T0, 700, 2, command="ls")]),                         # gap 20 s before it
        Turn(ts=T0 + timedelta(seconds=3300), prompt="ok", end_ts=T0 + timedelta(seconds=3320),   # gap 300 s, interrupted
             tool_calls=[tcall("a15", "Read", T0, 3305, 1, file_path="/home/dev/code/work/proj/a.txt")]),
        Turn(ts=T0 + timedelta(seconds=3330), prompt="keep going", kind="continuation",           # continuation: no gap
             end_ts=T0 + timedelta(seconds=3340)),
        Turn(ts=T0 + timedelta(seconds=4240), prompt="/gsd-plan-phase 1", kind="command",          # gap 900 s, no last tool
             command="gsd-plan-phase", end_ts=T0 + timedelta(seconds=4300),
             tool_calls=[tcall("a16", "Edit", T0, 4250, 20, file_path="/home/dev/code/work/proj/b.py")]),
        Turn(ts=T0 + timedelta(seconds=8300), prompt="back", end_ts=T0 + timedelta(seconds=8310)),  # gap 4000 s, away
        Turn(ts=T0 + timedelta(seconds=8355), prompt="and also", end_ts=T0 + timedelta(seconds=8400)),  # gap 45 s
    ]
    sa.sidechain_tool_calls = [tcall("s1", "Bash", T0, 20, 30, agent_id="ag1", command="pnpm test"),
                               tcall("s2", "Bash", T0, 8000, 7, agent_id="zz", command="ls")]
    sa.agent_types = {"ag1": "gsd-executor"}

    # --- session B: project "other", after the split, a 22-dialog subagent burst, two UTC days ---
    burst = [tcall(f"bu{i}", "Read", T1, i, 8, agent_id="ag2", file_path=f"/home/dev/code/work/other/f{i}.txt")
             for i in range(22)]
    sb = Session(id="sB", source="claude_code", account="work", project="/home/dev/code/work/other")
    sb.turns = [
        Turn(ts=T1 + timedelta(seconds=60), prompt="go", end_ts=T1 + timedelta(seconds=700), tool_calls=[
            tcall("b1", "Bash", T1, 100, 50, command="make"),
            tcall("b2", "Bash", T1, 200, 50, command="cargo test"),       # ties b1 on union_min
            tcall("b3", "Bash", T1, 300, 60, command="docker ps"),
            tcall("b4", "Bash", T1, 360, 60, command="docker ps"),        # starts where b3 ends: touching
            tcall("b5", "Edit", T1, 500, 12, file_path="/home/dev/code/work/other/x.py")]),
        Turn(ts=T1 + timedelta(seconds=89000), prompt="next day", end_ts=T1 + timedelta(seconds=89100),  # second UTC day
             tool_calls=[tcall("b6", "Bash", T1, 89010, 25, command="pnpm test")]),
    ]
    sb.sidechain_tool_calls = burst
    sb.agent_types = {"ag2": "gsd-executor"}

    rows = [
        hook_row(e0 + 1, session="sA", hash=pnpm, mode="default", project=proj),                          # a1: hash
        hook_row(e0 + 21, session="sA", hash=pnpm, mode="default", project=proj),                         # s1: hash
        hook_row(e0 + 101, session="sA", mode="default", project=proj),                                   # a2: session
        hook_row(e0 + 201, mode="acceptEdits", project=proj),                                             # a3: guess, 5 columns
        hook_row(e0 + 301, tool="Read", mode="plan", project=proj),                                       # a4: guess
        hook_row(e0 + 311, tool="Read", id="a5", session="sA", mode="plan", project=proj),                # a5: id
        hook_row(e0 + 321, tool="AskUserQuestion", session="sA", mode="default", project=proj),           # a6: session
        hook_row(e0 + 331, tool="Monitor", session="sA", mode="default", project=proj),                   # a7: session
        hook_row(e0 + 451, shape="Bash: café", mode="acceptEdits", project=proj),                         # a10: guess
        hook_row(e0 + 9000, mode="default", project=proj),                                                # no call: unmatched
        hook_row(e1 + 101, session="sB", mode="default", project=other),                                  # b1
        hook_row(e1 + 201, mode="plan", project=other, shape="Bash: cargo test"),                         # b2: guess
        hook_row(e1 + 301, session="sB", mode="default", project=other),                                  # b3
        hook_row(e1 + 361, session="sB", mode="default", project=other),                                  # b4
        hook_row(e1 + 501, tool="Edit", session="sB", mode="acceptEdits", project=other),                 # b5
        hook_row(e1 + 89011, session="sB", mode="default", project=other),                                # b6
        hook_row(e0 + 5, mode="default", project="~/code/home/blog"),                                     # other account
        hook_row(e0 + 6, kind="coached:cd", project=proj),                                                # not a prompt
        "a\tb\tc",                                                                                        # malformed
    ] + [hook_row(e1 + i + 1, tool="Read", session="sB", mode="default", project=other) for i in range(22)]
    log_path = os.path.join(tmpdir, "prompts.log")
    with open(log_path, "w", encoding="utf-8") as f:
        for line in rows:
            f.write(line + "\n")
    kwargs = {"perm_for_session": lambda s: PermissionConfig(allow=["Bash(git status *)"])}
    return [sa, sb], log_path, kwargs


def analyse_golden_text() -> str:
    """analyse() over rich_fixture as stable text: the result, plus the top-level and meta key order (waits.json
    is written without sort_keys, so order is part of the output). The temp log path never appears in it."""
    with mock.patch.dict(os.environ, {"HOME": "/home/dev"}), tempfile.TemporaryDirectory() as tmp:
        sessions, log_path, kwargs = rich_fixture(tmp)
        res = waits.analyse(sessions, log_path, **kwargs)
    return json.dumps({"key_order": list(res), "meta_key_order": list(res["meta"]), "result": res},
                      sort_keys=True, indent=1, default=str) + "\n"


class TestWaits(unittest.TestCase):
    def setUp(self):
        # os.path.expanduser must be deterministic, and the real HOME must stay out of reach
        patcher = mock.patch.dict(os.environ, {"HOME": "/home/dev"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_hook_shape_5col_row_is_counted_end_to_end(self):
        s = session([call("Bash", 40, at=0, command="pnpm test")])
        p = log_file(self, [hook_row(T0.timestamp(), shape="Bash: pnpm test")])
        res = waits.analyse([s], p)
        self.assertEqual(res["meta"]["logged_prompts"], 1)
        self.assertEqual(res["meta"]["matched_by"]["guess"], 1)
        self.assertEqual(res["approved_prompts"]["total_approved"], 1)

    def test_same_project_wins_between_competing_prompts(self):
        e = T0.timestamp()
        p = log_file(self, [hook_row(e + 1, project="~/code/other/thing", mode="plan"),   # nearer, wrong project
                            hook_row(e + 4, project="~/code/work/proj", mode="default")])  # farther, same project
        rows = waits.load_prompt_log(p)
        c = dcall("Bash", 0)  # tail == "work/proj"
        waits.match_prompts([c], rows)
        self.assertTrue(c["prompted"])
        self.assertEqual(c["mode"], "default")

    def test_norm_project_matches_tail(self):
        self.assertEqual(waits.norm_project("~/code/work/proj"), "work/proj")
        self.assertEqual(waits.norm_project("/home/dev/code/work/proj"), "work/proj")
        self.assertEqual(waits.norm_project("~/foo"), waits._tail("/home/dev/foo"))
        self.assertEqual(waits.norm_project("~/foo"), "dev/foo")
        self.assertEqual(waits.norm_project("~"), "home/dev")
        self.assertEqual(waits.norm_project(""), "")
        self.assertEqual(waits.norm_project("work/proj"), "work/proj")  # already normalised: unchanged

    def test_prompt_log_counts_every_row(self):
        e = T0.timestamp()
        bad_time = hook_row(e).split("\t")
        bad_time[0] = "not-a-time"
        p = log_file(self, [
            "",                                                   # blank: ignored, not a row
            "a\tb\tc",                                            # malformed: 3 columns
            "\t".join(bad_time),                                  # malformed: unparseable time
            hook_row(e, kind="coached:cd"),                       # other kind
            hook_row(e - 86400),                                  # before --since
            hook_row(e, project="~/code/other/thing"),            # other account
            hook_row(e),                                          # kept, 5 columns
            hook_row(e + 1, session="s1"),                        # kept, 7 columns
            hook_row(e + 2, session="s1", hash="0123456789ab"),   # kept, 8 columns
        ])
        s = session([call("Bash", 40, at=0, command="make")])
        res = waits.analyse([s], p, since=T0 - timedelta(seconds=3600))
        pl = res["meta"]["prompt_log"]
        self.assertEqual(pl, {"file_found": True, "rows_read": 8, "skipped_malformed": 2, "skipped_other_kind": 1,
                              "skipped_before_since": 1, "excluded_other_account": 1, "kept": 3})
        self.assertEqual(res["meta"]["logged_prompts"], 3)
        self.assertEqual(pl["rows_read"], pl["kept"] + pl["skipped_malformed"] + pl["skipped_other_kind"]
                         + pl["skipped_before_since"] + pl["excluded_other_account"])

    def test_prompt_log_without_file(self):
        zero = {"file_found": False, "rows_read": 0, "skipped_malformed": 0, "skipped_other_kind": 0,
                "skipped_before_since": 0, "excluded_other_account": 0, "kept": 0}
        for path in ("/nonexistent.log", ""):
            res = waits.analyse([session([call("Bash", 40, at=0, command="make")])], path)
            self.assertEqual(res["meta"]["prompt_log"], zero)
            self.assertEqual(res["meta"]["logged_prompts"], 0)
        empty = log_file(self, [])
        res = waits.analyse([session([call("Bash", 40, at=0, command="make")])], empty)
        self.assertEqual(res["meta"]["prompt_log"], {**zero, "file_found": True})

    def test_filter_uses_selected_sessions_not_calls(self):
        s1 = session([call("Bash", 40, at=0, command="make")])
        s2 = Session(id="s2", source="claude_code", account="work", project="/home/dev/code/work/other")
        t = Turn(ts=T0, prompt="go", end_ts=T0 + timedelta(seconds=60))
        t.tool_calls = [ToolCall(id="n1", name="Bash", input={"command": "ls"}, ts=T0)]  # no result_ts: no latency
        s2.turns.append(t)
        p = log_file(self, [hook_row(T0.timestamp(), project="~/code/work/other")])
        res = waits.analyse([s1, s2], p)
        pl = res["meta"]["prompt_log"]
        self.assertEqual((pl["kept"], pl["excluded_other_account"]), (1, 0))

    def test_hook_rows_5_7_8_columns_parse(self):
        e = T0.timestamp()
        lines = [hook_row(e), hook_row(e + 1, session="s1"), hook_row(e + 2, session="s1", hash="0123456789ab")]
        self.assertEqual([len(x.split("\t")) for x in lines], [5, 7, 8])
        rows = waits.load_prompt_log(log_file(self, lines))
        self.assertEqual([(r["project"], r["id"], r["session"], r["hash"]) for r in rows],
                         [("work/proj", "", "", ""), ("work/proj", "", "s1", ""), ("work/proj", "", "s1", "0123456789ab")])

    def test_hook_rows_5_7_8_columns_join_through_analyse(self):
        e = T0.timestamp()
        s = session([call("Bash", 40, at=0, command="make"), call("Bash", 40, at=20, command="ls"),
                     call("Bash", 40, at=40, command="pwd")])
        p = log_file(self, [hook_row(e + 1, session="s1", hash=waits.call_hash("Bash", {"command": "make"})),
                            hook_row(e + 21, session="s1"),
                            hook_row(e + 41)])
        res = waits.analyse([s], p)
        self.assertEqual(res["meta"]["logged_prompts"], 3)
        self.assertEqual(res["meta"]["matched_by"], {"id": 0, "hash": 1, "session": 1, "guess": 1, "unmatched_rows": 0})
        self.assertEqual(res["approved_prompts"]["total_approved"], 3)

    def test_prompt_log_note(self):
        self.assertEqual(waits.prompt_log_note(waits.new_prompt_log_stats()), "no prompt log file read")
        self.assertEqual(waits.prompt_log_note({**waits.new_prompt_log_stats(), "file_found": True}), "")
        st = {**waits.new_prompt_log_stats(), "file_found": True, "rows_read": 10,
              "excluded_other_account": 2, "skipped_malformed": 1}
        self.assertEqual(waits.prompt_log_note(st), "excluded 3 of 10 rows: 2 other account, 1 malformed")

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

    def test_rollup_approvals(self):
        def ap(tool, at, session, label, head, main=True, denial=None, **inp):
            tc = tcall(f"{tool}{at}", tool, T0, at, 40, **inp)
            return dcall(tool, at, 40, session=session, prompted=True, group="human", label=label, head=head,
                         main=main, denial=denial, _tc=tc)
        a1 = ap("Bash", 0, "s1", "Bash: pnpm test", "pnpm test --first", command="pnpm test --first")
        a2 = ap("Bash", 10, "s2", "Bash: pnpm test", "pnpm test --second", command="pnpm test --second")
        rd = ap("Read", 20, "s1", "Read", "/x/a.txt", main=False, file_path="/x/a.txt")
        rej = ap("Bash", 30, "s1", "Bash: git push", "git push", denial="user_rejected", command="git push")
        res = waits.rollup_approvals([a1, a2, rd, rej], 25)
        self.assertEqual((res["total_approved"], res["total_rejected"]), (3, 1))
        self.assertEqual([r["label"] for r in res["by_label"]], ["Bash: pnpm test", "Read"])
        first, second = res["by_label"]
        self.assertEqual((first["approved"], first["sessions"]), (2, 2))
        self.assertEqual(first["rule"], waits.rule_key(a1["_tc"]))
        self.assertEqual(first["risk"], waits.tool_risk(first["rule"]))
        self.assertEqual(first["example"], "pnpm test --first")  # first-seen head
        self.assertEqual((second["approved"], second["subagent_share"]), (1, 1.0))
        self.assertEqual(second["rule"], waits.rule_key(rd["_tc"]))
        self.assertEqual([r["label"] for r in waits.rollup_approvals([a1, a2, rd, rej], 1)["by_label"]],
                         ["Bash: pnpm test"])
        # unprompted, question and async calls never count
        q = dcall("AskUserQuestion", 40, prompted=True, group="question", label="AskUserQuestion", denial=None)
        self.assertEqual(waits.rollup_approvals([q, dcall("Bash", 50, prompted=False, group=None)], 25)["total_approved"], 0)
        # empty and single-element inputs
        empty = waits.rollup_approvals([], 25)
        self.assertEqual((empty["total_approved"], empty["total_rejected"], empty["by_label"]), (0, 0, []))
        one = waits.rollup_approvals([a1], 25)
        self.assertEqual((one["total_approved"], len(one["by_label"])), (1, 1))

    def test_rollup_dialog_groups(self):
        def pc(group, lat, at, **kw):
            return dcall("Bash", at, lat, prompted=True, group=group, main=kw.pop("main", True), **kw)
        calls = [pc("lt2s", 1, 0), pc("mid", 3, 10), pc("human", 40, 100), pc("human", 70, 300, main=False),
                 pc("burst", 8, 500), pc("question", 40, 600), pc("async", 900, 700), dcall("Bash", 800, 5)]
        res = waits.rollup_dialog_groups(calls)
        self.assertEqual(list(res), ["dialogs", "groups", "human", "human_main_thread", "human_subagent", "burst",
                                     "lt2s", "human_gt_60s", "human_gt_600s"])
        self.assertEqual(res["dialogs"], 5)
        self.assertEqual(res["groups"], {"lt2s": 1, "mid": 1, "human": 2, "burst": 1, "question": 1, "async": 1})
        self.assertEqual((res["human"]["n"], res["human_main_thread"]["n"], res["human_subagent"]["n"]), (2, 1, 1))
        self.assertEqual((res["human_gt_60s"], res["human_gt_600s"]), (1, 0))  # only the 70 s wait is over 60 s
        self.assertEqual((res["burst"]["n"], res["lt2s"]["n"]), (1, 1))
        empty = waits.rollup_dialog_groups([])
        self.assertEqual(empty["dialogs"], 0)
        self.assertEqual(empty["groups"], {g: 0 for g in ("lt2s", "mid", "human", "burst", "question", "async")})
        self.assertEqual((empty["human"]["n"], empty["human"]["union_min"]), (0, 0.0))
        self.assertEqual((empty["human_gt_60s"], empty["human_gt_600s"]), (0, 0))

    def test_rollup_by_label(self):
        def hc(label, at, lat=40, **kw):
            return dcall("Bash", at, lat, prompted=True, group="human", label=label, head=kw.pop("head", label),
                         main=kw.pop("main", True), denial=kw.pop("denial", None), **kw)
        # tie on union_min: first-seen order wins, and the input order decides it
        a, b = hc("Bash: café build", 0), hc("Read", 100)
        self.assertEqual([r["label"] for r in waits.rollup_by_label([a, b], 25)], ["Bash: café build", "Read"])
        self.assertEqual([r["label"] for r in waits.rollup_by_label([b, a], 25)], ["Read", "Bash: café build"])
        # exactly touching intervals in one session merge: two 60 s waits give 2.0 min
        touch = waits.rollup_by_label([hc("Bash: make", 0, 60), hc("Bash: make", 60, 60)], 25)
        self.assertEqual((len(touch), touch[0]["union_min"], touch[0]["n"]), (1, 2.0, 2))
        # NFC and NFD spellings of the same word are different keys: str equality, no normalisation
        nfc, nfd = unicodedata.normalize("NFC", "Bash: café"), unicodedata.normalize("NFD", "Bash: café")
        self.assertNotEqual(nfc, nfd)
        enc = waits.rollup_by_label([hc(nfc, 0), hc(nfd, 100)], 25)
        self.assertEqual([r["label"] for r in enc], [nfc, nfd])
        # denied count, subagent share, first-seen example, top, empty
        rows = waits.rollup_by_label([hc("Bash: x", 0, head="first", denial="user_rejected"),
                                      hc("Bash: x", 100, head="second", main=False), hc("Edit", 1000, 5)], 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["label"], rows[0]["denied"], rows[0]["subagent_share"], rows[0]["example"]),
                         ("Bash: x", 1, 0.5, "first"))
        self.assertEqual(waits.rollup_by_label([], 25), [])
        self.assertEqual(len(waits.rollup_by_label([a], 25)), 1)

    def test_rollup_by_project(self):
        def hc(tail, at, lat):
            return dcall("Bash", at, lat, prompted=True, group="human", tail=tail, label="Bash: x", head="x", main=True,
                         denial=None)
        calls = [hc(f"w/p{i:02d}", i * 3000, 60 * (i + 1)) for i in range(16)]  # 16 distinct tails, rising waits
        rows = waits.rollup_by_project(calls)
        self.assertEqual(len(rows), 15)
        self.assertEqual(rows[0]["project"], "w/p15")
        self.assertNotIn("w/p00", [r["project"] for r in rows])  # the smallest one is cut
        mins = [r["union_min"] for r in rows]
        self.assertEqual(mins, sorted(mins, reverse=True))
        merged = waits.rollup_by_project([hc("w/a", 0, 60), hc("w/a", 1000, 60), hc("w/b", 0, 30)])
        self.assertEqual([(r["project"], r["n"]) for r in merged], [("w/a", 2), ("w/b", 1)])
        self.assertEqual(waits.rollup_by_project([]), [])

    def test_rollup_by_day(self):
        def hc(epoch, lat=30):
            return dcall("Bash", 0, lat, prompted=True, group="human", epoch=epoch, label="Bash: x", head="x", main=True,
                         denial=None)
        late = datetime(2026, 11, 1, 23, 59, 59, tzinfo=timezone.utc).timestamp()
        early = datetime(2026, 11, 2, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        rows = waits.rollup_by_day([hc(late), hc(early)])
        self.assertEqual(sorted(r["day"] for r in rows), ["2026-11-01", "2026-11-02"])
        days = [hc(datetime(2026, 11, 1, 12, tzinfo=timezone.utc).timestamp() + i * 86400, 60 * (i + 1)) for i in range(11)]
        rows = waits.rollup_by_day(days)
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]["day"], "2026-11-11")
        self.assertNotIn("2026-11-01", [r["day"] for r in rows])  # the smallest of 11 is cut
        self.assertEqual(waits.rollup_by_day([]), [])

    def test_rollup_gaps(self):
        names = ["<30s", "30s-2m", "2-10m", "10-30m", ">30m (away)"]
        empty = waits.rollup_gaps([])
        self.assertEqual(list(empty), ["user_gap_buckets", "user_gap_live", "user_gap_by_last_tool"])
        self.assertEqual([(b["bucket"], b["n"], b["total_min"]) for b in empty["user_gap_buckets"]],
                         [(n, 0, 0.0) for n in names])
        self.assertEqual((empty["user_gap_live"]["n"], empty["user_gap_by_last_tool"]), (0, []))

        def at(secs):
            return T0 + timedelta(seconds=secs)
        s = Session(id="g1", source="claude_code", account="work", project="/home/dev/code/work/proj")
        s.turns = [Turn(ts=at(0), prompt="a", end_ts=at(10), tool_calls=[call("Bash", 1)]),
                   Turn(ts=at(30), prompt="b", end_ts=at(100), interrupted=True),            # gap 20 s after a Bash turn
                   Turn(ts=at(400), prompt="c", end_ts=at(410)),                              # gap 300 s after an interrupt
                   Turn(ts=at(4410), prompt="d", end_ts=at(4420)),                            # gap 4000 s: away
                   Turn(ts=at(4430), prompt="keep going", kind="continuation", end_ts=at(4440))]  # adds no gap
        res = waits.rollup_gaps([s])
        self.assertEqual([(b["bucket"], b["n"]) for b in res["user_gap_buckets"]],
                         [("<30s", 1), ("30s-2m", 0), ("2-10m", 1), ("10-30m", 0), (">30m (away)", 1)])
        self.assertEqual(res["user_gap_buckets"][2]["total_min"], 5.0)
        self.assertEqual((res["user_gap_live"]["n"], res["user_gap_live"]["after_interrupt"]), (2, 1))
        self.assertEqual({r["last_tool"] for r in res["user_gap_by_last_tool"]}, {"Bash", "(no tools)"})
        # boundary: a gap of exactly 30 s is in "30s-2m" (lo <= g < hi)
        edge = Session(id="g2", source="claude_code", account="work", project="/home/dev/code/work/proj")
        edge.turns = [Turn(ts=at(0), prompt="a", end_ts=at(10)), Turn(ts=at(40), prompt="b", end_ts=at(50))]
        res = waits.rollup_gaps([edge])
        self.assertEqual([b["n"] for b in res["user_gap_buckets"]], [0, 1, 0, 0, 0])
        # a single session with one turn has no gap at all
        one = Session(id="g3", source="claude_code", account="work", project="/home/dev/code/work/proj")
        one.turns = [Turn(ts=at(0), prompt="a", end_ts=at(10))]
        self.assertEqual(sum(b["n"] for b in waits.rollup_gaps([one])["user_gap_buckets"]), 0)

    def test_other_rollups_handle_empty(self):
        modes = waits.rollup_modes([])
        self.assertEqual(list(modes), ["by_mode", "before_after_mode_split"])
        self.assertEqual(modes["by_mode"], {})
        self.assertEqual(modes["before_after_mode_split"]["before"]["calls"], 0)
        pauses = waits.rollup_pauses([], 25)
        self.assertEqual(list(pauses), ["question_wait", "async_calls", "slow_tools_not_prompts", "tool_latency_by_tool",
                                        "fast_error_calls"])
        self.assertEqual(pauses["question_wait"]["n"], 0)
        self.assertEqual((pauses["async_calls"], pauses["slow_tools_not_prompts"], pauses["tool_latency_by_tool"],
                          pauses["fast_error_calls"]), ({}, [], [], 0))
        self.assertEqual(waits.rollup_background_style([]), {})
        self.assertEqual(waits.calls_detail([]), [])

    def test_analyse_delegates_to_rollups(self):
        names = ("rollup_approvals", "rollup_dialog_groups", "rollup_by_label", "rollup_by_project", "rollup_by_day",
                 "rollup_gaps")
        s = session([call("Bash", 40, at=0, command="pnpm test"), call("Read", 1, at=100, file_path="/x")])
        with contextlib.ExitStack() as stack:
            spies = {n: stack.enter_context(mock.patch.object(waits, n, wraps=getattr(waits, n))) for n in names}
            res = waits.analyse([s], "/nonexistent.log")
        for n, spy in spies.items():
            self.assertEqual(spy.call_count, 1, n)
        self.assertEqual(res["meta"]["calls_with_latency"], 2)

    def test_analyse_characterisation_golden(self):
        # Pins analyse() output (values, top-level key order, meta key order) for rich_fixture. The golden was
        # captured before analyse() was split into rollups; a mismatch means a refactor changed behaviour. Fix
        # cua/waits.py, never the golden.
        if not os.path.exists(GOLDEN):
            self.fail(f"{GOLDEN} is missing; create it once, on unrefactored code, with "
                      f"`uv run python -I tests/test_waits.py --write-golden`")
        with open(GOLDEN, encoding="utf-8") as f:
            want = f.read()
        got = analyse_golden_text()
        if got != want:
            diff = list(difflib.unified_diff(want.splitlines(), got.splitlines(), "golden", "analyse()", lineterm=""))
            self.fail("analyse() output differs from the golden:\n" + "\n".join(diff[:80]))

    def test_rich_fixture_covers_every_section(self):
        # keeps the golden meaningful: if the fixture stops exercising a section, the golden would pin nothing there
        with mock.patch.dict(os.environ, {"HOME": "/home/dev"}), tempfile.TemporaryDirectory() as tmp:
            sessions, log_path, kwargs = rich_fixture(tmp)
            res = waits.analyse(sessions, log_path, **kwargs)
        groups = res["prompt_wait"]["groups"]
        for g in ("lt2s", "mid", "human", "burst", "question", "async"):
            self.assertGreaterEqual(groups[g], 1, g)
        self.assertGreaterEqual(res["approved_prompts"]["total_rejected"], 1)
        self.assertGreaterEqual(sum(b["n"] >= 1 for b in res["user_gap_buckets"]), 3)
        split = res["before_after_mode_split"]
        self.assertGreaterEqual(split["before"]["calls"], 1)
        self.assertGreaterEqual(split["after"]["calls"], 1)
        self.assertGreaterEqual(len(res["prompt_wait_by_day"]), 2)
        pl = res["meta"]["prompt_log"]
        for k in ("excluded_other_account", "skipped_other_kind", "skipped_malformed"):
            self.assertGreaterEqual(pl[k], 1, k)
        for k in ("hash", "session", "guess"):
            self.assertGreaterEqual(res["meta"]["matched_by"][k], 1, k)
        self.assertTrue(any(not r["label"].isascii() for r in res["prompt_wait_by_label"]))  # non-ASCII label kept


if __name__ == "__main__":
    if "--write-golden" in sys.argv:
        os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
        with open(GOLDEN, "w", encoding="utf-8") as f:
            f.write(analyse_golden_text())
        print(GOLDEN)
    else:
        unittest.main()
