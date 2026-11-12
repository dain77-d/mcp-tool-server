"""End-to-end check: launch the server over stdio like Claude does, list its tools, call each one.

python demo.py            # hits the real arXiv API
"""
import asyncio
import os
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

SERVER = [str(Path(__file__).with_name("arxiv_mcp.py"))]
CALLS = [
    ("search_papers", {"query": "direct preference optimization", "max_results": 3}),
    ("get_paper", {"arxiv_id": "2305.18290"}),
    ("read_paper", {"arxiv_id": "2305.18290"}),
    ("read_paper", {"arxiv_id": "2305.18290", "section": "4"}),
]


async def main():
    # pass our env so the child sees the same venv / PYTHONPATH (stdio clients start from a minimal env)
    async with Client(StdioServerParameters(command=sys.executable, args=SERVER, env=dict(os.environ))) as client:
        tools = await client.list_tools()
        print("tools:", ", ".join(t.name for t in tools.tools))
        for name, args in CALLS:
            result = await client.call_tool(name, args)
            text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
            print(f"\n===== {name}({args}) {'ERROR' if result.is_error else ''}\n{text[:900]}{' …' if len(text) > 900 else ''}")
            await asyncio.sleep(3)  # arXiv asks for ~3 s between API calls


if __name__ == "__main__":
    asyncio.run(main())
