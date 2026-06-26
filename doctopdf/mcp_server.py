"""Read-only MCP server exposing DocToPDF's always-current knowledge base.

Starts a stdio MCP server (official ``mcp`` SDK) with a single ``search_knowledge``
tool. Because DocToPDF keeps the vector store synced to live content, an agent
calling this tool always gets the present version of each source, with a citation
(name, link) and freshness (``updated_at``) so it can say "from <name>, updated
<date>: …". Search only — it never mutates the store.

Register it with an MCP client (Claude Desktop / Claude Code), e.g.::

    {
      "mcpServers": {
        "doctopdf": {
          "command": "/path/to/doctopdf/.venv/bin/python",
          "args": ["-m", "doctopdf", "mcp"]
        }
      }
    }
"""

from __future__ import annotations

from . import config, rag

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - env-dependent
    raise SystemExit("The 'mcp' package is required: pip install mcp") from exc

server = FastMCP("doctopdf")

# One store handle for the process; the underlying Chroma client opens lazily on
# first query and is cheap to keep around.
_store = None


def _get_store():
    global _store
    if _store is None:
        _store = rag.RagStore(config.load_config())
    return _store


@server.tool()
def search_knowledge(query: str, k: int = 5) -> dict:
    """Search DocToPDF's continuously-synced knowledge base of watched documents,
    sheets, slides, and web pages.

    Returns the most relevant current chunks, each with the source name, kind, a
    link to open it, and when it was last updated — so answers can cite source
    and freshness. Read-only; reflects live content (DocToPDF re-embeds on change).

    Args:
        query: A natural-language question or search phrase.
        k: Maximum number of chunks to return (default 5).
    """
    try:
        results = _get_store().query(query, k)
    except rag.DimensionMismatch as exc:
        return {"error": "dimension_mismatch", "detail": str(exc), "results": []}
    except rag.EmbedError as exc:
        return {"error": "embedder_unavailable", "detail": str(exc), "results": []}
    except rag.RagUnavailable as exc:
        return {"error": "store_unavailable", "detail": str(exc), "results": []}
    except Exception as exc:  # noqa: BLE001 — return a structured error, never crash the tool
        return {"error": "query_failed", "detail": str(exc), "results": []}
    return {"error": None, "count": len(results), "results": results}


def main() -> None:
    server.run()   # stdio transport by default


if __name__ == "__main__":
    main()
