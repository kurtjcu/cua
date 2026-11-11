import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cua import config  # noqa: E402


def write_env(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
    f.write(text)
    f.close()
    return f.name


class TestConfig(unittest.TestCase):
    def test_defaults_when_no_file(self):
        env = config.load_env("/nonexistent.env")
        self.assertEqual(env["CLAUDE_DIR"], "~/.claude")
        self.assertEqual(env["PROJECT_GLOB_1_EXCLUDE"], "")
        self.assertEqual(config.accounts(env), [
            {"label": "me", "claude_dir": "~/.claude", "include_glob": None, "exclude_glob": None}])

    def test_parse_comments_and_inline_trailer(self):
        p = write_env("# comment\nCLAUDE_DIR=~/x  # trailer\nLABEL_1='quoted'\n\nBAD LINE\n")
        try:
            env = config.load_env(p)
        finally:
            os.unlink(p)
        self.assertEqual((env["CLAUDE_DIR"], env["LABEL_1"]), ("~/x", "quoted"))

    def test_two_shared_accounts_split_by_include_and_exclude(self):
        p = write_env(
            "CLAUDE_DIR=~/.claude\nLABEL_1=personal\nPROJECT_GLOB_1_EXCLUDE=*/code/hc/*\n"
            "CLAUDE_DIR_2=~/.claude\nLABEL_2=work\nPROJECT_GLOB_2=*/code/hc/*\n")
        try:
            env = config.load_env(p)
        finally:
            os.unlink(p)
        accts = config.accounts(env)
        self.assertEqual([a["label"] for a in accts], ["personal", "work"])
        # personal: no include, exclude the work glob
        self.assertEqual(config.label_filters(env, "personal"), (None, ["*/code/hc/*"]))
        # work: include the work glob, no exclude
        self.assertEqual(config.label_filters(env, "work"), (["*/code/hc/*"], None))
        self.assertEqual(config.account_for_label(env, "work")["claude_dir"], "~/.claude")

    def test_label_filters_unknown_label_is_none(self):
        env = config.load_env("/nonexistent.env")
        self.assertEqual(config.label_filters(env, "nobody"), (None, None))
        self.assertIsNone(config.account_for_label(env, "nobody"))


if __name__ == "__main__":
    unittest.main()
