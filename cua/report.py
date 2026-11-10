"""Render summary.json into Markdown, and compare two summaries."""
from __future__ import annotations


def _n(x) -> str:
    if isinstance(x, float):
        return f"{x:.1%}" if x <= 1 else f"{x:,.1f}"
    if isinstance(x, int):
        if x >= 1_000_000:
            return f"{x/1_000_000:.1f}M"
        if x >= 10_000:
            return f"{x/1000:.0f}k"
        return f"{x:,}"
    return str(x)


def _table(headers, rows) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |")
    return "\n".join(out)


def render_account(s: dict) -> str:
    m, e, b, p = s["meta"], s["efficiency"], s["blockers"], s["patterns"]
    L = [f"# Claude usage audit: {m['account']}", "",
         f"Range {m['date_range'][0]} → {m['date_range'][1]} · {m['sessions']} sessions · {m['prompts']} prompts · sources {m['sources']}",
         f"Estimated-token share: {_n(m['estimated_token_share'])} (claude.ai exports carry no token counts)", ""]
    t = e["tokens"]
    L += ["## 1. Efficiency", "", _table(["metric", "value"], [
        ["total tokens", _n(t["total"])], ["input / cache write / cache read / output", f"{_n(t['input'])} / {_n(t['cache_write'])} / {_n(t['cache_read'])} / {_n(t['output'])}"],
        ["cache read ratio", _n(e["cache_read_ratio"])], ["subagent share of tokens", _n(e["subagent_share"])],
        ["tokens per prompt (median / p90 / max)", f"{_n(e['tokens_per_prompt']['median'])} / {_n(e['tokens_per_prompt']['p90'])} / {_n(e['tokens_per_prompt']['max'])}"],
        ["prompts per session (median / p90)", f"{e['prompts_per_session']['median']} / {e['prompts_per_session']['p90']}"],
        ["tool calls per prompt (median / p90)", f"{e['tool_calls_per_prompt']['median']} / {e['tool_calls_per_prompt']['p90']}"],
        ["tool error rate", _n(e["tool_error_rate"])], ["correction rate (no/wrong/still broken…)", _n(e["correction_rate"])],
        ["interrupt rate", _n(e["interrupt_rate"])], ["bare 'continue/yes' rate", _n(e["bare_continue_rate"])],
        ["context re-explain rate", _n(e["context_reexplain_rate"])], ["compactions per session", e["compactions_per_session"]],
        ["retry loops (≥3 consecutive failures)", len(e["retry_loops"])], ["cost estimate (USD)", e["cost_estimate_usd"] if e["cost_estimate_usd"] is not None else "n/a (no pricing.json)"],
    ]), ""]
    L += ["### Heaviest sessions", "", _table(["session", "project", "prompts", "tokens", "compactions", "title"],
          [[h["session"], h["project"], h["prompts"], _n(h["tokens"]), h["compactions"], h["title"]] for h in e["heaviest_sessions"][:10]]), ""]
    L += ["### By project (top 12 by tokens)", "", _table(["project", "sessions", "prompts", "tokens", "corrections", "denials"],
          [[k, v["sessions"], v["prompts"], _n(v["tokens"]), v["corrections"], v["denials"]] for k, v in list(e["by_project"].items())[:12]]), ""]
    L += ["### By month", "", _table(["month", "sessions", "prompts", "tokens"],
          [[k, v["sessions"], v["prompts"], _n(v["tokens"])] for k, v in e["by_month"].items()]), ""]

    L += ["## 2. Blockers", "", f"Hard denials: **{b['denials_total']}** {b['denials_by_kind']} · estimated permission prompts: **{b['estimated_permission_prompts']}**", ""]
    if b["denials_by_rule"]:
        L += ["### Denials by rule", "", _table(["rule", "count", "kinds", "risk", "user feedback (sample)"],
              [[r["rule"], r["count"], r["kinds"], r["risk"], "; ".join(r["feedback"][:2])] for r in b["denials_by_rule"][:15]]), ""]
    if b["estimated_prompts_by_rule"]:
        L += ["### Where permission prompts come from (estimate)", "", _table(["rule", "calls", "sessions", "risk"],
              [[r["rule"], r["count"], r["sessions"], r["risk"]] for r in b["estimated_prompts_by_rule"][:20]]), ""]
    if b["tool_errors_by_rule"]:
        L += ["### Tool errors (not denials)", "", _table(["rule", "count", "example"],
              [[r["rule"], r["count"], (r["examples"] or [""])[0]] for r in b["tool_errors_by_rule"][:12]]), ""]

    L += ["## 3. Streamlining", "",
          "**Low-risk allowlist candidates** (add to `permissions.allow` after review):", "",
          "```json", "\n".join(f'  "{r}",' for r in b["suggested_allow_low_risk"]) or "  (none)", "```", "",
          "**Medium risk, review case by case:** " + (", ".join(f"`{r}`" for r in b["review_medium_risk"]) or "none"), "",
          "**High risk, keep prompting (or deny):** " + (", ".join(f"`{r}`" for r in b["never_allow_high_risk"]) or "none"), "",
          "**Existing allow rules never matched (stale?):** " + (", ".join(f"`{r}`" for r in b["existing_allow_rules_unused"]) or "none"), ""]

    L += ["## 4. Prompt patterns vs dev cycle", "", _table(["phase", "prompts", "tokens", "corrections after"],
          [[k, v, _n(p["phase_tokens"].get(k, 0)), p["phase_correction_after"].get(k, 0)] for k, v in p["phase_prompts"].items()]), ""]
    if p["phase_transitions"]:
        L += ["Top phase transitions: " + ", ".join(f"{k} ({v})" for k, v in list(p["phase_transitions"].items())[:10]), ""]
    if p["slash_commands"]:
        L += ["Slash commands: " + ", ".join(f"/{k} ({v})" for k, v in p["slash_commands"].items()), ""]
    L += ["### Repeating prompt clusters", "", _table(["size", "phase", "projects", "months", "avg tokens", "example"],
          [[c["size"], c["phase"], ", ".join(c["projects"][:3]), f"{c['months'][0]}…{c['months'][-1]}" if c["months"] else "", _n(c["avg_tokens"]), c["example"]] for c in p["prompt_clusters"][:20]]), ""]
    return "\n".join(L)


def _pct_s(x) -> str:
    return f"{x:.1%}"


def _dec(places):
    return lambda x: f"{x:,.{places}f}"


# (label, getter, formatter). The formatter is explicit: guessing "a float <= 1 is a share"
# printed 0.30 prompts/session as "30.0%".
COMPARE_METRICS = [
    ("sessions", lambda s: s["meta"]["sessions"], _n),
    ("prompts", lambda s: s["meta"]["prompts"], _n),
    ("total tokens", lambda s: s["efficiency"]["tokens"]["total"], _n),
    ("tokens/prompt median", lambda s: s["efficiency"]["tokens_per_prompt"]["median"], _n),
    ("tokens/prompt p90", lambda s: s["efficiency"]["tokens_per_prompt"]["p90"], _n),
    ("cache read ratio", lambda s: s["efficiency"]["cache_read_ratio"], _pct_s),
    ("subagent share", lambda s: s["efficiency"]["subagent_share"], _pct_s),
    ("prompts/session median", lambda s: s["efficiency"]["prompts_per_session"]["median"], _dec(1)),
    ("tool error rate", lambda s: s["efficiency"]["tool_error_rate"], _pct_s),
    ("correction rate", lambda s: s["efficiency"]["correction_rate"], _pct_s),
    ("interrupt rate", lambda s: s["efficiency"]["interrupt_rate"], _pct_s),
    ("bare continue rate", lambda s: s["efficiency"]["bare_continue_rate"], _pct_s),
    ("compactions/session", lambda s: s["efficiency"]["compactions_per_session"], _dec(2)),
    ("hard denials", lambda s: s["blockers"]["denials_total"], _n),
    ("denials/session", lambda s: s["blockers"]["denials_total"] / max(1, s["meta"]["sessions"]), _dec(2)),
    ("est. permission prompts", lambda s: s["blockers"]["estimated_permission_prompts"], _n),
    ("est. prompts/session", lambda s: s["blockers"]["estimated_permission_prompts"] / max(1, s["meta"]["sessions"]), _dec(2)),
]


def _share(d: dict) -> dict:
    tot = sum(d.values()) or 1
    return {k: v / tot for k, v in d.items()}


def render_compare(summaries: list[dict]) -> str:
    names = [s["meta"]["account"] for s in summaries]
    L = ["# Claude usage: " + " vs ".join(names), "", "## Headline metrics", "",
         _table(["metric"] + names, [[label] + [fmt(f(s)) for s in summaries] for label, f, fmt in COMPARE_METRICS]), ""]
    phases = sorted({k for s in summaries for k in s["patterns"]["phase_prompts"]})
    shares = [_share(s["patterns"]["phase_prompts"]) for s in summaries]
    L += ["## Dev-cycle mix (share of prompts)", "", _table(["phase"] + names, [[ph] + [f"{sh.get(ph, 0):.0%}" for sh in shares] for ph in phases]), ""]
    rules = [{r["rule"]: r["count"] for r in s["blockers"]["estimated_prompts_by_rule"]} for s in summaries]
    allr = sorted(set().union(*rules), key=lambda r: -sum(x.get(r, 0) for x in rules))[:25]
    L += ["## Permission prompt sources (estimated calls)", "", _table(["rule"] + names, [[r] + [x.get(r, 0) for x in rules] for r in allr]), ""]
    sigs = [{c["signature"] for c in s["patterns"]["prompt_clusters"]} for s in summaries]
    if len(summaries) == 2:
        common = sigs[0] & sigs[1]
        L += ["## Prompt clusters", "", f"Shared signatures: {len(common)}; only {names[0]}: {len(sigs[0]-sigs[1])}; only {names[1]}: {len(sigs[1]-sigs[0])}", ""]
        for sig in list(common)[:10]:
            L.append(f"- `{sig}`")
        L.append("")
    allow = [set(s["blockers"]["suggested_allow_low_risk"]) for s in summaries]
    L += ["## Allowlist candidates", "", "- **Both accounts:** " + (", ".join(f"`{r}`" for r in sorted(set.intersection(*allow))) or "none")]
    for n, a in zip(names, allow):
        L.append(f"- **{n} only:** " + (", ".join(f"`{r}`" for r in sorted(a - set.intersection(*allow))) or "none"))
    L.append("")
    return "\n".join(L)
