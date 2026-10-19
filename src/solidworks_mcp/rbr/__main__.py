"""Entry point: the upstream server plus the RBR extension.

    python -m solidworks_mcp.rbr            # MCP over stdio
    python -m solidworks_mcp.rbr --selftest # upstream selftest, with the RBR patches applied
"""

from .. import server
from . import register


def main() -> None:
    register(server)
    server.main()


if __name__ == "__main__":
    main()
