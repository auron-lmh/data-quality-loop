# -*- coding: utf-8 -*-
"""启动 Data Quality Loop MCP Server（stdio transport）。

用法:
    python -m data_quality_loop.mcp        # 需 PYTHONPATH=src

对接 Claude Desktop / Claude Code:
    mcpServers:
      data-quality-loop:
        command: python
        args: [-m, data_quality_loop.mcp]
"""
from .server import server


def main():
    server.run()          # FastMCP 默认 stdio transport


if __name__ == "__main__":
    main()
