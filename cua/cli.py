"""Command line entry point.

Machine/account specifics come from a .env at the repo root (see .env.example);
flags override it, so with a .env a single-account scan is just `--out`.

  python -m cua scan    --out out/me                                 # single account, dir from .env
  python -m cua scan    --label work --claude-dir ~/.claude-work --out out/work
  python -m cua compare out/me out/work --out out/compare.md
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from . import analyze, config, report
from .sources import claude_ai, claude_code


def _since(v: str | None):
    if not v:
        return None
    if v.endswith("d"):
        return datetime.now(timezone.utc) - timedelta(days=int(v[:-1]))
    return datetime.fromisoformat(v).replace(tzinfo=timezone.utc)


def project_selected(project: str | None, include: list[str] | None, exclude: list[str] | None) -> bool:
    """Glob filter on a project path. Matches the path as recorded and its realpath,
    so ~/code/x and /mnt/.../code/x (symlinked) are treated alike."""
    if not include and not exclude:
        return True
    cands = [project] if project else [""]
    if project:
        try:
            rp = os.path.realpath(project)
            if rp != project:
                cands.append(rp)
        except OSError:
            pass
    hit = lambda pats: any(fnmatch.fnmatch(c, p) for c in cands for p in pats)
    if include and not hit(include):
        return False
    return not (exclude and hit(exclude))


def _history_in_window(h: dict, since) -> bool:
    if since is None:
        return True
    ts = h.get("timestamp")
    if not isinstance(ts, (int, float)):
        return False
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc) >= since


def projects_realpath(config_dir: str) -> str | None:
    p = os.path.join(os.path.expanduser(config_dir), "projects")
    return os.path.realpath(p) if os.path.isdir(p) else None


def cmd_scan(a) -> int:
    env = config.load_env()
    if not a.claude_dir and not a.export:
        a.claude_dir = [env.get("CLAUDE_DIR") or "~/.claude"]
    if not a.label:
        a.label = env.get("LABEL_1") or "me"
    sessions, history = [], []
    perm = None
    project_dirs = []
    since = _since(a.since)
    sources = []
    for d in a.claude_dir or []:
        d = os.path.expanduser(d)
        if not os.path.isdir(os.path.join(d, "projects")):
            print(f"warn: {d}/projects not found", file=sys.stderr)
            continue
        rp = projects_realpath(d)
        if rp != os.path.join(d, "projects"):
            print(f"note: {d}/projects resolves to {rp}", file=sys.stderr)
        sources.append(rp)
        ss = claude_code.load_sessions(d, a.label, since)
        n_all = len(ss)
        ss = [s for s in ss if project_selected(s.project, a.include_project, a.exclude_project)]
        kept = f" ({n_all - len(ss)} filtered out by project)" if len(ss) != n_all else ""
        print(f"{d}: {len(ss)} Claude Code sessions{kept}", file=sys.stderr)
        sessions += ss
        project_dirs += sorted({s.project for s in ss if s.project and os.path.isdir(s.project)})
        if not a.no_history:
            history += [h for h in claude_code.load_prompt_history(d)
                        if _history_in_window(h, since)
                        and project_selected(h.get("project"), a.include_project, a.exclude_project)]
        cfg = claude_code.load_permission_config(d, project_dirs)
        if perm is None:
            perm = cfg
        else:
            perm.allow += cfg.allow; perm.deny += cfg.deny; perm.sources += cfg.sources
    for e in a.export or []:
        ss = claude_ai.load_export(os.path.expanduser(e), a.label)
        if since:
            ss = [s for s in ss if not s.start or s.start >= since]
        print(f"{e}: {len(ss)} claude.ai conversations", file=sys.stderr)
        sessions += ss
    if not sessions:
        print("no sessions found", file=sys.stderr)
        return 1
    pricing = None
    if a.pricing and os.path.exists(a.pricing):
        with open(a.pricing) as f:
            pricing = json.load(f)
    # One user-settings dir per scan (the skill passes one --claude-dir per account). With several,
    # every session is judged against the first dir's user settings.
    user_dir = os.path.expanduser(a.claude_dir[0]) if a.claude_dir else None
    eff_cache: dict = {}

    def perm_for_session(s):
        key = s.project if s.project and os.path.isdir(s.project) else None
        if key not in eff_cache:
            eff_cache[key] = claude_code.effective_permission_config(user_dir, key)
        return eff_cache[key]

    summary, samples = analyze.analyze(sessions, a.label, perm, project_mode=a.project_names,
                                       snippets=not a.no_snippets, pricing=pricing, prompt_history=history,
                                       perm_for_session=perm_for_session)
    summary["meta"]["claude_projects_sources"] = sources
    summary["meta"]["project_filter"] = {"include": a.include_project or [], "exclude": a.exclude_project or []}
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1, default=str)
    with open(os.path.join(a.out, "samples.jsonl"), "w") as f:
        for r in samples:
            f.write(json.dumps(r, default=str) + "\n")
    with open(os.path.join(a.out, "report.md"), "w") as f:
        f.write(report.render_account(summary))
    print(f"wrote {a.out}/summary.json, samples.jsonl ({len(samples)} rows), report.md", file=sys.stderr)
    return 0


def cmd_waits(a) -> int:
    from . import waits
    since = _since(a.since)
    d = os.path.expanduser(a.claude_dir)
    ss = [s for s in claude_code.load_sessions(d, a.label, since)
          if project_selected(s.project, a.include_project, a.exclude_project)]
    if not ss:
        print("no sessions found", file=sys.stderr)
        return 1
    cache: dict = {}

    def perm_for_session(s):
        key = s.project if s.project and os.path.isdir(s.project) else None
        if key not in cache:
            cache[key] = claude_code.effective_permission_config(d, key)
        return cache[key]

    res = waits.analyse(ss, a.prompt_log, since, perm_for_session=perm_for_session)
    detail = res.pop("_calls", [])
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "waits-calls.jsonl"), "w") as f:
        for r in detail:
            f.write(json.dumps(r) + "\n")
    res["meta"]["account"] = a.label
    res["meta"]["sessions"] = len(ss)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "waits.json"), "w") as f:
        json.dump(res, f, indent=1, default=str)
    m = res["meta"]["matched_by"]
    print(f"joined {res['meta']['log_prompts_matched']}/{res['meta']['logged_prompts']} log rows: "
          f"{m['id']} by tool_use_id, {m['hash']} by call hash, {m['session']} by session+time, "
          f"{m['guess']} by time/tool guess "
          f"({m['unmatched_rows']} unmatched)", file=sys.stderr)
    print(f"wrote {a.out}/waits.json ({len(ss)} sessions)", file=sys.stderr)
    return 0


def cmd_sdl(a) -> int:
    """SDL / change-management compliance signals from agent transcripts (reads raw; emits
    distilled compliance facts for the sdl-compliance-analyst). Only this layer touches transcripts."""
    from . import sdl
    env = config.load_env()
    if not a.claude_dir:
        a.claude_dir = [env.get("CLAUDE_DIR") or "~/.claude"]
    if not a.label:
        a.label = env.get("LABEL_1") or "me"
    since = _since(a.since)
    ss = []
    for d in a.claude_dir or []:
        d = os.path.expanduser(d)
        if not os.path.isdir(os.path.join(d, "projects")):
            print(f"warn: {d}/projects not found", file=sys.stderr)
            continue
        got = [s for s in claude_code.load_sessions(d, a.label, since)
               if project_selected(s.project, a.include_project, a.exclude_project)]
        print(f"{d}: {len(got)} Claude Code sessions", file=sys.stderr)
        ss += got
    if not ss:
        print("no sessions found", file=sys.stderr)
        return 1
    res = sdl.analyse(ss, since, ticket_prefix=env.get("TICKET_PREFIX", ""),
                      policy_name=env.get("POLICY_NAME") or "Change-management policy")
    events = res.pop("_events", [])
    res["meta"]["account"] = a.label
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "sdl-events.jsonl"), "w") as f:
        for e in events:
            f.write(json.dumps(e, default=str) + "\n")
    with open(os.path.join(a.out, "sdl.json"), "w") as f:
        json.dump(res, f, indent=1, default=str)
    m = res["meta"]
    print(f"wrote {a.out}/sdl.json, sdl-events.jsonl: {m['events']} events "
          f"({m['violations']} violations, {m['attention']} attention) across "
          f"{m['sessions_with_change_activity']} change sessions", file=sys.stderr)
    return 0


def cmd_preflight(a) -> int:
    """Names and paths only; never opens a transcript or export."""
    env = config.load_env()
    accts = config.accounts(env)
    dirs = a.claude_dir or [x["claude_dir"] for x in accts] or ["~/.claude"]
    labels = [x["label"] for x in accts] or ["me"]
    seen: dict[str, str] = {}
    problems = 0
    for d in dirs:
        full = os.path.expanduser(d)
        rp = projects_realpath(full)
        if rp is None:
            print(f"MISSING  {d}/projects")
            problems += 1
            continue
        n = sum(1 for e in os.scandir(rp) if e.is_dir())
        link = f" -> {rp}" if rp != os.path.join(full, "projects") else ""
        print(f"ok       {d}/projects{link} ({n} project folders)")
        if rp in seen:
            print(f"SHARED   {d}/projects is the same folder as {seen[rp]}/projects: "
                  "split accounts with --include-project/--exclude-project")
            problems += 1
        seen.setdefault(rp, d)
    if os.path.isdir(a.exports):
        zips = sorted(n for n in os.listdir(a.exports) if n.endswith(".zip"))
        for label in labels:
            mine = [z for z in zips if z.startswith(label + "-")]
            print(f"{'ok' if mine else 'none':<8} export {label}: {', '.join(mine) or '-'}")
        others = [n for n in os.listdir(a.exports) if n.endswith("-claude") and os.path.isdir(os.path.join(a.exports, n))]
        for o in sorted(others):
            print(f"extra    config dir {a.exports}/{o}")
    else:
        print(f"none     {a.exports}/ does not exist")
    return 1 if problems else 0


def cmd_lint(a) -> int:
    """Config files only; never opens a transcript."""
    from . import lint
    accounts = []
    for spec in a.account or []:
        label, _, pat = spec.partition("=")
        if not pat:
            print(f"bad --account {spec!r}: want LABEL=GLOB", file=sys.stderr)
            return 2
        accounts.append((label, pat))
    cj = os.path.expanduser(a.claude_json) if a.claude_json else None
    res = lint.lint(a.claude_dir, a.root or ["~/code"], accounts, claude_json=cj, max_depth=a.max_depth,
                    usage_path=a.usage)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "lint.json"), "w") as f:
        json.dump(res, f, indent=1)
    with open(os.path.join(a.out, "lint.md"), "w") as f:
        f.write(lint.render(res))
    warns = sum(1 for f in res["findings"] if f["severity"] == "warn")
    print(f"{res['meta']['projects']} projects, {len(res['findings'])} findings ({warns} warn); "
          f"wrote {a.out}/lint.json, lint.md", file=sys.stderr)
    return 0


def overlap_warnings(sums: list[dict]) -> list[str]:
    """Two accounts that read the same transcripts with no disjoint project filter
    compare a dataset with itself."""
    out = []
    for i in range(len(sums)):
        for j in range(i + 1, len(sums)):
            mi, mj = sums[i]["meta"], sums[j]["meta"]
            shared = set(mi.get("claude_projects_sources") or []) & set(mj.get("claude_projects_sources") or [])
            if not shared:
                continue
            fi, fj = mi.get("project_filter") or {}, mj.get("project_filter") or {}
            if any(fi.values()) or any(fj.values()):
                continue  # filtered: assume the caller split them deliberately
            out.append(f"{mi['account']} and {mj['account']} both read {', '.join(sorted(shared))} "
                       "with no project filter; their numbers describe the same sessions.")
    return out


def cmd_compare(a) -> int:
    sums = []
    for d in a.dirs:
        with open(os.path.join(d, "summary.json")) as f:
            sums.append(json.load(f))
    warns = overlap_warnings(sums)
    for w in warns:
        print(f"warn: {w}", file=sys.stderr)
    md = report.render_compare(sums)
    if warns:
        md = "\n".join(["> **Warning:** " + w for w in warns]) + "\n\n" + md
    if a.out:
        with open(a.out, "w") as f:
            f.write(md)
        print(f"wrote {a.out}", file=sys.stderr)
    else:
        print(md)
    return 0


def cmd_version(a) -> int:
    from . import update
    st = update.check(throttle=True)
    print(f"cua {st['version']}")
    if not st.get("repo"):
        print("not a git checkout — version/update need a cloned repo")
    elif not st.get("remote"):
        print("no git remote configured; add one (origin) to enable update checks")
    elif st.get("behind"):
        print(update.format_notice(st["behind"]))
    else:
        print("up to date")
    return 0


def cmd_update(a) -> int:
    from . import update
    rc, msg = update.run_update()
    print(msg, file=sys.stdout if rc == 0 else sys.stderr)
    if rc == 0:
        print(f"now at: {update.current_version()}")
    return rc


def cmd_help(a) -> int:
    from . import update
    print(update.HELP)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cua", description="Claude usage audit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="analyse one account")
    s.add_argument("--label", help="account name in output (default: LABEL_1 from .env, else 'me')")
    s.add_argument("--claude-dir", action="append", help="Claude Code config dir (repeatable; default: CLAUDE_DIR from .env)")
    s.add_argument("--export", action="append", help="claude.ai export .zip or folder (repeatable)")
    s.add_argument("--out", required=True)
    s.add_argument("--since", help="e.g. 90d or 2026-06-01")
    s.add_argument("--project-names", choices=["basename", "hash", "full"], default="basename",
                   help="how project paths appear in output (use hash for work data you might share)")
    s.add_argument("--no-snippets", action="store_true", help="omit all prompt/command text from output")
    s.add_argument("--no-history", action="store_true", help="skip history.jsonl")
    s.add_argument("--pricing", default="pricing.json", help="optional $/Mtok table, see pricing.example.json")
    s.add_argument("--include-project", action="append", metavar="GLOB",
                   help="keep only sessions whose project path matches (repeatable)")
    s.add_argument("--exclude-project", action="append", metavar="GLOB",
                   help="drop sessions whose project path matches (repeatable)")
    s.set_defaults(func=cmd_scan)
    w = sub.add_parser("waits", help="where sessions sit still: permission prompt waits and other pauses")
    w.add_argument("--label", required=True)
    w.add_argument("--claude-dir", required=True)
    w.add_argument("--out", required=True)
    w.add_argument("--since", default="30d")
    w.add_argument("--prompt-log", default="", help="optional permission-prompt hook log (tab-separated); enriches wait attribution")
    w.add_argument("--include-project", action="append", metavar="GLOB")
    w.add_argument("--exclude-project", action="append", metavar="GLOB")
    w.set_defaults(func=cmd_waits)
    sd = sub.add_parser("sdl", help="SDL / change-management compliance signals from agent transcripts")
    sd.add_argument("--label", help="account name in output (default: LABEL_1 from .env, else 'me')")
    sd.add_argument("--claude-dir", action="append", help="Claude Code config dir (repeatable; default: CLAUDE_DIR from .env)")
    sd.add_argument("--out", required=True)
    sd.add_argument("--since", help="e.g. 90d or 2026-06-01")
    sd.add_argument("--include-project", action="append", metavar="GLOB")
    sd.add_argument("--exclude-project", action="append", metavar="GLOB")
    sd.set_defaults(func=cmd_sdl)
    p = sub.add_parser("preflight", help="check config dirs and exports without reading transcripts")
    p.add_argument("--claude-dir", action="append", help="repeatable; default: the accounts in .env (see .env.example), else ~/.claude")
    p.add_argument("--exports", default="exports")
    p.set_defaults(func=cmd_preflight)
    li = sub.add_parser("lint", help="audit CLAUDE.md, skills, agents, hooks, settings and memory (config files only)")
    li.add_argument("--claude-dir", default="~/.claude")
    li.add_argument("--root", action="append", help="where to look for projects (repeatable; default ~/code)")
    li.add_argument("--account", action="append", metavar="LABEL=GLOB",
                    help="label projects by path, e.g. work=/path/to/work-repos/*")
    li.add_argument("--claude-json", default="~/.claude.json",
                    help="read MCP server names from this file (names only); '' to skip")
    li.add_argument("--max-depth", type=int, default=3)
    li.add_argument("--usage", metavar="SUMMARY_JSON",
                    help="a `cua scan` summary.json; adds the skill retirement table (use vs cost vs dependents)")
    li.add_argument("--out", default="out/lint")
    li.set_defaults(func=cmd_lint)
    c = sub.add_parser("compare", help="compare account outputs")
    c.add_argument("dirs", nargs="+")
    c.add_argument("--out")
    c.set_defaults(func=cmd_compare)
    v = sub.add_parser("version", help="show the installed version (git revision) and whether it is up to date")
    v.set_defaults(func=cmd_version)
    u = sub.add_parser("update", help="update cua to the latest version (git pull --ff-only)")
    u.set_defaults(func=cmd_update)
    hp = sub.add_parser("help", help="how to set up and use cua")
    hp.set_defaults(func=cmd_help)
    a = ap.parse_args(argv)
    # Throttled, fail-silent update notice on a real run (once/day via .git/FETCH_HEAD mtime).
    if a.cmd in ("scan", "sdl", "waits", "lint", "compare"):
        try:
            from . import update
            n = update.notice()
            if n:
                print(n, file=sys.stderr)
        except Exception:
            pass
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
