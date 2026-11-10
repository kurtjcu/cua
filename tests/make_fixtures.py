"""Generate synthetic Claude Code config dirs + a claude.ai export zip that mimic the real formats."""
from __future__ import annotations

import json
import os
import random
import sys
import zipfile
from datetime import datetime, timedelta, timezone

REJECT = ("The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if it was a file edit, "
          "the new_string was NOT written to the file). STOP what you are doing and wait for the user to tell you how to proceed.")
REJECT_FB = REJECT + " To tell you how to proceed, the user said:\n{fb}"
RULE_DENY = "Permission to use Bash with command {cmd} has been denied."

PROMPTS = [
    "fix this error: TypeError: cannot read property 'x' of undefined in src/app.ts",
    "fix this error: ModuleNotFoundError: No module named 'boto3'",
    "add a new endpoint for /orders/summary that returns json",
    "write tests for the extractor module",
    "deploy the stack to staging with cdk",
    "refactor the parser to be simpler",
    "plan the architecture for the OCR pipeline",
    "update the README with setup steps",
    "no, that's not what I asked, keep the old function signature",
    "still failing with the same error",
    "continue",
    "yes",
]
CMDS = ["npm run test", "npm install zod", "git status", "git push origin main", "cdk deploy Staging", "pytest -q",
        "docker compose up -d", "rm -rf dist", "aws s3 ls", "python -m pytest", "ls -la", "gh pr create --fill"]


def _line(**kw):
    return json.dumps(kw) + "\n"


def make_claude_dir(root: str, n_sessions: int, seed: int, projects: list[str]):
    rnd = random.Random(seed)
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "settings.json"), "w") as f:
        json.dump({"permissions": {"allow": ["Bash(git status)", "Bash(npm run test:*)", "Bash(terraform plan:*)"], "deny": ["Bash(rm -rf:*)"]}}, f)
    hist = open(os.path.join(root, "history.jsonl"), "w")
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for i in range(n_sessions):
        proj = rnd.choice(projects)
        slug = proj.replace("/", "-")
        d = os.path.join(root, "projects", slug)
        os.makedirs(d, exist_ok=True)
        sid = f"{seed:02d}{i:06d}-aaaa-bbbb-cccc-000000000000"
        ts = t0 + timedelta(days=rnd.randint(0, 150), hours=rnd.randint(7, 22))
        with open(os.path.join(d, sid + ".jsonl"), "w") as f:
            f.write(_line(type="summary", summary=f"Session {i}", leafUuid="x"))
            for turn in range(rnd.randint(2, 12)):
                ts += timedelta(minutes=rnd.randint(1, 15))
                prompt = rnd.choice(PROMPTS)
                if turn == 0 and rnd.random() < 0.2:
                    prompt = "<command-name>/review</command-name><command-args>PR 12</command-args>"
                f.write(_line(type="user", sessionId=sid, cwd=proj, gitBranch="main", timestamp=ts.isoformat(),
                              message={"role": "user", "content": prompt}, uuid=f"u{turn}"))
                hist.write(json.dumps({"display": prompt, "project": proj, "timestamp": int(ts.timestamp() * 1000)}) + "\n")
                for k in range(rnd.randint(1, 5)):
                    ts += timedelta(seconds=rnd.randint(5, 90))
                    mid = f"msg_{sid}_{turn}_{k}"
                    usage = {"input_tokens": rnd.randint(5, 50), "output_tokens": rnd.randint(50, 1500),
                             "cache_creation_input_tokens": rnd.randint(0, 5000), "cache_read_input_tokens": rnd.randint(5000, 80000)}
                    tool = rnd.choice(["Bash", "Bash", "Edit", "Read", "Grep", "WebFetch", "mcp__github__create_issue"])
                    tid = f"toolu_{sid}_{turn}_{k}"
                    if tool == "Bash":
                        inp = {"command": rnd.choice(CMDS)}
                    elif tool == "Edit":
                        inp = {"file_path": f"{proj}/src/a.py", "old_string": "a", "new_string": "b"}
                    elif tool == "WebFetch":
                        inp = {"url": "https://docs.aws.amazon.com/cdk/v2/guide/home.html", "prompt": "x"}
                    else:
                        inp = {"pattern": "foo"}
                    # streamed: text block and tool block in separate lines with the same message id + usage
                    for block in ({"type": "text", "text": "Let me check."}, {"type": "tool_use", "id": tid, "name": tool, "input": inp}):
                        f.write(_line(type="assistant", sessionId=sid, cwd=proj, timestamp=ts.isoformat(),
                                      isSidechain=False, message={"id": mid, "role": "assistant", "model": "claude-opus-5-5",
                                                                  "content": [block], "usage": usage}))
                    r = rnd.random()
                    is_err, text = False, "ok"
                    if tool == "Bash" and "rm -rf" in inp["command"]:
                        is_err, text = True, RULE_DENY.format(cmd=inp["command"])
                    elif r < 0.12 and tool not in ("Read", "Grep"):
                        is_err, text = True, (REJECT_FB.format(fb="don't push, just commit locally") if "push" in str(inp) else REJECT)
                    elif r < 0.2:
                        is_err, text = True, "Error: command failed with exit code 1"
                    f.write(_line(type="user", sessionId=sid, cwd=proj, timestamp=ts.isoformat(),
                                  message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "is_error": is_err, "content": text}]}))
                    if is_err and REJECT[:20] in text and rnd.random() < 0.3:
                        f.write(_line(type="user", sessionId=sid, cwd=proj, timestamp=ts.isoformat(),
                                      message={"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user for tool use]"}]}))
                        break
            if rnd.random() < 0.3:  # a subagent burst
                for k in range(3):
                    f.write(_line(type="assistant", sessionId=sid, isSidechain=True, timestamp=ts.isoformat(),
                                  message={"id": f"side_{sid}_{k}", "model": "claude-haiku-5-5", "content": [{"type": "tool_use", "id": f"s{k}", "name": "Grep", "input": {"pattern": "x"}}],
                                           "usage": {"input_tokens": 10, "output_tokens": 200, "cache_read_input_tokens": 20000}}))
            if rnd.random() < 0.25:
                f.write(_line(type="system", subtype="compact_boundary", sessionId=sid, timestamp=ts.isoformat()))
    hist.close()


def make_export(path: str, n: int, seed: int):
    rnd = random.Random(seed)
    convs = []
    t0 = datetime(2026, 2, 1, tzinfo=timezone.utc)
    for i in range(n):
        ts = t0 + timedelta(days=rnd.randint(0, 270))
        msgs = []
        for k in range(rnd.randint(1, 6)):
            p = rnd.choice(PROMPTS + ["deep dive on agent harnesses, use up to date repos", "what's the best 3d printer filament for outdoor use"])
            msgs.append({"uuid": f"h{k}", "sender": "human", "text": p, "content": [{"type": "text", "text": p}], "created_at": ts.isoformat(), "attachments": []})
            ts += timedelta(minutes=2)
            body = "Here is an answer. " * rnd.randint(20, 200)
            content = [{"type": "text", "text": body}]
            if rnd.random() < 0.3:
                content = [{"type": "tool_use", "id": f"t{k}", "name": "web_search", "input": {"query": "x"}},
                           {"type": "tool_result", "tool_use_id": f"t{k}", "content": [{"type": "text", "text": "results"}]}] + content
            msgs.append({"uuid": f"a{k}", "sender": "assistant", "text": body, "content": content, "created_at": ts.isoformat(), "attachments": []})
        convs.append({"uuid": f"conv-{seed}-{i}", "name": f"Chat {i}", "created_at": msgs[0]["created_at"], "chat_messages": msgs})
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("conversations.json", json.dumps(convs))
        z.writestr("projects.json", "[]")


if __name__ == "__main__":
    out = sys.argv[1]
    make_claude_dir(os.path.join(out, "personal-claude"), 60, 1, ["/home/dev/code/homelab", "/home/dev/code/sideproject", "/home/dev/code/claude-usage-audit"])
    make_claude_dir(os.path.join(out, "work-claude"), 90, 2, ["/home/dev/code/work/api", "/home/dev/code/work/templater", "/home/dev/code/work/infra"])
    make_export(os.path.join(out, "personal-export.zip"), 80, 3)
    make_export(os.path.join(out, "work-export.zip"), 40, 4)
    print("fixtures in", out)
