#!/usr/bin/env node
/**
 * cua prompt-logger — a PermissionRequest hook for Claude Code.
 *
 * WHY: Claude Code transcripts record only the permission prompts you *rejected*.
 * They do not record the ones you *approved*. So by default `cua waits` has to
 * INFER which calls prompted, from your allow rules. This hook closes that gap:
 * it logs every real permission prompt (approved ones included), as a SHAPE only,
 * so `cua waits` can report exactly which commands you keep approving by hand
 * (its `approved_prompts` block) — the precise allow-list triage.
 *
 * WHAT IT DOES: nothing but log. It is a pure observer — it NEVER allows or denies
 * a prompt, so installing it cannot change which dialogs you see or approve. On any
 * error it exits silently (fail-open), leaving the normal dialog untouched.
 *
 * PRIVACY: it records a SHAPE, never values. For a Bash call it keeps the tool name
 * and the first word or two of the command (e.g. `Bash: git status`); quoted strings,
 * `=values`, and long tokens are masked out before anything is written. For a file
 * tool it keeps a truncated path pattern, never contents. The log lives on your
 * machine only (default ~/.local/state/cua/prompts.log); nothing leaves your computer.
 *
 * LOG FORMAT (tab-separated, one row per prompt — the columns `cua waits` reads):
 *   time  kind  mode  project  shape  tool_use_id  session_id  call_hash
 *   - kind is always "prompt" here (this logger does not coach or auto-approve).
 *   - call_hash is a 12-hex fingerprint of the tool name + input, so `cua waits`
 *     can join a logged prompt to the exact transcript call. It is NOT reversible
 *     and stores nothing readable.
 *
 * CONFIG: set CUA_PROMPT_LOG to log somewhere other than the default path.
 * Point your .env PROMPT_LOG at the same file so `cua waits` reads it.
 *
 * Requires only Node's standard library. Register it under hooks.PermissionRequest
 * in ~/.claude/settings.json (the installer does this for you).
 */

'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const HOME = process.env.HOME || require('os').homedir() || '';
const LOG_PATH =
  process.env.CUA_PROMPT_LOG ||
  path.join(HOME, '.local/state/cua/prompts.log'); // survives reboots

// Mask anything that could carry a value: quoted strings, =assignments, long tokens.
const mask = (s) =>
  s
    .replace(/("[^"]*"|'[^']*')/g, '<q>')
    .replace(/=\S*/g, '=…')
    .replace(/[A-Za-z0-9_-]{24,}/g, '<long>');

function shapeOf(payload) {
  const tool = payload.tool_name || '?';
  const input = payload.tool_input || {};
  if (tool === 'Bash') {
    const words = mask(String(input.command || '').trim()).split(/\s+/);
    const shape = [words[0]];
    if (words[0] === 'git' && words[1] === '-C' && words[3]) shape.push('-C', words[3]);
    else if (words[1] && !words[1].startsWith('-')) shape.push(words[1]);
    const multi = /\n/.test(String(input.command || '').trim()) ? ' [multi-line]' : '';
    return `Bash: ${shape.join(' ').slice(0, 60)}${multi}`;
  }
  const p = input.file_path || input.notebook_path || input.path;
  if (p) {
    const cwd = payload.cwd || '';
    const rel =
      cwd && p.startsWith(cwd + '/')
        ? '<proj>/' + p.slice(cwd.length + 1)
        : HOME && p.startsWith(HOME)
          ? '~' + p.slice(HOME.length)
          : p;
    return `${tool}: ${rel.split('/').slice(0, 4).join('/')}${rel.split('/').length > 4 ? '/**' : ''}`;
  }
  if (tool === 'Skill') return `Skill: ${input.skill || '?'}`;
  if (tool === 'WebFetch') {
    try {
      return `WebFetch: ${new URL(input.url).hostname}`;
    } catch {
      return 'WebFetch';
    }
  }
  return tool;
}

// JSON with object keys sorted recursively, so the same input hashes the same
// everywhere. Mirrors Python's json.dumps(..., sort_keys=True,
// separators=(",", ":"), ensure_ascii=False) that `cua waits` uses to recompute it.
function canonical(v) {
  if (Array.isArray(v)) return '[' + v.map(canonical).join(',') + ']';
  if (v && typeof v === 'object') {
    return (
      '{' +
      Object.keys(v)
        .sort()
        .map((k) => JSON.stringify(k) + ':' + canonical(v[k]))
        .join(',') +
      '}'
    );
  }
  return JSON.stringify(v);
}

// 12-hex fingerprint of tool name + input (nothing readable is stored), so the
// audit tool can join a dialog to the exact transcript call. Must match cua's
// call_hash(): sha256 over canonical JSON of {"i": input, "n": name}.
function callHash(payload) {
  try {
    return crypto
      .createHash('sha256')
      .update(canonical({ i: payload.tool_input || {}, n: payload.tool_name || '' }))
      .digest('hex')
      .slice(0, 12);
  } catch {
    return '';
  }
}

function log(payload) {
  try {
    fs.mkdirSync(path.dirname(LOG_PATH), { recursive: true });
    const project = HOME ? (payload.cwd || '').replace(HOME, '~') : payload.cwd || '';
    fs.appendFileSync(
      LOG_PATH,
      [
        new Date().toISOString(),
        'prompt',
        payload.permission_mode || '?',
        project,
        shapeOf(payload),
        payload.tool_use_id || '',
        payload.session_id || '',
        callHash(payload),
      ].join('\t') + '\n'
    );
  } catch {}
}

function main() {
  let payload;
  try {
    payload = JSON.parse(fs.readFileSync(0, 'utf8'));
  } catch {
    process.exit(0); // fail open: normal dialog
  }
  log(payload);
  process.exit(0); // never allow/deny — just observe
}

if (require.main === module) {
  try {
    main();
  } catch {
    process.exit(0); // fail open
  }
}

module.exports = { shapeOf, callHash, canonical };
