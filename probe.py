"""End-to-end probe for the DSH MCP tools, including the approval-callback chain.

Run with `uv run python probe.py`. Connects to DSH web at
`$DSH_BASE_URL` (default http://127.0.0.1:3080) and exercises the tools'
business code paths (without the MCP transport) to validate schema, response
shape, and the prompt -> reply -> stats -> resume loop, plus the approval
chain: prompt that triggers an approval -> awaitingApproval result ->
dsh_respond_approval (accepted:true) -> dsh_wait_turn completes.

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
    HandlerDeps,
    create_session,
    get_session_stats,
    list_pending_approvals,
    list_workspaces,
    respond_approval,
    resume_session,
    send_message,
    wait_turn,
)


PROBE_WORKSPACE = os.environ.get("DSH_PROBE_WORKSPACE") or os.path.dirname(os.path.abspath(__file__))
PROBE_PROMPT = os.environ.get(
    "DSH_PROBE_PROMPT",
    "Reply with the literal string TASK_OK and nothing else.",
)
PROBE_TIMEOUT_S = float(os.environ.get("DSH_PROBE_TIMEOUT", "120"))
# The approval trigger writes OUTSIDE the session workspace (AppData\Roaming is
# neither the workspace root nor the platform temp area, so the sandbox denies
# it under workspace-write). The model then performs the sanctioned one-shot
# retry with sandbox_permissions, which is exactly the path that raises an
# approval request on DSH.
PROBE_APPROVAL_TARGET = os.environ.get(
    "DSH_PROBE_APPROVAL_TARGET",
    os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~"),
        f"dsh_probe_approval_{__import__('uuid').uuid4().hex[:8]}.txt",
    ),
)
PROBE_APPROVAL_PROMPT = os.environ.get("DSH_PROBE_APPROVAL_PROMPT") or (
    "Use the pwsh tool to create a file at '" + PROBE_APPROVAL_TARGET
    + "' containing the text 'probe' "
    "(PowerShell: Set-Content -LiteralPath '" + PROBE_APPROVAL_TARGET + "' -Value 'probe'). "
    "The first attempt will be denied by the sandbox. After the denial, retry the exact same command once "
    "with sandbox_permissions='danger-full-access' and justification='dsh-mcp approval probe'. "
    "Do not call any other tool. When the second attempt succeeds, reply with the literal text "
    "APPROVAL_OK and nothing else."
)


def _step(name, ok, **fields):
    payload = {"step": name, "ok": bool(ok)}
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return payload


async def run(base_url):
    client = DshClient(base_url=base_url, timeout_s=PROBE_TIMEOUT_S)
    await client.start()
    deps = HandlerDeps()
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
                deps,
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

        # ---- Approval callback chain --------------------------------------
        try:
            lp = await list_pending_approvals(client, {"session_id": sid}, deps)
        except (DshConnectionError, DshRpcError) as exc:
            _step("list_pending_approvals", False, error=str(exc))
            return 6
        _step(
            "list_pending_approvals",
            bool(lp.get("ok")) and lp.get("count") == 0,
            sessionId=sid,
            count=lp.get("count"),
        )
        if not (lp.get("ok")) or lp.get("count") != 0:
            return 6

        # Send a prompt that forces the agent to escalate sandbox permissions;
        # with auto_respond_approvals=false the call must come back parked on
        # the approval instead of waiting for the (blocked) turn.
        awaiting_result: dict[str, Any] | None = None
        for attempt in (1, 2):
            t0 = time.monotonic()
            try:
                awaiting_result = await send_message(
                    client,
                    {
                        "session_id": sid,
                        "prompt": PROBE_APPROVAL_PROMPT,
                        "timeout_s": PROBE_TIMEOUT_S,
                        "poll_interval_s": 0.75,
                        "auto_respond_approvals": False,
                    },
                    deps,
                )
            except (DshConnectionError, DshRpcError) as exc:
                _step("send_message_awaiting_approval", False, error=str(exc))
                return 7
            if awaiting_result.get("awaitingApproval"):
                break
            await asyncio.sleep(2.0)  # give the model a fresh shot on retry
        elapsed = round(time.monotonic() - t0, 2)
        pending = awaiting_result.get("pendingApprovals") or []
        awaiting = bool(awaiting_result.get("awaitingApproval"))
        _step(
            "send_message_awaiting_approval",
            awaiting and len(pending) > 0,
            sessionId=sid,
            awaitingApproval=awaiting,
            pendingCount=len(pending),
            toolName=pending[0].get("toolName") if pending else None,
            approvalId=pending[0].get("approvalId") if pending else None,
            hasRpcId=bool(pending[0].get("rpcId")) if pending else None,
            target=PROBE_APPROVAL_TARGET,
            elapsed_s=elapsed,
        )
        if not awaiting or not pending:
            return 7

        approval_id = pending[0]["approvalId"]
        try:
            rr = await respond_approval(
                client,
                {"session_id": sid, "approval_id": approval_id, "outcome": "allowed-once"},
                deps,
            )
        except (DshConnectionError, DshRpcError) as exc:
            _step("respond_approval", False, error=str(exc))
            return 8
        _step(
            "respond_approval",
            bool(rr.get("accepted")),
            sessionId=sid,
            approvalId=approval_id,
            outcome=rr.get("outcome"),
            accepted=rr.get("accepted"),
            receipt=rr.get("receipt"),
        )
        if not rr.get("accepted"):
            return 8

        t0 = time.monotonic()
        try:
            wt = await wait_turn(
                client,
                {
                    "session_id": sid,
                    "timeout_s": PROBE_TIMEOUT_S,
                    "poll_interval_s": 0.75,
                    "auto_respond_approvals": False,
                },
                deps,
            )
        except (DshConnectionError, DshRpcError) as exc:
            _step("wait_turn_after_approval", False, error=str(exc))
            return 9
        elapsed = round(time.monotonic() - t0, 2)
        wt_text = wt.get("assistantText") or ""
        wt_contains = "APPROVAL_OK" in wt_text
        _step(
            "wait_turn_after_approval",
            bool(wt.get("ok")) and wt_contains,
            sessionId=sid,
            ok_field=wt.get("ok"),
            turn=wt.get("turn"),
            elapsed_s=elapsed,
            assistantText=wt_text,
            reply_contains="APPROVAL_OK" if wt_contains else None,
        )
        if not (wt.get("ok")) or not wt_contains:
            return 9

        # The approval probe file lives outside the workspace. Remove it
        # best-effort (a sandboxed probe run cannot, which is exactly the
        # boundary the approval chain exists to cross).
        try:
            os.remove(PROBE_APPROVAL_TARGET)
        except OSError:
            pass

        try:
            lp2 = await list_pending_approvals(client, {"session_id": sid}, deps)
        except (DshConnectionError, DshRpcError) as exc:
            _step("list_pending_approvals_cleared", False, error=str(exc))
            return 10
        _step(
            "list_pending_approvals_cleared",
            bool(lp2.get("ok")) and lp2.get("count") == 0,
            sessionId=sid,
            count=lp2.get("count"),
        )
        if not (lp2.get("ok")) or lp2.get("count") != 0:
            return 10

        print("---ALL OK---")
        return 0
    finally:
        await client.close()


def main():
    base_url = os.environ.get("DSH_BASE_URL", "http://127.0.0.1:3080")
    return asyncio.run(run(base_url))


if __name__ == "__main__":
    sys.exit(main())
