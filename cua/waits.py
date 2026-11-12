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
import math
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlparse

from .permissions import (_bash_rules, _rule_matches, _rule_matches_seg, _seg_safe, bash_key, bash_segments,
                          rule_key, tool_risk, would_prompt)
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


# Column order hooks/prompt-logger.js writes (pinned by tests/test_prompt_logger.py:
# TestHookContractDocs and test_column_contract_sentinels).
PROMPT_LOG_COLUMNS = ("time", "kind", "mode", "project", "shape", "tool_use_id", "session_id", "call_hash")
_COL = {name: i for i, name in enumerate(PROMPT_LOG_COLUMNS)}


def new_prompt_log_stats() -> dict:
    """Zeroed counters for load_prompt_log's `stats` out-parameter; analyse() reports them as meta.prompt_log."""
    return {"file_found": False, "rows_read": 0, "skipped_malformed": 0, "skipped_other_kind": 0,
            "skipped_before_since": 0, "excluded_other_account": 0, "kept": 0}


def load_prompt_log(path: str, since: datetime | None = None, stats: dict | None = None) -> list[dict]:
    """One dict per real permission prompt the hook saw, sorted by time. Rows have 5 columns (oldest: no id,
    no session), 7 (since 2026-11-04 ~01:30Z: + tool_use_id, session_id) or 8 (+ call_hash); the order is
    PROMPT_LOG_COLUMNS and a column past the row's width reads as "". The project is normalised with
    norm_project so it compares equal to a session's `_tail`.
    `stats`, when given (see new_prompt_log_stats), receives file_found, rows_read and a count per skip reason;
    blank lines are not rows. Rows are classified in a fixed order: too few columns, non-prompt kind, bad
    time, before `since`. The account filter lives in analyse(), which adds excluded_other_account and kept."""
    out = []
    exists = os.path.exists(path)
    if stats is not None:
        stats["file_found"] = exists
    if not exists:
        return out

    def count(key: str) -> None:
        if stats is not None:
            stats[key] = stats.get(key, 0) + 1

    def col(p: list, name: str) -> str:
        i = _COL[name]
        return p[i] if len(p) > i else ""

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            count("rows_read")
            p = line.split("\t")
            if len(p) < 5:
                count("skipped_malformed")
                continue
            if col(p, "kind") not in ("prompt", "prompt:reissued"):
                count("skipped_other_kind")
                continue
            try:
                t = datetime.fromisoformat(col(p, "time").replace("Z", "+00:00"))
            except ValueError:
                count("skipped_malformed")
                continue
            if since and t < since:
                count("skipped_before_since")
                continue
            out.append({"epoch": t.timestamp(), "mode": col(p, "mode"), "project": norm_project(col(p, "project")),
                        "tool": col(p, "shape").split(":", 1)[0], "id": col(p, "tool_use_id").strip(),
                        "session": col(p, "session_id").strip(), "hash": col(p, "call_hash").strip()})
    out.sort(key=lambda r: r["epoch"])
    return out


def prompt_log_note(stats: dict) -> str:
    """One line naming the prompt-log rows that did not reach the join, for stderr. Empty when none were excluded."""
    if not stats.get("file_found"):
        return "no prompt log file read"
    parts = [(stats.get("excluded_other_account", 0), "other account"),
             (stats.get("skipped_malformed", 0), "malformed"),
             (stats.get("skipped_other_kind", 0), "non-prompt kind"),
             (stats.get("skipped_before_since", 0), "before --since")]
    total = sum(n for n, _ in parts)
    if not total:
        return ""
    return f"excluded {total} of {stats.get('rows_read', 0)} rows: " + ", ".join(f"{n} {label}" for n, label in parts if n)


def _tail(path: str | None) -> str:
    parts = [x for x in (path or "").rstrip("/").split("/") if x]
    return "/".join(parts[-2:])


def norm_project(p: str) -> str:
    """Project as `_tail` shapes it. The hook writes `~/code/work/proj`; a transcript records
    `/home/u/code/work/proj`. expanduser resolves the ~ so a project one level under HOME
    ("~/foo" vs "/home/u/foo") still agrees."""
    return _tail(os.path.expanduser((p or "").strip()))


def _mark(c: dict, r: dict, how: str) -> None:
    c["prompted"] = True
    c["mode"] = r["mode"]
    c["match"] = how


_JS_MAX_SAFE = 2 ** 53  # beyond this JS has already rounded an integer to a double


def _js_number(x) -> str:
    """A number as JavaScript JSON.stringify writes it (ECMAScript Number::toString)."""
    if isinstance(x, int):
        if abs(x) <= _JS_MAX_SAFE:
            return str(x)
        try:
            x = float(x)  # JS parsed it as a double, so 12345678901234567890 is already 12345678901234567000
        except OverflowError:
            # Beyond the double range JS parses Infinity, and JSON.stringify writes null. Python's json
            # keeps the exact int, which float() cannot hold.
            return "null"
    if math.isnan(x) or math.isinf(x):
        return "null"
    if x == 0:
        return "0"  # also -0.0
    sign = "-" if x < 0 else ""
    t = Decimal(repr(abs(x))).as_tuple()  # shortest round-trip digits, as JS uses
    digits = "".join(str(d) for d in t.digits).rstrip("0") or "0"
    n = len(t.digits) + t.exponent  # value = 0.DIGITS x 10^n
    k = len(digits)
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        body = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return sign + body


def _canon(v) -> str:
    """Canonical JSON the way the hook builds it: JS number formatting, object keys in UTF-16 code-unit
    order (JS .sort()), non-ASCII text written raw."""
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return _js_number(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ",".join(_canon(i) for i in v) + "]"
    if isinstance(v, dict):
        keys = sorted(v, key=lambda k: k.encode("utf-16-be", "surrogatepass"))
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _canon(v[k]) for k in keys) + "}"
    raise TypeError(f"cannot canonicalise {type(v).__name__}")


def call_hash(tool: str, inp: dict) -> str:
    """Fingerprint the permission-prompt hook logs in column 8 (the PermissionRequest event has no
    tool_use_id): 12 hex of sha256 over canonical JSON of {"n": tool, "i": input}. Numbers and key order
    follow JavaScript JSON.stringify, because the hook computes the hash in JS and JSON.parse discards
    the number's text (1.0 is just 1)."""
    # Known residual: JS escapes lone UTF-16 surrogates and this does not; no real payload carries them.
    return hashlib.sha256(_canon({"n": tool, "i": inp or {}}).encode("utf-8", "replace")).hexdigest()[:12]


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


def _prompted(calls: list[dict]) -> list[dict]:
    """Calls that raised a logged permission prompt, in calls order."""
    return [c for c in calls if c["prompted"]]


def _dialogs(calls: list[dict]) -> list[dict]:
    """Prompted calls that are permission dialogs: everything but questions and async tools."""
    return [c for c in _prompted(calls) if c["group"] not in ("question", "async")]


def _human(calls: list[dict]) -> list[dict]:
    """Dialogs a human answered (group "human"), in calls order."""
    return [c for c in _dialogs(calls) if c["group"] == "human"]


def rollup_dialog_groups(calls: list[dict]) -> dict:
    """The `prompt_wait` section: dialog counts per group and the wait stats of each human-relevant slice."""
    pr, dlg, human = _prompted(calls), _dialogs(calls), _human(calls)
    return {
        "dialogs": len(dlg),
        "groups": {g: sum(c["group"] == g for c in pr) for g in ("lt2s", "mid", "human", "burst", "question", "async")},
        "human": _cstats(human),
        "human_main_thread": _cstats([c for c in human if c["main"]]),
        "human_subagent": _cstats([c for c in human if not c["main"]]),
        "burst": _cstats([c for c in dlg if c["group"] == "burst"]),
        "lt2s": _cstats([c for c in dlg if c["group"] == "lt2s"]),
        "human_gt_60s": sum(c["lat"] > 60 for c in human), "human_gt_600s": sum(c["lat"] > 600 for c in human),
    }


def rollup_by_label(calls: list[dict], top: int) -> list[dict]:
    """The `prompt_wait_by_label` section: human waits per label, top `top` by descending union_min (ties keep
    calls order). Labels compare as Python str, with no Unicode normalisation."""
    by = defaultdict(list)
    for c in _human(calls):
        by[c["label"]].append(c)
    rows = []
    for k, v in by.items():
        st = _cstats(v)
        st.update(label=k, subagent_share=round(sum(not c["main"] for c in v) / len(v), 2),
                  denied=sum(1 for c in v if c["denial"]), example=v[0]["head"])
        rows.append(st)
    return sorted(rows, key=lambda r: -r["union_min"])[:top]


def rollup_by_project(calls: list[dict]) -> list[dict]:
    """The `prompt_wait_by_project` section: human waits per project tail, top 15 by descending union_min."""
    pj = defaultdict(list)
    for c in _human(calls):
        pj[c["tail"]].append(c)
    return sorted(({"project": k, **_cstats(v)} for k, v in pj.items()), key=lambda r: -r["union_min"])[:15]


def rollup_by_day(calls: list[dict]) -> list[dict]:
    """The `prompt_wait_by_day` section: human waits per UTC day (%Y-%m-%d), top 10 by descending union_min."""
    day = defaultdict(list)
    for c in _human(calls):
        day[datetime.fromtimestamp(c["epoch"], timezone.utc).strftime("%Y-%m-%d")].append(c)
    return sorted(({"day": k, **_cstats(v)} for k, v in day.items()), key=lambda r: -r["union_min"])[:10]


def rollup_approvals(calls: list[dict], top: int) -> dict:
    """The `approved_prompts` section: the prompts you actually said yes to, grouped by shape. Ground truth from
    the prompt-log hook (prompted) minus the ones the transcript marks user_rejected -- the real allow-list
    triage input that the rule-based estimate (cua scan's estimated_prompts_by_rule) cannot produce.
    Rows sort by (-approved, label)."""
    dlg = _dialogs(calls)
    appr = [c for c in dlg if not c["denial"]]
    rej = [c for c in dlg if c["denial"] == "user_rejected"]
    ab = defaultdict(list)
    for c in appr:
        ab[c["label"]].append(c)
    arows = []
    for k, v in ab.items():
        rk = rule_key(v[0]["_tc"])
        arows.append({"label": k, "rule": rk, "risk": tool_risk(rk), "approved": len(v),
                      "sessions": len({c["session"] for c in v}),
                      "subagent_share": round(sum(not c["main"] for c in v) / len(v), 2),
                      "example": v[0]["head"]})
    return {
        "total_approved": len(appr), "total_rejected": len(rej),
        "note": "dialogs you approved (a logged prompt that was not user_rejected), by shape; measured "
                "from the --prompt-log hook, not inferred. Empty when no prompt log is supplied.",
        "by_label": sorted(arows, key=lambda r: (-r["approved"], r["label"]))[:top],
    }


def rollup_modes(calls: list[dict]) -> dict:
    """The `by_mode` and `before_after_mode_split` sections, in that key order."""
    pr = _prompted(calls)
    out: dict = {"by_mode": {m: _dialog_summary([c for c in pr if c["mode"] == m]) for m in sorted({c["mode"] for c in pr})}}
    # Unprompted calls carry no mode, so only the time split has a call count to rate against.
    split = MODE_SPLIT.timestamp()
    out["before_after_mode_split"] = {"split_at": MODE_SPLIT.isoformat()}
    for name, cs in (("before", [c for c in calls if c["epoch"] < split]), ("after", [c for c in calls if c["epoch"] >= split])):
        p = [c for c in cs if c["prompted"]]
        out["before_after_mode_split"][name] = {
            **_dialog_summary(p, len(cs)),
            "by_mode": {m: _dialog_summary([c for c in p if c["mode"] == m]) for m in sorted({c["mode"] for c in p})}}
    return out


def rollup_pauses(calls: list[dict], top: int) -> dict:
    """The pauses that are NOT permission dialogs: `question_wait`, `async_calls`, `slow_tools_not_prompts`,
    `tool_latency_by_tool` and `fast_error_calls`, in that key order."""
    out: dict = {}
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
    return out


def rollup_gaps(sessions) -> dict:
    """The `user_gap_buckets`, `user_gap_live` and `user_gap_by_last_tool` sections: time the session waited for
    the human to type. Takes sessions, not calls, because gaps come from turns."""
    gaps = []
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
    out: dict = {}
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
    return out


def calls_detail(calls: list[dict]) -> list[dict]:
    """The `_calls` section: per-call detail for dialogs, slow calls and questions, sorted by epoch (a sorted
    copy; `calls` itself keeps its order)."""
    return [{k: (round(c[k], 1) if k == "lat" else c[k]) for k in
             ("session", "tool", "label", "lat", "tail", "main", "mode", "denial", "head", "epoch", "match",
              "group", "async", "turn_cmd", "workflow", "agent_type")}
            | c["extra"] | {"prompted": c["prompted"],
                            "why": why_prompted(c["_tc"], c["_cfg"]) if c["prompted"] else {}}
            for c in sorted(calls, key=lambda c: c["epoch"])
            if c["prompted"] or c["lat"] >= SLOW_S or c["tool"] == "AskUserQuestion"]


def rollup_background_style(calls: list[dict]) -> dict:
    """The `background_style` section: wait stats for the polling and background-work labels that occur."""
    return {lab: _cstats([c for c in calls if c["label"] == lab])
            for lab in ("Bash: sleep", "Monitor", "ScheduleWakeup", "TaskOutput", "Agent: general-purpose")
            if any(c["label"] == lab for c in calls)}


def _collect_calls(sessions, perm_for_session=None) -> list[dict]:
    """One dict per tool call that has a latency (tool_use and tool_result both timestamped): each session's
    main-thread calls, then its subagent calls. Never re-sort the result; tie order and `example` depend on it."""
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
    return calls


def _load_and_join(calls: list[dict], sessions, log_path: str, since: datetime | None) -> tuple[list, dict, dict]:
    """Read the prompt log, keep the rows of the selected sessions, join them to `calls` (sets prompted, mode and
    match) and classify the dialogs (sets group). Returns (kept log rows, match counts, prompt_log stats)."""
    # the log covers both accounts: keep rows of the selected sessions. The sets come from the sessions, not from
    # calls with latency, so a selected session whose calls all lack result_ts still claims its rows.
    tails = {_tail(s.project) for s in sessions}
    sids = {s.id for s in sessions}
    pl = new_prompt_log_stats()
    log = []
    for r in load_prompt_log(log_path, since, pl):
        if r["project"] in tails or (r["session"] and r["session"] in sids):
            log.append(r)
        else:
            pl["excluded_other_account"] += 1
    pl["kept"] = len(log)
    mstats = match_prompts(calls, log)
    classify_dialogs(calls)
    return log, mstats, pl


def analyse(sessions, log_path: str = LOG_DEFAULT, since: datetime | None = None, top: int = 25,
            perm_for_session=None) -> dict:
    """Build waits.json: collect the calls, join them to the prompt log, then one rollup per section. The top-level
    key order is part of the output (waits.json is written without sort_keys)."""
    calls = _collect_calls(sessions, perm_for_session)
    log, mstats, pl = _load_and_join(calls, sessions, log_path, since)

    out: dict = {"meta": {"calls_with_latency": len(calls), "logged_prompts": len(log), "prompt_log": pl,
                          "log_prompts_matched": sum(c["prompted"] for c in calls), "matched_by": mstats,
                          "join_window_s": JOIN_WINDOW_S, "slow_threshold_s": SLOW_S,
                          "thresholds": {"auto_lt_s": AUTO_S, "human_ge_s": HUMAN_S, "burst_dialogs_per_min": BURST_PER_MIN,
                                         "wait_cap_s": IDLE_CAP_S, "mode_split": MODE_SPLIT.isoformat()},
                          "note": "human minutes are the union of wait intervals per session; label/project figures are unions within the group"}}
    # A. permission dialogs: how long the human took, and which shapes cost the most
    out["prompt_wait"] = rollup_dialog_groups(calls)
    out.update(rollup_modes(calls))
    out["prompt_wait_by_label"] = rollup_by_label(calls, top)
    out["prompt_wait_by_project"] = rollup_by_project(calls)
    out["prompt_wait_by_day"] = rollup_by_day(calls)
    out["approved_prompts"] = rollup_approvals(calls, top)
    # B. pauses that are NOT permission dialogs
    out.update(rollup_pauses(calls, top))
    # C. gaps waiting for the human to type
    out.update(rollup_gaps(sessions))
    # D. per-call detail (dialogs and slow calls), then the polling style
    out["_calls"] = calls_detail(calls)
    out["background_style"] = rollup_background_style(calls)
    return out
