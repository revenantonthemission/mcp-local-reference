"""Entry point — ``python -m mcp_local_reference`` or the console script."""

from mcp_local_reference.server import create_server


def main() -> None:
    server = create_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
