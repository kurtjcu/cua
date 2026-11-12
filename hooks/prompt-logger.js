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
 * PRIVACY: a row holds the time, the permission mode, the project directory (home shown
 * as ~, empty when it is not a string), a SHAPE, the tool-use and session ids, and the
 * call fingerprint described under LOG FORMAT. The SHAPE is the tool name plus a short
 * pattern; it never holds file contents or a Bash command's arguments.
 * For a Bash call the shape is the program name only: the leading run of letters, digits
 * and `_ . + -` at the start of the command after masking, plus a `[multi-line]` flag.
 * Nothing after that name is written, with or without whitespace: `git status` logs
 * `Bash: git`, `cat<<<x` logs `Bash: cat` and `echo${IFS}x` logs `Bash: echo`.
 * THE PATH RULE: a program given with a path logs its file name only when the path ends
 * at a space, tab, newline or the end of the command and no `/` appears anywhere after it.
 * So `/opt/x/deploy.sh --y` logs `Bash: deploy.sh`, and nothing before the last `/` is
 * logged. Any other path logs `<other>`: `/home/u/Acme Corp/run.sh`, `/x/acme;ls`,
 * `/opt/c=acme/run` and `./run.sh src/a` all do.
 * It is also `<other>` for a name of 24 or more characters (counting `.` and `+`), a
 * quoted first word, `{a,b}`, a subshell, `$(...)`, and a command that is not a string.
 * Masking is applied first: quoted strings (read the way the shell reads them, escapes
 * included), everything after an unpaired or backslash-escaped quote, `=values`, and
 * tokens of 24+ characters.
 * THE LIMITS: the hook cannot tell a program name from a secret typed as the command
 * word itself, so a bare `hunter2` logs `Bash: hunter2`. Nor can it see a path through an
 * unquoted space, because the shell runs the word before the space: `Acme Corp/run.sh`
 * logs `Bash: Acme` and `~/a b` logs `Bash: a`.
 * Only Bash shapes are masked. A file tool logs a path pattern (project- or
 * home-relative, the first four segments), Skill the skill name and WebFetch the
 * hostname, each cut to 60 characters.
 * Control characters in any column are written as spaces. A new log is created 0600
 * inside a 0700 directory. The log lives on your machine only (default
 * ~/.local/state/cua/prompts.log); nothing leaves your computer.
 *
 * LOG FORMAT (tab-separated, one row per prompt — the columns `cua waits` reads):
 *   time  kind  mode  project  shape  tool_use_id  session_id  call_hash
 *   - kind is always "prompt" here (this logger does not coach or auto-approve).
 *   - call_hash is an unsalted 12-hex fingerprint of the tool name + input, so
 *     `cua waits` can join a logged prompt to the exact transcript call. It cannot
 *     be read back, but anyone holding the log can confirm a guess at an exact command.
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
const HOME_DIR = HOME.replace(/\/+$/, ''); // no trailing slash, so HOME + '/' is the path boundary
const LOG_PATH =
  process.env.CUA_PROMPT_LOG ||
  path.join(HOME, '.local/state/cua/prompts.log'); // survives reboots

// Any control character, DEL, NEL, or line/paragraph separator would shift columns or start a new row
// (the reader splits lines on several of them). Write each run as one space.
const clean = (s) => String(s).replace(/[\u0000-\u001f\u007f\u0085\u2028\u2029]+/g, ' ');

// A non-string value (a crafted or odd cwd) is treated as absent, so homeRel and startsWith only ever
// see a string and the row is not dropped.
const asText = (v) => (typeof v === 'string' ? v : '');

// Replace every quoted string with <q>, reading quotes the way the shell does. A regex that pairs
// quote characters cannot do this: it splits a string at a backslash-escaped quote and leaks the
// text behind it. Rules:
//   - outside quotes, a backslash-escaped quote masks the rest of the command (we cannot tell what
//     it guards), and so does a quote that never closes;
//   - 'single' strings have no escapes and end at the next single quote;
//   - "double" and $'ANSI-C' strings let a backslash consume the next character.
// This over-masks apostrophes (don't -> don<q>), which is the safe direction.
function maskQuotes(text) {
  let out = '';
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    if (c === '\\') {
      const n = text[i + 1];
      if (n === '"' || n === "'") return out + '<q>';
      out += n === undefined ? c : c + n;
      i += n === undefined ? 1 : 2;
    } else if (c === "'" || c === '"') {
      const escapes = c === '"' || text[i - 1] === '$';
      let j = i + 1;
      while (j < text.length && text[j] !== c) j += escapes && text[j] === '\\' ? 2 : 1;
      out += '<q>';
      if (j >= text.length) return out;
      i = j + 1;
    } else {
      out += c;
      i += 1;
    }
  }
  return out;
}

// Mask anything that could carry a value: quoted strings, =assignments, long tokens.
const mask = (s) =>
  maskQuotes(s)
    .replace(/=\S*/g, '=…')
    .replace(/[A-Za-z0-9_-]{24,}/g, '<long>');

const SHAPE_MAX = 60; // longest body of any shape, in characters
const OTHER = '<other>'; // the Bash shape when the command does not start with a command name
// The mask's long-token length: a Bash name this long or longer, counting dots and plus signs, is
// logged as OTHER.
const LONG_WORD = 24;

// The program name of a Bash command: the leading run of command-name characters of the masked
// command. A whitespace split is NOT the program name: the shell needs no whitespace between a
// program and its arguments (here-strings, ${IFS}, brace expansion, redirections, semicolons, pipes,
// && and backticks all join them), so everything after the leading run is dropped, with or without
// whitespace. A path-qualified program logs its file name only when the path ends at a space, tab,
// newline or the end of the command and no / follows anywhere after it. Otherwise a space, an escaped
// space, a semicolon, an equals sign, an ampersand or a redirection inside a path would make a
// directory look like the program, and paths can name users, clients and projects. A name of 24 or
// more characters, counting dots and plus signs, is OTHER, and so is a command that is not a string,
// a quoted, symbol-led or long first word, a subshell and an expansion. The limits: a secret typed as
// the command word itself IS the program name, and an unquoted space in the first word ends what the
// shell runs, so `Acme Corp/run.sh` logs `Acme`.
function programOf(command) {
  const text = asText(command).trim();
  const masked = mask(text);
  const lead = /^[A-Za-z0-9_.+~\/-]*/.exec(masked)[0];
  if (lead.includes('/')) {
    // A word break must follow the path (not \s: a no-break space is not a break to the shell), and the
    // raw command must hold no slash after it. The lead is a prefix of the raw text because no mask
    // rule rewrites a lead that ends at a word break.
    if (!/^[ \t\n]?$/.test(masked.charAt(lead.length)) || text.indexOf('/', lead.length) !== -1) return OTHER;
  }
  const base = lead.slice(lead.lastIndexOf('/') + 1);
  return /^[A-Za-z0-9_.+-]+$/.test(base) && base.length < LONG_WORD ? base : OTHER;
}

// Replace HOME with ~ only at a path boundary: HOME itself or HOME + '/...'. A sibling such as
// HOME + '2/x' is left alone.
function homeRel(p) {
  if (HOME_DIR && (p === HOME_DIR || p.startsWith(HOME_DIR + '/'))) return '~' + p.slice(HOME_DIR.length);
  return p;
}

function shapeOf(payload) {
  const tool = payload.tool_name || '?';
  const input = payload.tool_input || {};
  if (tool === 'Bash') {
    // The shape is programOf's token only; nothing after it is ever logged (URLs, DSNs, paths and
    // key:value strings all live in arguments). `cua waits` reads only the tool prefix of the shape
    // (cua/waits.py), so nothing is lost for measurement.
    const multi = /\n/.test(asText(input.command).trim()) ? ' [multi-line]' : '';
    return `Bash: ${programOf(input.command)}${multi}`;
  }
  const p = [input.file_path, input.notebook_path, input.path].find((x) => typeof x === 'string' && x);
  if (p) {
    const cwd = asText(payload.cwd);
    const rel =
      cwd && p.startsWith(cwd + '/')
        ? '<proj>/' + p.slice(cwd.length + 1)
        : homeRel(p);
    // Every shape body is cut to SHAPE_MAX characters. The tool name before the colon is never cut:
    // cua/waits.py reads it as the tool.
    return `${tool}: ${(
      rel.split('/').slice(0, 4).join('/') + (rel.split('/').length > 4 ? '/**' : '')
    ).slice(0, SHAPE_MAX)}`;
  }
  if (tool === 'Skill') return `Skill: ${String(input.skill || '?').slice(0, SHAPE_MAX)}`;
  if (tool === 'WebFetch') {
    try {
      return `WebFetch: ${new URL(input.url).hostname.slice(0, SHAPE_MAX)}`;
    } catch {
      return 'WebFetch';
    }
  }
  return tool;
}

// JSON with object keys sorted recursively (UTF-16 code-unit order, JS .sort()), so the
// same input hashes the same everywhere. This is the reference form: `cua waits`'
// call_hash() in Python emulates this JSON.stringify output (number formatting, key
// order, raw non-ASCII text) to recompute the hash.
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
// audit tool can join a dialog to the exact transcript call. cua's call_hash()
// emulates this: sha256 over canonical JSON of {"i": input, "n": name}.
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
    // Modes apply only when the directory or file is created; an existing log keeps its mode.
    fs.mkdirSync(path.dirname(LOG_PATH), { recursive: true, mode: 0o700 });
    const project = homeRel(asText(payload.cwd));
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
      ].map(clean).join('\t') + '\n',
      { mode: 0o600 }
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

module.exports = { shapeOf, callHash, canonical, homeRel };
