"""Permission analysis: normalise tool calls into rule keys, match against settings, estimate prompts,
and suggest allowlist rules with a risk tier so nothing dangerous is waved through blindly.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shlex

from .model import PermissionConfig, ToolCall

# Tools that never raise a permission prompt in Claude Code.
NO_PROMPT_TOOLS = {
    "Read", "Glob", "Grep", "LS", "TodoWrite", "TodoRead", "Task", "Agent", "NotebookRead", "ExitPlanMode",
    "EnterPlanMode", "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput", "AskUserQuestion",
    "Skill", "ToolSearch", "KillShell", "BashOutput", "SlashCommand",
}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Approximate: Claude Code treats a small set of read-only shell commands as safe.
SAFE_BASH = {"ls", "pwd", "echo", "cat", "head", "tail", "wc", "which", "whoami", "date", "true", "env", "printenv"}

MULTI_LEVEL = {"git", "npm", "pnpm", "yarn", "bun", "npx", "docker", "docker-compose", "kubectl", "helm", "aws", "cdk",
               "gh", "pip", "pip3", "uv", "poetry", "cargo", "go", "make", "terraform", "dotnet", "brew", "apt", "apt-get",
               "systemctl", "az", "gcloud", "pio", "platformio", "conda", "mvn", "gradle", "./gradlew", "nx", "turbo"}

# Global flags that take a separate value, so the value isn't mistaken for the subcommand.
VALUE_FLAGS = {
    "git": {"-C", "-c", "--git-dir", "--work-tree", "--namespace"},
    "gh": {"-R", "--repo"},
    "npm": {"--prefix", "-w", "--workspace"}, "pnpm": {"-C", "--dir", "-F", "--filter"},
    "yarn": {"--cwd"}, "uv": {"--directory", "--project"}, "make": {"-C", "-f"},
    "docker": {"-H", "--context"}, "kubectl": {"-n", "--namespace", "--context"},
    "aws": {"--profile", "--region"}, "cargo": {"--manifest-path"}, "go": {"-C"},
}

HIGH_RISK = [
    r"^(sudo|su|doas)\b", r"^rm\b", r"^(chmod|chown)\b", r"^(dd|mkfs|fdisk|parted)\b", r"\|\s*(sh|bash|zsh)\b",
    r"^git (push|reset|clean|rebase|filter-branch)\b", r"^git checkout -- ", r"^(terraform|tofu) (apply|destroy|import)\b",
    r"^cdk (deploy|destroy)\b", r"^kubectl (delete|apply|replace|scale|drain)\b", r"^helm (install|upgrade|uninstall)\b",
    r"^docker (system prune|rm|rmi|volume rm)\b", r"^(npm|pnpm|yarn) publish\b", r"^gh (repo delete|release create|pr merge)\b",
    r"^aws \S+ (delete|remove|terminate|put|create|update|deploy|rm|cp|sync|mv)", r"^(kill|pkill|killall)\b",
    r"^(shutdown|reboot)\b", r"^curl\b.*-X\s*(POST|PUT|DELETE|PATCH)",
]
MEDIUM_RISK = [
    r"^(npm|pnpm|yarn|bun) (install|add|i|remove|uninstall|update)\b", r"^(pip3?|uv pip|uv|poetry|conda) (install|add|remove)\b",
    r"^docker (run|build|compose|exec)\b", r"^docker-compose\b", r"^git (commit|checkout|switch|merge|stash|branch|tag|cherry-pick|pull|fetch)\b",
    r"^(mv|cp|ln|mkdir|touch)\b", r"^(curl|wget)\b", r"^(apt|apt-get|brew) ", r"^npx\b", r"^(ssh|scp|rsync)\b",
    r"^systemctl\b", r"^(wine|wine64)\b", r"^gh\b", r"^aws\b", r"^cdk\b", r"^(python3?|node|bash|sh) ",
]
LOW_OVERRIDE = [  # read-only / local-verification commands that would otherwise hit a medium pattern
    r"^git (status|diff|log|show|branch --list|remote -v|rev-parse|blame)\b",
    r"^(aws|gcloud|az) \S+ (ls|list|describe|get)", r"^aws sts get-caller-identity", r"^cdk (synth|diff|ls|list)\b",
    r"^gh (pr|issue|run|repo) (view|list|status|checks|diff)\b", r"^(python3?|uv run|poetry run) (-m )?(pytest|mypy|ruff|black --check)\b",
    r"^(npm|pnpm|yarn|bun) (run )?(test|lint|build|typecheck|check|format:check)\b", r"^docker (ps|images|logs|inspect|info|version)\b",
    r"^(docker compose|docker-compose) (ps|logs|config)\b", r"^systemctl (status|is-active|list-units)\b", r"^npx (tsc|eslint|prettier --check|vitest|jest)\b",
    r"^(terraform|tofu) (plan|validate|fmt -check|show)\b", r"^kubectl (get|describe|logs)\b",
]
_LOW = [re.compile(p) for p in LOW_OVERRIDE]
_HIGH = [re.compile(p) for p in HIGH_RISK]
_MED = [re.compile(p) for p in MEDIUM_RISK]


def split_compound(cmd: str) -> list[str]:
    """Split on unquoted &&, ||, ;, | and newlines, the way Claude Code checks each subcommand.
    Quote-aware so `git commit -m "a; b"` stays one segment. `|&`, `&>` and `2>&1` are not separators."""
    out, buf, q, i, s = [], [], None, 0, cmd or ""
    while i < len(s):
        ch = s[i]
        if q:
            buf.append(ch)
            if ch == "\\" and q == '"' and i + 1 < len(s):
                buf.append(s[i + 1]); i += 2; continue
            if ch == q:
                q = None
            i += 1
            continue
        if ch in "'\"":
            q = ch; buf.append(ch); i += 1; continue
        if ch == "\\" and i + 1 < len(s):
            if s[i + 1] != "\n":  # line continuation joins lines
                buf.append(ch); buf.append(s[i + 1])
            i += 2; continue
        two = s[i:i + 2]
        if two in ("&&", "||"):
            out.append("".join(buf)); buf = []; i += 2; continue
        if ch in ";\n" or (ch == "|" and two != "|&"):
            out.append("".join(buf)); buf = []; i += 1; continue
        buf.append(ch); i += 1
    out.append("".join(buf))
    return [p.strip() for p in out if p.strip()]


def _bash_spec_regex(spec: str) -> re.Pattern:
    """Claude Code Bash rule spec -> regex over one subcommand.
    `x:*` is the legacy prefix form (x, or x followed by a space and anything).
    `*` matches anything, anywhere; a trailing ` *` also matches the bare command (`ls *` matches `ls`).
    Anything else is an exact match."""
    if spec.endswith(":*"):
        return re.compile(re.escape(spec[:-2]) + r"(?:\s.*)?\Z", re.S)
    trailing = spec.endswith(" *")
    body = spec[:-2] if trailing else spec
    pat = ".*".join(re.escape(p) for p in body.split("*"))
    if trailing:
        pat += r"(?:\s.*)?"
    return re.compile(pat + r"\Z", re.S)


_SPEC_CACHE: dict[str, re.Pattern] = {}


def bash_seg_matches(spec: str, seg: str) -> bool:
    rx = _SPEC_CACHE.get(spec)
    if rx is None:
        rx = _SPEC_CACHE[spec] = _bash_spec_regex(spec)
    return bool(rx.match(seg))


def bash_segments(cmd: str) -> list[str]:
    return [_strip_env(s) for s in split_compound(cmd)]


def _strip_env(cmd: str) -> str:
    return re.sub(r"^(?:[A-Z_][A-Z0-9_]*=\S*\s+)+", "", cmd.strip())


def bash_key(cmd: str) -> str:
    """'cd foo && FOO=1 npm run test -- -x' -> 'npm run test' (first non-cd segment)."""
    for seg in split_compound(cmd):
        seg = _strip_env(seg)
        if seg.startswith("cd ") or seg == "cd":
            continue
        try:
            toks = shlex.split(seg, posix=True)
        except ValueError:
            toks = seg.split()
        if not toks:
            continue
        head = toks[0].split("/")[-1] if toks[0].startswith("/") else toks[0]
        if head in ("python", "python3") and len(toks) > 2 and toks[1] == "-m":
            return " ".join([head, "-m", toks[2]])
        if head in MULTI_LEVEL and len(toks) > 1:
            sub, skip = [], False
            vflags = VALUE_FLAGS.get(head, set())
            for t in toks[1:]:
                if skip:
                    skip = False
                    continue
                if t in vflags:  # `git -C <path>`: the value is not a subcommand
                    skip = True
                    continue
                if not t.startswith("-"):
                    sub.append(t)
            depth = 2 if head in ("npm", "pnpm", "yarn", "bun") and sub[:1] == ["run"] else 1
            if head in ("aws", "gcloud", "az") :
                depth = 2
            return " ".join([head] + sub[:depth])
        return head
    return cmd.strip().split(" ")[0] if cmd.strip() else "?"


def risk_of_bash(key_or_cmd: str) -> str:
    c = _strip_env(key_or_cmd)
    if any(p.search(c) for p in _HIGH):
        return "high"
    if any(p.search(c) for p in _LOW):
        return "low"
    if any(p.search(c) for p in _MED):
        return "medium"
    return "low"


def rule_key(tc: ToolCall) -> str:
    """Key used for grouping and as the suggested permission rule."""
    n, i = tc.name, tc.input or {}
    if n == "Bash":
        return f"Bash({bash_key(i.get('command', ''))}:*)"
    if n in ("WebFetch",):
        m = re.match(r"https?://([^/:]+)", i.get("url", "") or "")
        return f"WebFetch(domain:{m.group(1) if m else '?'})"
    if n in EDIT_TOOLS:
        return n
    if n.startswith("mcp__"):
        parts = n.split("__")
        return "__".join(parts[:2])  # whole server
    return n


_RULE_RE = re.compile(r"^([\w\-]+)(?:\((.*)\))?$", re.S)
HOME = os.path.expanduser("~")


def _parse_rule(rule: str):
    m = _RULE_RE.match(rule.strip())
    return (m.group(1), m.group(2)) if m else (None, None)


def _path_matches(spec: str, path: str) -> bool:
    """Path rules: `//abs` is absolute, `~/x` is home-relative. `/x` (relative to the settings file)
    and `./x` / bare `x` (relative to the project) can't be resolved here without the rule's origin,
    so they match as a path suffix — an over-match, stated in the docs."""
    if not path:
        return False
    if spec.startswith("//"):
        return fnmatch.fnmatch(path, spec[1:])
    if spec.startswith("~/"):
        return fnmatch.fnmatch(path, HOME + spec[1:])
    rel = spec[2:] if spec.startswith("./") else spec.lstrip("/")
    return fnmatch.fnmatch(path, "*/" + rel) or fnmatch.fnmatch(path, rel)


def _rule_matches_seg(spec: str | None, seg: str) -> bool:
    return spec is None or spec in ("", "*") or bash_seg_matches(spec, seg)


def _rule_matches(rule: str, tc: ToolCall) -> bool:
    """True if `rule` matches the call. For Bash this means it matches at least one subcommand,
    which is what an allow-rule hit count wants; use would_prompt for the all-subcommands check."""
    tool, spec = _parse_rule(rule)
    if tool is None:
        return False
    if tool.startswith("mcp__"):
        return tc.name == tool or tc.name.startswith(tool + "__")
    if tool != tc.name:
        return False
    if spec is None or spec in ("", "*"):
        return True
    i = tc.input or {}
    if tool == "Bash":
        return any(bash_seg_matches(spec, s) for s in bash_segments(i.get("command", "")))
    if tool == "WebFetch" and spec.startswith("domain:"):
        m = re.match(r"https?://([^/:?#]+)", i.get("url") or "")
        host = m.group(1).lower() if m else ""
        dom = spec[7:].lower()
        if dom.startswith("*."):
            return host.endswith(dom[1:])
        return host == dom
    return _path_matches(spec, i.get("file_path") or i.get("path") or i.get("notebook_path") or "")


def _bash_rules(rules: list[str]) -> list[str | None]:
    out = []
    for r in rules:
        tool, spec = _parse_rule(r)
        if tool == "Bash":
            out.append(spec)
    return out


def _seg_safe(seg: str) -> bool:
    head = seg.split(" ")[0] if seg else ""
    return head in SAFE_BASH or head == "cd"


def would_prompt(tc: ToolCall, cfg: PermissionConfig) -> bool:
    """Best-effort estimate of whether this call raised a permission prompt.

    Order follows Claude Code: deny > ask > allow, and for Bash each subcommand is checked on
    its own. Any denied subcommand denies the call (no prompt); any `ask` subcommand prompts;
    otherwise every subcommand must be allowed by some rule (or be a read-only builtin)."""
    if tc.denial in ("user_rejected",):
        return True
    if tc.name in NO_PROMPT_TOOLS:
        return False
    mode = (cfg.default_mode or "default")
    if mode in ("bypassPermissions", "auto"):  # auto: the classifier decides; its refusals show as classifier_denied
        return False
    if mode == "acceptEdits" and tc.name in EDIT_TOOLS:
        return False
    if tc.name == "Bash":
        segs = bash_segments((tc.input or {}).get("command", ""))
        if not segs:
            return False
        deny, ask, allow = _bash_rules(cfg.deny), _bash_rules(cfg.ask), _bash_rules(cfg.allow)
        if any(_rule_matches_seg(d, s) for d in deny for s in segs):
            return False  # hard-denied, no prompt (shows as rule_denied)
        if any(_rule_matches_seg(a, s) for a in ask for s in segs):
            return True
        return not all(any(_rule_matches_seg(a, s) for a in allow) or _seg_safe(s) for s in segs)
    if any(_rule_matches(r, tc) for r in cfg.deny):
        return False
    if any(_rule_matches(r, tc) for r in cfg.ask):
        return True
    return not any(_rule_matches(r, tc) for r in cfg.allow)


def tool_risk(key: str) -> str:
    if key.startswith("Bash("):
        return risk_of_bash(key[5:-3] if key.endswith(":*)") else key[5:-1])
    if key in EDIT_TOOLS:
        return "medium"
    if key.startswith("WebFetch"):
        return "low"
    if key.startswith("mcp__"):
        return "medium"
    return "medium"
