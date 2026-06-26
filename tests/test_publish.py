"""Tests for the publishing pipeline (doctopdf/publish.py + app wiring).

Covers the safe-git push path (against a local bare repo, no network/auth) and
the manual-approval gate (against the app's publish worker with a fake store).

Run: python -m unittest tests.test_publish   (from the repo root)
"""

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from doctopdf import publish


def _git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Render / sanitize / template
# ---------------------------------------------------------------------------

class RenderTests(unittest.TestCase):
    def test_sanitizes_and_strips_images_keeps_content(self):
        md = ("# Doc\n\nHi **bold** [link](https://x.com).\n\n"
              "![pic](http://x/i.png)\n\n<script>alert(1)</script>\n\n"
              "| A | B |\n|---|---|\n| 1 | 2 |\n")
        page, warn = publish.build_page(md, "My Doc", "default")
        self.assertNotIn("<script>", page)
        self.assertNotIn("alert(1)", page)
        self.assertNotIn("<img", page)           # images stripped, not broken-linked
        self.assertIn("image", (warn or ""))     # …with a warning
        self.assertIn('href="https://x.com"', page)
        self.assertIn("<table>", page)
        self.assertIn("My Doc", page)

    def test_custom_template_path(self):
        with tempfile.TemporaryDirectory() as d:
            tpl = Path(d) / "tpl.html"
            tpl.write_text("<x>{{ title }}|{{ content }}</x>")
            page, _ = publish.build_page("# Hi\n\ntext", "T", str(tpl))
            self.assertTrue(page.startswith("<x>T|"))
            self.assertIn("<h1>Hi</h1>", page)

    def test_target_key_stable_and_distinct(self):
        a = {"source_id": "s", "type": "git_pages", "repo": "r", "branch": "b", "path": "p"}
        self.assertEqual(publish.target_key(a), publish.target_key(dict(a)))
        self.assertNotEqual(publish.target_key(a), publish.target_key({**a, "branch": "z"}))

    def test_image_only_doc_skips_not_blank(self):
        # A doc that's only an image renders an empty body — must skip, not publish
        # a blank page over a live one.
        with self.assertRaises(publish.PublishSkip):
            publish.publish({"type": "git_pages", "repo": "r", "branch": "b"},
                            "Doc", "![pic](http://x/i.png)")

    def test_image_count_covers_html_and_markdown(self):
        _, warn = publish.build_page("# T\n\n![a](u)\n\n<img src='b'>\n\ntext", "T", "default")
        self.assertIn("2 image", warn)        # both the markdown image and the raw <img>

    def test_template_injection_via_title_is_inert(self):
        # A doc titled like a marker must not inject the body into <title>/<h1>.
        page, _ = publish.build_page("# heading\n\nBODYTEXT", "{{content}}", "default")
        self.assertNotIn("BODYTEXT</title>", page)
        self.assertIn("{{content}}", page)    # the literal title, escaped, stays put


# ---------------------------------------------------------------------------
# Safe git push (local bare repo as the "remote")
# ---------------------------------------------------------------------------

class SafeGitPushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        publish.PUBLISH_DIR = self.tmp / "work"          # keep working copies isolated
        self.remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)

    def _clone_remote(self, name, branch="gh-pages"):
        d = self.tmp / name
        subprocess.run(["git", "clone", "-q", "--branch", branch, str(self.remote), str(d)],
                       check=True)
        return d

    def test_first_publish_creates_branch_and_pushes(self):
        c = publish.git_publish(str(self.remote), "gh-pages",
                                {"index.html": b"<h1>v1</h1>"}, "first")
        self.assertTrue(c)
        clone = self._clone_remote("v1")
        self.assertTrue((clone / "index.html").is_file())
        self.assertEqual((clone / "index.html").read_bytes(), b"<h1>v1</h1>")

    def test_unchanged_is_noop(self):
        publish.git_publish(str(self.remote), "gh-pages", {"index.html": b"x"}, "a")
        self.assertIsNone(
            publish.git_publish(str(self.remote), "gh-pages", {"index.html": b"x"}, "b"))

    def test_change_pushes_new_commit(self):
        c1 = publish.git_publish(str(self.remote), "gh-pages", {"index.html": b"x"}, "a")
        c2 = publish.git_publish(str(self.remote), "gh-pages", {"index.html": b"y"}, "b")
        self.assertTrue(c2 and c2 != c1)
        self.assertEqual(self._clone_remote("v2", ).joinpath("index.html").read_bytes(), b"y")

    def test_diverged_remote_rebases_without_clobbering(self):
        publish.git_publish(str(self.remote), "gh-pages", {"other.txt": b"x"}, "a")
        # Someone else commits a DIFFERENT file to the app branch underneath us.
        other = self._clone_remote("other")
        (other / "external.txt").write_text("external work")
        _git(other, "add", "-A")
        _git(other, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qam", "ext")
        _git(other, "push", "-q", "origin", "gh-pages")
        # Our next publish must land our file AND preserve their commit (rebase, not
        # a force that silently discards it).
        c = publish.git_publish(str(self.remote), "gh-pages", {"index.html": b"ours"}, "b")
        self.assertTrue(c)
        v = self._clone_remote("v3")
        self.assertEqual((v / "index.html").read_bytes(), b"ours")
        self.assertTrue((v / "external.txt").is_file())          # their work survives
        log = _git(v, "log", "--oneline").stdout
        self.assertIn("ext", log)                                # their commit is in history

    def test_empty_snapshot_raises_skip(self):
        with self.assertRaises(publish.PublishSkip):
            publish.publish({"type": "git_pages", "repo": str(self.remote),
                             "branch": "gh-pages"}, "Doc", "   ")

    def test_missing_repo_raises_error(self):
        with self.assertRaises(publish.PublishError):
            publish.git_publish("", "gh-pages", {"i": b"x"}, "m")

    def test_path_traversal_rejected(self):
        with self.assertRaises(publish.PublishError):
            publish.git_publish(str(self.remote), "gh-pages",
                                {"../escape.html": b"x"}, "m")


# ---------------------------------------------------------------------------
# Manual-approval gate (app publish worker, fake publisher)
# ---------------------------------------------------------------------------

class ApprovalGateTests(unittest.TestCase):
    def _ctl(self):
        from doctopdf.app import DocToPDFController
        from collections import deque
        ctl = type("Ctl", (), {})()
        ctl._lock = threading.RLock()
        ctl._pub_pending = {}
        ctl._pub_status = {}
        ctl._pub_retry = {}
        ctl._pub_backoff = {}
        ctl._recent_pub = deque(maxlen=10)
        ctl.notes = []
        ctl._notify = lambda t, m: ctl.notes.append((t, m))
        ctl._pub_set_status = lambda k, **f: DocToPDFController._pub_set_status(ctl, k, **f)
        ctl._pub_enqueue = lambda *a, **k: DocToPDFController._pub_enqueue(ctl, *a, **k)
        ctl._run = lambda task: DocToPDFController._pub_run(ctl, task)
        ctl._approve = lambda key: DocToPDFController._pub_approve(ctl, key)
        # Capture enqueues instead of using a real queue.
        ctl.enqueued = []

        class Q:
            def put(self, item): ctl.enqueued.append(item)
        ctl._pub_q = Q()
        return ctl

    def setUp(self):
        self.published = []
        self._orig = publish.publish
        publish.publish = lambda target, name, md: (
            self.published.append((name, md)) or
            {"status": "published", "url": target.get("site_url"), "warning": None})

    def tearDown(self):
        publish.publish = self._orig

    def test_manual_change_holds_pending_without_publishing(self):
        ctl = self._ctl()
        tgt = {"source_id": "S", "type": "git_pages", "repo": "r", "branch": "b",
               "approval": "manual", "site_url": "https://site"}
        key = publish.target_key(tgt)
        ctl._run((tgt, key, "Doc", "# content", False))   # auto=False → change, not explicit
        self.assertEqual(self.published, [])               # NOT published
        self.assertIn(key, ctl._pub_pending)               # held pending
        self.assertEqual(ctl._pub_status[key]["status"], "pending")
        self.assertTrue(any("pending" in m.lower() for _, m in ctl.notes))

    def test_approve_publishes_held_snapshot(self):
        ctl = self._ctl()
        tgt = {"source_id": "S", "type": "git_pages", "repo": "r", "branch": "b",
               "approval": "manual"}
        key = publish.target_key(tgt)
        ctl._run((tgt, key, "Doc", "# held", False))       # → pending
        ctl._approve(key)                                  # user approves
        # Approve enqueues a manual_ok task; run it.
        self.assertEqual(len(ctl.enqueued), 1)
        ctl._run(ctl.enqueued[0])
        self.assertEqual(self.published, [("Doc", "# held")])
        self.assertNotIn(key, ctl._pub_pending)            # cleared after publish

    def test_auto_publishes_immediately(self):
        ctl = self._ctl()
        tgt = {"source_id": "S", "type": "git_markdown", "repo": "r", "branch": "b",
               "approval": "auto"}
        key = publish.target_key(tgt)
        ctl._run((tgt, key, "Doc", "# now", False))
        self.assertEqual(self.published, [("Doc", "# now")])
        self.assertEqual(ctl._pub_status[key]["status"], "published")

    def test_push_failure_sets_error_and_schedules_retry(self):
        ctl = self._ctl()
        def boom(target, name, md):
            raise publish.PublishError("auth failed")
        publish.publish = boom
        tgt = {"source_id": "S", "type": "git_markdown", "repo": "r", "branch": "b"}
        key = publish.target_key(tgt)
        ctl._run((tgt, key, "Doc", "# x", True))
        self.assertEqual(ctl._pub_status[key]["status"], "error")
        self.assertIn(key, ctl._pub_retry)                 # scheduled to retry
        self.assertTrue(any("failed" in t.lower() for t, _ in ctl.notes))


if __name__ == "__main__":
    unittest.main()
