"""Normalised in-memory model shared by all sources."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0
    estimated: bool = False  # True when derived from character counts

    def add(self, o: "Usage") -> None:
        self.input += o.input
        self.output += o.output
        self.cache_write += o.cache_write
        self.cache_read += o.cache_read
        self.estimated = self.estimated or o.estimated

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_write + self.cache_read

    @property
    def total_in(self) -> int:
        return self.input + self.cache_write + self.cache_read


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]
    ts: datetime | None
    result_ts: datetime | None = None  # when the tool_result came back; result_ts - ts is the wait
    result_error: bool = False
    result_text: str = ""
    denial: str | None = None  # None | user_rejected | question_dismissed | rule_denied | not_granted | hook_blocked | classifier_denied | sandbox_blocked
    user_feedback: str = ""    # text the user typed when rejecting
    agent_id: str = ""         # set for subagent (sidechain) calls; maps to Session.agent_types


@dataclass
class Turn:
    """One user prompt and everything the assistant did until the next prompt."""
    ts: datetime | None
    prompt: str
    kind: str = "prompt"  # prompt | command | continuation
    command: str | None = None
    usage: Usage = field(default_factory=Usage)
    tool_calls: list[ToolCall] = field(default_factory=list)
    assistant_messages: int = 0
    interrupted: bool = False
    end_ts: datetime | None = None
    models: dict[str, int] = field(default_factory=dict)


@dataclass
class Session:
    id: str
    source: str           # claude_code | claude_ai
    account: str
    project: str
    title: str = ""
    git_branch: str | None = None
    turns: list[Turn] = field(default_factory=list)
    compactions: int = 0
    sidechain_usage: Usage = field(default_factory=Usage)
    sidechain_tool_calls: list[ToolCall] = field(default_factory=list)
    # subagent model use: agent id -> {model: [assistant messages, total tokens]}; agent id -> subagent_type
    sidechain_by_agent: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    agent_types: dict[str, str] = field(default_factory=dict)
    api_errors: int = 0
    first_ts: datetime | None = None  # earliest record of any kind, incl. subagent-only files
    permission_mode: str | None = None  # as recorded in the transcript, when Claude Code logs it

    @property
    def start(self) -> datetime | None:
        ts = [t.ts for t in self.turns if t.ts]
        return min(ts) if ts else None

    @property
    def end(self) -> datetime | None:
        ts = [t.end_ts or t.ts for t in self.turns if (t.end_ts or t.ts)]
        return max(ts) if ts else None

    @property
    def usage(self) -> Usage:
        u = Usage()
        for t in self.turns:
            u.add(t.usage)
        u.add(self.sidechain_usage)
        return u

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [c for t in self.turns for c in t.tool_calls] + self.sidechain_tool_calls


@dataclass
class PermissionConfig:
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    ask: list[str] = field(default_factory=list)
    default_mode: str | None = None
    sources: list[str] = field(default_factory=list)
