"""Parser for Claude Code session transcripts (<config dir>/projects/<slug>/<session>.jsonl).

Works for any config dir, e.g. ~/.claude (personal) or ~/.claude-work (CLAUDE_CONFIG_DIR=~/.claude-work).
Formats drift between Claude Code versions, so everything is parsed defensively.
"""
from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime
from typing import Iterator

from ..model import PermissionConfig, Session, ToolCall, Turn, Usage

INTERRUPT_RE = re.compile(r"\[Request interrupted by user")
COMMAND_RE = re.compile(r"<command-name>\s*/?([^<\s]+)\s*</command-name>")

# (denial kind, regex) checked in order against tool_result text
DENIAL_PATTERNS = [
    ("classifier_denied", re.compile(r"auto mode classifier|denied by the .*classifier", re.I)),
    ("hook_blocked", re.compile(r"(PreToolUse|hook).{0,80}(block|denied|prevent)", re.I)),
    ("rule_denied", re.compile(r"Permission to use .{0,200} has been denied", re.I)),
    ("not_granted", re.compile(r"requested permissions? to use .{0,200} but you haven'?t granted", re.I)),
    ("user_rejected", re.compile(r"user doesn'?t want to (proceed|take this action)|tool use was rejected", re.I)),
    ("sandbox_blocked", re.compile(r"Operation not permitted|sandbox(ed)? .{0,40}(block|deny|denied|restrict)|Read-only file system", re.I)),
]
FEEDBACK_RE = re.compile(r"(?:the user said|user provided the following reason[^:]*):\s*(.+)", re.I | re.S)


def _ts(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    out.append(b.get("text", ""))
                elif b.get("type") == "tool_result":
                    out.append(_text_of(b.get("content")))
            elif isinstance(b, str):
                out.append(b)
        return "\n".join(out)
    return ""


def classify_denial(text: str, is_error: bool) -> str | None:
    """Only an error result can be a denial. Successful output is file contents or command output,
    and plenty of it (GSD workflow docs, .githooks/pre-commit, these audit notes) mentions a hook
    blocking something; matching it inflated hook_blocked with ordinary Reads and Greps."""
    if not text or not is_error:
        return None
    for kind, pat in DENIAL_PATTERNS:
        if pat.search(text):
            return kind
    return None


def _usage(u: dict | None) -> Usage:
    if not u:
        return Usage()
    return Usage(
        input=int(u.get("input_tokens") or 0),
        output=int(u.get("output_tokens") or 0),
        cache_write=int(u.get("cache_creation_input_tokens") or 0),
        cache_read=int(u.get("cache_read_input_tokens") or 0),
    )


def _agent_id(r: dict, path: str) -> str:
    """Id of the subagent that wrote a sidechain record: the record's agentId, else agent-<id>.jsonl."""
    aid = r.get("agentId")
    if aid:
        return str(aid)
    base = os.path.basename(path)
    return base[len("agent-"):-len(".jsonl")] if base.startswith("agent-") and base.endswith(".jsonl") else "?"


def iter_session_files(config_dir: str) -> Iterator[str]:
    root = os.path.join(os.path.expanduser(config_dir), "projects")
    yield from sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True))


def parse_session_file(path: str, account: str) -> Session | None:
    records = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                # JSONDecodeError is a subclass of ValueError. CPython 3.11+ raises a plain ValueError for
                # an integer literal longer than its 4300-digit limit, so one such line is skipped like
                # any other unparseable line instead of aborting the run.
                continue
    if not records:
        return None

    sid = next((r.get("sessionId") for r in records if r.get("sessionId")), os.path.splitext(os.path.basename(path))[0])
    cwd = next((r.get("cwd") for r in records if r.get("cwd")), None)
    if not cwd:  # fall back to the slugged directory name
        cwd = os.path.basename(os.path.dirname(path)).replace("-", "/")
    branch = next((r.get("gitBranch") for r in records if r.get("gitBranch")), None)
    is_subagent_file = "/subagents/" in path.replace(os.sep, "/") or os.path.basename(path).startswith("agent-")
    s = Session(id=sid, source="claude_code", account=account, project=cwd, git_branch=branch)

    seen_msg_ids: set[str] = set()
    calls_by_id: dict[str, ToolCall] = {}
    current: Turn | None = None

    for r in records:
        rtype = r.get("type")
        ts = _ts(r.get("timestamp"))
        if ts and (s.first_ts is None or ts < s.first_ts):
            s.first_ts = ts
        pm = r.get("permissionMode")
        if isinstance(pm, str) and pm and not is_subagent_file and not r.get("isSidechain"):
            s.permission_mode = pm  # last recorded main-thread mode wins
        if rtype == "summary":
            s.title = s.title or (r.get("summary") or "")
            continue
        if rtype == "system":
            sub = (r.get("subtype") or "").lower()
            if "compact" in sub:
                s.compactions += 1
            if "api_error" in sub or r.get("level") == "error":
                s.api_errors += 1
            continue
        if r.get("isCompactSummary"):
            s.compactions += 1
            continue
        msg = r.get("message") or {}
        sidechain = bool(r.get("isSidechain")) or is_subagent_file

        if rtype == "assistant":
            mid = msg.get("id") or r.get("uuid")
            usage = Usage()
            if mid not in seen_msg_ids:  # streamed messages repeat usage per content block
                seen_msg_ids.add(mid)
                usage = _usage(msg.get("usage"))
            if r.get("isApiErrorMessage"):
                s.api_errors += 1
            model = msg.get("model") or "unknown"
            new_calls = []
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tc = ToolCall(id=b.get("id", ""), name=b.get("name", "?"), input=b.get("input") or {}, ts=ts,
                                  agent_id=_agent_id(r, path) if sidechain else "")
                    calls_by_id[tc.id] = tc
                    new_calls.append(tc)
            if sidechain:
                s.sidechain_usage.add(usage)
                s.sidechain_tool_calls.extend(new_calls)
                if usage.total:  # repeated stream chunks carry an empty Usage, so this counts messages once
                    e = s.sidechain_by_agent.setdefault(_agent_id(r, path), {}).setdefault(model, [0, 0])
                    e[0] += 1
                    e[1] += usage.total
                continue
            if current is None:
                current = Turn(ts=ts, prompt="", kind="continuation")
                s.turns.append(current)
            current.usage.add(usage)
            current.tool_calls.extend(new_calls)
            if usage.total:
                current.assistant_messages += 1
                current.models[model] = current.models.get(model, 0) + 1
            current.end_ts = ts or current.end_ts
            continue

        if rtype != "user":
            continue
        content = msg.get("content")
        blocks = content if isinstance(content, list) else []
        tool_results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
        for b in tool_results:
            tc = calls_by_id.get(b.get("tool_use_id", ""))
            text = _text_of(b.get("content"))
            if tc is None:
                continue
            tc.result_ts = ts
            if tc.name in ("Agent", "Task") and isinstance(r.get("toolUseResult"), dict):
                aid = r["toolUseResult"].get("agentId")  # links the subagent transcript to its subagent_type
                if aid:
                    s.agent_types[str(aid)] = str((tc.input or {}).get("subagent_type") or "general-purpose")
            tc.result_error = bool(b.get("is_error"))
            tc.result_text = text[:2000]
            tc.denial = classify_denial(text, tc.result_error)
            if tc.denial == "user_rejected" and tc.name == "AskUserQuestion":
                tc.denial = "question_dismissed"  # an answer to a question, not a permission refusal
            if tc.denial in ("user_rejected", "question_dismissed"):
                m = FEEDBACK_RE.search(text)
                if m:
                    tc.user_feedback = m.group(1).strip()[:500]
        text = _text_of(content) if not tool_results else _text_of([b for b in blocks if b.get("type") == "text"])
        if sidechain or r.get("isMeta"):
            continue
        if INTERRUPT_RE.search(text or ""):
            if current:
                current.interrupted = True
            continue
        if tool_results and not text.strip():
            if current:
                current.end_ts = ts or current.end_ts
            continue
        if not text.strip() or "<local-command-stdout>" in text or text.startswith("Caveat:"):
            continue
        cmd = COMMAND_RE.search(text)
        current = Turn(ts=ts, prompt=text, kind="command" if cmd else "prompt", command=cmd.group(1) if cmd else None)
        if cmd:
            args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
            current.prompt = f"/{cmd.group(1)} {args.group(1).strip() if args else ''}".strip()
            if cmd.group(1) in ("compact",):
                s.compactions += 1
        s.turns.append(current)

    if not s.turns and not s.sidechain_tool_calls and not s.sidechain_by_agent:
        return None  # a text-only subagent has no tool calls but its tokens still count
    return s


def load_sessions(config_dir: str, account: str, since: datetime | None = None) -> list[Session]:
    """Load all sessions; subagent transcripts are merged into their parent session id when possible.

    The --since window is applied AFTER merging, on the session's earliest record of any kind.
    Filtering per file on Session.start used to let subagent-only files through (they have no
    main-thread turns, so start is None), and kept the subagents of out-of-window parents as
    zero-prompt orphan sessions."""
    by_id: dict[str, Session] = {}
    for p in iter_session_files(config_dir):
        s = parse_session_file(p, account)
        if not s:
            continue
        if s.id in by_id:
            parent = by_id[s.id]
            parent.sidechain_usage.add(s.sidechain_usage)
            parent.sidechain_tool_calls.extend(s.sidechain_tool_calls)
            for aid, ms in s.sidechain_by_agent.items():
                dst = parent.sidechain_by_agent.setdefault(aid, {})
                for m, (n, tok) in ms.items():
                    e = dst.setdefault(m, [0, 0])
                    e[0] += n
                    e[1] += tok
            parent.agent_types.update(s.agent_types)
            for t in s.turns:  # main-chain turns from a split file
                parent.turns.append(t)
            parent.compactions += s.compactions
            parent.api_errors += s.api_errors
            parent.permission_mode = parent.permission_mode or s.permission_mode
            if s.first_ts and (parent.first_ts is None or s.first_ts < parent.first_ts):
                parent.first_ts = s.first_ts
        else:
            by_id[s.id] = s
    out = []
    for s in by_id.values():
        if since:
            began = min((t for t in (s.start, s.first_ts) if t), default=None)
            if began is None or began < since:  # undated sessions can't be shown to be in window
                continue
        s.turns.sort(key=lambda t: t.ts.timestamp() if t.ts else 0)
        out.append(s)
    return out


def load_prompt_history(config_dir: str) -> list[dict]:
    """<config>/history.jsonl: every prompt typed, incl. ones in sessions since deleted."""
    p = os.path.join(os.path.expanduser(config_dir), "history.jsonl")
    out = []
    if os.path.exists(p):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def _settings_files(config_dir: str | None, project_dir: str | None) -> list[str]:
    """In increasing precedence for defaultMode: user, project shared, project local."""
    files = []
    if config_dir:
        files += [os.path.join(os.path.expanduser(config_dir), n) for n in ("settings.json", "settings.local.json")]
    if project_dir:
        files += [os.path.join(project_dir, ".claude", n) for n in ("settings.json", "settings.local.json")]
    return files


def _load_into(cfg: PermissionConfig, fp: str) -> None:
    if not os.path.exists(fp) or fp in cfg.sources:
        return
    # Settings are read as UTF-8 with undecodable bytes replaced, like the transcript and history readers.
    # So a stray byte, or a locale that is not UTF-8, cannot raise UnicodeDecodeError (a ValueError) and
    # silently drop the file's rules. The ValueError catch now covers only JSON that does not parse,
    # including an integer literal over the 4300-digit limit.
    try:
        with open(fp, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    perms = data.get("permissions") or {}
    cfg.allow += [r for r in perms.get("allow") or [] if isinstance(r, str)]
    cfg.deny += [r for r in perms.get("deny") or [] if isinstance(r, str)]
    cfg.ask += [r for r in perms.get("ask") or [] if isinstance(r, str)]
    cfg.default_mode = perms.get("defaultMode") or cfg.default_mode  # later file wins
    cfg.sources.append(fp)


def load_permission_config(config_dir: str, project_dirs: list[str] | None = None) -> PermissionConfig:
    """Every rule from the user file and all given projects, pooled. Use for reporting which rules
    exist and which never matched; use effective_permission_config to estimate prompts."""
    cfg = PermissionConfig()
    for fp in _settings_files(config_dir, None):
        _load_into(cfg, fp)
    for d in project_dirs or []:
        for fp in _settings_files(None, d):
            _load_into(cfg, fp)
    return cfg


def effective_permission_config(config_dir: str | None, project_dir: str | None) -> PermissionConfig:
    """What one session actually ran under: the user settings plus that session's own project."""
    cfg = PermissionConfig()
    for fp in _settings_files(config_dir, project_dir):
        _load_into(cfg, fp)
    return cfg
