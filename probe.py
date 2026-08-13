"""End-to-end probe for all five DSH MCP tools.

Run with `uv run python probe.py`. Connects to DSH web at
`$DSH_BASE_URL` (default http://127.0.0.1:3080) and exercises the five tools'
business code paths (without the MCP transport) to validate schema, response
shape, and the prompt -> reply -> stats -> resume loop.

Designed to be safe to re-run: every step is idempotent or unique per probe.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

from dsh_mcp.client import DshClient, DshConnectionError, DshRpcError
from dsh_mcp.server import (
    create_session,
    get_session_stats,
    list_workspaces,
    resume_session,
    send_message,
)


PROBE_WORKSPACE = os.environ.get("DSH_PROBE_WORKSPACE") or os.path.dirname(os.path.abspath(__file__))
PROBE_PROMPT = os.environ.get(
    "DSH_PROBE_PROMPT",
    "Reply with the literal string TASK_OK and nothing else.",
)
PROBE_TIMEOUT_S = float(os.environ.get("DSH_PROBE_TIMEOUT", "120"))


def _step(name, ok, **fields):
    payload = {"step": name, "ok": bool(ok)}
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return payload


async def run(base_url):
    client = DshClient(base_url=base_url, timeout_s=PROBE_TIMEOUT_S)
    await client.start()
    try:
        try:
            ws_result = await list_workspaces(client, {})
        except (DshConnectionError, DshRpcError) as exc:
            _step("list_workspaces", False, error=str(exc))
            return 1
        _step(
            "list_workspaces",
            True,
            count=ws_result["count"],
            firstPath=ws_result["workspaces"][0]["path"] if ws_result["workspaces"] else None,
        )

        try:
            cs_result = await create_session(
                client,
                {"workspace_path": PROBE_WORKSPACE, "model": "deepseek-v4-flash"},
            )
        except (DshConnectionError, DshRpcError) as exc:
            _step("create_session", False, error=str(exc))
            return 2
        sid = cs_result["sessionId"]
        _step(
            "create_session",
            True,
            sessionId=sid,
            workspacePath=cs_result["workspacePath"],
            model=cs_result["model"],
            selected=cs_result["selected"],
        )

        t0 = time.monotonic()
        try:
            sm_result = await send_message(
                client,
                {
                    "session_id": sid,
                    "prompt": PROBE_PROMPT,
                    "timeout_s": PROBE_TIMEOUT_S,
                    "poll_interval_s": 0.75,
                },
            )
        except (DshConnectionError, DshRpcError) as exc:
            _step("send_message", False, error=str(exc))
            return 3
        elapsed = round(time.monotonic() - t0, 2)
        text = sm_result.get("assistantText") or ""
        contains = "TASK_OK" in text
        usage = sm_result.get("tokenUsage") or {}
        cache_read = int(usage.get("cacheReadTokens") or 0)
        _step(
            "send_message",
            bool(sm_result.get("ok")) and contains and cache_read >= 0,
            sessionId=sid,
            elapsed_s=elapsed,
            assistantText=text,
            reply_contains="TASK_OK" if contains else None,
            cacheReadTokens=cache_read,
            cacheWriteTokens=int(usage.get("cacheWriteTokens") or 0),
            uncachedInputTokens=int(usage.get("uncachedInputTokens") or 0),
            outputTokens=int(usage.get("outputTokens") or 0),
            turn=sm_result.get("turn"),
            assistantStep=sm_result.get("step"),
            ok_field=sm_result.get("ok"),
        )
        if not (sm_result.get("ok")) or not contains:
            return 3

        try:
            stats = await get_session_stats(client, {"session_id": sid})
        except (DshConnectionError, DshRpcError) as exc:
            _step("get_session_stats", False, error=str(exc))
            return 4
        stats_tu = stats.get("tokenUsage") or {}
        _step(
            "get_session_stats",
            True,
            sessionId=sid,
            messageCount=stats.get("messageCount"),
            cacheReadTokens=int(stats_tu.get("cacheReadTokens") or 0),
            cacheWriteTokens=int(stats_tu.get("cacheWriteTokens") or 0),
            title=stats.get("title"),
        )

        try:
            resume = await resume_session(client, {"session_id": sid})
        except (DshConnectionError, DshRpcError) as exc:
            _step("resume_session", False, error=str(exc))
            return 5
        _step(
            "resume_session",
            True,
            sessionId=sid,
            model=resume.get("model"),
            cacheReadTokens=int((resume.get("tokenUsage") or {}).get("cacheReadTokens") or 0),
        )

        print("---ALL OK---")
        return 0
    finally:
        await client.close()


def main():
    base_url = os.environ.get("DSH_BASE_URL", "http://127.0.0.1:3080")
    return asyncio.run(run(base_url))


if __name__ == "__main__":
    sys.exit(main())
