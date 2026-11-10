"""Parser for claude.ai data exports (Settings → Privacy → Export data).

Accepts the export .zip or the extracted folder; reads conversations.json (and projects.json if present).
claude.ai exports carry no token counts, so usage is estimated at ~4 chars/token and flagged as estimated.
"""
from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime

from ..model import Session, ToolCall, Turn, Usage
from .claude_code import classify_denial

CHARS_PER_TOKEN = 4


def _ts(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_json(path: str, name: str):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            cand = [n for n in z.namelist() if n.endswith(name)]
            if not cand:
                return None
            with z.open(cand[0]) as f:
                return json.load(f)
    fp = os.path.join(path, name)
    if os.path.exists(fp):
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    return None


def _msg_text(m: dict) -> tuple[str, list[dict]]:
    """Return (visible text, content blocks)."""
    blocks = m.get("content") or []
    parts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    text = "\n".join(p for p in parts if p) or m.get("text") or ""
    return text, blocks


def _est(chars: int) -> int:
    return max(0, chars // CHARS_PER_TOKEN)


def load_export(path: str, account: str) -> list[Session]:
    convs = _read_json(path, "conversations.json") or []
    projects = {p.get("uuid"): p.get("name") for p in (_read_json(path, "projects.json") or [])}
    sessions = []
    for c in convs:
        project = projects.get((c.get("project") or {}).get("uuid") if isinstance(c.get("project"), dict) else c.get("project_uuid")) or "(no project)"
        s = Session(id=c.get("uuid", ""), source="claude_ai", account=account, project=project, title=c.get("name") or "")
        current: Turn | None = None
        context_chars = 0  # running conversation size, to estimate re-sent input per turn
        for m in c.get("chat_messages") or []:
            text, blocks = _msg_text(m)
            attach_chars = sum(len(a.get("extracted_content") or "") for a in (m.get("attachments") or []))
            ts = _ts(m.get("created_at"))
            if m.get("sender") == "human":
                current = Turn(ts=ts, prompt=text)
                s.turns.append(current)
                context_chars += len(text) + attach_chars
                continue
            if current is None:
                current = Turn(ts=ts, prompt="", kind="continuation")
                s.turns.append(current)
            out_chars = len(text)
            calls = {}
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    tc = ToolCall(id=b.get("id", "") or str(len(calls)), name=b.get("name", "?"), input=b.get("input") or {}, ts=ts)
                    calls[tc.id] = tc
                    current.tool_calls.append(tc)
                    out_chars += len(json.dumps(tc.input))
                elif b.get("type") == "tool_result":
                    rtext = json.dumps(b.get("content"))[:2000]
                    tc = calls.get(b.get("tool_use_id", "")) or (current.tool_calls[-1] if current.tool_calls else None)
                    if tc:
                        tc.result_error = bool(b.get("is_error"))
                        tc.result_text = rtext
                        tc.denial = classify_denial(rtext, tc.result_error)
                    context_chars += len(rtext)
            current.usage.add(Usage(input=_est(context_chars), output=_est(out_chars), estimated=True))
            current.assistant_messages += 1
            current.end_ts = ts
            context_chars += out_chars
        if s.turns:
            sessions.append(s)
    return sessions
