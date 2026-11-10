"""Wait-time analysis: where does the session sit still, and who is it waiting on?

Two measurements, both from transcript timestamps:
  * tool latency: tool_use.ts -> tool_result.ts, per call. A call that raised a real permission prompt
    includes the time the human took to answer. Real prompts come from an optional permission-prompt
    hook log (`--prompt-log`, `kind == prompt`), joined to calls because cua's rule estimate cannot see
    auto-approving hooks.
    Join tiers, best first: exact tool_use_id (the PermissionRequest event never supplies one); call hash
    (column 8, tool+input fingerprint); same session + tool + nearest timestamp (7-column rows); tool +
    project tail + nearest timestamp (old 5-column rows).
  * user gaps: end of one turn -> next prompt, i.e. time the session waited for the human to type.

What counts as a human wait (permission dialogs only):
  * AskUserQuestion is a question, not permission friction: reported separately.
  * Async tools (Monitor, ScheduleWakeup, background Agent/Bash) have meaningless latency: reported separately.
  * Subagent bursts (>= BURST_PER_MIN dialogs in one session in one minute) are queue time: not human.
  * < 2s were resolved without a human; 2-5s are ambiguous; >= 5s are human waits.
  * Human minutes are the UNION of wait intervals per session, each wait capped at 30 minutes, so parallel
    waits are not double-counted. Per-label and per-project figures are unions within that group, so they
    do not sum to the total.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlparse

from .permissions import (_bash_rules, _rule_matches, _rule_matches_seg, _seg_safe, bash_key, bash_segments,
                          would_prompt)
from .redact import redact

LOG_DEFAULT = ""  # optional permission-prompt hook log; set via --prompt-log
JOIN_WINDOW_S = 10.0
IDLE_CAP_S = 1800.0  # a gap beyond this is the user being away, not a wait worth optimising
SLOW_S = 30.0
AUTO_S = 2.0         # resolved faster than this: no human involved
HUMAN_S = 5.0        # at or above this: a human wait
BURST_PER_MIN = 20   # dialogs in one session in one minute: subagent queue, not a human
ASYNC_TOOLS = ("Monitor", "ScheduleWakeup")
NOT_WORKFLOW = {"clear", "compact", "model", "help", "list-agents", "loop", "config", "permissions", "status", "usage"}
MODE_SPLIT = datetime(2026, 11, 4, 2, 0, tzinfo=timezone.utc)  # Auto mode trial starts


def load_prompt_log(path: str, since: datetime | None = None) -> list[dict]:
    """One dict per real permission prompt the hook saw. Old rows have 5 columns (no id, no session);
    since 2026-11-04 ~01:30Z rows have 7: time, kind, mode, project, shape, tool_use_id, session_id."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) < 5 or p[1] not in ("prompt", "prompt:reissued"):
                continue
            try:
                t = datetime.fromisoformat(p[0].replace("Z", "+00:00"))
            except ValueError:
                continue
            if since and t < since:
                continue
            out.append({"epoch": t.timestamp(), "mode": p[2], "project": p[3], "tool": p[4].split(":", 1)[0],
                        "id": p[5].strip() if len(p) > 5 else "", "session": p[6].strip() if len(p) > 6 else "",
                        "hash": p[7].strip() if len(p) > 7 else ""})
    out.sort(key=lambda r: r["epoch"])
    return out


def _tail(path: str | None) -> str:
    parts = [x for x in (path or "").rstrip("/").split("/") if x]
    return "/".join(parts[-2:])


def _mark(c: dict, r: dict, how: str) -> None:
    c["prompted"] = True
    c["mode"] = r["mode"]
    c["match"] = how


def call_hash(tool: str, inp: dict) -> str:
    """Fingerprint the coach hook logs in column 8 (the PermissionRequest event has no tool_use_id):
    12 hex of sha256 over canonical JSON of {"n": tool, "i": input}."""
    s = json.dumps({"n": tool, "i": inp or {}}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()[:12]


def match_prompts(calls: list[dict], log: list[dict]) -> dict:
    """Mark calls that coincide with a logged prompt; one log row matches at most one call.
    Tier 1 by tool_use_id; tier 2 by call hash (same tool and input, same session, nearest time);
    tier 3 by session + tool + nearest time; tier 4 (old rows with no session) by tool + nearest time,
    same project tail preferred. A row whose hash or id finds no call falls through to the later tiers.
    Sets prompted, mode, match on calls and returns how many log rows matched each way."""
    n = {"id": 0, "hash": 0, "session": 0, "guess": 0, "unmatched_rows": 0}
    used: set[int] = set()
    by_id = {c["id"]: c for c in calls if c.get("id")}
    for i, r in enumerate(log):
        c = by_id.get(r["id"]) if r["id"] else None
        if c is not None and not c.get("prompted"):
            _mark(c, r, "id")
            used.add(i)
            n["id"] += 1
    by_hash: dict = defaultdict(list)
    for c in calls:
        if c.get("hash") and not c.get("prompted"):
            by_hash[c["hash"]].append(c)
    for i, r in enumerate(log):
        if i in used or not r.get("hash"):
            continue
        cands = [c for c in by_hash.get(r["hash"], ()) if not c.get("prompted") and c["tool"] == r["tool"]
                 and (not r["session"] or r["session"] == c.get("session"))
                 and abs(c["epoch"] - r["epoch"]) <= JOIN_WINDOW_S]
        if cands:
            _mark(min(cands, key=lambda c: abs(c["epoch"] - r["epoch"])), r, "hash")
            used.add(i)
            n["hash"] += 1
    times = [r["epoch"] for r in log]
    for c in sorted(calls, key=lambda c: c["epoch"]):
        if c.get("prompted"):
            continue
        lo = bisect.bisect_left(times, c["epoch"] - JOIN_WINDOW_S)
        hi = bisect.bisect_right(times, c["epoch"] + JOIN_WINDOW_S)
        best, best_key = None, None
        for i in range(lo, hi):
            r = log[i]
            if i in used or r["tool"] != c["tool"] or r["id"]:
                continue  # a row with an id only ever matches by id
            if r["session"] and r["session"] != c.get("session"):
                continue
            key = (not r["session"], r["project"] != c.get("tail"), abs(r["epoch"] - c["epoch"]))
            if best_key is None or key < best_key:
                best, best_key = i, key
        if best is not None:
            used.add(best)
            how = "session" if log[best]["session"] else "guess"
            _mark(c, log[best], how)
            n[how] += 1
    n["unmatched_rows"] = len(log) - len(used)
    return n


def _pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def _stats(xs: list[float]) -> dict:
    """total_min caps each wait at IDLE_CAP_S so one prompt left open overnight can't swamp the sum;
    away_gt_30m counts those separately."""
    return {"n": len(xs), "p50_s": round(_pct(xs, .5), 1), "p90_s": round(_pct(xs, .9), 1),
            "total_min": round(sum(min(x, IDLE_CAP_S) for x in xs) / 60, 1),
            "away_gt_30m": sum(x > IDLE_CAP_S for x in xs)}


def union_min(cs: list[dict]) -> float:
    """Minutes covered by the union of the calls' wait intervals, per session, each wait capped at 30 min."""
    by_s = defaultdict(list)
    for c in cs:
        by_s[c.get("session")].append((c["epoch"], c["epoch"] + min(c["lat"], IDLE_CAP_S)))
    total = 0.0
    for iv in by_s.values():
        iv.sort()
        cur_s, cur_e = iv[0]
        for a, b in iv[1:]:
            if a > cur_e:
                total += cur_e - cur_s
                cur_s, cur_e = a, b
            else:
                cur_e = max(cur_e, b)
        total += cur_e - cur_s
    return round(total / 60, 1)


def _cstats(cs: list[dict]) -> dict:
    """_stats over calls, with union minutes next to the (over-counting) capped sum."""
    st = _stats([c["lat"] for c in cs])
    st["sum_capped_min"] = st.pop("total_min")
    st["union_min"] = union_min(cs) if cs else 0.0
    return st


def call_label(tool: str, inp: dict) -> str:
    """Shape used for grouping: Bash command key, otherwise the tool name (+ subagent type for Agent)."""
    if tool == "Bash":
        cmd = (inp or {}).get("command", "")
        if re.search(r"\bsleep\s+\d", cmd):
            return "Bash: sleep"
        return "Bash: " + bash_key(cmd)
    if tool in ("Agent", "Task"):
        return f"{tool}: {(inp or {}).get('subagent_type', '?')}"
    return tool


def is_async(tool: str, inp: dict) -> bool:
    """Tools whose tool_use -> tool_result gap is not a wait the session sat through."""
    if tool in ASYNC_TOOLS:
        return True
    return tool in ("Agent", "Task", "Bash") and bool((inp or {}).get("run_in_background"))


CONFIRM_RE = re.compile(
    r"\b(proceed|continue|go ahead|shall i|should i (?:proceed|continue|go|start|run|ship)|ready (?:to|for)|"
    r"want me to (?:proceed|continue|go|start|run|ship)|ok to|okay to|good to go|looks good)\b", re.I)
CONFIRM_LABELS = {"yes", "no", "ok", "okay", "continue", "proceed", "go ahead", "go", "confirm", "approve", "skip",
                  "yes, proceed", "yes, continue", "yes, go ahead", "not now", "cancel", "stop"}


def question_extra(inp: dict) -> dict:
    """Shape of an AskUserQuestion call, never its text: counts, option counts, and whether it reads as a
    confirm-and-continue prompt (question text matches CONFIRM_RE, or every option label is a bare yes/no/go)."""
    qs = (inp or {}).get("questions")
    if not isinstance(qs, list) or not qs:
        return {"q_n": 0}
    opts = [len(q.get("options") or []) for q in qs if isinstance(q, dict)]
    confirm = 0
    for q in qs:
        if not isinstance(q, dict):
            continue
        labels = [str(o.get("label", "")).strip().lower().rstrip(".!") for o in (q.get("options") or []) if isinstance(o, dict)]
        if CONFIRM_RE.search(str(q.get("question", ""))) or (labels and all(
                l in CONFIRM_LABELS or l.endswith("(recommended)") and l[:-13].strip() in CONFIRM_LABELS for l in labels)):
            confirm += 1
    return {"q_n": len(qs), "q_opts_max": max(opts) if opts else 0,
            "q_multi": any(isinstance(q, dict) and q.get("multiSelect") for q in qs),
            "q_confirm": confirm == len(qs)}


def web_extra(tool: str, inp: dict) -> dict:
    """WebFetch host (never the full URL), WebSearch query length (never the query), AskUserQuestion shape."""
    inp = inp or {}
    if tool == "AskUserQuestion":
        return question_extra(inp)
    if tool == "WebFetch":
        try:
            return {"host": urlparse(str(inp.get("url", ""))).hostname or ""}
        except ValueError:
            return {"host": ""}
    if tool == "WebSearch":
        return {"query_len": len(str(inp.get("query", "")))}
    return {}


def why_prompted(tc, cfg) -> dict:
    """Which rules did (not) match this call, under the settings its session ran with."""
    if cfg is None:
        return {}
    if tc.name != "Bash":
        ask = [r for r in cfg.ask if _rule_matches(r, tc)]
        allow = [r for r in cfg.allow if _rule_matches(r, tc)]
        return {"est_prompt": would_prompt(tc, cfg), "ask_hit": ask[:3], "allow_hit": allow[:3], "cfg_mode": cfg.default_mode}
    segs = bash_segments((tc.input or {}).get("command", ""))
    bash_allow, bash_ask = _bash_rules(cfg.allow), _bash_rules(cfg.ask)
    unmatched = [s for s in segs if not (any(_rule_matches_seg(a, s) for a in bash_allow) or _seg_safe(s))]
    ask_hit = sorted({a for a in bash_ask for s in segs if _rule_matches_seg(a, s)})
    return {"est_prompt": would_prompt(tc, cfg), "n_segs": len(segs),
            "unmatched_segs": [redact(s, 120) for s in unmatched[:4]], "ask_hit": ask_hit[:3], "cfg_mode": cfg.default_mode}


def classify_dialogs(calls: list[dict]) -> None:
    """Set c['group'] on every prompted call: question | async | burst | lt2s | mid | human."""
    dialogs = [c for c in calls if c["prompted"] and c["tool"] != "AskUserQuestion" and not c["async"]]
    per_min: dict = defaultdict(int)
    for c in dialogs:
        per_min[(c["session"], int(c["epoch"] // 60))] += 1
    for c in calls:
        if not c["prompted"]:
            c["group"] = None
        elif c["tool"] == "AskUserQuestion":
            c["group"] = "question"
        elif c["async"]:
            c["group"] = "async"
        elif per_min[(c["session"], int(c["epoch"] // 60))] >= BURST_PER_MIN:
            c["group"] = "burst"
        elif c["lat"] < AUTO_S:
            c["group"] = "lt2s"
        elif c["lat"] < HUMAN_S:
            c["group"] = "mid"
        else:
            c["group"] = "human"


def _dialog_summary(cs: list[dict], n_calls: int | None = None) -> dict:
    """cs: prompted calls (any group). Dialogs = everything but questions and async tools.
    n_calls (all calls in the same slice) adds a rate; unprompted calls carry no mode, so per-mode has none."""
    d = [c for c in cs if c["group"] not in ("question", "async")]
    h = [c for c in d if c["group"] == "human"]
    out = {"dialogs": len(d), "lt_2s": sum(c["group"] == "lt2s" for c in d), "s2_5": sum(c["group"] == "mid" for c in d),
           "burst": sum(c["group"] == "burst" for c in d), "human_n": len(h),
           "human_p50_s": round(_pct([c["lat"] for c in h], .5), 1), "human_union_min": union_min(h) if h else 0.0}
    if n_calls is not None:
        out = {"calls": n_calls, "dialogs_per_1000_calls": round(1000 * len(d) / n_calls, 1) if n_calls else 0.0, **out}
    return out


def analyse(sessions, log_path: str = LOG_DEFAULT, since: datetime | None = None, top: int = 25,
            perm_for_session=None) -> dict:
    calls = []
    for s in sessions:
        tail = _tail(s.project)
        cfg = perm_for_session(s) if perm_for_session else None
        main = {id(c) for t in s.turns for c in t.tool_calls}
        turn_cmd = {id(c): t.command for t in s.turns for c in t.tool_calls}  # slash command that started the turn
        # subagent calls belong to no turn: attribute them to the session's latest turn at that moment
        starts = sorted((t.ts.timestamp(), t.command) for t in s.turns if t.ts and t.kind != "continuation")
        start_times = [x[0] for x in starts]
        # the workflow in force: latest real slash command earlier in the session (GSD runs carry on through
        # "ok keep going" nudges and task-notification wakes, which are not commands themselves)
        flow = sorted((t.ts.timestamp(), t.command) for t in s.turns if t.ts and t.command and t.command not in NOT_WORKFLOW)
        flow_times = [x[0] for x in flow]

        def cmd_at(epoch):
            i = bisect.bisect_right(start_times, epoch) - 1
            return starts[i][1] if i >= 0 else None

        def flow_at(epoch):
            i = bisect.bisect_right(flow_times, epoch) - 1
            return flow[i][1] if i >= 0 else None
        for c in s.tool_calls:
            if not (c.ts and c.result_ts):
                continue
            lat = (c.result_ts - c.ts).total_seconds()
            if lat < 0:
                continue
            calls.append({"id": c.id, "hash": call_hash(c.name, c.input), "tool": c.name, "label": call_label(c.name, c.input), "lat": lat,
                          "epoch": c.ts.timestamp(), "tail": tail, "session": s.id, "main": id(c) in main,
                          "denial": c.denial, "err": c.result_error, "prompted": False, "mode": None, "match": None,
                          "async": is_async(c.name, c.input),
                          "turn_cmd": turn_cmd[id(c)] if id(c) in turn_cmd else cmd_at(c.ts.timestamp()),
                          "workflow": flow_at(c.ts.timestamp()),
                          "agent_type": (s.agent_types.get(c.agent_id) or "(unlinked)") if c.agent_id else "(main)",
                          "_tc": c, "_cfg": cfg,
                          "head": redact((c.input or {}).get("command") or (c.input or {}).get("file_path") or "", 300),
                          "extra": web_extra(c.name, c.input)})
    tails = {c["tail"] for c in calls}
    sids = {c["session"] for c in calls}
    log = [r for r in load_prompt_log(log_path, since)
           if r["project"] in tails or (r["session"] and r["session"] in sids)]  # the log covers both accounts
    mstats = match_prompts(calls, log)
    classify_dialogs(calls)

    out: dict = {"meta": {"calls_with_latency": len(calls), "logged_prompts": len(log),
                          "log_prompts_matched": sum(c["prompted"] for c in calls), "matched_by": mstats,
                          "join_window_s": JOIN_WINDOW_S, "slow_threshold_s": SLOW_S,
                          "thresholds": {"auto_lt_s": AUTO_S, "human_ge_s": HUMAN_S, "burst_dialogs_per_min": BURST_PER_MIN,
                                         "wait_cap_s": IDLE_CAP_S, "mode_split": MODE_SPLIT.isoformat()},
                          "note": "human minutes are the union of wait intervals per session; label/project figures are unions within the group"}}

    # --- A. permission dialogs: how long the human took, and which shapes cost the most ---
    pr = [c for c in calls if c["prompted"]]
    dlg = [c for c in pr if c["group"] not in ("question", "async")]
    human = [c for c in dlg if c["group"] == "human"]
    out["prompt_wait"] = {
        "dialogs": len(dlg),
        "groups": {g: sum(c["group"] == g for c in pr) for g in ("lt2s", "mid", "human", "burst", "question", "async")},
        "human": _cstats(human),
        "human_main_thread": _cstats([c for c in human if c["main"]]),
        "human_subagent": _cstats([c for c in human if not c["main"]]),
        "burst": _cstats([c for c in dlg if c["group"] == "burst"]),
        "lt2s": _cstats([c for c in dlg if c["group"] == "lt2s"]),
        "human_gt_60s": sum(c["lat"] > 60 for c in human), "human_gt_600s": sum(c["lat"] > 600 for c in human),
    }
    out["by_mode"] = {m: _dialog_summary([c for c in pr if c["mode"] == m]) for m in sorted({c["mode"] for c in pr})}
    # Unprompted calls carry no mode, so only the time split has a call count to rate against.
    split = MODE_SPLIT.timestamp()
    out["before_after_mode_split"] = {"split_at": MODE_SPLIT.isoformat()}
    for name, cs in (("before", [c for c in calls if c["epoch"] < split]), ("after", [c for c in calls if c["epoch"] >= split])):
        p = [c for c in cs if c["prompted"]]
        out["before_after_mode_split"][name] = {
            **_dialog_summary(p, len(cs)),
            "by_mode": {m: _dialog_summary([c for c in p if c["mode"] == m]) for m in sorted({c["mode"] for c in p})}}

    by = defaultdict(list)
    for c in human:
        by[c["label"]].append(c)
    rows = []
    for k, v in by.items():
        st = _cstats(v)
        st.update(label=k, subagent_share=round(sum(not c["main"] for c in v) / len(v), 2),
                  denied=sum(1 for c in v if c["denial"]), example=v[0]["head"])
        rows.append(st)
    out["prompt_wait_by_label"] = sorted(rows, key=lambda r: -r["union_min"])[:top]
    pj = defaultdict(list)
    for c in human:
        pj[c["tail"]].append(c)
    out["prompt_wait_by_project"] = sorted(({"project": k, **_cstats(v)} for k, v in pj.items()),
                                           key=lambda r: -r["union_min"])[:15]
    day = defaultdict(list)
    for c in human:
        day[datetime.fromtimestamp(c["epoch"], timezone.utc).strftime("%Y-%m-%d")].append(c)
    out["prompt_wait_by_day"] = sorted(({"day": k, **_cstats(v)} for k, v in day.items()),
                                       key=lambda r: -r["union_min"])[:10]

    # --- B. pauses that are NOT permission dialogs ---
    q = [c for c in calls if c["tool"] == "AskUserQuestion"]  # prompted or not: it is a question either way
    out["question_wait"] = _cstats(q)
    out["question_wait"]["by_project"] = [{"project": k, **_cstats([c for c in q if c["tail"] == k])}
                                          for k in sorted({c["tail"] for c in q},
                                                          key=lambda k: -sum(min(c["lat"], IDLE_CAP_S) for c in q if c["tail"] == k))[:8]]
    out["async_calls"] = {lab: _cstats(v) for lab, v in sorted(
        ((lab, [c for c in calls if c["async"] and c["label"] == lab]) for lab in {c["label"] for c in calls if c["async"]}),
        key=lambda kv: -len(kv[1]))}
    np_calls = [c for c in calls if not c["prompted"] and not c["async"] and c["tool"] != "AskUserQuestion"]
    sl = defaultdict(list)
    for c in np_calls:
        if c["lat"] >= SLOW_S:
            sl[c["label"]].append(c)
    out["slow_tools_not_prompts"] = sorted(
        ({"label": k, **_cstats(v), "subagent_share": round(sum(not c["main"] for c in v) / len(v), 2),
          "example": v[0]["head"]} for k, v in sl.items()), key=lambda r: -r["union_min"])[:top]
    fast_err = [c for c in np_calls if c["err"] and c["lat"] < 5]
    out["tool_latency_by_tool"] = sorted(
        ({"tool": k, **_cstats([c for c in np_calls if c["tool"] == k])} for k in {c["tool"] for c in np_calls}),
        key=lambda r: -r["union_min"])[:15]
    out["fast_error_calls"] = len(fast_err)

    # --- C. gaps waiting for the human to type ---
    gaps, ask_follow = [], 0
    for s in sessions:
        turns = sorted(s.turns, key=lambda t: t.ts.timestamp() if t.ts else 0)
        for a, b in zip(turns, turns[1:]):
            end = a.end_ts or a.ts
            if not (end and b.ts) or b.kind == "continuation":
                continue
            g = (b.ts - end).total_seconds()
            if g < 0:
                continue
            last = a.tool_calls[-1].name if a.tool_calls else None
            gaps.append({"g": g, "last_tool": last, "interrupted": a.interrupted, "kind": b.kind,
                         "tail": _tail(s.project), "prompt_len": len(b.prompt),
                         "short": len(b.prompt.strip()) <= 25})
    buckets = [("<30s", 0, 30), ("30s-2m", 30, 120), ("2-10m", 120, 600), ("10-30m", 600, 1800), (">30m (away)", 1800, 1e12)]
    out["user_gap_buckets"] = [{"bucket": n, "n": sum(lo <= x["g"] < hi for x in gaps),
                                "total_min": round(sum(x["g"] for x in gaps if lo <= x["g"] < hi) / 60, 1)}
                               for n, lo, hi in buckets]
    live = [x for x in gaps if x["g"] < IDLE_CAP_S]
    out["user_gap_live"] = {**_stats([x["g"] for x in live]),
                            "after_interrupt": sum(x["interrupted"] for x in live),
                            "next_prompt_short_le25ch": sum(x["short"] for x in live),
                            "next_prompt_is_command": sum(x["kind"] == "command" for x in live)}
    by_last = defaultdict(list)
    for x in live:
        by_last[x["last_tool"] or "(no tools)"].append(x["g"])
    out["user_gap_by_last_tool"] = sorted(({"last_tool": k, **_stats(v)} for k, v in by_last.items()),
                                          key=lambda r: -r["total_min"])[:10]
    # --- D. per-call detail (dialogs and slow calls) ---
    out["_calls"] = [{k: (round(c[k], 1) if k == "lat" else c[k]) for k in
                      ("session", "tool", "label", "lat", "tail", "main", "mode", "denial", "head", "epoch", "match",
                       "group", "async", "turn_cmd", "workflow", "agent_type")}
                     | c["extra"] | {"prompted": c["prompted"],
                                     "why": why_prompted(c["_tc"], c["_cfg"]) if c["prompted"] else {}}
                     for c in sorted(calls, key=lambda c: c["epoch"])
                     if c["prompted"] or c["lat"] >= SLOW_S or c["tool"] == "AskUserQuestion"]
    out["background_style"] = {lab: _cstats([c for c in calls if c["label"] == lab])
                               for lab in ("Bash: sleep", "Monitor", "ScheduleWakeup", "TaskOutput", "Agent: general-purpose")
                               if any(c["label"] == lab for c in calls)}
    return out
