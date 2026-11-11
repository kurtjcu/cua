#!/usr/bin/env python3
"""Install the cua prompt-logger as a Claude Code PermissionRequest hook.

This asks before it touches anything. It:
  1. copies prompt-logger.js into your Claude hooks directory, and
  2. registers it under hooks.PermissionRequest in your Claude settings.json
     (merging into the existing file — your other hooks are left alone).

The logger is a pure observer: it only writes a shape-only log of the permission
prompts you see (see hooks/README.md). It never allows or denies a prompt.

Usage:
    python3 hooks/install.py                 # installs into ~/.claude
    python3 hooks/install.py --config-dir ~/.claude-work
    python3 hooks/install.py --uninstall     # removes the hook entry + file
    python3 hooks/install.py --yes           # skip the confirmation prompt

Stdlib only. Run from the repo root.
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK_SRC = HERE / "prompt-logger.js"
DEFAULT_LOG = "~/.local/state/cua/prompts.log"


def load_settings(settings_path: Path) -> dict:
    if not settings_path.exists():
        return {}
    try:
        return json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        sys.exit(f"error: could not read {settings_path}: {e}")


def hook_command(hook_dest: Path) -> str:
    return f"node {hook_dest}"


def already_registered(settings: dict, hook_dest: Path) -> bool:
    cmd = hook_command(hook_dest)
    for entry in settings.get("hooks", {}).get("PermissionRequest", []):
        for h in entry.get("hooks", []):
            if h.get("command") == cmd:
                return True
    return False


def add_hook(settings: dict, hook_dest: Path) -> dict:
    hooks = settings.setdefault("hooks", {})
    pr = hooks.setdefault("PermissionRequest", [])
    pr.append({"hooks": [{"type": "command", "command": hook_command(hook_dest)}]})
    return settings


def remove_hook(settings: dict, hook_dest: Path) -> dict:
    cmd = hook_command(hook_dest)
    pr = settings.get("hooks", {}).get("PermissionRequest", [])
    kept = []
    for entry in pr:
        entry["hooks"] = [h for h in entry.get("hooks", []) if h.get("command") != cmd]
        if entry["hooks"]:
            kept.append(entry)
    if "hooks" in settings and "PermissionRequest" in settings["hooks"]:
        if kept:
            settings["hooks"]["PermissionRequest"] = kept
        else:
            del settings["hooks"]["PermissionRequest"]
    return settings


def confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config-dir", default="~/.claude",
                    help="Claude config dir to install into (default: ~/.claude)")
    ap.add_argument("--uninstall", action="store_true", help="remove the hook instead of installing it")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    if not HOOK_SRC.exists():
        sys.exit(f"error: {HOOK_SRC} not found (run from the repo root)")

    config_dir = Path(os.path.expanduser(args.config_dir))
    hooks_dir = config_dir / "hooks"
    hook_dest = hooks_dir / "prompt-logger.js"
    settings_path = config_dir / "settings.json"
    log_path = os.path.expanduser(DEFAULT_LOG)

    if args.uninstall:
        print(f"This will REMOVE the cua prompt-logger from {config_dir}:")
        print(f"  - delete {hook_dest} (if present)")
        print(f"  - remove its PermissionRequest entry from {settings_path}")
        print(f"  (your log at {log_path} is left in place; delete it yourself if you want)")
        if not confirm("\nProceed? [y/N] ", args.yes):
            print("Aborted. Nothing changed.")
            return
        settings = load_settings(settings_path)
        settings = remove_hook(settings, hook_dest)
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        if hook_dest.exists():
            hook_dest.unlink()
        print("Removed. Restart Claude Code so it reloads settings.")
        return

    print("cua prompt-logger — install")
    print()
    print("What this does:")
    print(f"  1. copy   {HOOK_SRC}")
    print(f"       ->   {hook_dest}")
    print(f"  2. add a PermissionRequest hook entry to {settings_path}")
    print(f"       (merged in; your other hooks are untouched)")
    print()
    print("What it logs (shape only, never values — see hooks/README.md):")
    print(f"  one tab-separated row per permission prompt -> {log_path}")
    print("  e.g.  2026-11-12T...  prompt  default  ~/code/x  Bash: git status  ...")
    print()
    print("It is a pure observer: it NEVER approves or denies a prompt, and fails")
    print("open on any error. Nothing leaves your machine.")
    print()

    settings = load_settings(settings_path)
    if already_registered(settings, hook_dest) and hook_dest.exists():
        print("Already installed. Nothing to do.")
        return

    if not confirm("Install? [y/N] ", args.yes):
        print("Aborted. Nothing changed.")
        return

    hooks_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HOOK_SRC, hook_dest)
    os.chmod(hook_dest, 0o755)
    if not already_registered(settings, hook_dest):
        settings = add_hook(settings, hook_dest)
    config_dir.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")

    print()
    print("Installed. Two more steps:")
    print(f"  1. add this line to your .env so `cua waits` reads the log:")
    print(f"       PROMPT_LOG={DEFAULT_LOG}")
    print("  2. restart Claude Code so it loads the new hook.")


if __name__ == "__main__":
    main()
