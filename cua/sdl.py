"""Secure-development-lifecycle / change-management compliance signals from agent transcripts.

Reads Claude Code sessions and distils what the AGENT did that a change-management
policy cares about: branch discipline, issue-tracker traceability, pull-request
workflow, segregation of duties, secret handling, dependency intake and test gates.

The checks are generic; the organisation-specific parts are configuration:
  * `ticket_prefix` — your issue-tracker key(s), e.g. "PROJ" or "PROJ,OPS". Blank
    disables the traceability checks (no ticket convention to hold code to).
  * `policy_name` / a policy document — named by the caller and cited by the
    `sdl-compliance-analyst`; shipped with a generic `example-sdl-policy.md`.

Like every extractor here it is the only layer that touches raw transcripts, so it
emits distilled compliance *facts* — a check id, a default severity, a control
category, shapes and booleans, and a redacted short head — never a commit body, PR
body or file contents. The analyst reads the result from out/ and assigns the final
verdict.

What an agent can and cannot be judged on from a transcript:
  * Judgeable here: branch it worked on, branch names it created, commit subjects'
    ticket references, pushes to protected branches, PRs it opened and their base,
    whether it merged/approved/bypassed a PR, secret-bearing writes, new
    dependencies, and whether tests ran before a PR.
  * NOT judgeable here (the analyst lists these for human attestation, never a
    fake pass): UAT sign-off, release approval, independent peer review, dependency
    security assessment, retention, and branch-protection / environment setup.

Branch state is replayed from the agent's own `git checkout`/`switch` commands,
seeded by the session's first recorded gitBranch. A branch switched outside
Claude's own commands is not seen; pushes name their target explicitly, so the
push checks do not depend on the replay.
"""
from __future__ import annotations

import bisect
import re
import shlex
from collections import defaultdict
from datetime import datetime, timezone

from .permissions import bash_segments
from .redact import redact

# Protected / long-lived branches a change must reach only through a reviewed PR.
PROTECTED_RE = re.compile(r"^(?:\w+/)?(?:main|master|develop|development|release|release/.+|production|prod|live)$", re.I)
# Compliant working-branch prefixes (feature/… for standard work, hotfix/… for emergencies).
WORKING_RE = re.compile(r"^(?:feature|hotfix)/", re.I)

GIT_VALUE_FLAGS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
GH_VALUE_FLAGS = {"-R", "--repo"}

INSTALL_RE = [
    (re.compile(r"^(?:npm|pnpm|yarn|bun)\s+(?:install|add|i)\b"), "npm"),
    (re.compile(r"^(?:pip3?|uv\s+pip|uv|poetry|conda)\s+(?:install|add)\b"), "python"),
    (re.compile(r"^dotnet\s+add\b"), "nuget"),
    (re.compile(r"^go\s+get\b"), "go"),
    (re.compile(r"^cargo\s+add\b"), "cargo"),
]
# Lockfile restores / no-package installs that do NOT introduce a new dependency.
RESTORE_RE = re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:ci|install|i)\s*$|--frozen|--immutable|-r\b|requirements[\w.-]*\.txt|\buv\s+sync\b|\bpoetry\s+install\b|\bdotnet\s+restore\b")
TEST_RE = re.compile(r"(?:^|\s|&&|\|)(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b|\bpytest\b|\bvitest\b|\bjest\b|\bdotnet\s+test\b|\bgo\s+test\b|\bcargo\s+test\b|\bphpunit\b|\bmix\s+test\b|\bunittest\b", re.I)
SECRET_PATH_RE = re.compile(r"(?:^|/)(?:\.env(?:\.[\w.-]+)?|\.npmrc|id_rsa|id_ed25519|credentials|[\w.-]*secrets?\.(?:json|ya?ml|txt|env))$|\.(?:pem|pfx|p12|key)$", re.I)
# Strong, low-false-positive markers: a real key/token/private key in any file is a finding.
# sk- needs a left boundary so it doesn't fire inside a word like "ta**sk-**261014" (a task slug).
SECRET_STRONG_RE = re.compile(
    r"AKIA[0-9A-Z]{16}|(?<![A-Za-z0-9])sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9\-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----")
# Weak marker: a `password = …` style assignment. Only trusted in non-doc files with a secret-looking value,
# because prose ("password: required") trips it — the classic file-contents-as-finding bug.
SECRET_ASSIGN_RE = re.compile(
    r"(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|client[_-]?secret|connection[ _-]?string|auth[_-]?token)"
    r"\s*[=:]\s*['\"]?([^\s'\"]{8,})", re.I)
DOC_PATH_RE = re.compile(r"\.(?:md|mdx|rst|txt|adoc)$|/\.planning/|/docs?/", re.I)
# Test fixtures routinely carry dummy credentials; a `password=` there is not a committed secret.
TEST_PATH_RE = re.compile(r"(?:^|/)(?:tests?|__tests__|spec|e2e|fixtures?)/|\.(?:test|spec)\.[tj]sx?$|_test\.py$|(?:^|/)test_[\w-]+\.py$|conftest\.py$|\.spec\.\w+$", re.I)
PLACEHOLDER_RE = re.compile(
    r"^(?:<.*>|\$\{?.+|\{\{.+|%\w+%|\*+|x+|\.+|changeme|placeholder|example|redacted|required|optional|true|false|"
    r"none|null|todo|env|secret|password|your[_-].*|xxx+|dummy|test)$", re.I)
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def ticket_re(prefix: str | None):
    """Compile an issue-id matcher from a prefix spec like "PROJ" or "PROJ,OPS".
    Returns None when no prefix is configured (traceability checks are then skipped)."""
    keys = [p.strip() for p in (prefix or "").split(",") if p.strip()]
    if not keys:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(k) for k in keys) + r")-\d+\b", re.I)


def _value_looks_secret(v: str) -> bool:
    """A captured assignment value that reads like a real credential, not a prose word or placeholder."""
    if PLACEHOLDER_RE.match(v):
        return False
    return len(v) >= 16 or bool(re.search(r"\d", v)) or (v.lower() != v and v.upper() != v)

# check id -> (default severity, one-line meaning, control category)
CHECKS: dict[str, tuple[str, str, str]] = {
    "push.protected": ("violation", "Pushed to a protected branch (main/develop/release/production)", "protected-branch"),
    "branch.protected_commit": ("violation", "Committed directly on a protected branch", "protected-branch"),
    "pr.self_merge": ("violation", "Agent merged a pull request (merge is a human approver act)", "segregation-of-duties"),
    "pr.self_approve": ("violation", "Agent approved a pull request", "segregation-of-duties"),
    "pr.bypass": ("violation", "Used an admin/auto bypass on a pull request", "segregation-of-duties"),
    "secrets.secret_path_write": ("violation", "Wrote to a secret-bearing path (.env, key, credentials)", "secrets"),
    "secrets.key_or_token": ("violation", "Wrote a key/token/private key into a file", "secrets"),
    "secrets.credential_assignment": ("attention", "Wrote a password/secret assignment in source (not a test/doc) — check it is not a real secret", "secrets"),
    "secrets.git_add_secret": ("violation", "Staged a secret-bearing path for commit", "secrets"),
    "branch.working_no_ticket": ("attention", "Working branch not named feature/… with a tracked issue id", "traceability"),
    "trace.commit_no_ticket": ("attention", "Commit not traceable to an issue id (neither branch nor subject)", "traceability"),
    "pr.base_not_release": ("attention", "PR targets a non-integration base (not develop/release/main) — possible branch stacking", "branch-flow"),
    "pr.no_ticket": ("attention", "PR does not reference a tracked issue id", "traceability"),
    "deps.new_dependency": ("attention", "Added a third-party dependency (needs a supply-chain assessment)", "dependencies"),
}
SEV_RANK = {"violation": 2, "attention": 1, "info": 0, None: -1}

NOT_JUDGEABLE = [
    "UAT / acceptance sign-off recorded on the issue tracker",
    "Release approval, a recorded act distinct from authoring the code",
    "Independent peer review actually occurred and the reviewer != author "
    "(the audit sees the PR opened, not that a human reviewed it)",
    "Supply-chain / security assessment performed for a new dependency",
    "Static analysis and its findings resolved before review",
    "Penetration-test cadence and remediation SLAs",
    "Change-record retention",
    "Environment segregation and branch-protection configuration (the senior developer's job)",
]


def _toks(seg: str) -> list[str]:
    try:
        return shlex.split(seg, posix=True)
    except ValueError:
        return seg.split()


def _git_sub(toks: list[str]) -> tuple[str, list[str]]:
    """('git', '-C', '/x', 'commit', '-m', 'y') -> ('commit', ['-m', 'y'])."""
    i = 1
    while i < len(toks):
        t = toks[i]
        if t in GIT_VALUE_FLAGS:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        return t, toks[i + 1:]
    return "", []


def _gh_sub(toks: list[str]) -> list[str]:
    """tokens after `gh`, with -R/--repo values skipped: ('gh','pr','create') -> ['pr','create']."""
    out, i = [], 1
    while i < len(toks):
        t = toks[i]
        if t in GH_VALUE_FLAGS:
            i += 2
            continue
        out.append(t)
        i += 1
    return out


def _branch_from(sub: str, rest: list[str]) -> tuple[str | None, bool]:
    """Branch named by `git checkout`/`switch` and whether it is being created (-b/-B/-c/-C)."""
    if "--" in rest:  # `git checkout -- file`: a path op, not a branch move
        rest = rest[:rest.index("--")]
    i = 0
    while i < len(rest):
        t = rest[i]
        if t in ("-b", "-B", "-c", "-C"):
            return (rest[i + 1] if i + 1 < len(rest) else None), True
        if t.startswith("-"):
            i += 1
            continue
        return t, False
    return None, False


def _push_targets(rest: list[str]) -> tuple[set[str], bool, bool]:
    """Branches a `git push` writes to (dst side of any refspec), force flag, and whether any
    refspec was given at all (none => current branch)."""
    force = any(t == "-f" or t.startswith("--force") for t in rest)
    pos = [t for t in rest if not t.startswith("-")]
    refs = pos[1:] if pos else []  # pos[0] is the remote
    branches = {r.split(":")[-1] for r in refs if r}
    return branches, force, bool(refs)


def _val(toks: list[str], *names: str) -> str | None:
    for i, t in enumerate(toks):
        if t in names and i + 1 < len(toks):
            return toks[i + 1]
        for n in names:
            if t.startswith(n + "="):
                return t[len(n) + 1:]
    return None


def _is_branchy(name: str | None) -> bool:
    return bool(name) and name not in (".", "..", "HEAD", "-") and not name.startswith("-")


def analyse(sessions, since: datetime | None = None, top: int = 40, ticket_prefix: str = "",
            policy_name: str = "Change-management policy") -> dict:
    tkt = ticket_re(ticket_prefix)
    events: list[dict] = []
    sess_rows: list[dict] = []

    for s in sessions:
        if getattr(s, "source", "claude_code") != "claude_code":
            continue
        proj = redact(s.project, 120) if s.project else "(unknown)"
        starts = sorted((t.ts.timestamp(), t.command) for t in s.turns if t.ts and t.command)
        start_times = [x[0] for x in starts]

        def flow_at(epoch: float) -> str | None:
            i = bisect.bisect_right(start_times, epoch) - 1
            return starts[i][1] if i >= 0 else None

        calls = sorted((c for c in s.tool_calls if c.ts), key=lambda c: c.ts.timestamp())
        cur_branch = s.git_branch
        srow = {"session": s.id, "project": proj, "start_branch": s.git_branch, "agents": set(),
                "commits": 0, "pushes": 0, "prs_created": 0, "tests_run": False,
                "violation": 0, "attention": 0, "pr_created_epoch": None}

        def emit(check: str, epoch: float, branch: str | None, agent: str, detail: dict, head: str,
                 sev: str | None = None) -> None:
            default_sev, meaning, ctrl = CHECKS[check]
            sev = sev or default_sev
            events.append({"check": check, "severity": sev, "control": ctrl, "meaning": meaning,
                           "session": s.id, "project": proj, "branch": branch, "agent": agent,
                           "workflow": flow_at(epoch), "ts": datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
                           "detail": detail, "head": head})
            srow[sev] = srow.get(sev, 0) + 1

        for c in calls:
            epoch = c.ts.timestamp()
            agent = (s.agent_types.get(c.agent_id) or "(unlinked subagent)") if c.agent_id else "(main)"
            srow["agents"].add(agent)
            inp = c.input or {}

            if c.name in EDIT_TOOLS:
                path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or ""
                head = redact(path, 160)
                if path and SECRET_PATH_RE.search(path):
                    emit("secrets.secret_path_write", epoch, cur_branch, agent, {"path_shape": "secret-bearing"}, head)
                body = " ".join(str(inp.get(k) or "") for k in ("content", "new_string", "new_str"))
                if body and SECRET_STRONG_RE.search(body):
                    emit("secrets.key_or_token", epoch, cur_branch, agent, {"marker": "key/token"}, head)
                elif body and not (path and (DOC_PATH_RE.search(path) or TEST_PATH_RE.search(path))) \
                        and any(_value_looks_secret(m.group(1)) for m in SECRET_ASSIGN_RE.finditer(body)):
                    emit("secrets.credential_assignment", epoch, cur_branch, agent, {"marker": "assignment"}, head)
                continue

            if c.name != "Bash":
                continue
            for seg in bash_segments(inp.get("command", "")):
                toks = _toks(seg)
                if not toks:
                    continue
                head0 = redact(seg, 200)
                head = toks[0].split("/")[-1]

                if head == "git":
                    sub, rest = _git_sub(toks)
                    if sub in ("checkout", "switch"):
                        name, created = _branch_from(sub, rest)
                        if _is_branchy(name):
                            if created and tkt:
                                if not PROTECTED_RE.match(name) and not (WORKING_RE.match(name) and tkt.search(name)):
                                    emit("branch.working_no_ticket", epoch, name, agent, {"branch": redact(name, 80)}, head0)
                            cur_branch = name
                    elif sub == "commit":
                        srow["commits"] += 1
                        msg = _val(rest, "-m", "--message") or ""
                        has_ticket = bool(tkt and (tkt.search(msg) or (cur_branch and tkt.search(cur_branch))))
                        if cur_branch and PROTECTED_RE.match(cur_branch):
                            emit("branch.protected_commit", epoch, cur_branch, agent, {"ticket_referenced": has_ticket}, head0)
                        elif tkt and not has_ticket:
                            emit("trace.commit_no_ticket", epoch, cur_branch, agent, {"branch": redact(cur_branch or "", 80)}, head0)
                    elif sub == "push":
                        srow["pushes"] += 1
                        branches, force, explicit = _push_targets(rest)
                        targets = branches or ({cur_branch} if cur_branch else set())
                        hit = sorted(b for b in targets if b and PROTECTED_RE.match(b))
                        if hit:
                            emit("push.protected", epoch, cur_branch, agent,
                                 {"targets": [redact(b, 60) for b in hit], "force": force,
                                  "target_known": explicit or bool(cur_branch)}, head0)
                    elif sub == "add":
                        for a in rest:
                            if not a.startswith("-") and SECRET_PATH_RE.search(a):
                                emit("secrets.git_add_secret", epoch, cur_branch, agent, {"path_shape": "secret-bearing"}, head0)
                    continue

                if head == "gh":
                    gsub = _gh_sub(toks)
                    if gsub[:2] == ["pr", "merge"]:
                        bypass = any(f in toks for f in ("--admin", "--auto"))
                        emit("pr.self_merge", epoch, cur_branch, agent, {"admin_or_auto": bypass}, head0)
                        if bypass:
                            emit("pr.bypass", epoch, cur_branch, agent, {"flags": "admin/auto"}, head0)
                    elif gsub[:2] == ["pr", "review"] and ("--approve" in toks or "-a" in toks):
                        emit("pr.self_approve", epoch, cur_branch, agent, {}, head0)
                    elif gsub[:2] == ["pr", "create"]:
                        srow["prs_created"] += 1
                        if srow["pr_created_epoch"] is None:
                            srow["pr_created_epoch"] = epoch
                        base = _val(toks, "-B", "--base")
                        if base and not PROTECTED_RE.match(base):
                            # a compliant PR targets an integration/release branch (develop, release/*, main);
                            # a non-protected base means stacking on another feature branch (forbidden)
                            emit("pr.base_not_release", epoch, cur_branch, agent, {"base": redact(base, 60)}, head0)
                        if tkt:
                            # look for the issue id in title/body, not in the --base/--head branch values
                            ticket_hay = " ".join(str(_val(toks, n) or "") for n in ("-t", "--title", "-b", "--body")) or seg
                            if not tkt.search(ticket_hay):
                                emit("pr.no_ticket", epoch, cur_branch, agent, {}, head0)
                    continue

                if TEST_RE.search(seg):
                    srow["tests_run"] = True
                for pat, eco in INSTALL_RE:
                    if pat.match(seg) and not RESTORE_RE.search(seg):
                        verb = re.search(r"\b(install|add|i|get)\b", seg)
                        tail = seg[verb.end():] if verb else ""
                        pkgs = [t for t in _toks(tail) if not t.startswith("-") and t not in (".",) and "=" not in t.split("/")[0]]
                        if pkgs:
                            emit("deps.new_dependency", epoch, cur_branch, agent,
                                 {"ecosystem": eco, "packages": [redact(p, 40) for p in pkgs[:8]], "n": len(pkgs)}, head0)
                        break

        srow["pr_without_tests"] = bool(srow["prs_created"] and not srow["tests_run"])
        srow["agents"] = sorted(srow["agents"])
        srow["worst"] = "violation" if srow.get("violation") else ("attention" if srow.get("attention") else None)
        if srow.get("violation") or srow.get("attention") or srow["pr_without_tests"] or srow["commits"] or srow["pushes"] or srow["prs_created"]:
            sess_rows.append(srow)

    # ---- rollups ----
    by_check: dict[str, dict] = {}
    for check, (sev, meaning, ctrl) in CHECKS.items():
        es = [e for e in events if e["check"] == check]
        if not es:
            continue
        by_check[check] = {"severity": sev, "control": ctrl, "meaning": meaning, "n": len(es),
                           "sessions": len({e["session"] for e in es}),
                           "agents": sorted({e["agent"] for e in es}),
                           "examples": [e["head"] for e in es[:5]]}
    by_agent: dict[str, dict] = defaultdict(lambda: {"violation": 0, "attention": 0, "info": 0, "events": 0, "sessions": set()})
    for e in events:
        a = by_agent[e["agent"]]
        a[e["severity"]] = a.get(e["severity"], 0) + 1
        a["events"] += 1
        a["sessions"].add(e["session"])
    for a in by_agent.values():
        a["sessions"] = len(a["sessions"])
    by_project: dict[str, dict] = defaultdict(lambda: {"violation": 0, "attention": 0, "events": 0})
    for e in events:
        p = by_project[e["project"]]
        p[e["severity"]] = p.get(e["severity"], 0) + 1
        p["events"] += 1

    sess_rows.sort(key=lambda r: (SEV_RANK[r["worst"]], r.get("violation", 0), r.get("attention", 0)), reverse=True)
    git_sessions = sum(1 for r in sess_rows if r["commits"] or r["pushes"] or r["prs_created"])
    out = {
        "meta": {
            "sessions_total": sum(1 for s in sessions if getattr(s, "source", "claude_code") == "claude_code"),
            "sessions_with_change_activity": git_sessions,
            "events": len(events),
            "violations": sum(e["severity"] == "violation" for e in events),
            "attention": sum(e["severity"] == "attention" for e in events),
            "since": since.isoformat() if since else None,
            "policy_name": policy_name,
            "ticket_prefix": ticket_prefix or None,
            "checks": {k: {"severity": v[0], "meaning": v[1], "control": v[2]} for k, v in CHECKS.items()},
            "not_judgeable_from_transcripts": list(NOT_JUDGEABLE),
        },
        "by_check": by_check,
        "by_agent": dict(sorted(by_agent.items(), key=lambda kv: -kv[1]["events"])),
        "by_project": dict(sorted(by_project.items(), key=lambda kv: -kv[1]["events"])),
        "sessions": sess_rows[:top],
        "pr_without_tests": [r["session"] for r in sess_rows if r["pr_without_tests"]],
        "_events": events,
    }
    return out
