# cua prompt-logger (optional)

A tiny [Claude Code `PermissionRequest` hook](https://docs.claude.com/en/docs/claude-code/hooks)
that records every permission prompt you see — including the ones you **approve**.

## Why you'd want it

Claude Code transcripts record only the prompts you *reject*. They don't record the
ones you *approve*. So by default `cua waits` has to **infer** which calls prompted,
from your allow rules.

With this hook installed, `cua waits` instead reports the prompts you **actually
approved** (its `approved_prompts` block) — grouped by shape, risk-rated, ready to
turn into allow rules. It's the difference between a guess and a measurement.

It's entirely optional. `cua` works without it; the estimate still covers permissions.

## What it does

Nothing but log. It is a **pure observer**:

- It **never** allows or denies a prompt — installing it cannot change which dialogs
  you see or what you approve.
- It **fails open**: on any error it exits silently and you get the normal dialog.
- It needs only Node's standard library.

## What it logs — and what it doesn't

Each row holds the time, the permission mode, the project path, a short **shape** of
the call, the tool-use and session ids, and a call fingerprint. The shape never holds
file contents or a Bash command's arguments.
One tab-separated row per prompt:

```
time   kind   mode   project   shape   tool_use_id   session_id   call_hash
```

- **project** is the working directory, with your home shown as `~` (empty when Claude
  Code sends none).
- **shape** is the tool plus a short pattern: `Bash: git`, `Edit: <proj>/src/app.ts`,
  `WebFetch: docs.claude.com`. For a Bash command it keeps the **program name only**:
  the leading run of letters, digits and `_ . + -` at the start of the command after
  masking, plus a `[multi-line]` flag when the command spans lines. `cat<<<x` logs
  `Bash: cat` and `echo${IFS}x` logs `Bash: echo`; nothing after the name is
  written, with or without whitespace. A program given with a path logs its file
  name only when the path ends at a space, tab, newline or the end of the command and
  no `/` follows anywhere after it: `/opt/x/deploy.sh --y` logs `Bash: deploy.sh`, and
  nothing before the last `/` is logged. Any other path logs `Bash: <other>`:
  `/home/u/Acme Corp/run.sh`, `/x/acme;ls`, `/opt/c=acme/run` and `./run.sh src/a` all do.
  The shape is also the fixed token `Bash: <other>` for a name of 24 or more characters
  (counting `.` and `+`), a quoted first word, `{a,b}`, a subshell, `$(...)`, and a
  command that is not a string. A command that starts with an assignment
  (`TOKEN=abc123 npm test`) logs the variable name, `Bash: TOKEN`; no value is ever
  logged. Masking is applied before the name is taken: quoted strings (read the way
  the shell reads them, backslash escapes included), everything after an unpaired or
  escaped quote, `=values`, and tokens of 24 or more letters, digits, `_` or `-`.
  Only Bash shapes are masked. File tools keep a path pattern (`Edit: <proj>/src/app.ts`),
  Skill its name and WebFetch its hostname (`WebFetch: docs.claude.com`), each cut
  to 60 characters, and never file contents, URL paths or query strings.
- `echo hunter2` logs `Bash: echo`. `cua waits` reads only the tool name from the
  shape, so dropping the arguments does not change what it measures. The limit: the
  hook cannot tell a program name from a secret typed as the command word itself, so
  a bare `hunter2` logs `Bash: hunter2`. Nor can it see a path through an unquoted
  space, because the shell runs the word before the space: `Acme Corp/run.sh` logs
  `Bash: Acme` and `~/a b` logs `Bash: a`.
- **call_hash** is an unsalted 12-hex fingerprint of the tool name + input so
  `cua waits` can match a logged prompt to the exact transcript call. It cannot be
  read back, but anyone holding the log can confirm a guess at an exact command (it
  is an unsalted hash).
- Control characters in any column are written as spaces, so a crafted tool input
  cannot shift columns or forge rows.

**File modes.** A new log is created `0600` in a `0700` directory. An existing log
keeps its mode; tighten it with `chmod 600 ~/.local/state/cua/prompts.log` and
`chmod 700 ~/.local/state/cua`.

The repo copy of the hook and the copy in `~/.claude/hooks/` are separate files:
changes to the repo hook reach your machine only after you re-run
`python3 hooks/install.py`.

The log stays on your machine (default `~/.local/state/cua/prompts.log`). Nothing
leaves your computer.

## Install

From the repo root:

```bash
python3 hooks/install.py
```

It shows exactly what it will change and asks before touching anything. It:

1. copies `prompt-logger.js` into `~/.claude/hooks/`, and
2. adds a `PermissionRequest` entry to `~/.claude/settings.json` (merged in — your
   other hooks are left alone).

Then do the two steps it prints:

1. add `PROMPT_LOG=~/.local/state/cua/prompts.log` to your `.env`, and
2. **restart Claude Code** — settings are read at startup and not hot-reloaded.

Options:

```bash
python3 hooks/install.py --config-dir ~/.claude-work   # a second account's config dir
python3 hooks/install.py --yes                         # skip the confirmation prompt
python3 hooks/install.py --uninstall                   # remove the hook entry + file
```

Log somewhere else by setting `CUA_PROMPT_LOG` in the hook's environment; point
`PROMPT_LOG` in `.env` at the same file.

## Verify

After a restart, approve one prompt, then:

```bash
tail -n 2 ~/.local/state/cua/prompts.log
python3 -m cua waits        # reads PROMPT_LOG; see the approved_prompts block
```

## Uninstall

```bash
python3 hooks/install.py --uninstall
```

Removes the hook file and its settings entry. Your existing log is left in place —
delete it yourself if you want it gone.
