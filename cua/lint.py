"""Config-layer lint: CLAUDE.md files, skills, agents, commands, hooks, settings, memory.

Reads configuration files only. It never opens a transcript: under
<claude-dir>/projects/ it touches only the */memory/*.md files.

Findings are heuristics. Each says what it saw so a person can judge it; none
of them change anything.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field

from .permissions import bash_seg_matches

CHARS_PER_TOKEN = 4  # rough; good enough to rank files against each other
MAX_INSTR_LINES = 200  # Anthropic's suggested ceiling per CLAUDE.md
MEMORY_LOAD_LINES = 200  # Claude Code loads the first 200 lines of MEMORY.md
MEMORY_LOAD_BYTES = 25_000
SKILL_DESC_LIMIT = 1024  # hard limit in the skill spec
SKILL_DESC_LONG = 400
SKILL_BODY_LINES = 500  # authoring guidance: keep SKILL.md under 500 lines
AGENT_LONG_LINES = 600
MIN_DUP_LINE = 50  # shorter lines repeat by chance (headings, "## Rules")

PRUNE_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
              ".next", "target", ".planning", ".claude", ".cache", "site-packages"}
NETWORK_RE = re.compile(r"\b(curl|wget|fetch\(|https?://|http\.request|https\.request|urllib|requests\.|net\.connect|nc\s)")
DECIDES_RE = re.compile(r"exit\s+2\b|process\.exit\(2\)|permissionDecision|\"decision\"\s*:|decision:\s*['\"](block|approve|deny|allow)")
LOGS_RE = re.compile(r"appendFile|writeFile|createWriteStream|>>\s*\S|open\([^)]*['\"]a['\"]|logging\.|\.log\b")
SCRIPT_RE = re.compile(r"""(?<![\w./~$-])([~./$\w][^\s"'`;|&()]*\.(?:js|cjs|mjs|sh|py|ts))\b""")
SHELL_OPS_RE = re.compile(r"[;|&<>]|\$\(")
SKILL_SHELL_RE = re.compile(r"!`[^`\n]+`")
MCP_REF_RE = re.compile(r"mcp__([A-Za-z0-9_-]+?)__")
CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
IMPORT_RE = re.compile(r"(?<![\w`/@])@((?:~/|\.{1,2}/|/)?[\w.-]+(?:/[\w.-]+)*)")
IMPORT_FILE_RE = re.compile(r"\.(md|markdown|txt)$")  # unresolved refs without these are likely @scope/pkg


@dataclass
class Finding:
    check: str
    severity: str  # warn | info
    path: str
    message: str
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------- helpers

def _read(p: str) -> str:
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _tok(chars: int) -> int:
    return round(chars / CHARS_PER_TOKEN)


def _short(p: str) -> str:
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if p == home or p.startswith(home + "/") else p


def frontmatter(text: str) -> dict:
    """Minimal YAML frontmatter: `key: value`, quoted values, and `key: |`/`>` blocks."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out: dict = {}
    key = None
    block: list[str] = []
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m and not line.startswith((" ", "\t")):
            if key is not None and block:
                out[key] = " ".join(b.strip() for b in block).strip()
            key, val = m.group(1), m.group(2).strip()
            block = []
            if val in ("|", ">", "|-", ">-", ""):
                out[key] = ""
            else:
                out[key] = val.strip("'\"")
                key = None
        elif key is not None:
            block.append(line)
    if key is not None and block:
        out[key] = " ".join(b.strip() for b in block).strip()
    return out


def body_lines(text: str) -> int:
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def strip_code(text: str) -> str:
    return INLINE_CODE_RE.sub("", CODE_FENCE_RE.sub("", text))


def memory_dir_name(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _norm_line(line: str) -> str:
    s = re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", line.strip())
    s = re.sub(r"[*_`]", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


# ---------------------------------------------------------------- discovery

def discover_projects(roots: list[str], max_depth: int = 3) -> list[str]:
    """Dirs that carry any project-level Claude config. Does not follow symlinks."""
    found = []
    seen = set()
    for root in roots:
        root = os.path.realpath(os.path.expanduser(root))
        base = root.count(os.sep)
        for dp, dns, fns in os.walk(root):
            depth = dp.count(os.sep) - base
            names = set(fns) | set(dns)
            if names & {"CLAUDE.md", "CLAUDE.local.md", ".mcp.json"} or ".claude" in dns:
                rp = os.path.realpath(dp)
                if rp not in seen and rp != os.path.realpath(os.path.expanduser("~")):
                    seen.add(rp)
                    found.append(rp)
            dns[:] = [] if depth >= max_depth else [d for d in dns if d not in PRUNE_DIRS and not d.startswith(".")]
    return sorted(found)


def instruction_files(project: str, global_dir: str) -> list[str]:
    """CLAUDE.md files Claude Code loads at startup for a session in `project`."""
    files = [os.path.join(global_dir, "CLAUDE.md")]
    parts = project.strip(os.sep).split(os.sep)
    for i in range(1, len(parts) + 1):
        d = os.sep + os.sep.join(parts[:i])
        for name in ("CLAUDE.md", "CLAUDE.local.md", os.path.join(".claude", "CLAUDE.md")):
            files.append(os.path.join(d, name))
    out, seen = [], set()
    for f in files:
        if os.path.isfile(f):
            rp = os.path.realpath(f)
            if rp not in seen:
                seen.add(rp)
                out.append(f)
    return out


def resolve_imports(path: str, depth: int = 0, seen: set | None = None) -> tuple[list[str], list[str]]:
    """(@imports that resolve, @imports that don't). Code spans and fences are ignored,
    as Claude Code ignores them."""
    seen = seen if seen is not None else {os.path.realpath(path)}
    ok, missing = [], []
    if depth >= 5:
        return ok, missing
    for ref in IMPORT_RE.findall(strip_code(_read(path))):
        ref = ref.rstrip(".")
        if "/" not in ref and "." not in ref:
            continue  # @mentions, @latest
        tgt = os.path.expanduser(ref) if ref.startswith("~") else os.path.join(os.path.dirname(path), ref)
        tgt = os.path.normpath(tgt)
        if not os.path.isfile(tgt):
            if IMPORT_FILE_RE.search(ref):
                missing.append(ref)
            continue
        rp = os.path.realpath(tgt)
        if rp in seen:
            continue
        seen.add(rp)
        ok.append(tgt)
        o2, m2 = resolve_imports(tgt, depth + 1, seen)
        ok += o2
        missing += m2
    return ok, missing


def _md_entries(d: str, pattern: str) -> list[str]:
    """SKILL.md under d/*/ (pattern 'skill') or *.md directly in d (pattern 'flat')."""
    if not os.path.isdir(d):
        return []
    out = []
    for e in sorted(os.scandir(d), key=lambda e: e.name):
        if e.name.startswith("."):
            continue
        if pattern == "skill" and e.is_dir():
            sk = os.path.join(e.path, "SKILL.md")
            if os.path.isfile(sk):
                out.append(sk)
        elif pattern == "flat" and e.is_file() and e.name.endswith(".md"):
            out.append(e.path)
    return out


def plugin_dirs(global_dir: str, settings: dict) -> list[str]:
    enabled = {k for k, v in (settings.get("enabledPlugins") or {}).items() if v}
    meta = _load_json(os.path.join(global_dir, "plugins", "installed_plugins.json")) or {}
    out = []
    for key, recs in (meta.get("plugins") or {}).items():
        if key not in enabled:
            continue
        for r in recs if isinstance(recs, list) else [recs]:
            p = (r or {}).get("installPath")
            if p and os.path.isdir(p):
                out.append(p)
    return out


def _load_json(p: str):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- artifacts

@dataclass
class Artifact:
    kind: str  # skill | agent | command
    name: str
    path: str
    scope: str  # global | plugin | project
    description: str
    lines: int
    fm: dict

    @property
    def listing_tokens(self) -> int:
        return _tok(len(self.name) + len(self.description) + 4)


def load_artifacts(d: str, kind: str, scope: str) -> list[Artifact]:
    pattern = "skill" if kind == "skill" else "flat"
    out = []
    for p in _md_entries(d, pattern):
        text = _read(p)
        fm = frontmatter(text)
        name = fm.get("name") or (os.path.basename(os.path.dirname(p)) if kind == "skill" else os.path.basename(p)[:-3])
        desc = fm.get("description", "")
        if kind == "command" and not desc:
            body = text.split("\n---", 1)[-1] if text.startswith("---") else text
            desc = next((l.strip() for l in body.splitlines() if l.strip()), "")[:120]
        out.append(Artifact(kind, name, p, scope, desc, body_lines(text), fm))
    return out


# ---------------------------------------------------------------- checks

def check_instruction_file(path: str, findings: list[Finding]) -> None:
    text = _read(path)
    n = body_lines(text)
    if n > MAX_INSTR_LINES:
        findings.append(Finding("instructions.size", "warn", path,
                                f"{n} lines (~{_tok(len(text))} tokens); guidance is under {MAX_INSTR_LINES} per file",
                                {"lines": n, "tokens": _tok(len(text))}))
    missing = []
    for span in INLINE_CODE_RE.findall(CODE_FENCE_RE.sub("", text)):
        s = span.strip()
        # /x alone is usually a slash command, not a path
        if not re.match(r"^(~/|/[\w.-]+/)[\w./-]*$", s) or "..." in s:
            continue
        if not os.path.exists(os.path.expanduser(s)):
            missing.append(s)
    if missing:
        findings.append(Finding("instructions.missing_path", "info", path,
                                f"{len(missing)} path(s) in code spans not found on this host "
                                "(some may exist only inside a box)", {"paths": sorted(set(missing))[:20]}))
    _, bad = resolve_imports(path)
    if bad:
        findings.append(Finding("instructions.broken_import", "warn", path,
                                f"{len(bad)} @import(s) don't resolve", {"imports": bad[:20]}))


def check_duplication(global_md: str, files: list[str], findings: list[Finding]) -> None:
    glines = {_norm_line(l) for l in _read(global_md).splitlines()}
    glines = {l for l in glines if len(l) >= MIN_DUP_LINE}
    by_hash: dict[str, list[str]] = {}
    line_files: dict[str, set] = {}
    for f in files:
        text = _read(f)
        by_hash.setdefault(hashlib.sha1(text.encode()).hexdigest(), []).append(f)
    for h, group in by_hash.items():
        if len(group) > 1:
            findings.append(Finding("instructions.identical_files", "warn", group[0],
                                    f"{len(group)} byte-identical copies", {"copies": group}))
        text = _read(group[0])
        lines = {_norm_line(l) for l in text.splitlines()}
        lines = {l for l in lines if len(l) >= MIN_DUP_LINE}
        dup = sorted(lines & glines)
        if dup:
            findings.append(Finding("instructions.repeats_global", "info", group[0],
                                    f"{len(dup)} line(s) also in the global CLAUDE.md, which loads anyway",
                                    {"lines": len(dup), "sample": dup[:3]}))
        for l in lines - glines:
            line_files.setdefault(l, set()).add(group[0])
    common = sorted(((len(v), k) for k, v in line_files.items() if len(v) >= 3), reverse=True)
    for n, line in common[:15]:
        findings.append(Finding("instructions.repeated_across_projects", "info", "",
                                f"line in {n} project files: \"{line[:100]}\"",
                                {"files": sorted(line_files[line])}))


def check_artifacts(arts: list[Artifact], known_mcp: set[str] | None, findings: list[Finding]) -> None:
    by_name: dict[tuple, list[Artifact]] = {}
    for a in arts:
        by_name.setdefault((a.kind, a.name), []).append(a)
        if a.kind == "skill":
            if not a.description:
                findings.append(Finding("skill.no_description", "warn", a.path,
                                        "no description: the model can't tell when to use it"))
            elif len(a.description) > SKILL_DESC_LIMIT:
                findings.append(Finding("skill.description_too_long", "warn", a.path,
                                        f"description is {len(a.description)} chars (limit {SKILL_DESC_LIMIT})"))
            elif len(a.description) > SKILL_DESC_LONG:
                findings.append(Finding("skill.description_long", "info", a.path,
                                        f"description is {len(a.description)} chars; it loads in every session",
                                        {"chars": len(a.description)}))
            if a.description and not re.search(r"\b(use|load|trigger|invoke)\w*\b.{0,40}\b(when|for|if|whenever|before|after)\b|\bwhen\b",
                                                a.description, re.I):
                findings.append(Finding("skill.no_when_clause", "info", a.path,
                                        "description says what, not when to use it"))
            if a.lines > SKILL_BODY_LINES:
                findings.append(Finding("skill.long_body", "info", a.path,
                                        f"SKILL.md is {a.lines} lines; guidance is under {SKILL_BODY_LINES}, rest in references"))
            dirname = os.path.basename(os.path.dirname(a.path))
            if a.fm.get("name") and a.fm["name"] != dirname:
                findings.append(Finding("skill.name_mismatch", "info", a.path,
                                        f"frontmatter name '{a.fm['name']}' differs from folder '{dirname}'"))
        if a.kind in ("skill", "command"):
            shell = SKILL_SHELL_RE.findall(_read(a.path))
            if shell:
                findings.append(Finding(f"{a.kind}.runs_shell", "info", a.path,
                                        f"{len(shell)} !`cmd` block(s) run before the model reads the file",
                                        {"commands": [s[2:-1][:80] for s in shell[:5]]}))
        if a.kind == "agent":
            if "tools" in a.fm and not a.fm["tools"].strip():
                findings.append(Finding("agent.empty_tools", "warn", a.path,
                                        "`tools:` is present but empty; set the list or drop the key"))
            if a.lines > AGENT_LONG_LINES:
                findings.append(Finding("agent.long", "info", a.path, f"{a.lines} lines of system prompt",
                                        {"lines": a.lines}))
            if known_mcp is not None:
                unknown = sorted({m for m in MCP_REF_RE.findall(_read(a.path))
                                  if m not in known_mcp and len(m) > 1})  # mcp__X__ is a placeholder
                if unknown:
                    findings.append(Finding("agent.unknown_mcp", "info", a.path,
                                            f"names MCP server(s) not configured anywhere: {', '.join(unknown)}"))
    agents = [a for a in arts if a.kind == "agent" and a.scope == "global"]
    no_model = [a.name for a in agents if not a.fm.get("model")]
    if no_model:
        findings.append(Finding("agent.no_model", "info", os.path.dirname(agents[0].path),
                                f"{len(no_model)} of {len(agents)} global agents set no model: and inherit the session model",
                                {"agents": no_model}))
    for (kind, name), group in by_name.items():
        scopes = {a.scope for a in group}
        if len(group) > 1 and "project" in scopes and scopes & {"global", "plugin"}:
            texts = {hashlib.sha1(_read(a.path).encode()).hexdigest() for a in group}
            proj = [a for a in group if a.scope == "project"]
            findings.append(Finding(f"{kind}.shadowed", "warn" if len(texts) > 1 else "info", proj[0].path,
                                    f"project {kind} '{name}' overrides the {'/'.join(sorted(scopes - {'project'}))} one"
                                    + (" and differs from it" if len(texts) > 1 else " (identical copy)"),
                                    {"copies": [a.path for a in group]}))


def _rule_parts(rule: str):
    m = re.match(r"^([\w*-]+)(?:\((.*)\))?$", rule.strip())
    return (m.group(1), m.group(2)) if m else (rule, None)


def subsumes(a: str, b: str) -> bool:
    """True when allow rule `a` already covers everything `b` covers."""
    ta, sa = _rule_parts(a)
    tb, sb = _rule_parts(b)
    if a == b or ta != tb:
        return False
    if sa is None or sa in ("*", "**"):
        return sb is not None
    if sb is None:
        return False
    # Not judged: whether `x *` also matches a bare `x` (keep both to be safe), and
    # compound commands, which Claude Code splits before matching.
    if ta == "Bash":
        if SHELL_OPS_RE.search(sb):
            return False
        if sa.endswith(":*") or sa.endswith(" *"):
            word = sa[:-2]
            if "*" in word:
                return False
            pb = sb[:-2] if sb.endswith((":*", " *")) else sb
            return pb != word and pb.startswith(word + " ")
        if "*" not in sb:
            return bash_seg_matches(sa, sb)
        if sa.endswith("*") and "*" not in sa[:-1]:
            return sb.startswith(sa[:-1])
        return False
    if sa.endswith(":*") and "*" not in sa[:-2]:
        pb = sb[:-2] if sb.endswith(":*") else sb
        return pb != sa[:-2] and sb.startswith(sa[:-2])
    if sa.endswith("/**") and "*" not in sa[:-3]:
        return sb.startswith(sa[:-2])
    if "*" not in sb:
        return re.fullmatch(_glob_re(sa), sb) is not None
    return False


def _glob_re(glob: str) -> str:
    """gitignore-style: * stays inside one path segment, ** crosses them."""
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return "".join(out)


def check_settings(path: str, cfg: dict, global_perm: dict | None, findings: list[Finding]) -> None:
    perm = cfg.get("permissions") or {}
    allow = [r for r in perm.get("allow") or [] if isinstance(r, str)]
    deny = [r for r in perm.get("deny") or [] if isinstance(r, str)]
    ask = [r for r in perm.get("ask") or [] if isinstance(r, str)]
    for name, lst in (("allow", allow), ("deny", deny), ("ask", ask)):
        dups = sorted({r for r in lst if lst.count(r) > 1})
        if dups:
            findings.append(Finding("settings.duplicate_rule", "info", path, f"{len(dups)} duplicate {name} rule(s)",
                                    {"rules": dups}))
    both = sorted(set(allow) & set(deny))
    if both:
        findings.append(Finding("settings.allow_and_deny", "warn", path,
                                f"{len(both)} rule(s) in both allow and deny (deny wins)", {"rules": both}))
    red = sorted({(b, a) for b in allow for a in allow if subsumes(a, b)})
    if red:
        findings.append(Finding("settings.redundant_allow", "info", path,
                                f"{len(red)} allow rule(s) already covered by a broader one",
                                {"pairs": [f"{b}  <-  {a}" for b, a in red[:20]]}))
    if ask:
        findings.append(Finding("settings.ask_rules", "info", path,
                                f"{len(ask)} ask rule(s); no allow rule anywhere can override them", {"rules": ask[:20]}))
    mode = perm.get("defaultMode") or cfg.get("defaultMode")
    if mode == "bypassPermissions":
        findings.append(Finding("settings.bypass_mode", "warn", path, "defaultMode is bypassPermissions"))
    elif mode in ("acceptEdits", "auto"):
        findings.append(Finding("settings.default_mode", "info", path, f"defaultMode is {mode}"))
    if global_perm is not None:
        gdeny, gallow = set(global_perm.get("deny") or []), set(global_perm.get("allow") or [])
        if deny and len(set(deny) & gdeny) >= max(5, 0.8 * len(deny)):
            findings.append(Finding("settings.copies_global_deny", "info", path,
                                    f"{len(set(deny) & gdeny)} of {len(deny)} deny rules repeat the global list, which applies anyway"))
        again = sorted(set(allow) & gallow)
        if len(again) >= 5:
            findings.append(Finding("settings.repeats_global_allow", "info", path,
                                    f"{len(again)} allow rules repeat the global list", {"sample": again[:10]}))
    broad = [r for r in allow if _rule_parts(r)[1] in (None, "*", "**", "//**", "/**")
             and _rule_parts(r)[0] in ("Bash", "Write", "Edit", "WebFetch")]
    if broad:
        findings.append(Finding("settings.broad_allow", "warn", path, f"broad allow rule(s): {', '.join(broad)}"))


def _scripts_in(cmd: str, project_dir: str | None) -> list[str]:
    if project_dir:
        cmd = re.sub(r"\$\{?CLAUDE_PROJECT_DIR\}?", project_dir, cmd)
    out = []
    for p in SCRIPT_RE.findall(cmd):
        if "$" in p:
            continue  # other variables: can't resolve offline
        if p.startswith("~"):
            p = os.path.expanduser(p)
        elif not p.startswith("/"):
            if not project_dir:
                continue  # relative to an unknown cwd
            p = os.path.join(project_dir, p)
        out.append(os.path.normpath(p))
    return out


def hook_scripts(cfg: dict, project_dir: str | None = None) -> list[tuple[str, str, str]]:
    """(event, matcher, script path) for every hook command that names a script file.
    The statusLine command is listed under event 'statusLine'."""
    out = []
    for event, groups in (cfg.get("hooks") or {}).items():
        for g in groups if isinstance(groups, list) else []:
            for h in g.get("hooks") or []:
                for p in _scripts_in(h.get("command") or "", project_dir):
                    out.append((event, g.get("matcher") or "*", p))
    sl = cfg.get("statusLine")
    if isinstance(sl, dict):
        for p in _scripts_in(sl.get("command") or "", project_dir):
            out.append(("statusLine", "-", p))
    return out


def check_hooks(hooks_dir: str, settings_files: list[tuple[str, dict]], findings: list[Finding]) -> list[dict]:
    wired: dict[str, list[str]] = {}
    gdir = os.path.realpath(os.path.dirname(hooks_dir))
    for sp, cfg in settings_files:
        pdir = None if os.path.realpath(os.path.dirname(sp)) == gdir else os.path.dirname(os.path.dirname(sp))
        for event, matcher, script in hook_scripts(cfg, pdir):
            wired.setdefault(os.path.realpath(script), []).append(f"{event}[{matcher}] in {_short(sp)}")
            if not os.path.isfile(script):
                findings.append(Finding("hooks.missing_script", "warn", sp,
                                        f"{event} hook runs {script}, which doesn't exist"))
    inventory = []
    if os.path.isdir(hooks_dir):
        for e in sorted(os.scandir(hooks_dir), key=lambda e: e.name):
            if not e.is_file():
                continue
            if re.search(r"\.(bak|orig|old)\b|~$|\.bak-", e.name):
                findings.append(Finding("hooks.backup_file", "info", e.path, "backup copy in the hooks dir"))
                continue
            if not e.name.endswith((".js", ".cjs", ".mjs", ".sh", ".py", ".ts")):
                continue
            text = _read(e.path)
            rp = os.path.realpath(e.path)
            row = {"file": e.name, "wired": wired.get(rp, []),
                   "decides": bool(DECIDES_RE.search(text)), "logs": bool(LOGS_RE.search(text)),
                   "network": bool(NETWORK_RE.search(text))}
            inventory.append(row)
            if not row["wired"] and e.name.endswith((".js", ".cjs", ".mjs", ".sh", ".py")):
                # helpers run by other hooks are not wired themselves; name who mentions it
                stem = os.path.splitext(e.name)[0]
                refs = sorted(o.name for o in os.scandir(hooks_dir)
                              if o.is_file() and o.name != e.name and not re.search(r"\.(bak|orig)", o.name)
                              and o.name.endswith((".js", ".cjs", ".mjs", ".sh", ".py"))
                              and stem in _read(o.path))
                findings.append(Finding("hooks.unwired", "info", e.path,
                                        "not run by any settings file" + (f"; mentioned by {', '.join(refs)}" if refs
                                                                           else " and not mentioned by any other hook"),
                                        {"files": refs}))
                row["mentioned_by"] = refs
            if row["wired"] and row["decides"] and not row["logs"]:
                findings.append(Finding("hooks.decides_without_log", "info", e.path,
                                        "blocks or approves calls but writes no log, so its decisions can't be counted",
                                        {"wired": row["wired"]}))
            if row["wired"] and row["network"]:
                findings.append(Finding("hooks.network", "info", e.path,
                                        "mentions network calls (curl/fetch/URLs); check what it sends"))
    return inventory


def check_memory(projects_dir: str, findings: list[Finding]) -> dict[str, dict]:
    """Per memory dir: MEMORY.md size and index consistency. Opens only */memory/*.md."""
    out: dict[str, dict] = {}
    if not os.path.isdir(projects_dir):
        return out
    for e in sorted(os.scandir(projects_dir), key=lambda e: e.name):
        md = os.path.join(e.path, "memory")
        if not os.path.isdir(md):
            continue
        files = sorted(n for n in os.listdir(md) if n.endswith(".md"))
        idx = os.path.join(md, "MEMORY.md")
        if not files:
            findings.append(Finding("memory.empty", "info", md, "memory dir with no files"))
            continue
        if "MEMORY.md" not in files:
            findings.append(Finding("memory.no_index", "warn", md,
                                    f"{len(files)} memory file(s) but no MEMORY.md, so none of them load"))
            continue
        text = _read(idx)
        n = body_lines(text)
        loaded = "\n".join(text.splitlines()[:MEMORY_LOAD_LINES])[:MEMORY_LOAD_BYTES]
        out[e.name] = {"lines": n, "bytes": len(text.encode()), "tokens": _tok(len(loaded)), "files": len(files) - 1}
        if n > MEMORY_LOAD_LINES or len(text.encode()) > MEMORY_LOAD_BYTES:
            findings.append(Finding("memory.index_truncated", "warn", idx,
                                    f"MEMORY.md is {n} lines / {len(text.encode())} bytes; only the first "
                                    f"{MEMORY_LOAD_LINES} lines / {MEMORY_LOAD_BYTES // 1000} KB load"))
        elif n and len(text) / n > 200:
            findings.append(Finding("memory.index_dense", "info", idx,
                                    f"{len(text) // n} chars per index line; the index should hold one-line pointers",
                                    {"tokens": _tok(len(text))}))
        links = set(re.findall(r"\]\(([^)#\s]+\.md)\)", text))
        dangling = sorted(l for l in links if not os.path.isfile(os.path.join(md, l)))
        if dangling:
            findings.append(Finding("memory.dangling_link", "warn", idx, f"{len(dangling)} index link(s) to missing files",
                                    {"links": dangling}))
        orphans = sorted(f for f in files if f != "MEMORY.md" and f not in links)
        if orphans:
            findings.append(Finding("memory.orphan", "info", md,
                                    f"{len(orphans)} memory file(s) not linked from MEMORY.md", {"files": orphans[:20]}))
    return out


def mcp_servers(global_dir: str, claude_json: str | None, projects: list[str], plugins: list[str]) -> dict:
    """Server names only, from ~/.claude.json, project .mcp.json and plugin .mcp.json."""
    out = {"global": [], "by_project": {}}
    cj = _load_json(claude_json) if claude_json else None
    if isinstance(cj, dict):
        out["global"] = sorted((cj.get("mcpServers") or {}).keys())
        for p, rec in (cj.get("projects") or {}).items():
            names = sorted(((rec or {}).get("mcpServers") or {}).keys())
            if names:
                out["by_project"][os.path.realpath(p)] = names
    for p in projects:
        mj = _load_json(os.path.join(p, ".mcp.json"))
        if isinstance(mj, dict):
            names = sorted((mj.get("mcpServers") or {}).keys())
            out["by_project"].setdefault(p, [])
            out["by_project"][p] = sorted(set(out["by_project"][p]) | set(names))
    for pd in plugins:
        mj = _load_json(os.path.join(pd, ".mcp.json"))
        if isinstance(mj, dict):
            out["global"] = sorted(set(out["global"]) | set((mj.get("mcpServers") or {}).keys()))
    return out


# ---------------------------------------------------------------- retirement

# Command names that are also ordinary words or built-in commands: only `/name` counts as a reference,
# and typed counts may include the built-in of the same name.
COMMON_WORDS = {"status", "docs", "readme", "resume", "onboard", "catchup", "audit", "help", "review",
                "config", "init", "clear", "compact", "model", "usage", "loop", "stream1", "stream2", "stream3"}
BUILTIN_COMMANDS = {"resume", "status", "help", "review", "config", "init", "clear", "compact", "model",
                    "usage", "loop", "permissions", "memory", "agents", "doctor", "cost", "context"}

def gsd_clusters(global_dir: str) -> dict[str, list[str]]:
    """skill name -> GSD surface clusters, parsed from gsd-core's clusters.cjs (text only)."""
    text = _read(os.path.join(global_dir, "gsd-core", "bin", "lib", "clusters.cjs"))
    out: dict[str, list[str]] = {}
    for cluster, body in re.findall(r"(\w+):\s*Object\.freeze\(\[(.*?)\]\)", text, re.S):
        for stem in re.findall(r"'([\w-]+)'", body):
            out.setdefault(f"gsd-{stem}", []).append(cluster)
    return out


def reference_corpus(global_dir: str, extra: list[str]) -> list[tuple[str, str]]:
    """(path, text) of prose that can chain one skill into another: skill bodies, agents,
    commands and GSD workflow/reference markdown. Code (gsd-core/bin) is excluded because its
    registries name every skill."""
    files = []
    for sub in ("skills", "agents", "commands"):
        d = os.path.join(global_dir, sub)
        for dp, dns, fns in os.walk(d):
            dns[:] = [x for x in dns if not x.startswith(".") and x != "synced"]
            files += [os.path.join(dp, f) for f in fns if f.endswith(".md")]
    gc = os.path.join(global_dir, "gsd-core")
    for sub in ("workflows", "references", "commands", "contexts", "templates", "agents"):
        for dp, _, fns in os.walk(os.path.join(gc, sub)):
            files += [os.path.join(dp, f) for f in fns if f.endswith(".md")]
    files += extra
    return [(f, _read(f)) for f in sorted(set(files))]


def retirement(arts: list[Artifact], usage: dict, clusters: dict[str, list[str]],
               corpus: list[tuple[str, str]]) -> list[dict]:
    """One row per skill/command name: use, cost, dependents, and a suggested verdict."""
    by_name: dict[str, list[Artifact]] = {}
    for a in arts:
        if a.kind in ("skill", "command"):
            by_name.setdefault(a.name, []).append(a)
    rows = []
    for name, group in sorted(by_name.items()):
        u = usage.get(name) or {}
        uses30 = u.get("typed", 0) + u.get("model", 0) + u.get("subagent", 0)
        hist = u.get("history_typed", 0)
        own = {os.path.realpath(a.path) for a in group}
        # a skill's own folder tree (references/, scripts/) doesn't count as a dependent
        own_roots = tuple(os.path.dirname(p) + os.sep for p in own if p.endswith("SKILL.md"))
        stem = name[4:] if name.startswith("gsd-") else None
        generic = "-" not in name and name.lower() in COMMON_WORDS
        pats = [re.compile(r"(?<![\w-])/" + re.escape(name) + r"(?![\w-])")] if generic else \
            [re.compile(r"(?<![\w-])/?" + re.escape(name) + r"(?![\w-])")]
        if stem:
            pats.append(re.compile(r"(?<![\w-])gsd:" + re.escape(stem) + r"(?![\w-])"))
        def not_a_dependent(p: str) -> bool:
            rp = os.path.realpath(p)
            if rp in own or rp.startswith(own_roots or ("\0",)):
                return True
            # GSD's own source/implementation for this command, and menus that list every command
            # (ns-* routers, /gsd-help modes). None of these chain the command into other work.
            if not stem:
                return False
            if "/commands/gsd/" in rp and (rp.endswith(f"/{stem}.md") or os.path.basename(rp).startswith("ns-")):
                return True
            if "/gsd-core/workflows/" in rp and (rp.endswith(f"/workflows/{stem}.md") or "/workflows/help/" in rp
                                                 or f"/workflows/{stem}/" in rp):
                return True
            return "/skills/gsd-ns-" in rp
        refs = sorted({_short(p) for p, t in corpus if not not_a_dependent(p) and any(rx.search(t) for rx in pats)})
        cl = clusters.get(name, [])
        if name.lower() in BUILTIN_COMMANDS and not (u.get("model") or u.get("subagent")):
            verdict = "unclear: typed counts may be the built-in"
        elif uses30 or hist:
            verdict = "keep"
        elif "core_loop" in cl:
            verdict = "keep (core loop)"
        elif refs:
            verdict = "unused, but referenced: check before retiring"
        else:
            verdict = "retire candidate"
        rows.append({
            "name": name, "scopes": sorted({a.scope for a in group}),
            "paths": [_short(a.path) for a in group], "clusters": cl,
            "typed": u.get("typed", 0), "model": u.get("model", 0), "subagent": u.get("subagent", 0),
            "history_typed": hist, "last": u.get("last"),
            "listing_tokens": max(a.listing_tokens for a in group), "body_lines": max(a.lines for a in group),
            "referenced_by": len(refs), "refs_sample": refs[:5], "verdict": verdict,
            "name_clashes_builtin": name.lower() in BUILTIN_COMMANDS,
        })
    order = {"retire candidate": 0, "unused, but referenced: check before retiring": 1,
             "unclear: typed counts may be the built-in": 2, "keep (core loop)": 3, "keep": 4}
    rows.sort(key=lambda r: (order[r["verdict"]], -r["listing_tokens"]))
    return rows


def cluster_summary(rows: list[dict], clusters: dict[str, list[str]]) -> list[dict]:
    """Per GSD cluster: how much of it is used. gsd-surface switches whole clusters."""
    members: dict[str, list[str]] = {}
    for name, cls in clusters.items():
        for c in cls:
            members.setdefault(c, []).append(name)
    by = {r["name"]: r for r in rows}
    out = []
    for c, names in sorted(members.items()):
        present = [n for n in names if n in by]
        used = [n for n in present if by[n]["verdict"] == "keep"]
        shared = [n for n in present if len(clusters.get(n, [])) > 1]
        out.append({"cluster": c, "skills": len(present), "used": used,
                    "unused": [n for n in present if n not in used],
                    "listing_tokens": sum(by[n]["listing_tokens"] for n in present),
                    "unused_tokens": sum(by[n]["listing_tokens"] for n in present if n not in used),
                    "also_in_other_clusters": shared})
    return out


# ---------------------------------------------------------------- driver

def account_of(path: str, accounts: list[tuple[str, str]]) -> str:
    for label, pat in accounts:
        if fnmatch.fnmatch(path, os.path.realpath(os.path.expanduser(pat.rstrip("*").rstrip("/"))) + "*"):
            return label
    return "-"


def lint(claude_dir: str, roots: list[str], accounts: list[tuple[str, str]] | None = None,
         claude_json: str | None = None, max_depth: int = 3, usage_path: str | None = None) -> dict:
    gdir = os.path.expanduser(claude_dir)
    accounts = accounts or []
    findings: list[Finding] = []
    gsettings_path = os.path.join(gdir, "settings.json")
    gsettings = _load_json(gsettings_path) or {}
    projects = discover_projects(roots, max_depth)
    plugins = plugin_dirs(gdir, gsettings)

    # global artifacts
    garts = (load_artifacts(os.path.join(gdir, "skills"), "skill", "global")
             + load_artifacts(os.path.join(gdir, "agents"), "agent", "global")
             + load_artifacts(os.path.join(gdir, "commands"), "command", "global"))
    for pd in plugins:
        garts += (load_artifacts(os.path.join(pd, "skills"), "skill", "plugin")
                  + load_artifacts(os.path.join(pd, "agents"), "agent", "plugin")
                  + load_artifacts(os.path.join(pd, "commands"), "command", "plugin"))

    mcp = mcp_servers(gdir, claude_json, projects, plugins)
    known_mcp = set(mcp["global"]) | {n for v in mcp["by_project"].values() for n in v} if claude_json else None

    # settings files
    settings_files = [(gsettings_path, gsettings)]
    lp = os.path.join(gdir, "settings.local.json")
    if os.path.isfile(lp):
        settings_files.append((lp, _load_json(lp) or {}))
    for p in projects:
        for n in ("settings.json", "settings.local.json"):
            sp = os.path.join(p, ".claude", n)
            if os.path.isfile(sp):
                settings_files.append((sp, _load_json(sp) or {}))
    gperm = gsettings.get("permissions") or {}
    for sp, cfg in settings_files:
        check_settings(sp, cfg, None if sp == gsettings_path else gperm, findings)
    backups = sorted(n for n in os.listdir(gdir) if re.match(r"settings\.json\.(bak|orig)", n)) if os.path.isdir(gdir) else []
    if backups:
        findings.append(Finding("settings.backups", "info", gdir, f"{len(backups)} settings.json backup(s)", {"files": backups}))

    hooks = check_hooks(os.path.join(gdir, "hooks"), settings_files, findings)
    memory = check_memory(os.path.join(gdir, "projects"), findings)

    # per-project budget and project artifacts
    all_instr: set[str] = set()
    budget = []
    parts = []
    gmd = os.path.join(gdir, "CLAUDE.md")
    home_code = os.path.join(os.path.expanduser("~"), "code")
    home_code_rp = os.path.realpath(home_code)
    for p in projects:
        parts_p = (load_artifacts(os.path.join(p, ".claude", "skills"), "skill", "project")
                   + load_artifacts(os.path.join(p, ".claude", "agents"), "agent", "project")
                   + load_artifacts(os.path.join(p, ".claude", "commands"), "command", "project"))
        parts += parts_p
        instr = instruction_files(p, gdir)
        imports = []
        for f in instr:
            imports += resolve_imports(f)[0]
        all_instr.update(f for f in instr if os.path.realpath(f) != os.path.realpath(gmd))
        instr_chars = sum(len(_read(f)) for f in instr + imports)
        cands = [memory_dir_name(p)]
        if p.startswith(home_code_rp + os.sep):
            cands.append(memory_dir_name(home_code + p[len(home_code_rp):]))
        mem = next((memory[c] for c in cands if c in memory), None)
        names = {(a.kind, a.name) for a in parts_p}
        listed = parts_p + [a for a in garts if (a.kind, a.name) not in names]
        row = {
            "project": _short(p), "account": account_of(p, accounts),
            "instruction_files": [_short(f) for f in instr + imports],
            "instruction_lines": sum(body_lines(_read(f)) for f in instr + imports),
            "instruction_tokens": _tok(instr_chars),
            "memory_tokens": mem["tokens"] if mem else 0,
            "skill_listing_tokens": sum(a.listing_tokens for a in listed if a.kind == "skill"),
            "agent_listing_tokens": sum(a.listing_tokens for a in listed if a.kind == "agent"),
            "command_listing_tokens": sum(a.listing_tokens for a in listed if a.kind == "command"),
            "skills": sum(1 for a in listed if a.kind == "skill"),
            "agents": sum(1 for a in listed if a.kind == "agent"),
            "mcp_servers": sorted(set(mcp["global"]) | set(mcp["by_project"].get(p, []))),
        }
        row["total_tokens"] = (row["instruction_tokens"] + row["memory_tokens"] + row["skill_listing_tokens"]
                               + row["agent_listing_tokens"] + row["command_listing_tokens"])
        budget.append(row)

    for f in [gmd] + sorted(all_instr):
        if os.path.isfile(f):
            check_instruction_file(f, findings)
    check_duplication(gmd, sorted(all_instr), findings)
    check_artifacts(garts + parts, known_mcp, findings)

    retire = None
    if usage_path:
        summ = _load_json(usage_path) or {}
        sk = ((summ.get("patterns") or {}).get("skills")) or {}
        clusters = gsd_clusters(gdir)
        rows = retirement(garts + parts, sk.get("by_name") or {}, clusters,
                          reference_corpus(gdir, [a.path for a in parts]))
        dr = (summ.get("meta") or {}).get("date_range") or [None, None]
        retire = {"usage_file": usage_path, "transcripts": dr, "history_since": sk.get("history_since"),
                  "rows": rows, "clusters": cluster_summary(rows, clusters),
                  "unlisted_used": sorted(n for n in (sk.get("by_name") or {})
                                          if n not in {r["name"] for r in rows})}

    for f in findings:
        f.path = _short(f.path) if f.path else ""
        for k in ("copies", "files"):
            if isinstance(f.detail.get(k), list):
                f.detail[k] = [_short(x) if x.startswith("/") else x for x in f.detail[k]]
    budget.sort(key=lambda r: -r["total_tokens"])
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.check] = counts.get(f.check, 0) + 1
    return {
        "schema": "cua.lint/1",
        "meta": {"claude_dir": _short(gdir), "roots": [_short(os.path.realpath(os.path.expanduser(r))) for r in roots],
                 "projects": len(projects), "plugins": [_short(p) for p in plugins],
                 "chars_per_token": CHARS_PER_TOKEN,
                 "global": {"skills": sum(1 for a in garts if a.kind == "skill"),
                            "agents": sum(1 for a in garts if a.kind == "agent"),
                            "commands": sum(1 for a in garts if a.kind == "command"),
                            "claude_md_tokens": _tok(len(_read(gmd))),
                            "skill_listing_tokens": sum(a.listing_tokens for a in garts if a.kind == "skill"),
                            "agent_listing_tokens": sum(a.listing_tokens for a in garts if a.kind == "agent")},
                 "mcp_global": mcp["global"], "mcp_known": known_mcp is not None,
                 "finding_counts": dict(sorted(counts.items()))},
        "budget": budget,
        "hooks": hooks,
        "findings": [asdict(f) for f in findings],
        "retirement": retire,
    }


def render(res: dict, top: int = 25, per_check: int = 12) -> str:
    m = res["meta"]
    g = m["global"]
    L = ["# Config lint", "",
         f"Config dir `{m['claude_dir']}`, {m['projects']} project dirs under {', '.join(m['roots'])}. "
         f"Token figures are estimates at ~{m['chars_per_token']} chars per token.", "",
         "## Always loaded, before the first prompt", "",
         f"- Global CLAUDE.md: ~{g['claude_md_tokens']} tokens",
         f"- Skill listing: {g['skills']} global/plugin skills, ~{g['skill_listing_tokens']} tokens",
         f"- Agent listing: {g['agents']} global/plugin agents, ~{g['agent_listing_tokens']} tokens",
         f"- MCP servers (global): {', '.join(m['mcp_global']) or '-'}"
         + ("" if m["mcp_known"] else " (~/.claude.json not read; pass --claude-json)"),
         "- Not counted: built-in tool definitions, MCP tool schemas, plugin SessionStart output.", "",
         f"### Heaviest projects (top {top})", "",
         "| Project | Account | Instr. lines | Instr. tok | Memory tok | Skills tok | Agents tok | Total tok |",
         "|---|---|---:|---:|---:|---:|---:|---:|"]
    for r in res["budget"][:top]:
        L.append(f"| {r['project']} | {r['account']} | {r['instruction_lines']} | {r['instruction_tokens']} | "
                 f"{r['memory_tokens']} | {r['skill_listing_tokens']} | {r['agent_listing_tokens']} | {r['total_tokens']} |")
    L += ["", "## Findings", ""]
    by: dict[str, list[dict]] = {}
    for f in res["findings"]:
        by.setdefault(f["check"], []).append(f)
    order = sorted(by, key=lambda c: (0 if any(f["severity"] == "warn" for f in by[c]) else 1, c))
    for c in order:
        fs = by[c]
        sev = "warn" if any(f["severity"] == "warn" for f in fs) else "info"
        L += [f"### {c} ({len(fs)}, {sev})", ""]
        for f in fs[:per_check]:
            where = f"`{f['path']}`: " if f["path"] else ""
            L.append(f"- {where}{f['message']}")
            for k in ("rules", "pairs", "paths", "imports", "copies", "agents", "commands", "links", "sample", "files"):
                v = f["detail"].get(k)
                if isinstance(v, list) and v:
                    shown = v[:6]
                    L.append(f"  - {k}: " + "; ".join(f"`{x}`" for x in shown) + (f" (+{len(v) - 6})" if len(v) > 6 else ""))
                    break
        if len(fs) > per_check:
            L.append(f"- … {len(fs) - per_check} more in lint.json")
        L.append("")
    hooks = res["hooks"]
    if hooks:
        L += ["## Hooks", "", "| File | Wired | Decides | Logs | Network |", "|---|---|---|---|---|"]
        for h in hooks:
            L.append(f"| {h['file']} | {len(h['wired'])} | {'yes' if h['decides'] else ''} | "
                     f"{'yes' if h['logs'] else ''} | {'yes' if h['network'] else ''} |")
        L += ["", "Decides/Logs/Network are text heuristics over the script source.", ""]
    if res.get("retirement"):
        L += render_retirement(res["retirement"])
    return "\n".join(L)


def render_retirement(rt: dict) -> list[str]:
    tr = rt["transcripts"]
    L = ["## Skill retirement", "",
         f"Use comes from transcripts {str(tr[0])[:10]} to {str(tr[1])[:10]} (typed, model, subagent) and from "
         f"history.jsonl since {str(rt['history_since'])[:10]} (typed only). \"Referenced\" = named by another "
         "skill, agent, command or GSD workflow, which can chain it without anyone typing it.", "",
         "### GSD clusters (what `/gsd-surface disable <cluster>` would switch off)", "",
         "| Cluster | Skills | Used | Unused | Unused tok | Shared with other clusters |", "|---|---:|---|---|---:|---|"]
    for c in rt["clusters"]:
        L.append(f"| {c['cluster']} | {c['skills']} | {', '.join(n[4:] for n in c['used']) or '-'} | "
                 f"{', '.join(n[4:] for n in c['unused']) or '-'} | {c['unused_tokens']} | "
                 f"{', '.join(n[4:] for n in c['also_in_other_clusters']) or '-'} |")
    L += ["", "### Every skill and command", "",
          "| Verdict | Name | Scope | Cluster | Typed | Model | Subagent | History typed | Last | Refs | Listing tok |",
          "|---|---|---|---|---:|---:|---:|---:|---|---:|---:|"]
    for r in rt["rows"]:
        name = r["name"] + (" (same name as a built-in)" if r.get("name_clashes_builtin") else "")
        L.append(f"| {r['verdict']} | {name} | {'/'.join(r['scopes'])} | {', '.join(r['clusters']) or '-'} | "
                 f"{r['typed']} | {r['model']} | {r['subagent']} | {r['history_typed']} | "
                 f"{str(r['last'] or '')[:10]} | {r['referenced_by']} | {r['listing_tokens']} |")
    if rt["unlisted_used"]:
        L += ["", "Used but not installed as a file skill (built-ins, plugins, removed skills): "
              + ", ".join(f"`{n}`" for n in rt["unlisted_used"][:40])]
    L.append("")
    return L
