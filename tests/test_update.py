import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cua import update  # noqa: E402


class TestUpdate(unittest.TestCase):
    def test_format_notice(self):
        self.assertEqual(update.format_notice(0), "")
        self.assertEqual(update.format_notice(None), "")
        self.assertIn("1 update available", update.format_notice(1))
        self.assertIn("3 updates available", update.format_notice(3))

    def test_should_fetch_missing_file(self):
        old = update.FETCH_HEAD
        update.FETCH_HEAD = "/nonexistent/dir/FETCH_HEAD"
        try:
            self.assertTrue(update.should_fetch())  # never fetched -> fetch
        finally:
            update.FETCH_HEAD = old

    def test_should_fetch_age(self):
        old = update.FETCH_HEAD
        f = tempfile.NamedTemporaryFile(delete=False)
        f.close()
        update.FETCH_HEAD = f.name
        try:
            t = os.path.getmtime(f.name)
            self.assertFalse(update.should_fetch(now=t, ttl=86400))       # just fetched
            self.assertFalse(update.should_fetch(now=t + 3600, ttl=86400))  # an hour later
            self.assertTrue(update.should_fetch(now=t + 90000, ttl=86400))  # a day+ later
        finally:
            update.FETCH_HEAD = old
            os.unlink(f.name)

    def test_ticket_prefix_unrelated_notice_silenced_by_env(self):
        os.environ["CUA_NO_UPDATE_CHECK"] = "1"
        try:
            self.assertEqual(update.notice(), "")  # env var short-circuits, no git calls
        finally:
            del os.environ["CUA_NO_UPDATE_CHECK"]

    def test_is_repo_and_version(self):
        # the test suite runs from inside this git checkout
        self.assertTrue(update.is_repo())
        self.assertNotIn("unknown", update.current_version())

    def test_check_is_fail_silent_shape(self):
        # whatever the git/remote state, check() returns a dict and never raises
        r = update.check(throttle=True, do_fetch=False)
        self.assertIsInstance(r, dict)
        self.assertIn("repo", r)


if __name__ == "__main__":
    unittest.main()
