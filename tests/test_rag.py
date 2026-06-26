"""Tests for the change-aware RAG vector sync (doctopdf/rag.py).

Uses a real (temp-dir) Chroma store with a *fake deterministic embedder*, so the
hash-reconciliation, removal-cleanup, empty-skip, and dimension-mismatch paths
are exercised end-to-end without needing a live Ollama.

Run: python -m unittest tests.test_rag   (from the repo root)
"""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from doctopdf import rag


def fake_embed_factory():
    """Deterministic 16-dim embedder + a call counter (to prove incrementality).

    Identical text → identical vector, so a re-embed of an unchanged chunk would
    show up both as a wasted call and (it doesn't happen) — that's the point.
    """
    calls = {"n": 0, "texts": []}

    def embed(texts):
        calls["n"] += 1
        calls["texts"].extend(texts)
        out = []
        for t in texts:
            digest = hashlib.sha256(t.encode("utf-8")).digest()
            out.append([b / 255.0 for b in digest[:16]])
        return out

    return embed, calls


def make_store(tmp, model="fake-embed", embed_fn=None):
    cfg = {"rag": {
        "enabled": True,
        "store_path": str(tmp),
        "embedder": {"provider": "fake", "model": model, "url": ""},
        "chunk": {"size": 200, "overlap": 40},
    }}
    return rag.RagStore(cfg, embed_fn=embed_fn)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

class ChunkingTests(unittest.TestCase):
    def test_empty_yields_nothing(self):
        self.assertEqual(rag.chunk_text(""), [])
        self.assertEqual(rag.chunk_text("   \n\n  "), [])

    def test_small_text_one_chunk(self):
        self.assertEqual(rag.chunk_text("hello world", size=1000), ["hello world"])

    def test_large_text_splits_and_respects_size(self):
        text = "\n\n".join(f"Paragraph {i} " + "word " * 30 for i in range(8))
        chunks = rag.chunk_text(text, size=200, overlap=40)
        self.assertGreater(len(chunks), 1)
        # Allow some slack for the carried-over overlap prefix.
        for c in chunks:
            self.assertLessEqual(len(c), 200 + 40)

    def test_overlap_carries_context(self):
        text = "\n\n".join(f"P{i} " + "x" * 80 for i in range(4))
        chunks = rag.chunk_text(text, size=120, overlap=30)
        self.assertGreater(len(chunks), 1)
        # Each chunk after the first starts with a tail of the previous one.
        self.assertTrue(chunks[1].startswith(chunks[0][-30:].strip()[:5]))

    def test_chunk_hash_includes_target(self):
        self.assertNotEqual(rag.chunk_hash("T1", "same"), rag.chunk_hash("T2", "same"))
        self.assertEqual(rag.chunk_hash("T1", "same"), rag.chunk_hash("T1", "same"))


# ---------------------------------------------------------------------------
# Hash-based incremental reconciliation
# ---------------------------------------------------------------------------

class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.embed, self.calls = fake_embed_factory()
        self.store = make_store(self.tmp, embed_fn=self.embed)
        # Five clearly-separated paragraphs → multiple chunks at size=200.
        self.paras = [f"Paragraph number {i}. " + "content " * 10 for i in range(5)]

    def _text(self, paras=None):
        return "\n\n".join(paras or self.paras)

    def test_baseline_adds_all(self):
        res = self.store.sync("T1", "Doc One", "doc", "http://x", self._text())
        self.assertEqual(res["status"], "ok")
        self.assertGreater(res["added"], 0)
        self.assertEqual(res["deleted"], 0)
        self.assertEqual(self.store.stats()["count"], res["added"])

    def test_unchanged_reembeds_nothing(self):
        first = self.store.sync("T1", "Doc One", "doc", "http://x", self._text())
        calls_after_first = self.calls["n"]
        again = self.store.sync("T1", "Doc One", "doc", "http://x", self._text())
        self.assertEqual(again["added"], 0)
        self.assertEqual(again["deleted"], 0)
        self.assertEqual(again["unchanged"], first["added"])
        # No embedder call for a re-sync of identical content (the efficiency win).
        self.assertEqual(self.calls["n"], calls_after_first)

    def test_one_edit_reembeds_only_changed_chunk(self):
        self.store.sync("T1", "Doc One", "doc", "http://x", self._text())
        count_before = self.store.stats()["count"]
        self.calls["texts"].clear()
        calls_before = self.calls["n"]

        edited = list(self.paras)
        edited[2] = "Paragraph number 2. EDITED " + "content " * 10
        res = self.store.sync("T1", "Doc One", "doc", "http://x", self._text(edited))

        # Exactly the touched chunk(s) are added; the rest are untouched.
        self.assertGreaterEqual(res["added"], 1)
        self.assertLess(res["added"], count_before)
        self.assertGreater(res["unchanged"], 0)
        self.assertEqual(self.calls["n"], calls_before + 1)        # one embed batch
        self.assertEqual(len(self.calls["texts"]), res["added"])   # only new chunks

    def test_shrink_deletes_removed_chunks(self):
        self.store.sync("T1", "Doc One", "doc", "http://x", self._text())
        full = self.store.stats()["count"]
        res = self.store.sync("T1", "Doc One", "doc", "http://x", self._text(self.paras[:2]))
        self.assertGreater(res["deleted"], 0)
        self.assertLess(self.store.stats()["count"], full)

    def test_empty_snapshot_does_not_wipe(self):
        self.store.sync("T1", "Doc One", "doc", "http://x", self._text())
        before = self.store.stats()["count"]
        res = self.store.sync("T1", "Doc One", "doc", "http://x", "   ")
        self.assertEqual(res["status"], "skip-empty")
        self.assertEqual(self.store.stats()["count"], before)   # untouched


# ---------------------------------------------------------------------------
# Removal cleanup + isolation
# ---------------------------------------------------------------------------

class RemovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.embed, _ = fake_embed_factory()
        self.store = make_store(self.tmp, embed_fn=self.embed)
        self.store.sync("T1", "Doc One", "doc", "l1", "\n\n".join(f"a {i} " * 20 for i in range(3)))
        self.store.sync("T2", "Doc Two", "doc", "l2", "\n\n".join(f"b {i} " * 20 for i in range(3)))

    def test_delete_target_removes_only_its_chunks(self):
        total = self.store.stats()["count"]
        self.assertGreater(total, 0)
        removed = self.store.delete_target("T1")
        self.assertGreater(removed, 0)
        # T2's chunks survive; T1's are gone.
        self.assertEqual(self.store.stats()["count"], total - removed)
        hits = self.store.query("b 1", k=10)
        self.assertTrue(all(h["name"] == "Doc Two" for h in hits))

    def test_delete_unknown_target_is_noop(self):
        before = self.store.stats()["count"]
        self.assertEqual(self.store.delete_target("nope"), 0)
        self.assertEqual(self.store.stats()["count"], before)


# ---------------------------------------------------------------------------
# Query citations + dimension-mismatch guard
# ---------------------------------------------------------------------------

class QueryAndMismatchTests(unittest.TestCase):
    def test_query_returns_citation_fields(self):
        tmp = Path(tempfile.mkdtemp())
        embed, _ = fake_embed_factory()
        store = make_store(tmp, embed_fn=embed)
        store.sync("T1", "Pricing Doc", "doc", "http://link", "The price is forty two dollars.")
        hits = store.query("The price is forty two dollars.", k=1)
        self.assertEqual(len(hits), 1)
        h = hits[0]
        for field in ("name", "kind", "link", "updated_at", "score", "text"):
            self.assertIn(field, h)
        self.assertEqual(h["name"], "Pricing Doc")
        self.assertEqual(h["link"], "http://link")
        self.assertGreaterEqual(h["score"], 0.99)   # exact text → ~identical vector

    def test_embedder_swap_forces_reindex(self):
        tmp = Path(tempfile.mkdtemp())
        embed, _ = fake_embed_factory()
        make_store(tmp, model="model-A", embed_fn=embed).sync(
            "T1", "Doc", "doc", "l", "some content here for the index")

        swapped = make_store(tmp, model="model-B", embed_fn=embed)
        with self.assertRaises(rag.DimensionMismatch):
            swapped.sync("T1", "Doc", "doc", "l", "more content")
        with self.assertRaises(rag.DimensionMismatch):
            swapped.query("content")

        # A reindex clears the embedder record; the new model can then build.
        swapped.reindex()
        res = swapped.sync("T1", "Doc", "doc", "l", "more content")
        self.assertEqual(res["status"], "ok")


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")


class ConfigAndRoutingTests(unittest.TestCase):
    """Embedder config merge + provider routing (regressions for review fixes)."""

    def test_deep_fill_preserves_api_key(self):
        from doctopdf import config
        stored = {"enabled": True,
                  "embedder": {"provider": "openai", "model": "m", "api_key": "sk-x"}}
        filled = config._deep_fill(stored, config.DEFAULT_CONFIG["rag"])
        # api_key isn't in the default embedder, but must survive the merge.
        self.assertEqual(filled["embedder"]["api_key"], "sk-x")
        self.assertEqual(filled["embedder"]["model"], "m")
        self.assertIsNone(filled["embedder"]["url"])        # backfilled default
        self.assertEqual(filled["mcp"], {"enabled": True})  # absent sub-block filled

    def test_openai_not_misrouted_to_default_url(self):
        # With the default (null) embedder URL, an OpenAI request must hit the
        # OpenAI base, never the Ollama localhost default.
        captured = {}

        def fake_post(url, **kw):
            captured["url"] = url
            return _Resp(200, {"data": [{"index": 0, "embedding": [1.0]}]})

        with mock.patch("doctopdf.rag.requests.post", side_effect=fake_post):
            rag.embed(["hi"], {"provider": "openai", "model": "m",
                               "url": None, "api_key": "sk-x"})
        self.assertIn("api.openai.com", captured["url"])
        self.assertNotIn("11434", captured["url"])


class OllamaEmbedTests(unittest.TestCase):
    """The embedder HTTP path (mocked) — batch endpoint + legacy fallback."""

    def test_batch_endpoint(self):
        with mock.patch("doctopdf.rag.requests.post",
                        return_value=_Resp(200, {"embeddings": [[1.0, 2.0], [3.0, 4.0]]})) as p:
            out = rag.embed(["a", "b"], {"provider": "ollama", "model": "m"})
        self.assertEqual(out, [[1.0, 2.0], [3.0, 4.0]])
        self.assertIn("/api/embed", p.call_args.args[0])

    def test_falls_back_to_singular_on_501(self):
        # First call (/api/embed) → 501; subsequent (/api/embeddings) → per-prompt.
        calls = {"n": 0}

        def fake_post(url, **kw):
            calls["n"] += 1
            if url.endswith("/api/embed"):
                return _Resp(501)
            return _Resp(200, {"embedding": [9.0]})

        with mock.patch("doctopdf.rag.requests.post", side_effect=fake_post):
            out = rag.embed(["x", "y"], {"provider": "ollama", "model": "m"})
        self.assertEqual(out, [[9.0], [9.0]])    # one vector per prompt
        self.assertEqual(calls["n"], 3)          # 1 probe + 2 singular calls

    def test_outage_raises_embed_error(self):
        import requests
        with mock.patch("doctopdf.rag.requests.post",
                        side_effect=requests.ConnectionError("refused")):
            with self.assertRaises(rag.EmbedError):
                rag.embed(["x"], {"provider": "ollama", "model": "m"})


class AppRemovalAndReindexTests(unittest.TestCase):
    """App-level wiring: two-strike removal + reindex re-baseline (review fixes)."""

    def _ctrl(self):
        # A bare object carrying just the fields the methods touch.
        import threading
        from doctopdf.app import DocToPDFController
        obj = type("Ctl", (), {})()
        obj._rag_indexed = set()
        obj._rag_drop_pending = set()
        obj._rag = object()                 # truthy; helper doesn't call it
        obj._drops_for = lambda live: DocToPDFController._rag_drops_for(obj, live)
        return obj, DocToPDFController

    def test_two_strike_purges_only_after_second_absence(self):
        ctl, _ = self._ctrl()
        ctl._rag_indexed = {"A", "B", "C"}
        # Cycle 1: C absent → remembered, NOT purged yet.
        self.assertEqual(ctl._drops_for({"A", "B"}), [])
        self.assertEqual(ctl._rag_drop_pending, {"C"})
        # Cycle 2: C still absent → purged now.
        self.assertEqual(ctl._drops_for({"A", "B"}), ["C"])
        self.assertNotIn("C", ctl._rag_indexed)
        self.assertEqual(ctl._rag_drop_pending, set())

    def test_transient_absence_does_not_purge(self):
        ctl, _ = self._ctrl()
        ctl._rag_indexed = {"A", "B", "C"}
        self.assertEqual(ctl._drops_for({"A", "B"}), [])     # C absent once
        # C reappears next cycle → must be forgotten, never purged.
        self.assertEqual(ctl._drops_for({"A", "B", "C"}), [])
        self.assertEqual(ctl._rag_drop_pending, set())
        self.assertIn("C", ctl._rag_indexed)

    def test_reindex_rebaselines_all_state(self):
        import threading
        from doctopdf.app import DocToPDFController, RAG_RETRY_BASE

        class FakeStore:
            def __init__(self): self.reindexed = False
            def reindex(self): self.reindexed = True
            def stats(self): return {"count": 0, "last_sync": None}

        ctl = type("Ctl", (), {})()
        ctl._rag = FakeStore()
        ctl._lock = threading.RLock()
        ctl._wake = threading.Event()
        ctl._state = {}
        ctl._rag_pending = {"X": ("sync",)}
        ctl._rag_dim_mismatch = True
        ctl._rag_backoff = 999
        ctl._rag_indexed = {"A", "B"}
        ctl._rag_drop_pending = {"C"}
        ctl._prev_text = {"A": "t", "url": "t"}
        ctl._modified = {"A": "m"}
        ctl._web_next = {"url": 1.0}
        ctl._web_backoff = {"url": 2.0}
        ctl._force = False
        ctl._rag_set_note = lambda n: ctl._state.__setitem__("rag_note", n)
        ctl._rag_refresh_stats = lambda: None

        DocToPDFController._rag_reindex(ctl)

        self.assertTrue(ctl._rag.reindexed)
        self.assertFalse(ctl._rag_dim_mismatch)
        self.assertEqual(ctl._rag_pending, {})
        self.assertEqual(ctl._rag_backoff, RAG_RETRY_BASE)
        # Every per-target baseline is cleared so the forced poll re-embeds ALL
        # targets (the web-target regression fix).
        self.assertEqual(ctl._prev_text, {})
        self.assertEqual(ctl._modified, {})
        self.assertEqual(ctl._web_next, {})
        self.assertEqual(ctl._web_backoff, {})
        self.assertEqual(ctl._rag_indexed, set())
        self.assertEqual(ctl._rag_drop_pending, set())
        self.assertTrue(ctl._force)
        self.assertTrue(ctl._wake.is_set())


if __name__ == "__main__":
    unittest.main()
