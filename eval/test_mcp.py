# -*- coding: utf-8 -*-
"""MCP Server 冒烟测试:启动 stdio server → list_tools → call scan_anomalies

运行(需 PYTHONPATH=src):
    python eval/test_mcp.py
"""
import asyncio
import json
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def main():
    params = StdioServerParameters(
        command="python",
        args=["-m", "data_quality_loop.mcp"],
        env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src")},
        cwd=ROOT,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("MCP 工具清单:", names)

            # 只读扫描(不需要 LLM,快)
            r = await session.call_tool("scan_anomalies", {})
            data = json.loads(r.content[0].text)
            print(f"scan_anomalies → 异常数: {data['count']}")
            for a in data["anomalies"][:3]:
                print(f"    {a['key']}: {a['detail']}")

            # 表清单
            r = await session.call_tool("list_quality_tables", {})
            print("list_quality_tables →", r.content[0].text)

    print("\n✅ MCP 冒烟测试通过")


if __name__ == "__main__":
    asyncio.run(main())
