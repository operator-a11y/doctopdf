"""Tests for the diagnostic poll log: it writes lines to its file and, above all,
never raises into the watch loop even when logging itself fails."""

import logging
import tempfile
import unittest
from pathlib import Path

from doctopdf import polllog


class TestPollLog(unittest.TestCase):
    def setUp(self):
        # Redirect the log to a temp dir and reset the cached logger so _get()
        # rebuilds its handler against the temp path.
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmp.name)
        self._orig_dir, self._orig_path = polllog.LOG_DIR, polllog.LOG_PATH
        self._orig_logger = polllog._logger
        polllog.LOG_DIR = self._dir
        polllog.LOG_PATH = self._dir / "poll.log"
        polllog._logger = None

    def tearDown(self):
        lg = polllog._logger
        if lg is not None:
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)
        polllog.LOG_DIR, polllog.LOG_PATH = self._orig_dir, self._orig_path
        polllog._logger = self._orig_logger
        self._tmp.cleanup()

    def test_writes_line_to_file(self):
        polllog.log("Q3 Pricing  mtime=2026-06-28T14:32:18Z  (no change)")
        text = polllog.LOG_PATH.read_text(encoding="utf-8")
        self.assertIn("Q3 Pricing", text)
        self.assertIn("(no change)", text)

    def test_appends_multiple(self):
        polllog.log("one")
        polllog.log("two")
        lines = [ln for ln in polllog.LOG_PATH.read_text().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)

    def test_rotation_is_bounded(self):
        h = polllog._get().handlers[0]
        self.assertIsInstance(h, logging.handlers.RotatingFileHandler)
        self.assertGreater(h.maxBytes, 0)
        self.assertGreater(h.backupCount, 0)

    def test_never_raises_even_if_logging_breaks(self):
        # If the underlying logger explodes, log() must swallow it — the watch
        # loop must never die because of diagnostics.
        polllog._logger = None

        def boom():
            raise RuntimeError("logger unavailable")

        orig = polllog._get
        polllog._get = boom
        try:
            polllog.log("should not raise")  # must not propagate
        finally:
            polllog._get = orig


if __name__ == "__main__":
    unittest.main()
