"""Change-aware vector sync for DocToPDF — chunk → embed → upsert/delete.

The engine already captures a plain-text snapshot of every watched source and
fires a change event when it changes; this module is a downstream consumer of
that same signal. On each snapshot it reconciles the target's chunk set **by
content hash** against what's already stored, so a one-line edit re-embeds one
chunk, not the whole document. The store is a local Chroma DB (persistent, in
process — no server); embeddings are local via Ollama by default, so content
never leaves the machine.

Surfaces: :func:`RagStore.query` (used by the ``doctopdf query`` CLI and the
``search_knowledge`` MCP tool). Everything degrades gracefully — if the embedder
or the store is unavailable the caller queues a retry and keeps watching; RAG is
strictly additive and never blocks exports or alerts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import requests

from . import config

# Reuse a single text format from each snapshot — the engine already decodes it.
EMBED_TIMEOUT = 120          # local embedding can be slow on first model load
EMBED_BATCH = 64             # chunks per embedder call (cap to be gentle on Ollama)
COLLECTION = "doctopdf"
META_FILE = "doctopdf-meta.json"   # sidecar: embedder id, dim, last_sync

DEFAULT_STORE_PATH = "~/Documents/DocExports/.vectorstore"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RagError(Exception):
    """Base for RAG problems; carries a short, user-facing message."""


class RagUnavailable(RagError):
    """The vector store can't be opened (chromadb missing / path unusable).

    Distinct from an embedder outage: this means the store layer itself is out,
    so the app disables RAG and surfaces it rather than retrying forever.
    """


class EmbedError(RagError):
    """The embedder (Ollama / cloud) failed — transient, retry later."""


class DimensionMismatch(RagError):
    """The configured embedder differs from the one the store was built with.

    Mixing embedding spaces corrupts search, so sync/query refuse until a full
    ``doctopdf rag reindex`` rebuilds the store with the current embedder.
    """


# ---------------------------------------------------------------------------
# Chunking — a lightweight, boundary-aware recursive splitter (no LangChain)
# ---------------------------------------------------------------------------

# Split preference: paragraphs, then lines, then sentences, then words, then
# (last resort) raw characters. Boundaries higher in this list are preferred so
# chunks fall on natural seams.
_SEPARATORS = ["\n\n", "\n", ". ", " ", ""]


def _split_recursive(text: str, size: int, seps: list) -> list:
    """Break ``text`` into pieces no larger than ``size``, preferring the
    earliest separator that does the job."""
    if len(text) <= size:
        return [text]
    if not seps:
        # No separators left — hard-cut on size.
        return [text[i:i + size] for i in range(0, len(text), size)]
    sep = seps[0]
    parts = text.split(sep) if sep else list(text)
    out, cur = [], ""
    for i, part in enumerate(parts):
        piece = part + (sep if sep and i < len(parts) - 1 else "")
        if len(piece) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.extend(_split_recursive(piece, size, seps[1:]))
        elif len(cur) + len(piece) <= size:
            cur += piece
        else:
            if cur:
                out.append(cur)
            cur = piece
    if cur:
        out.append(cur)
    return out


def chunk_text(text: str, size: int = 1000, overlap: int = 150) -> list:
    """Chunk ``text`` into ~``size``-char pieces on natural boundaries, with
    ``overlap`` characters of context carried from the previous chunk.

    Empty/whitespace input yields ``[]`` — callers treat that as "skip", never
    "wipe", consistent with the web-denoise rule.
    """
    text = (text or "").strip()
    if not text:
        return []
    size = max(1, int(size or 1000))
    overlap = max(0, min(int(overlap or 0), size - 1))

    pieces = _split_recursive(text, size, _SEPARATORS)
    # Merge adjacent small pieces back up toward `size` so we don't emit a chunk
    # per line for tightly-wrapped text.
    merged, cur = [], ""
    for p in pieces:
        if not p.strip():
            continue
        if not cur:
            cur = p
        elif len(cur) + len(p) <= size:
            cur += p
        else:
            merged.append(cur)
            cur = p
    if cur:
        merged.append(cur)

    if overlap and len(merged) > 1:
        with_ctx = [merged[0].strip()]
        for i in range(1, len(merged)):
            tail = merged[i - 1][-overlap:]
            with_ctx.append((tail + merged[i]).strip())
        merged = with_ctx

    return [c for c in (m.strip() for m in merged) if c]


def chunk_hash(target_id: str, chunk: str) -> str:
    """Stable id for a chunk within a target — keyed by content so insertions
    elsewhere in the document don't shift every downstream chunk's id."""
    h = hashlib.sha1()
    h.update(target_id.encode("utf-8"))
    h.update(b"\x00")
    h.update(chunk.encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Embedding — local Ollama by default; optional cloud (OpenAI)
# ---------------------------------------------------------------------------

def _batches(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _embed_ollama(texts: list, url: str, model: str) -> list:
    url = (url or "http://localhost:11434").rstrip("/")
    out: list = []
    for batch in _batches(texts, EMBED_BATCH):
        try:
            r = requests.post(f"{url}/api/embed",
                              json={"model": model, "input": batch}, timeout=EMBED_TIMEOUT)
            if r.status_code in (404, 501):
                # Older Ollama without the batch /api/embed (404) or a build that
                # reports it unimplemented (501) — fall back to per-prompt calls.
                for t in batch:
                    rr = requests.post(f"{url}/api/embeddings",
                                       json={"model": model, "prompt": t}, timeout=EMBED_TIMEOUT)
                    rr.raise_for_status()
                    out.append(rr.json()["embedding"])
                continue
            r.raise_for_status()
            out.extend(r.json()["embeddings"])
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise EmbedError(f"Ollama embedder unavailable ({model}): {exc}") from exc
    return out


def _embed_openai(texts: list, url: str, model: str, api_key: Optional[str]) -> list:
    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise EmbedError("OpenAI embedder selected but no api_key / $OPENAI_API_KEY set.")
    base = (url or "https://api.openai.com/v1").rstrip("/")
    out: list = []
    for batch in _batches(texts, 128):
        try:
            r = requests.post(f"{base}/embeddings",
                              headers={"Authorization": f"Bearer {key}"},
                              json={"model": model, "input": batch}, timeout=EMBED_TIMEOUT)
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            out.extend(d["embedding"] for d in data)
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise EmbedError(f"OpenAI embedder unavailable ({model}): {exc}") from exc
    return out


def embed(texts: list, embedder: dict) -> list:
    """Embed ``texts`` with the configured provider. Raises :class:`EmbedError`
    on any failure so the caller can queue a retry."""
    if not texts:
        return []
    provider = (embedder.get("provider") or "ollama").lower()
    model = embedder.get("model") or "nomic-embed-text"
    url = embedder.get("url")
    if provider == "openai":
        return _embed_openai(texts, url or "https://api.openai.com/v1",
                             model, embedder.get("api_key"))
    return _embed_ollama(texts, url, model)


def embedder_id(embedder: dict) -> str:
    """Stable id for the embedder, used to detect a dimension-changing swap."""
    provider = (embedder.get("provider") or "ollama").lower()
    model = embedder.get("model") or "nomic-embed-text"
    return f"{provider}:{model}"


# ---------------------------------------------------------------------------
# Kind / link helpers (so retrieval can cite source + open it)
# ---------------------------------------------------------------------------

_KIND_BY_GTYPE = {"document": "doc", "spreadsheet": "sheet",
                  "presentation": "slides", "drawing": "drawing"}
_GLINK_PATH = {"document": "document", "spreadsheet": "spreadsheets",
               "presentation": "presentation", "drawing": "drawings"}


def kind_for(gtype: Optional[str]) -> str:
    return _KIND_BY_GTYPE.get(gtype or "", gtype or "doc")


def google_link(gtype: Optional[str], file_id: str) -> str:
    path = _GLINK_PATH.get(gtype or "", "document")
    return f"https://docs.google.com/{path}/d/{file_id}/edit"


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class RagStore:
    """A persistent local Chroma collection plus a sidecar metadata file.

    Embeddings are computed by us (via :func:`embed`) and passed explicitly, so
    Chroma's default embedding model is never invoked. All store mutations go
    through one instance; the app serializes them on a single RAG worker thread.
    """

    def __init__(self, cfg: dict, embed_fn: Optional[Callable[[list], list]] = None):
        rag_cfg = (cfg.get("rag") or {})
        self.enabled = bool(rag_cfg.get("enabled", True))
        self.path = Path(os.path.expanduser(
            rag_cfg.get("store_path") or DEFAULT_STORE_PATH)).resolve()
        self.embedder = dict(rag_cfg.get("embedder") or {})
        chunk = rag_cfg.get("chunk") or {}
        self.chunk_size = int(chunk.get("size", 1000) or 1000)
        self.chunk_overlap = int(chunk.get("overlap", 150) or 150)
        self.embedder_id = embedder_id(self.embedder)
        # Tests inject a deterministic embedder to avoid needing a live Ollama.
        self._embed_fn = embed_fn or (lambda texts: embed(texts, self.embedder))
        self._meta_path = self.path / META_FILE
        self._lock = threading.Lock()
        self._client = None
        self._coll = None

    # -- store lifecycle ---------------------------------------------------
    def _ensure(self) -> None:
        if self._coll is not None:
            return
        try:
            import chromadb
            from chromadb.config import Settings
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise RagUnavailable("chromadb not installed — run: pip install chromadb") from exc
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(
                path=str(self.path), settings=Settings(anonymized_telemetry=False))
            self._coll = self._client.get_or_create_collection(
                COLLECTION, metadata={"hnsw:space": "cosine"})
        except RagError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface any open failure cleanly
            raise RagUnavailable(f"Vector store unavailable: {exc}") from exc

    # -- sidecar metadata --------------------------------------------------
    def _load_meta(self) -> dict:
        try:
            return json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_meta(self, **updates) -> None:
        with self._lock:
            m = self._load_meta()
            m.update(updates)
            self.path.mkdir(parents=True, exist_ok=True)
            tmp = self._meta_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(m, indent=2), encoding="utf-8")
            os.replace(tmp, self._meta_path)

    def _check_embedder(self) -> None:
        """Refuse to mix embedding spaces — force a clean reindex on a swap."""
        stored = self._load_meta().get("embedder")
        if stored and stored != self.embedder_id:
            raise DimensionMismatch(
                f"Store was built with '{stored}' but config now uses "
                f"'{self.embedder_id}'. Run `doctopdf rag reindex` to rebuild.")

    # -- the differentiator: hash-based incremental reconcile --------------
    def sync(self, target_id: str, name: str, kind: str, link: str, text: str) -> dict:
        """Reconcile a target's chunk set against the store by content hash.

        Returns ``{status, added, deleted, unchanged}``. An empty/failed snapshot
        is a no-op ("skip") — it must never wipe a target's existing chunks.
        Raises :class:`EmbedError` (retry) or :class:`DimensionMismatch` (reindex).
        """
        if not (text and text.strip()):
            return {"status": "skip-empty", "added": 0, "deleted": 0, "unchanged": 0}
        self._ensure()
        self._check_embedder()

        chunks = chunk_text(text, self.chunk_size, self.chunk_overlap)
        if not chunks:
            return {"status": "skip-empty", "added": 0, "deleted": 0, "unchanged": 0}
        new = {chunk_hash(target_id, c): c for c in chunks}

        stored_ids = set(self._coll.get(where={"target_id": target_id},
                                        include=[]).get("ids") or [])
        to_add = [h for h in new if h not in stored_ids]
        to_delete = [h for h in stored_ids if h not in new]

        # Embed the new chunks FIRST. If the embedder is down this raises before
        # any mutation, so a failed sync leaves the store exactly as it was.
        if to_add:
            vectors = self._embed_fn([new[h] for h in to_add])
            updated = _now_iso()
            metas = [{"target_id": target_id, "name": name, "kind": kind,
                      "link": link or "", "chunk_hash": h, "updated_at": updated}
                     for h in to_add]
            self._coll.upsert(ids=to_add, embeddings=vectors,
                              documents=[new[h] for h in to_add], metadatas=metas)
            meta_upd = {"embedder": self.embedder_id}
            if "dim" not in self._load_meta() and vectors:
                meta_upd["dim"] = len(vectors[0])
            self._save_meta(**meta_upd)
        if to_delete:
            self._coll.delete(ids=to_delete)
        if to_add or to_delete:
            self._save_meta(last_sync=_now_iso())
        return {"status": "ok", "added": len(to_add), "deleted": len(to_delete),
                "unchanged": len(new) - len(to_add)}

    def delete_target(self, target_id: str) -> int:
        """Remove every chunk belonging to ``target_id`` (target/folder-child
        removal). Returns the number of chunks deleted."""
        self._ensure()
        ids = self._coll.get(where={"target_id": target_id}, include=[]).get("ids") or []
        if ids:
            self._coll.delete(ids=ids)
            self._save_meta(last_sync=_now_iso())
        return len(ids)

    def query(self, question: str, k: int = 5) -> list:
        """Return the top-``k`` chunks for ``question`` with source + freshness."""
        if not (question and question.strip()):
            return []
        self._ensure()
        self._check_embedder()
        qv = self._embed_fn([question])[0]
        k = max(1, int(k or 5))
        res = self._coll.query(query_embeddings=[qv], n_results=k,
                               include=["metadatas", "documents", "distances"])
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        out = []
        for doc, meta, dist in zip(docs, metas, dists):
            meta = meta or {}
            out.append({
                "text": doc, "name": meta.get("name"), "kind": meta.get("kind"),
                "link": meta.get("link"), "updated_at": meta.get("updated_at"),
                "score": round(1.0 - float(dist), 4),   # cosine distance -> similarity
            })
        return out

    def stats(self) -> dict:
        """Indexed-chunk count + last-sync time + embedder, for status display."""
        count = 0
        try:
            self._ensure()
            count = self._coll.count()
        except RagError:
            raise
        except Exception:  # noqa: BLE001 — status must never raise
            pass
        m = self._load_meta()
        return {"count": count, "last_sync": m.get("last_sync"),
                "embedder": m.get("embedder") or self.embedder_id, "dim": m.get("dim")}

    def reindex(self) -> None:
        """Drop all vectors and reset the embedder record so the next sync starts
        fresh (required after an embedder swap). Callers then re-sync targets."""
        self._ensure()
        try:
            self._client.delete_collection(COLLECTION)
        except Exception:  # noqa: BLE001 — may not exist yet
            pass
        self._coll = self._client.get_or_create_collection(
            COLLECTION, metadata={"hnsw:space": "cosine"})
        try:
            self._meta_path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# CLI (wired from doctopdf/__main__.py)
# ---------------------------------------------------------------------------

def cli_query(argv: list) -> int:
    """``doctopdf query "<question>" [-k N]`` — print top-k chunks with citations."""
    k = 5
    terms = []
    it = iter(argv)
    for a in it:
        if a in ("-k", "--k"):
            try:
                k = int(next(it))
            except (StopIteration, ValueError):
                print("error: -k needs an integer", flush=True)
                return 2
        else:
            terms.append(a)
    question = " ".join(terms).strip()
    if not question:
        print('usage: doctopdf query "<question>" [-k N]', flush=True)
        return 2

    store = RagStore(config.load_config())
    try:
        results = store.query(question, k)
    except DimensionMismatch as exc:
        print(f"Dimension mismatch: {exc}", flush=True)
        return 3
    except EmbedError as exc:
        print(f"Embedder unavailable: {exc}", flush=True)
        return 4
    except RagUnavailable as exc:
        print(f"Vector store unavailable: {exc}", flush=True)
        return 5
    except Exception as exc:  # noqa: BLE001 — a clean message beats a raw traceback
        print(f"Query failed: {exc}", flush=True)
        return 6
    if not results:
        print("No matching chunks. Is anything indexed yet? (start the app to sync)", flush=True)
        return 0
    for i, r in enumerate(results, 1):
        when = r.get("updated_at") or "?"
        print(f"\n[{i}] {r.get('name') or '?'}  ·  {r.get('kind') or '?'}  ·  "
              f"updated {when}  ·  score {r.get('score')}", flush=True)
        if r.get("link"):
            print(f"    {r['link']}", flush=True)
        snippet = " ".join((r.get("text") or "").split())
        print(f"    {snippet[:400]}{'…' if len(snippet) > 400 else ''}", flush=True)
    return 0


def cli_reindex(argv: list) -> int:
    """``doctopdf rag reindex`` — clear the store so it rebuilds with the current
    embedder (use after changing the embedder model)."""
    store = RagStore(config.load_config())
    try:
        store.reindex()
    except RagUnavailable as exc:
        print(f"Vector store unavailable: {exc}", flush=True)
        return 5
    print("Vector store cleared. Watched targets re-embed on the app's next cycle "
          "(or use “Rebuild index” in the menu, or restart the app).", flush=True)
    return 0
