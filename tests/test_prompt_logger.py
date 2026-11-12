"""The prompt-logger hook, run for real under node, against the Python reader.

hooks/prompt-logger.js writes the rows `cua waits` reads, and computes in JavaScript the call hash that
waits.call_hash() recomputes in Python. These tests spawn the real hook script and compare. They skip
cleanly when node is missing (set CUA_HOOK_TEST_NODE to point at a node binary).

The hook subprocess gets an environment built from scratch (PATH, a temp HOME, a temp CUA_PROMPT_LOG),
so a test can never reach the real HOME, the real prompt log or the installed hook.

It also checks, without node, that hooks/install.py reads a settings file safely, calling load_settings
on temp files only.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cua import waits  # noqa: E402

HOOK = os.path.join(ROOT, "hooks", "prompt-logger.js")
README = os.path.join(ROOT, "hooks", "README.md")
NODE_BIN_ENV = "CUA_HOOK_TEST_NODE"
NODE = shutil.which(os.environ.get(NODE_BIN_ENV) or "node")
# No apostrophes or single quotes in this text: unittest prints a skip reason with repr().
NODE_SKIP_REASON = ("node not found on PATH (tried CUA_HOOK_TEST_NODE, then node); prompt-logger hook tests "
                    "skipped. Run inside the dev distrobox or set CUA_HOOK_TEST_NODE to a node binary.")
# Every Bash shape any test checks must look like this: a command-name token of 1 to 23 characters (the
# documented bound: 24 or more logs the other-token) or the other-token, plus the optional multi-line
# flag, nothing else. A future operator, argument or over-long word leak fails the regex.
BASH_SHAPE_RE = re.compile(r"^Bash: ([A-Za-z0-9_.+-]{1,23}|<other>)( \[multi-line\])?$")


# (JSON number literal, what JavaScript JSON.stringify writes for it). Verified against node v22.
# Shared by the node-free TestJsNumber and the node-gated hash matrix so they cannot drift apart.
JS_NUMBER_CASES = (
    ("1.0", "1"),
    ("1e20", "100000000000000000000"),
    ("1e16", "10000000000000000"),
    ("1e-7", "1e-7"),
    ("-0.0", "0"),
    ("12345678901234567890", "12345678901234567000"),
    ("0.1", "0.1"),
    ("1e21", "1e+21"),
    ("1.5e-6", "0.0000015"),
    ("0.000001", "0.000001"),
    ("9007199254740993", "9007199254740992"),
    ("1152921504606846976", "1152921504606847000"),
    ("123.456", "123.456"),
    ("-2.5e-8", "-2.5e-8"),
    ("100", "100"),
    ("5e-324", "5e-324"),
    # Beyond the double range JavaScript parses Infinity, and JSON.stringify writes null.
    ("1" + "0" * 400, "null"),
    ("-1" + "0" * 400, "null"),
    ("1e400", "null"),
)


def payload_text(home, tool_input_json="{}", tool="Bash", cwd=None, **fields):
    """The hook's stdin as text. `tool_input_json` is spliced in raw so number literals such as 1.0 reach
    node exactly as written (a Python dict would already have lost the literal). None omits tool_input."""
    env = {"session_id": "sess-1", "cwd": cwd if cwd is not None else home + "/code/work/proj",
           "permission_mode": "default", "tool_name": tool}
    env.update(fields)
    text = json.dumps(env)
    if tool_input_json is None:
        return text
    return text[:-1] + ', "tool_input": ' + tool_input_json + "}"


@unittest.skipUnless(NODE, NODE_SKIP_REASON)
class TestPromptLoggerHook(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.log = os.path.join(self.tmp, "prompts.log")

    def run_hook_raw(self, text, log=None, home=None):
        # The env has exactly these three keys: the real HOME and the real prompt log are unreachable.
        return subprocess.run([NODE, HOOK], input=text.encode("utf-8"),
                              env={"PATH": os.environ.get("PATH", ""), "HOME": home or self.home,
                                   "CUA_PROMPT_LOG": log or self.log},
                              capture_output=True, timeout=10)

    def run_hook(self, text, home=None):
        r = self.run_hook_raw(text, home=home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, b"")
        with open(self.log, encoding="utf-8") as f:
            lines = f.read().splitlines()
        return lines[-1].split("\t")

    def test_hash_parity_zero_fraction_float(self):
        raw = '{"command":"ls","t":1.0}'
        row = self.run_hook(payload_text(self.home, raw))
        self.assertEqual(row[7], waits.call_hash("Bash", json.loads(raw)))

    def test_pinned_payload_hash(self):
        # Same payload as tests/test_waits.py test_call_hash_matches_the_hook.
        inp = {"description": "naïve ünï", "command": "ls -la /tmp", "timeout": 5000,
               "nested": {"b": [1, 2, {"z": 1, "a": None}], "a": True}}
        row = self.run_hook(payload_text(self.home, json.dumps(inp, ensure_ascii=False)))
        self.assertEqual(row[7], "0fc42cbdff13")
        self.assertEqual(waits.call_hash("Bash", inp), "0fc42cbdff13")

    def assert_parity(self, raw, tool="Bash"):
        row = self.run_hook(payload_text(self.home, raw, tool=tool))
        self.assertRegex(row[7], r"^[0-9a-f]{12}$")
        self.assertEqual(row[7], waits.call_hash(tool, json.loads(raw)))

    def test_hash_parity_matrix(self):
        cases = ['{"command":"x","v":%s}' % lit for lit, _ in JS_NUMBER_CASES]
        cases += [
            '{"description":"naïve 中文 😀","command":"ls"}',
            '{"😀":1,"～":2,"a":3,"é":4}',
            # control characters: the escapes \u0001 and \u2028 written as escape text, plus one raw DEL (0x7f)
            '{"command":"a\\u0001b\\u2028c\x7fd"}',
            '{"a":[1.0]}',
            '{"a":null}',
            '{}',
        ]
        for raw in cases:
            with self.subTest(case=raw):
                self.assert_parity(raw)

    def test_hash_parity_absent_tool_input(self):
        row = self.run_hook(payload_text(self.home, tool_input_json=None))
        self.assertEqual(row[7], waits.call_hash("Bash", None))
        self.assertEqual(row[7], waits.call_hash("Bash", {}))

    def test_hash_parity_non_bash_tool(self):
        self.assert_parity('{"file_path":"/x/a.txt","old_string":"a","new_string":"b"}', tool="Edit")


    def bash_row(self, command, **fields):
        return self.run_hook(payload_text(self.home, json.dumps({"command": command}), **fields))

    def bash_line(self, command):
        """Run the real hook on one Bash command; return (raw logged line, its fields). The shape must
        match BASH_SHAPE_RE whatever the command was."""
        if os.path.exists(self.log):
            os.remove(self.log)
        r = self.run_hook_raw(payload_text(self.home, json.dumps({"command": command})))
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        with open(self.log, encoding="utf-8") as f:
            line = f.read().splitlines()[-1]
        fields = line.split("\t")
        self.assertEqual(len(fields), len(waits.PROMPT_LOG_COLUMNS))
        self.assertRegex(fields[4], BASH_SHAPE_RE)
        return line, fields

    def read_back(self):
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            return waits.load_prompt_log(self.log)

    def test_no_whitespace_arguments_never_logged(self):
        # Plain Python strings: ${IFS}, $IFS and $9 are literal text for the shell to expand, and the
        # no-break space below is a single U+00A0 character.
        cases = (
            ("echo;curl${IFS}-u${IFS}admin:hunter2${IFS}http://x", "Bash: echo"),
            ("echo$IFS$9hunter2", "Bash: echo"),
            ("echo>/tmp/hunter2", "Bash: echo"),
            ("echo>/home/u/hunter2", "Bash: echo"),
            ("echo|hunter2", "Bash: echo"),
            ("echo&&hunter2", "Bash: echo"),
            ("echo`hunter2`", "Bash: echo"),
            ("echo(hunter2)", "Bash: echo"),
            ("echo hunter2", "Bash: echo"),
            ("echo hunter2", "Bash: echo"),
            ("{echo,hunter2}", "Bash: <other>"),
            ("(echo hunter2)", "Bash: <other>"),
            ("$(echo hunter2)", "Bash: <other>"),
            ("cat<<<hunter2", "Bash: cat"),
            ("curl-u:admin:hunter2 x", "Bash: curl-u"),
            ("user@host:hunter2", "Bash: user"),
        )
        for command, shape in cases:
            with self.subTest(command=command):
                line, fields = self.bash_line(command)
                self.assertEqual(fields[4], shape)
                self.assertNotIn("hunter2", line)
                rows = self.read_back()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["tool"], "Bash")
                for v in rows[0].values():
                    self.assertNotIn("hunter2", str(v))

    def test_path_qualified_program_logs_basename(self):
        # A path-qualified program logs its file name only when the path ends at a space, tab, newline or
        # the end of the command and no / follows anywhere after it. Anything else is the other-token, so
        # no text before the last / of the command is ever logged.
        # (command, expected shape, literals that must be absent from the whole raw line)
        cases = (
            ("/home/u/clients/acme-corp/deploy.sh --x", "Bash: deploy.sh", ("acme-corp", "clients", "/home/u")),
            ("~/bin/deploy.sh --x", "Bash: deploy.sh", ("~/bin",)),
            ("./scripts/run-tests.sh", "Bash: run-tests.sh", ("scripts",)),
            ("/usr/bin/ x", "Bash: <other>", ("/usr/bin",)),
            ("/home/alice/Acme Corp/run.sh", "Bash: <other>", ("Acme", "Corp", "alice", "run.sh")),
            (r"/home/alice/Acme\ Corp/run.sh", "Bash: <other>", ("Acme", "alice")),
            ("/home/alice/Acme Big Corp/run.sh --x", "Bash: <other>", ("Acme", "alice")),
            ("/home/alice/Acme\tCorp/run.sh", "Bash: <other>", ("Acme", "alice")),
            ("~/Acme Corp", "Bash: <other>", ("Acme",)),
            ("/home/alice/clients/acme-corp;ls", "Bash: <other>", ("acme-corp", "clients", "alice")),
            ("/x/acme-corp&&ls", "Bash: <other>", ("acme-corp",)),
            ("/x/acme-corp>out", "Bash: <other>", ("acme-corp",)),
            ("/opt/client=acme/run", "Bash: <other>", ("client", "acme")),
            ("./scripts/run.sh tests/x.py", "Bash: <other>", ("scripts", "run.sh")),
        )
        for command, shape, absent in cases:
            with self.subTest(command=command):
                line, fields = self.bash_line(command)
                self.assertEqual(fields[4], shape)
                for text in absent:
                    self.assertNotIn(text, line)

    def test_command_word_itself_is_the_logged_name(self):
        # The documented limits: the hook cannot tell a program name from a secret typed as the command
        # word; a name under 24 characters, counting . and +, is logged, and 24 or more is the other-token,
        # with or without a path; an unquoted space ends the word the shell runs, so the part before it is
        # logged. The docs say so, and this test keeps them honest.
        cases = (
            ("hunter2", "Bash: hunter2"),
            ("./hunter2 --flag", "Bash: hunter2"),
            ("k" * 24, "Bash: <other>"),
            ("", "Bash: <other>"),
            ("a" * 20 + "." + "b" * 20 + "." + "c" * 20, "Bash: <other>"),
            ("AbCdEfGhIjKlMnOpQrSt+UvWxYz0123456+789AbCdEfGhIjKlMn", "Bash: <other>"),
            ("a" * 11 + "." + "b" * 12, "Bash: <other>"),
            ("a" * 11 + "." + "b" * 11, "Bash: " + "a" * 11 + "." + "b" * 11),
            ("/opt/x/" + "a" * 11 + "." + "b" * 12 + " --y", "Bash: <other>"),
            ("Acme Corp/run.sh", "Bash: Acme"),
            ("~/a b", "Bash: a"),
        )
        for command, shape in cases:
            with self.subTest(command=command):
                line, fields = self.bash_line(command)
                self.assertEqual(fields[4], shape)
                if shape == "Bash: <other>":
                    for piece in re.split(r"[./+ ]", command):
                        if len(piece) >= 8:
                            self.assertNotIn(piece, line)

    def test_non_string_command_logs_other(self):
        # A command that is not a string is never stringified into the shape.
        for command in (["secretword"], ["ls\nrm"], 987654321, True, {"a": "secretword"}):
            with self.subTest(command=command):
                line, fields = self.bash_line(command)
                self.assertEqual(fields[4], "Bash: <other>")
                self.assertNotIn("secretword", line)
                self.assertNotIn("987654321", line)
                rows = self.read_back()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["tool"], "Bash")

    def test_mask_shapes(self):
        # A Bash shape is the program name only. (command, exact logged shape, literals that must not
        # appear anywhere in the logged line)
        cases = (
            ('curl -H "Authorization: Bearer sk-live-SECRET" https://x', "Bash: curl", ("sk-live-SECRET", "Bearer")),
            ("echo 'hunter2 is the pw'", "Bash: echo", ("hunter2", "the pw")),
            ("export API_KEY=supersecretvalue", "Bash: export", ("supersecretvalue", "API_KEY")),
            # A leading assignment logs the variable name only: the value and the program are not kept.
            ("TOKEN=abc123 npm test", "Bash: TOKEN", ("abc123", "npm")),
            ("deploy --token=abcdef123456", "Bash: deploy", ("abcdef123456",)),
            ("cat ghp_abcdefghijklmnopqrstuvwxyz0123456789", "Bash: cat",
             ("ghp_abcdefghijklmnopqrstuvwxyz0123456789",)),
            ('echo "unterminated secret', "Bash: echo", ("unterminated",)),
            ("echo don't stop", "Bash: echo", ("stop",)),
            ("git -C /home/u/code/x status", "Bash: git", ("/home/u", "status")),
            ("ls\nrm -rf /", "Bash: ls [multi-line]", ("rm",)),
            ("curl https://user:pw123@host.example/x", "Bash: curl", ("pw123", "host.example")),
            ("psql postgres://admin:hunter2@db/x", "Bash: psql", ("hunter2", "admin")),
            ("echo password:hunter2", "Bash: echo", ("hunter2", "password")),
            ("ssh-add /home/u/.ssh/key_secretname", "Bash: ssh-add", ("key_secretname", "/home/u")),
            ("git -C /home/u/x push https://u:s3cr3tpw@h/r", "Bash: git", ("s3cr3tpw", "push")),
            ("git status", "Bash: git", ("status",)),
            ("docker compose up", "Bash: docker", ("compose",)),
        )
        for command, shape, secrets in cases:
            with self.subTest(command=command):
                self.run_hook_raw(payload_text(self.home, json.dumps({"command": command})))
                with open(self.log, encoding="utf-8") as f:
                    line = f.read().splitlines()[-1]
                os.remove(self.log)
                fields = line.split("\t")
                self.assertEqual(fields[4], shape)
                self.assertRegex(fields[4], BASH_SHAPE_RE)
                self.assertEqual(len(fields), len(waits.PROMPT_LOG_COLUMNS))
                self.assertNotIn('"', fields[4])
                self.assertNotIn("'", fields[4])
                for secret in secrets:
                    self.assertNotIn(secret, line)

    def test_second_word_never_logged(self):
        # (command, expected shape, the argument that must not reach the line)
        cases = (
            ("echo hunter2", "Bash: echo", "hunter2"),
            ("curl https://user:pw9q@h/x", "Bash: curl", "pw9q"),
            ("git push", "Bash: git", "push"),
            ("npm test", "Bash: npm", "test"),
            ("kubectl get secrets", "Bash: kubectl", "secrets"),
        )
        for command, shape, arg in cases:
            with self.subTest(command=command):
                row = self.bash_row(command)
                os.remove(self.log)
                self.assertEqual(row[4], shape)
                self.assertNotIn(arg, "\t".join(row))
                self.assertRegex(row[4], BASH_SHAPE_RE)

    def test_mask_quoted_secrets_never_logged(self):
        # Raw strings: every backslash below is one literal backslash in the command the hook sees.
        commands = (
            r'echo "pw is \"hunter2\" ok"',
            r'echo "\"hunter2\""',
            r"echo 'it'\''s hunter2'",
            r"echo 'it'\''shunter2'",
            r'"/opt/my \"hunter2\" tool" --run',
            r'curl -d "{\"token\":\"hunter2\"}" https://x',
            r"echo $'it\'shunter2'",
            r'echo \"hunter2\"',
            r'echo "x\\" "hunter2"',
            r'git commit -m "fix \"hunter2\" now"',
        )
        for cmd in commands:
            with self.subTest(command=cmd):
                if os.path.exists(self.log):
                    os.remove(self.log)
                r = self.run_hook_raw(payload_text(self.home, json.dumps({"command": cmd})))
                self.assertEqual((r.returncode, r.stdout), (0, b""))
                with open(self.log, encoding="utf-8") as f:
                    line = f.read().splitlines()[-1]
                fields = line.split("\t")
                self.assertEqual(len(fields), len(waits.PROMPT_LOG_COLUMNS))
                self.assertNotIn("hunter2", line)
                self.assertRegex(fields[4], BASH_SHAPE_RE)
                self.assertNotIn('"', fields[4])
                self.assertNotIn("'", fields[4])
                with mock.patch.dict(os.environ, {"HOME": self.home}):
                    rows = waits.load_prompt_log(self.log)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["tool"], "Bash")
                for v in rows[0].values():
                    self.assertNotIn("hunter2", str(v))

    def test_mask_long_token_threshold(self):
        # Only the leading token is logged, so the 24-character threshold is pinned on it. The mask turns
        # a 24-character first word into the long-token marker, which is not a command-name token, so
        # the shape is the other-token.
        self.assertEqual(self.bash_row("a" * 23 + " x")[4], "Bash: " + "a" * 23)
        os.remove(self.log)
        row = self.bash_row("a" * 24 + " x")
        self.assertEqual(row[4], "Bash: <other>")
        self.assertNotIn("a" * 24, "\t".join(row))
        os.remove(self.log)
        self.assertEqual(self.bash_row("a_-" * 8 + " x")[4], "Bash: <other>")  # 24 characters

    def test_project_home_boundary(self):
        cases = ((self.home, "~"),
                 (self.home + "/code/work/proj", "~/code/work/proj"),
                 (self.home + "2/x", self.home + "2/x"),
                 ("/mnt" + self.home + "/x", "/mnt" + self.home + "/x"))
        for cwd, project in cases:
            with self.subTest(cwd=cwd):
                row = self.run_hook(payload_text(self.home, cwd=cwd))
                self.assertEqual(row[3], project)

    def test_file_shape_home_boundary(self):
        def shape(file_path):
            raw = json.dumps({"file_path": file_path})
            return self.run_hook(payload_text(self.home, raw, tool="Edit", cwd="/elsewhere"))[4]

        self.assertEqual(shape(self.home + "/notes/a.txt"), "Edit: ~/notes/a.txt")
        self.assertFalse(shape(self.home + "2/notes/a.txt").startswith("Edit: ~"))

    def test_column_contract_sentinels(self):
        text = payload_text(self.home, json.dumps({"command": "ls -la"}), permission_mode="plan",
                            tool_use_id="toolu_SENT", session_id="sess-SENT", cwd=self.home + "/code/work/proj")
        raw = self.run_hook(text)
        self.assertEqual(len(raw), len(waits.PROMPT_LOG_COLUMNS))
        self.assertEqual(len(raw), 8)
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            rows = waits.load_prompt_log(self.log)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["mode"], r["id"], r["session"], r["tool"], r["project"]),
                         ("plan", "toolu_SENT", "sess-SENT", "Bash", "work/proj"))
        self.assertEqual(r["hash"], waits.call_hash("Bash", {"command": "ls -la"}))

    def test_control_chars_cannot_forge_columns_or_rows(self):
        # Python escapes: the payload carries real control characters.
        forged = self.home + "/a\n2026-01-01T00:00:00.000Z\tprompt\tdefault\t~/x\tBash: forged"
        text = payload_text(self.home, json.dumps({"file_path": forged}), tool="Edit",
                            session_id="s\tX\ny", tool_use_id="t\r\x85Z", permission_mode="plan\x0bx",
                            cwd=self.home + "/code/w\u2028ork")
        r = self.run_hook_raw(text)
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        with open(self.log, "rb") as f:
            raw = f.read()
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertTrue(raw.endswith(b"\n"))
        for bad in (b"\r", b"\x0b", "\u0085".encode("utf-8"), "\u2028".encode("utf-8")):
            self.assertNotIn(bad, raw)
        self.assertEqual(len(raw.decode("utf-8").rstrip("\n").split("\t")), len(waits.PROMPT_LOG_COLUMNS))
        stats = waits.new_prompt_log_stats()
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            rows = waits.load_prompt_log(self.log, stats=stats)
        self.assertEqual(len(rows), 1)
        self.assertEqual(stats["rows_read"], 1)
        self.assertEqual((rows[0]["session"], rows[0]["id"], rows[0]["mode"]), ("s X y", "t Z", "plan x"))

    def test_log_file_and_dir_are_private(self):
        state = os.path.join(self.tmp, "state", "cua")
        log = os.path.join(state, "prompts.log")
        r = self.run_hook_raw(payload_text(self.home), log=log)
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        self.assertEqual(os.stat(log).st_mode & 0o077, 0)
        self.assertEqual(os.stat(state).st_mode & 0o077, 0)

    def test_home_trailing_slash(self):
        home = self.home + "/"
        row = self.run_hook(payload_text(self.home, cwd=self.home + "/code/work/proj"), home=home)
        self.assertEqual(row[3], "~/code/work/proj")
        os.remove(self.log)
        raw = json.dumps({"file_path": self.home + "/notes/a.txt"})
        row = self.run_hook(payload_text(self.home, raw, tool="Edit", cwd="/elsewhere"), home=home)
        self.assertEqual(row[4], "Edit: ~/notes/a.txt")

    def test_non_string_file_path_still_logs(self):
        row = self.run_hook(payload_text(self.home, json.dumps({"file_path": 123}), tool="Edit"))
        self.assertEqual(len(row), len(waits.PROMPT_LOG_COLUMNS))
        self.assertEqual(row[4], "Edit")
        os.remove(self.log)
        raw = json.dumps({"file_path": 123, "path": self.home + "/notes/a.txt"})
        row = self.run_hook(payload_text(self.home, raw, tool="Edit", cwd="/elsewhere"))
        self.assertEqual(row[4], "Edit: ~/notes/a.txt")

    def test_non_string_cwd_still_logs(self):
        for cwd in (123, {"a": 1}, ["x"], True):
            with self.subTest(cwd=cwd):
                if os.path.exists(self.log):
                    os.remove(self.log)
                r = self.run_hook_raw(payload_text(self.home, json.dumps({"command": "ls"}), cwd=cwd))
                self.assertEqual((r.returncode, r.stdout), (0, b""))
                with open(self.log, encoding="utf-8") as f:
                    lines = f.read().splitlines()
                self.assertEqual(len(lines), 1)
                fields = lines[0].split("\t")
                self.assertEqual(len(fields), len(waits.PROMPT_LOG_COLUMNS))
                self.assertEqual((fields[3], fields[4]), ("", "Bash: ls"))
                rows = self.read_back()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["project"], "")
        os.remove(self.log)
        raw = json.dumps({"file_path": self.home + "/notes/a.txt"})
        row = self.run_hook(payload_text(self.home, raw, tool="Edit", cwd=123))
        self.assertEqual(row[4], "Edit: ~/notes/a.txt")
        os.remove(self.log)
        row = self.run_hook(payload_text(self.home, json.dumps({"file_path": "x/a.txt"}), tool="Edit", cwd=["x"]))
        self.assertEqual(row[4], "Edit: x/a.txt")  # the array is not used as a project prefix

    def test_non_bash_shapes_capped_not_masked(self):
        deep = "/" + "/".join(["d" * 30] * 3) + "/x.txt"
        host = "a" * 40 + "." + "b" * 40 + ".example.com"
        cases = (
            ("Skill", {"skill": "x/" * 100}, "Skill: " + ("x/" * 100)[:60], "Skill", ()),
            # 33 characters: would become the long-token marker if non-Bash shapes were masked.
            ("Skill", {"skill": "spike-findings-claude-usage-audit"},
             "Skill: spike-findings-claude-usage-audit", "Skill", ()),
            ("Edit", {"file_path": deep}, "Edit: " + ("/" + "/".join(["d" * 30] * 3) + "/**")[:60], "Edit", ()),
            ("WebFetch", {"url": "https://" + host + "/p/q?token=secretq"}, "WebFetch: " + host[:60],
             "WebFetch", ("secretq",)),
        )
        for tool, tool_input, shape, expect_tool, absent in cases:
            with self.subTest(tool=tool, shape=shape):
                if os.path.exists(self.log):
                    os.remove(self.log)
                row = self.run_hook(payload_text(self.home, json.dumps(tool_input), tool=tool, cwd="/elsewhere"))
                self.assertEqual(len(row), len(waits.PROMPT_LOG_COLUMNS))
                self.assertEqual(row[4], shape)
                with open(self.log, encoding="utf-8") as f:
                    line = f.read().splitlines()[-1]
                for text in absent:
                    self.assertNotIn(text, line)
                rows = self.read_back()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["tool"], expect_tool)

    def test_default_log_path_untouched(self):
        self.run_hook(payload_text(self.home))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local", "state", "cua", "prompts.log")))

    def test_fail_open(self):
        for text in ("not json", ""):
            with self.subTest(stdin=text):
                r = self.run_hook_raw(text)
                self.assertEqual((r.returncode, r.stdout), (0, b""))
                self.assertFalse(os.path.exists(self.log))
        blocker = os.path.join(self.tmp, "afile")
        with open(blocker, "w") as f:
            f.write("x")
        r = self.run_hook_raw(payload_text(self.home), log=os.path.join(blocker, "prompts.log"))
        self.assertEqual((r.returncode, r.stdout), (0, b""))


class TestJsNumber(unittest.TestCase):
    def test_js_number_matches_json_stringify(self):
        for literal, expected in JS_NUMBER_CASES:
            with self.subTest(literal=literal):
                self.assertEqual(waits._js_number(json.loads(literal)), expected)

    def test_call_hash_survives_huge_integer(self):
        h = waits.call_hash("Bash", {"v": 10 ** 400})
        self.assertRegex(h, r"^[0-9a-f]{12}$")
        self.assertEqual(h, waits.call_hash("Bash", {"v": None}))

    def test_canon_key_order_and_literals(self):
        self.assertEqual(waits._canon({"😀": 1, "～": 2, "a": 3, "é": 4}), '{"a":3,"é":4,"😀":1,"～":2}')
        self.assertEqual(waits._canon(True), "true")
        self.assertEqual(waits._canon(False), "false")
        self.assertEqual(waits._canon(1), "1")
        self.assertEqual(waits._canon(None), "null")
        self.assertEqual(waits._canon([]), "[]")
        self.assertEqual(waits._canon({}), "{}")
        self.assertEqual(waits._canon("naïve"), '"naïve"')


class TestHookContractDocs(unittest.TestCase):
    """The column list is written in three places: waits.PROMPT_LOG_COLUMNS, the hook's LOG FORMAT comment
    and the README. They must agree, node or no node."""

    def column_line(self, path):
        with open(path, encoding="utf-8") as f:
            found = [m.group(0) for m in (re.search(r"\btime\s+kind\s+mode.*call_hash", ln) for ln in f) if m]
        self.assertEqual(len(found), 1, path)
        return found[0].split()

    def test_hook_log_format_comment_matches_reader(self):
        self.assertEqual(self.column_line(HOOK), list(waits.PROMPT_LOG_COLUMNS))

    def test_readme_column_line_matches_reader(self):
        self.assertEqual(self.column_line(README), list(waits.PROMPT_LOG_COLUMNS))

    def test_docs_state_what_the_hook_keeps(self):
        # No doc may claim the hook never logs the command (a single-word command is the program name), and
        # the installer consent text must name every field a row holds.
        install = os.path.join(ROOT, "hooks", "install.py")
        docs = (HOOK, README, os.path.join(ROOT, "README.md"), install, os.path.join(ROOT, "CHANGELOG.md"))
        texts = {}
        for path in docs:
            with open(path, encoding="utf-8") as f:
                texts[path] = f.read()
            self.assertNotIn("never the command", texts[path], path)
        for phrase in ("project path", "session ids", "call fingerprint", "confirm a guess", "program name only",
                       "never its arguments", "path pattern", "skill name", "hostname", "file contents"):
            self.assertIn(phrase, texts[install], phrase)


class TestInstallerSettingsRead(unittest.TestCase):
    """hooks/install.py rewrites the user's settings file, so reading one it cannot decode or parse must
    end in the clean error exit with the file untouched. Only load_settings runs, on temp files."""

    def test_unreadable_settings_exit_cleanly(self):
        spec = importlib.util.spec_from_file_location("cua_hook_install", os.path.join(ROOT, "hooks", "install.py"))
        install = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(install)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "settings.json")
        cases = (
            ("huge integer", ('{"n": ' + "1" + "0" * 4999 + "}").encode("utf-8")),
            ("invalid utf-8", b'{"note": "caf\xff"}'),
        )
        for name, data in cases:
            with self.subTest(case=name):
                with open(path, "wb") as f:
                    f.write(data)
                with self.assertRaises(SystemExit) as cm:
                    install.load_settings(install.Path(path))
                self.assertTrue(str(cm.exception.code).startswith("error: could not read"), cm.exception.code)
                with open(path, "rb") as f:
                    self.assertEqual(f.read(), data)
        with self.subTest(case="valid"):
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"hooks": {}}')
            self.assertEqual(install.load_settings(install.Path(path)), {"hooks": {}})


class TestHookHarness(unittest.TestCase):
    def test_skip_reason_names_node(self):
        self.assertTrue(NODE_SKIP_REASON.startswith("node not found on PATH"))
        self.assertIn(NODE_BIN_ENV, NODE_SKIP_REASON)
        self.assertNotIn("'", NODE_SKIP_REASON)


if __name__ == "__main__":
    unittest.main()
