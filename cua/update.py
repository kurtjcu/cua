"""Version, update and onboarding help for cua.

Remote-agnostic: the "version" is the repo's current git revision and "latest" is
whatever upstream (`@{u}`) the checkout tracks. No remote is assumed or added here;
with no remote (or no tracking branch) the update check stays silent. The once-a-day
throttle uses the mtime of .git/FETCH_HEAD, so it needs no cache file outside the repo.
Every git call is fail-silent with a short timeout, so an offline or non-git checkout
never blocks or errors a cua run.
"""
from __future__ import annotations

import os
import subprocess
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GIT_DIR = os.path.join(REPO, ".git")
FETCH_HEAD = os.path.join(GIT_DIR, "FETCH_HEAD")
FETCH_TTL = 86400  # once a day
TIMEOUT = 5

HELP = """cua — Claude usage audit toolkit

What it is: deterministic, stdlib-only (Python 3.9+) tools that turn raw Claude
Code / claude.ai logs into redacted summaries, then let agents analyse them. Raw
transcripts are read ONLY by `python3 -m cua`; everything agents read lives in out/.

Setup:
  1. git clone <repo> && cd claude-usage-audit
  2. python3 --version           # need 3.9+ (no pip install — pure stdlib)
  3. cp .env.example .env         # set your accounts/paths (see .env.example)
  4. python3 -m cua preflight     # checks your config dirs; opens no transcript

Run an audit (from the repo root):
  Usage audit (compare two accounts):     /cua-audit   (skill: usage-audit)
  SDL / change-management compliance:     /cua-sdl     (skill: sdl-audit)

Commands:
  preflight   check config dirs & exports (opens no transcript)
  scan        analyse one account -> out/<account>/summary.json, samples.jsonl
  sdl         SDL / change-management compliance signals -> out/<account>/sdl.json
  waits       where sessions sit idle (permission waits, pauses)
  lint        audit CLAUDE.md, skills, agents, hooks, settings, memory
  compare     compare two scanned accounts
  version     show the installed version (git revision)
  update      update to the latest version (git pull --ff-only)
  help        this guide

Outputs go to out/ (gitignored). Nothing is applied to your real settings —
proposals land in out/proposed/ for you to review and apply yourself.

Updates: cua checks at most once a day on a run and prints a one-line notice when a
newer version is available. Run /cua-update (python3 -m cua update) to pull it.
Set CUA_NO_UPDATE_CHECK=1 to silence the check."""


def _git(*args: str, timeout: int = TIMEOUT) -> tuple[int, str, str]:
    try:
        p = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.SubprocessError):
        return 1, "", "git unavailable"


def is_repo() -> bool:
    return os.path.isdir(GIT_DIR)


def has_remote() -> bool:
    rc, out, _ = _git("remote")
    return rc == 0 and bool(out)


def upstream_ref() -> str | None:
    rc, out, _ = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    return out or None if rc == 0 else None


def current_version() -> str:
    """The release version from cua/__init__.py, with the git revision appended when
    this is a git checkout (so a dev build is distinguishable from a clean release)."""
    from . import __version__
    rc, sha, _ = _git("rev-parse", "--short", "HEAD")
    if rc != 0 or not sha:
        return __version__
    _, date, _ = _git("show", "-s", "--format=%cs", "HEAD")
    return f"{__version__} ({sha}{f', {date}' if date else ''})"


def should_fetch(now: float | None = None, ttl: int = FETCH_TTL) -> bool:
    """True if we have never fetched or the last fetch is older than ttl."""
    try:
        return (now or time.time()) - os.path.getmtime(FETCH_HEAD) >= ttl
    except OSError:
        return True


def behind_ahead() -> tuple[int, int] | None:
    """(ahead, behind) of the local HEAD vs its upstream, or None if not comparable."""
    up = upstream_ref()
    if not up:
        return None
    rc, out, _ = _git("rev-list", "--left-right", "--count", f"HEAD...{up}")
    if rc != 0:
        return None
    try:
        ahead, behind = (int(x) for x in out.split())
        return ahead, behind
    except ValueError:
        return None


def format_notice(behind: int) -> str:
    if behind and behind > 0:
        return f"cua: {behind} update{'' if behind == 1 else 's'} available — run /cua-update (python3 -m cua update)"
    return ""


def check(throttle: bool = True, do_fetch: bool = True) -> dict:
    """Fail-silent snapshot of update state. Fetches (throttled) then compares to upstream."""
    if not is_repo():
        return {"repo": False, "version": current_version()}
    res = {"repo": True, "remote": has_remote(), "version": current_version(),
           "upstream": upstream_ref(), "fetched": False, "ahead": 0, "behind": 0}
    if not res["remote"]:
        return res
    if do_fetch and (not throttle or should_fetch()):
        rc, _, _ = _git("fetch", "--quiet", timeout=TIMEOUT)
        res["fetched"] = rc == 0
    ba = behind_ahead()
    if ba:
        res["ahead"], res["behind"] = ba
    return res


def notice(throttle: bool = True) -> str:
    """One-line 'update available' string for printing on a run, or '' — never raises."""
    if os.environ.get("CUA_NO_UPDATE_CHECK"):
        return ""
    try:
        return format_notice(check(throttle=throttle).get("behind", 0))
    except Exception:
        return ""


def run_update() -> tuple[int, str]:
    if not is_repo():
        return 1, "not a git repository — clone the repo to use updates"
    if not has_remote():
        return 1, "no git remote configured; add one (origin) to enable updates"
    _git("fetch", "--quiet", timeout=30)
    rc, out, err = _git("pull", "--ff-only", timeout=30)
    return (0 if rc == 0 else 1), (out or err or "done")
