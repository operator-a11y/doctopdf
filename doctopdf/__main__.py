"""CLI entry point: ``python -m doctopdf [subcommand]``.

  (none) | app      Launch the menu-bar app (same as ``python -m doctopdf.app``).
  query "<q>" [-k N] Search the synced knowledge base; print top-k with citations.
  mcp               Start the read-only MCP server over stdio.
  rag reindex       Clear the vector store so it rebuilds with the current embedder.
"""

from __future__ import annotations

import sys

USAGE = (
    'usage: python -m doctopdf [app | query "<question>" [-k N] | mcp | rag reindex]'
)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "app"

    if cmd == "app":
        from .app import main as app_main
        app_main()
        return 0
    if cmd == "query":
        from . import rag
        return rag.cli_query(argv[1:])
    if cmd == "mcp":
        from . import mcp_server
        mcp_server.main()
        return 0
    if cmd == "rag":
        if len(argv) >= 2 and argv[1] == "reindex":
            from . import rag
            return rag.cli_reindex(argv[2:])
        print("usage: python -m doctopdf rag reindex", flush=True)
        return 2

    print(USAGE, flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
