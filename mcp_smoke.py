"""MCP stdio smoke test.

Spawns `dsh-mcp` as a subprocess over stdio, performs initialize + tools/list,
then exercises two tools (list_workspaces + create_session) to confirm the
MCP transport end-to-end.

Run with `uv run python mcp_smoke.py`. Exits non-zero on any deviation.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


HERE = os.path.dirname(os.path.abspath(__file__))


async def run() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "dsh_mcp.server"],
        cwd=HERE,
        env={
            **os.environ,
            "DSH_BASE_URL": os.environ.get("DSH_BASE_URL", "http://127.0.0.1:3080"),
            "DSH_TIMEOUT_S": "60",
            "DSH_MCP_LOG": "WARNING",
        },
    )
    print("[smoke] launching dsh-mcp over stdio", flush=True)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(
                "[smoke] server initialised:",
                init.serverInfo.name,
                init.serverInfo.version,
                flush=True,
            )
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("[smoke] tools advertised:", names, flush=True)
            expected = {
                "dsh_list_workspaces",
                "dsh_create_session",
                "dsh_send_message",
                "dsh_wait_turn",
                "dsh_list_pending_approvals",
                "dsh_respond_approval",
                "dsh_get_session_stats",
                "dsh_resume_session",
            }
            missing = expected - set(names)
            if missing:
                print("[smoke] FAIL: missing tools:", missing, flush=True)
                return 2

            res = await session.call_tool("dsh_list_workspaces", {})
            if res.isError:
                print("[smoke] FAIL: list_workspaces error", flush=True)
                return 3
            parsed = json.loads(res.content[0].text)
            if not parsed.get("ok"):
                print("[smoke] FAIL: list_workspaces ok=False", res.content[0].text, flush=True)
                return 3
            print("[smoke] OK dsh_list_workspaces count=%d" % parsed.get("count", 0), flush=True)

            workspace_path = os.path.join(HERE)
            res2 = await session.call_tool(
                "dsh_create_session",
                {"workspace_path": workspace_path, "model": "deepseek-v4-flash"},
            )
            if res2.isError:
                print("[smoke] FAIL: create_session error", flush=True)
                print(res2.content[0].text, flush=True)
                return 4
            parsed2 = json.loads(res2.content[0].text)
            if not parsed2.get("ok") or not parsed2.get("sessionId"):
                print("[smoke] FAIL: create_session did not return sessionId", flush=True)
                print(res2.content[0].text, flush=True)
                return 4
            print(
                "[smoke] OK dsh_create_session sessionId=%s model=%s"
                % (parsed2["sessionId"], parsed2.get("model")),
                flush=True,
            )
            print("[smoke] OK: MCP transport verified for 2 tools", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        sys.exit(130)
