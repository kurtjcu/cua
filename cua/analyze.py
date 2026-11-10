"""Turn normalised sessions into one redacted summary.json per account.

The summary is the contract between the deterministic layer and the agent personas: agents read
summary.json (+ samples.jsonl) and never need the raw transcripts.
"""
from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone

from . import classify, permissions
from .model import PermissionConfig, Session, Usage
from .redact import project_label, redact

SCHEMA_VERSION = 1


def _pct(values, q):
    if not values:
        return 0
    values = sorted(values)
    k = max(0, min(len(values) - 1, int(round(q * (len(values) - 1)))))
    return values[k]


def _u(u: Usage) -> dict:
    return {"input": u.input, "output": u.output, "cache_write": u.cache_write, "cache_read": u.cache_read, "total": u.total}


_SIG_SUBS = [
    (re.compile(r"</?[\w-]+>"), ""),                      # <tool_use_error> wrappers
    (re.compile(r"`[^`]*`|'[^'\n]*'|\"[^\"\n]*\""), "<q>"),  # quoted commands, names, paths
    (re.compile(r"(?:~|\.{0,2})/[^\s,;:)]+"), "<path>"),
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "<sha>"),
    (re.compile(r"\d+"), "N"),
    (re.compile(r"\s+"), " "),
]


def block_signature(text: str) -> str:
    """Stable label for a denial message: first non-empty line with the variable parts masked,
    so the same hook's message groups together across calls."""
    text = _SIG_SUBS[0][0].sub("", text or "")
    line = next((l for l in text.splitlines() if l.strip()), "")
    for rx, rep in _SIG_SUBS[1:]:
        line = rx.sub(rep, line)
    return line.strip()[:110] or "(empty)"


def block_outcome(blocked, nxt) -> str:
    """What the agent did with the very next tool call in the same thread."""
    if nxt is None:
        return "nothing_after"
    if nxt.name == blocked.name and nxt.input == blocked.input:
        return "reissued"
    if nxt.denial:
        return "blocked_again"
    if nxt.result_error:
        return "errored"
    return "recovered"


def _month(dt):
    return dt.strftime("%Y-%m") if dt else "unknown"


def _cost(u: Usage, model: str, pricing: dict | None) -> float:
    if not pricing:
        return 0.0
    p = None
    for k, v in pricing.items():
        if k in (model or ""):
            p = v
            break
    p = p or pricing.get("default")
    if not p:
        return 0.0
    return (u.input * p.get("input", 0) + u.output * p.get("output", 0) + u.cache_write * p.get("cache_write", 0)
            + u.cache_read * p.get("cache_read", 0)) / 1_000_000


def subagent_models(sessions: list[Session]) -> dict:
    """Which models the subagents ran on, overall and per subagent_type: {model: {msgs, tokens}}.
    Subagents whose type could not be linked to an Agent/Task call are filed under "(unlinked)"."""
    overall: dict = defaultdict(lambda: {"msgs": 0, "tokens": 0})
    by_type: dict = defaultdict(lambda: defaultdict(lambda: {"msgs": 0, "tokens": 0}))
    for s in sessions:
        for aid, ms in s.sidechain_by_agent.items():
            typ = s.agent_types.get(aid, "(unlinked)")
            for m, (n, tok) in ms.items():
                for d in (overall[m], by_type[typ][m]):
                    d["msgs"] += n
                    d["tokens"] += tok
    return {"subagent_models": dict(sorted(overall.items(), key=lambda kv: -kv[1]["tokens"])),
            "subagent_models_by_type": {t: dict(sorted(v.items(), key=lambda kv: -kv[1]["tokens"]))
                                        for t, v in sorted(by_type.items(), key=lambda kv: -sum(x["tokens"] for x in kv[1].values()))}}


def _skill_name(raw) -> str:
    return str(raw or "").strip().lstrip("/").split()[0] if str(raw or "").strip() else ""


def skill_usage(sessions: list[Session], history: list[dict] | None, project_mode: str) -> dict:
    """How each skill/command was used: typed as a slash command (transcripts), loaded by the
    model via the Skill tool (main thread or subagent), and typed according to history.jsonl,
    which outlives the transcripts. Names only, never arguments."""
    rows: dict[str, dict] = defaultdict(lambda: {"typed": 0, "model": 0, "subagent": 0, "history_typed": 0,
                                                 "sessions": set(), "projects": Counter(), "last": None})

    def bump(name, field, proj, ts, sid=None):
        if not name:
            return
        r = rows[name]
        r[field] += 1
        if proj:
            r["projects"][proj] += 1
        if sid:
            r["sessions"].add(sid)
        if ts and (r["last"] is None or ts > r["last"]):
            r["last"] = ts

    for s in sessions:
        proj = project_label(s.project, project_mode)
        for t in s.turns:
            if t.command:
                bump(_skill_name(t.command), "typed", proj, t.ts, s.id)
            for c in t.tool_calls:
                if c.name == "Skill":
                    bump(_skill_name(c.input.get("skill") or c.input.get("command")), "model", proj, c.ts, s.id)
        for c in s.sidechain_tool_calls:
            if c.name == "Skill":
                bump(_skill_name(c.input.get("skill") or c.input.get("command")), "subagent", proj, c.ts, s.id)
    first_hist = None
    for h in history or []:
        d = h.get("display")
        if not isinstance(d, str) or not d.startswith("/"):
            continue
        ts = h.get("timestamp")
        when = datetime.fromtimestamp(ts / 1000, tz=timezone.utc) if isinstance(ts, (int, float)) else None
        if when and (first_hist is None or when < first_hist):
            first_hist = when
        bump(_skill_name(d), "history_typed", project_label(h.get("project"), project_mode), when)
    out = {}
    for name, r in sorted(rows.items(), key=lambda kv: -(kv[1]["typed"] + kv[1]["model"] + kv[1]["subagent"] + kv[1]["history_typed"])):
        out[name] = {"typed": r["typed"], "model": r["model"], "subagent": r["subagent"],
                     "history_typed": r["history_typed"], "sessions": len(r["sessions"]),
                     "projects": dict(r["projects"].most_common(5)),
                     "last": r["last"].isoformat() if r["last"] else None}
    return {"by_name": out, "history_since": first_hist.isoformat() if first_hist else None,
            "note": "typed/model/subagent come from transcripts (retention window); history_typed from "
                    "history.jsonl. A typed slash command that loads a skill does not also log a Skill call."}


def analyze(sessions: list[Session], account: str, perm_cfg: PermissionConfig | None = None,
            project_mode: str = "basename", snippets: bool = True, pricing: dict | None = None,
            prompt_history: list[dict] | None = None,
            perm_for_session=None) -> tuple[dict, list[dict]]:
    """perm_cfg is the pooled rule set (for "which rules exist / never matched").
    perm_for_session(session) -> PermissionConfig gives the rules that session actually ran under;
    without it every session is judged against the pooled set, which over-applies project rules."""
    perm_cfg = perm_cfg or PermissionConfig()
    mode_counts = Counter()
    denial_events = Counter()
    block_outcomes = defaultdict(lambda: {"count": 0, "events": 0, "where": Counter(), "tools": Counter(), "outcomes": Counter(),
                                          "same_block_again": 0, "sessions": set(), "examples": [], "message": ""})
    snip = (lambda t, n=160: redact(t, n)) if snippets else (lambda t, n=160: "")
    samples: list[dict] = []  # redacted per-turn rows for agents to read

    tot = Usage()
    side = Usage()
    est_turns = 0
    turns_per_session, tokens_per_prompt, out_per_prompt, tools_per_turn = [], [], [], []
    n_turns = n_prompts = interrupts = corrections = continues = reexplain = compactions = api_errors = 0
    tool_calls = tool_errors = 0
    models = Counter()
    cost = 0.0
    by_month = defaultdict(lambda: {"sessions": 0, "prompts": 0, "tokens": 0, "output": 0})
    by_project = defaultdict(lambda: {"sessions": 0, "prompts": 0, "tokens": 0, "corrections": 0, "denials": 0})
    by_source = Counter()
    phase_prompts, phase_tokens, phase_corrections = Counter(), Counter(), Counter()
    phase_month = defaultdict(Counter)
    transitions = Counter()
    hour_hist = Counter()
    weekday_hist = Counter()
    slash = Counter()
    heavy = []
    prompts_for_clustering = []

    denial_kinds = Counter()
    denial_by_key = defaultdict(lambda: {"count": 0, "kinds": Counter(), "feedback": [], "examples": []})
    prompt_by_key = defaultdict(lambda: {"count": 0, "examples": [], "sessions": set()})
    error_by_key = defaultdict(lambda: {"count": 0, "examples": []})
    allow_hits = Counter()
    retry_loops = []

    for s in sessions:
        by_source[s.source] += 1
        # what happened after each denial, per thread (main conversation vs merged subagents)
        for where, stream in (("main", [c for t in s.turns for c in t.tool_calls]), ("subagent", s.sidechain_tool_calls)):
            for i, c in enumerate(stream):
                if not c.denial:
                    continue
                nxt = stream[i + 1] if i + 1 < len(stream) else None
                prev = stream[i - 1] if i > 0 else None
                # an "event" is a run of consecutive same-kind denials in one thread: rejecting one of
                # a burst of parallel calls rejects them all, and that is one decision, not N
                if prev is None or prev.denial != c.denial:
                    denial_events[c.denial] += 1
                o = block_outcomes[(c.denial, block_signature(c.result_text))]
                o["count"] += 1
                if prev is None or prev.denial != c.denial or block_signature(prev.result_text) != block_signature(c.result_text):
                    o["events"] += 1
                o["where"][where] += 1
                o["tools"][permissions.rule_key(c)] += 1
                o["outcomes"][block_outcome(c, nxt)] += 1
                if nxt is not None and nxt.denial and block_signature(nxt.result_text) == block_signature(c.result_text):
                    o["same_block_again"] += 1
                o["sessions"].add(s.id)
                if len(o["examples"]) < 3:
                    o["examples"].append(snip(json.dumps(c.input)[:300], 160))
                if not o["message"]:
                    o["message"] = snip(c.result_text, 300)
        s_cfg = perm_for_session(s) if perm_for_session else perm_cfg
        if s.source == "claude_code":
            if s.permission_mode:  # what the transcript says was in effect beats the settings files
                s_cfg = PermissionConfig(allow=s_cfg.allow, deny=s_cfg.deny, ask=s_cfg.ask,
                                         default_mode=s.permission_mode, sources=s_cfg.sources)
                mode_counts[f"{s.permission_mode} (recorded)"] += 1
            else:
                mode_counts[f"{s_cfg.default_mode or 'default'} (from settings)"] += 1
        su = s.usage
        tot.add(su)
        side.add(s.sidechain_usage)
        compactions += s.compactions
        api_errors += s.api_errors
        proj = project_label(s.project, project_mode)
        by_project[proj]["sessions"] += 1
        by_project[proj]["tokens"] += su.total
        by_month[_month(s.start)]["sessions"] += 1
        real_turns = [t for t in s.turns if t.kind != "continuation"]
        turns_per_session.append(len(real_turns))
        prev_phase = None
        first_prompt = next((t.prompt for t in real_turns if t.prompt), "")
        heavy.append({"session": s.id[:8], "source": s.source, "project": proj, "title": snip(s.title or first_prompt, 100),
                      "start": s.start.isoformat() if s.start else None, "prompts": len(real_turns),
                      "tokens": su.total, "output": su.output, "compactions": s.compactions,
                      "subagent_tokens": s.sidechain_usage.total})

        consecutive_fail = defaultdict(int)
        for t in s.turns:
            n_turns += 1
            for m, c in t.models.items():
                models[m] += c
            for m in t.models or {"unknown": 1}:
                cost += _cost(t.usage, m, pricing) / max(1, len(t.models or {"x": 1}))
            if t.usage.estimated:
                est_turns += 1
            if t.kind == "continuation":
                continue
            n_prompts += 1
            by_project[proj]["prompts"] += 1
            mo = _month(t.ts)
            by_month[mo]["prompts"] += 1
            by_month[mo]["tokens"] += t.usage.total
            by_month[mo]["output"] += t.usage.output
            if t.ts:
                hour_hist[t.ts.hour] += 1
                weekday_hist[t.ts.strftime("%a")] += 1
            tokens_per_prompt.append(t.usage.total)
            out_per_prompt.append(t.usage.output)
            tools_per_turn.append(len(t.tool_calls))
            if t.command:
                slash[t.command] += 1
            ph = classify.phase_of(t.prompt, [c.name for c in t.tool_calls])
            phase_prompts[ph] += 1
            phase_tokens[ph] += t.usage.total
            phase_month[mo][ph] += 1
            corr = classify.is_correction(t.prompt)
            cont = classify.is_bare_continue(t.prompt)
            if corr:
                corrections += 1
                by_project[proj]["corrections"] += 1
                if prev_phase:
                    phase_corrections[prev_phase] += 1
            if cont:
                continues += 1
            if classify.reexplains_context(t.prompt):
                reexplain += 1
            if t.interrupted:
                interrupts += 1
            if prev_phase and not corr and not cont:
                transitions[f"{prev_phase}->{ph}"] += 1
            if not corr and not cont:
                prev_phase = ph
                if t.prompt and len(t.prompt) > 8:
                    prompts_for_clustering.append((t.prompt, {"project": proj, "month": mo, "phase": ph, "tokens": t.usage.total, "source": s.source}))
            denials_here = [c.denial for c in t.tool_calls if c.denial]
            samples.append({
                "account": account, "source": s.source, "session": s.id[:8], "project": proj,
                "ts": t.ts.isoformat() if t.ts else None, "phase": ph, "prompt": snip(t.prompt, 240),
                "tokens": t.usage.total, "output": t.usage.output, "tools": Counter(c.name for c in t.tool_calls).most_common(6),
                "tool_errors": sum(c.result_error for c in t.tool_calls), "denials": denials_here,
                "interrupted": t.interrupted, "correction": corr, "continue": cont,
            })

        for c in s.tool_calls:
            tool_calls += 1
            key = permissions.rule_key(c)
            if c.result_error:
                tool_errors += 1
                consecutive_fail[key] += 1
                if consecutive_fail[key] == 3:
                    retry_loops.append({"session": s.id[:8], "project": proj, "tool": key, "example": snip(json.dumps(c.input)[:300], 160)})
                if not c.denial:
                    e = error_by_key[key]
                    e["count"] += 1
                    if len(e["examples"]) < 3:
                        e["examples"].append(snip(c.result_text, 160))
            else:
                consecutive_fail[key] = 0
            if c.denial:
                denial_kinds[c.denial] += 1
                by_project[proj]["denials"] += 1
                d = denial_by_key[key]
                d["count"] += 1
                d["kinds"][c.denial] += 1
                if c.user_feedback and len(d["feedback"]) < 5 and snip(c.user_feedback, 200) not in d["feedback"]:
                    d["feedback"].append(snip(c.user_feedback, 200))
                if len(d["examples"]) < 3:
                    d["examples"].append(snip(json.dumps(c.input)[:300], 160))
            if s.source == "claude_code":
                for r in s_cfg.allow:
                    if permissions._rule_matches(r, c):
                        allow_hits[r] += 1
                if permissions.would_prompt(c, s_cfg):
                    p = prompt_by_key[key]
                    p["count"] += 1
                    p["sessions"].add(s.id)
                    if len(p["examples"]) < 3:
                        p["examples"].append(snip(json.dumps(c.input)[:300], 160))

    # history.jsonl adds prompts from sessions that no longer exist on disk
    if prompt_history:
        known = Counter(redact(r["prompt"], 240)[:60] for r in samples if r["prompt"])
        for h in prompt_history:
            txt = h.get("display") or ""
            if not txt or txt.startswith("/") or "<command-name>" in txt:
                continue
            k = redact(txt, 240)[:60]
            if known[k] > 0:  # same prompt already seen in a live transcript
                known[k] -= 1
                continue
            ts = h.get("timestamp")
            mo = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m") if isinstance(ts, (int, float)) else "unknown"
            prompts_for_clustering.append((txt, {"project": project_label(h.get("project"), project_mode), "month": mo,
                                                 "phase": classify.phase_of(txt), "tokens": 0, "source": "history"}))

    clusters = classify.cluster_prompts(prompts_for_clustering)
    top_clusters = []
    for c in clusters[:40]:
        if c["size"] < 2:
            break
        mem = c["members"]
        top_clusters.append({
            "size": c["size"], "signature": c["signature"], "example": snip(c["rep"], 200),
            "phase": Counter(m["phase"] for m in mem).most_common(1)[0][0],
            "projects": [p for p, _ in Counter(m["project"] for m in mem).most_common(5)],
            "months": sorted({m["month"] for m in mem}),
            "avg_tokens": int(statistics.mean(m["tokens"] for m in mem)) if mem else 0,
        })

    def risk_table(d: dict, extra=None):
        rows = []
        for k, v in sorted(d.items(), key=lambda kv: -kv[1]["count"]):
            row = {"rule": k, "count": v["count"], "risk": permissions.tool_risk(k), "examples": v["examples"]}
            if extra:
                row.update(extra(v))
            rows.append(row)
        return rows

    est_prompts = sum(v["count"] for v in prompt_by_key.values())
    prompt_rows = risk_table(prompt_by_key, lambda v: {"sessions": len(v["sessions"])})
    suggestions = [r for r in prompt_rows if r["risk"] == "low" and r["count"] >= 3]
    review = [r for r in prompt_rows if r["risk"] == "medium" and r["count"] >= 5]

    cache_den = tot.input + tot.cache_write + tot.cache_read
    summary = {
        "schema": SCHEMA_VERSION,
        "meta": {
            "account": account, "generated": datetime.now(timezone.utc).isoformat(), "sources": dict(by_source),
            "sessions": len(sessions), "prompts": n_prompts, "turns": n_turns,
            "date_range": [min((s.start for s in sessions if s.start), default=None), max((s.end for s in sessions if s.end), default=None)],
            "snippets": snippets, "project_mode": project_mode,
            "estimated_token_share": round(est_turns / n_turns, 3) if n_turns else 0,
            "permission_config_sources": [redact(p, 200) for p in perm_cfg.sources],
            # sessions per effective permission mode; "default_mode" kept for older readers
            "permission_modes": dict(mode_counts.most_common()),
            "default_mode": (mode_counts.most_common(1)[0][0].split(" ")[0] if mode_counts else perm_cfg.default_mode),
        },
        "efficiency": {
            "tokens": _u(tot), "subagent_tokens": _u(side),
            "subagent_share": round(side.total / tot.total, 3) if tot.total else 0,
            "cache_read_ratio": round(tot.cache_read / cache_den, 3) if cache_den else 0,
            "output_to_input_ratio": round(tot.output / max(1, tot.total_in), 4),
            "tokens_per_prompt": {"median": _pct(tokens_per_prompt, .5), "p90": _pct(tokens_per_prompt, .9), "max": max(tokens_per_prompt or [0])},
            "output_per_prompt": {"median": _pct(out_per_prompt, .5), "p90": _pct(out_per_prompt, .9)},
            "prompts_per_session": {"median": _pct(turns_per_session, .5), "p90": _pct(turns_per_session, .9), "max": max(turns_per_session or [0])},
            "tool_calls_per_prompt": {"median": _pct(tools_per_turn, .5), "p90": _pct(tools_per_turn, .9)},
            "tool_calls": tool_calls, "tool_error_rate": round(tool_errors / tool_calls, 3) if tool_calls else 0,
            "interrupt_rate": round(interrupts / n_prompts, 3) if n_prompts else 0,
            "correction_rate": round(corrections / n_prompts, 3) if n_prompts else 0,
            "bare_continue_rate": round(continues / n_prompts, 3) if n_prompts else 0,
            "context_reexplain_rate": round(reexplain / n_prompts, 3) if n_prompts else 0,
            "compactions": compactions, "compactions_per_session": round(compactions / len(sessions), 2) if sessions else 0,
            "api_errors": api_errors, "retry_loops": retry_loops[:25],
            "models": dict(models.most_common()), "cost_estimate_usd": round(cost, 2) if pricing else None,
            "models_note": "models counts main-thread assistant messages only; subagent_models/subagent_models_by_type cover subagents",
            **subagent_models(sessions),
            "by_month": dict(sorted(by_month.items())),
            "by_project": dict(sorted(by_project.items(), key=lambda kv: -kv[1]["tokens"])[:30]),
            "heaviest_sessions": sorted(heavy, key=lambda h: -h["tokens"])[:20],
            "hour_of_day": dict(sorted(hour_hist.items())), "weekday": dict(weekday_hist),
        },
        "blockers": {
            "denials_by_kind": dict(denial_kinds), "denials_total": sum(denial_kinds.values()),
            "denial_events_by_kind": dict(denial_events),  # consecutive same-kind denials in a thread count once
            "denials_by_rule": risk_table(denial_by_key, lambda v: {"kinds": dict(v["kinds"]), "feedback": v["feedback"]})[:40],
            "estimated_permission_prompts": est_prompts,
            "estimated_prompts_by_rule": prompt_rows[:50],
            "tool_errors_by_rule": [{"rule": k, **v} for k, v in sorted(error_by_key.items(), key=lambda kv: -kv[1]["count"])[:30]],
            "suggested_allow_low_risk": [r["rule"] for r in suggestions],
            "review_medium_risk": [r["rule"] for r in review],
            "never_allow_high_risk": [r["rule"] for r in prompt_rows if r["risk"] == "high"][:20],
            "existing_allow_rules_unused": [r for r in perm_cfg.allow if allow_hits[r] == 0],
            "existing_allow_rules_hits": dict(allow_hits.most_common(40)),
            # one row per distinct denial message; outcomes = what the next call in that thread did
            "block_outcomes": [
                {"kind": k, "signature": sig, "count": v["count"], "events": v["events"], "sessions": len(v["sessions"]),
                 "where": dict(v["where"]), "outcomes": dict(v["outcomes"]), "same_block_again": v["same_block_again"],
                 "tools": dict(v["tools"].most_common(5)), "message": v["message"], "examples": v["examples"]}
                for (k, sig), v in sorted(block_outcomes.items(), key=lambda kv: -kv[1]["count"])[:40]
            ],
        },
        "patterns": {
            "phase_prompts": dict(phase_prompts.most_common()), "phase_tokens": dict(phase_tokens.most_common()),
            "phase_correction_after": dict(phase_corrections.most_common()),
            "phase_by_month": {m: dict(c) for m, c in sorted(phase_month.items())},
            "phase_transitions": dict(transitions.most_common(30)),
            "slash_commands": dict(slash.most_common(30)),
            "skills": skill_usage(sessions, prompt_history, project_mode),
            "prompt_clusters": top_clusters,
        },
    }
    dr = summary["meta"]["date_range"]
    summary["meta"]["date_range"] = [d.isoformat() if d else None for d in dr]
    return summary, samples
