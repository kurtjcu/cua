"""Redaction helpers. Everything that leaves the local machine passes through here."""
from __future__ import annotations

import os
import re

_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "<AWS_KEY>"),
    (re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*\S+"), "aws_secret_access_key=<SECRET>"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "<API_KEY>"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "<GH_TOKEN>"),
    (re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"), "<SLACK_TOKEN>"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"), "<JWT>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "<PRIVATE_KEY>"),
    (re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*['\"]?[^\s'\"]{4,}"), r"\1=<SECRET>"),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "<EMAIL>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<IP>"),
    (re.compile(r"https?://[^\s)\"']+"), "<URL>"),
]

_HOME = os.path.expanduser("~")


def redact(text: str | None, max_len: int = 160) -> str:
    if not text:
        return ""
    t = str(text)
    if _HOME and _HOME != "/":
        t = t.replace(_HOME, "~")
    t = re.sub(r"/(?:home|Users)/[^/\s]+", "~", t)
    for pat, repl in _PATTERNS:
        t = pat.sub(repl, t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > max_len:
        t = t[: max_len - 1] + "…"
    return t


def project_label(path: str | None, mode: str = "basename") -> str:
    """How a project/cwd is shown in reports. mode: basename | hash | full."""
    if not path:
        return "(unknown)"
    path = path.rstrip("/")
    if mode == "full":
        return redact(path, 200)
    if mode == "hash":
        import hashlib
        return "proj-" + hashlib.sha1(path.encode()).hexdigest()[:8]
    return os.path.basename(path) or path
