"""MCP server entry point for DSH cordis RPC."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import anyio
import mcp.server.stdio
import mcp.types as types
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions

from .client import DshClient, DshConnectionError, DshRpcError
from .models import (
    ApprovalRequest,
    RespondReceipt,
    SessionCreateValue,
    SessionHistoryValue,
    WorkspaceListValue,
)
from .mux import ApprovalRegistry, scan_pending_approvals, watch_mux


log = logging.getLogger("dsh-mcp.server")


DEFAULT_TIMEOUT = 120.0
POLL_INTERVAL = 0.75
APPROVAL_SAMPLING_TIMEOUT = 25.0
MUX_SCAN_TIMEOUT = 6.0
APPROVAL_OUTCOMES = ("allowed-once", "rejected")


@dataclass
class HandlerDeps:
    """Per-server context passed to every tool handler.

    ``registry`` is shared across handlers so an approval captured by
    ``dsh_send_message`` / ``dsh_wait_turn`` / ``dsh_list_pending_approvals``
    can be answered by ``dsh_respond_approval`` with the same rpcId. ``server``
    is the live MCP Server instance used for ``sampling/createMessage``
    callbacks; it is ``None`` outside an MCP request (e.g. probe.py), which
    disables the sampling track.
    """

    registry: ApprovalRegistry = field(default_factory=ApprovalRegistry)
    server: Optional[Server] = None


def _ok(value):
    text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    return [types.TextContent(type="text", text=text)]


def _fail(message, **extra):
    payload = {"ok": False, "error": message, **extra}
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False))]


async def list_workspaces(client, args, deps=None):
    value = WorkspaceListValue.model_validate(await client.call("workspace.list", {}))
    return {
        "ok": True,
        "count": len(value.items),
        "workspaces": [w.model_dump() for w in value.items],
        "archivedSessionIds": value.archivedSessionIds,
    }


async def _ensure_workspace(client, path):
    norm = os.path.normpath(path)
    listed = WorkspaceListValue.model_validate(await client.call("workspace.list", {}))
    for w in listed.items:
        if os.path.normpath(w.path).lower() == norm.lower():
            return w.workspaceId
    create = await client.call("workspace.create", {"path": norm})
    if not isinstance(create, dict) or "workspace" not in create:
        raise DshRpcError(code="workspace-create-failed", message="workspace.create produced no workspace", endpoint="workspace.create")
    return str(create["workspace"]["workspaceId"])


async def create_session(client, args, deps=None):
    workspace_path = args.get("workspace_path")
    if not workspace_path or not isinstance(workspace_path, str):
        raise ValueError("workspace_path (string) is required")
    model = args.get("model") or "deepseek-v4-flash"
    reasoning_effort = args.get("reasoning_effort")
    workspace_id = await _ensure_workspace(client, workspace_path)
    payload = {"workspaceId": workspace_id, "agentPreset": "standard"}
    create_resp = SessionCreateValue.model_validate(await client.call("session.create", payload))
    session_id = create_resp.sessionId
    provider = args.get("provider") or "deepseek-official"
    select_kwargs = {
        "sessionId": session_id,
        "provider": provider,
        "model": model,
    }
    if reasoning_effort:
        select_kwargs["reasoningEffort"] = reasoning_effort
    try:
        selected = await client.call("session.selectModel", select_kwargs)
    except DshRpcError as exc:
        log.warning("selectModel failed (%s); keeping default model", exc)
        selected = None
    return {
        "ok": True,
        "sessionId": session_id,
        "workspaceId": workspace_id,
        "workspacePath": os.path.normpath(workspace_path),
        "model": model,
        "provider": provider,
        "reasoningEffort": reasoning_effort,
        "selected": selected,
    }


def _event_type(event):
    inner = event.get("event") if isinstance(event, dict) else None
    if isinstance(inner, dict):
        return str(inner.get("type", ""))
    return ""


def _event_seq(event):
    inner = event.get("event") if isinstance(event, dict) else None
    if isinstance(inner, dict):
        seq = inner.get("seq")
        if isinstance(seq, int):
            return seq
    return -1


async def send_message(client, args, deps: HandlerDeps | None = None):
    session_id = args.get("session_id")
    prompt = args.get("prompt")
    if not session_id or not isinstance(session_id, str):
        raise ValueError("session_id (string) is required")
    if not prompt or not isinstance(prompt, str):
        raise ValueError("prompt (string) is required")
    timeout_s = float(args.get("timeout_s") or DEFAULT_TIMEOUT)
    interval = float(args.get("poll_interval_s") or POLL_INTERVAL)
    auto_respond = bool(args.get("auto_respond_approvals", True))
    approval_timeout_s = float(args.get("approval_timeout_s") or APPROVAL_SAMPLING_TIMEOUT)
    deps = deps or HandlerDeps()
    try:
        baseline = SessionHistoryValue.model_validate(
            await client.call("session.history", {"sessionId": session_id})
        )
    except DshRpcError:
        baseline = None
    if baseline is not None:
        max_seq = max(
            (_event_seq(e.model_dump()) for e in baseline.events),
            default=-1,
        )
    else:
        max_seq = -1
    prompt_payload = {
        "sessionId": session_id,
        "mode": "queue",
        "content": [{"type": "text", "text": prompt}],
    }
    prompt_resp = await client.call("session.prompt", prompt_payload)
    if not isinstance(prompt_resp, dict) or not prompt_resp.get("accepted"):
        return {"ok": False, "error": "session.prompt rejected", "raw": prompt_resp}
    return await _wait_for_turn(
        client, deps, session_id, max_seq, timeout_s, interval,
        auto_respond, approval_timeout_s,
    )


async def wait_turn(client, args, deps: HandlerDeps | None = None):
    """Wait for the in-flight turn to finish without sending a new prompt."""
    session_id = args.get("session_id")
    if not session_id or not isinstance(session_id, str):
        raise ValueError("session_id (string) is required")
    timeout_s = float(args.get("timeout_s") or DEFAULT_TIMEOUT)
    interval = float(args.get("poll_interval_s") or POLL_INTERVAL)
    auto_respond = bool(args.get("auto_respond_approvals", True))
    approval_timeout_s = float(args.get("approval_timeout_s") or APPROVAL_SAMPLING_TIMEOUT)
    deps = deps or HandlerDeps()
    try:
        baseline = SessionHistoryValue.model_validate(
            await client.call("session.history", {"sessionId": session_id})
        )
    except DshRpcError:
        baseline = None
    max_seq = -1
    if baseline is not None:
        max_seq = max(
            (_event_seq(e.model_dump()) for e in baseline.events),
            default=-1,
        )
    return await _wait_for_turn(
        client, deps, session_id, max_seq, timeout_s, interval,
        auto_respond, approval_timeout_s,
    )


async def list_pending_approvals(client, args, deps: HandlerDeps | None = None):
    """List still-pending approval requests, optionally for one session."""
    session_id = args.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise ValueError("session_id (string) is required")
    deps = deps or HandlerDeps()
    timeout_s = float(args.get("scan_timeout_s") or MUX_SCAN_TIMEOUT)
    found = await scan_pending_approvals(client, session_id=session_id, timeout_s=timeout_s)
    for rec in found:
        deps.registry.put(rec)
    return {
        "ok": True,
        "sessionId": session_id,
        "count": len(found),
        "pending": [_approval_dict(rec) for rec in found],
    }


async def respond_approval(client, args, deps: HandlerDeps | None = None):
    """Answer one pending approval; the receipt says whether DSH accepted it."""
    session_id = args.get("session_id")
    approval_id = args.get("approval_id")
    outcome = args.get("outcome")
    if not session_id or not isinstance(session_id, str):
        raise ValueError("session_id (string) is required")
    if not approval_id or not isinstance(approval_id, str):
        raise ValueError("approval_id (string) is required")
    if outcome not in APPROVAL_OUTCOMES:
        raise ValueError("outcome must be one of: " + ", ".join(APPROVAL_OUTCOMES))
    deps = deps or HandlerDeps()
    rpc_id = args.get("rpc_id")
    if rpc_id is not None and not isinstance(rpc_id, str):
        raise ValueError("rpc_id (string) is required")
    rec: dict[str, Any] | None = None
    if not rpc_id:
        rec = deps.registry.get(session_id, approval_id)
        if rec is not None:
            rpc_id = rec.get("rpcId")
    if not rpc_id:
        found = await scan_pending_approvals(client, session_id=session_id, timeout_s=MUX_SCAN_TIMEOUT)
        for candidate in found:
            deps.registry.put(candidate)
            if candidate.get("approvalId") == approval_id:
                rec = candidate
                rpc_id = candidate.get("rpcId")
                break
    if not rpc_id:
        return {
            "ok": False,
            "error": "approval is not pending (or rpcId unknown); "
                     "call dsh_list_pending_approvals first",
            "sessionId": session_id,
            "approvalId": approval_id,
        }
    try:
        receipt = await _post_respond(
            client, {"rpcId": rpc_id, "sessionId": session_id, "approvalId": approval_id}, outcome
        )
    except (DshConnectionError, DshRpcError) as exc:
        return {"ok": False, "error": "respond failed: " + str(exc),
                "sessionId": session_id, "approvalId": approval_id}
    if receipt.get("accepted"):
        deps.registry.pop(session_id, approval_id)
    return {
        "ok": bool(receipt.get("accepted")),
        "accepted": bool(receipt.get("accepted")),
        "sessionId": session_id,
        "approvalId": approval_id,
        "outcome": outcome,
        "receipt": receipt,
        **({"error": "DSH did not accept the response: " + str(receipt.get("reason"))}
           if not receipt.get("accepted") else {}),
    }


def _assemble_send_result(final_history, max_seq, session_id, last_turn):
    text_parts = []
    usage_chunks = []
    final_usage = None
    final_step = 0
    for entry in final_history.events:
        event_dict = entry.model_dump()
        if _event_seq(event_dict) <= max_seq:
            continue
        inner = event_dict.get("event") or {}
        data = inner.get("data") or {}
        etype = inner.get("type")
        if etype == "assistant/chunk":
            chunk = data.get("chunk") or {}
            if isinstance(chunk, dict):
                ctype = chunk.get("type")
                if ctype == "text":
                    delta = chunk.get("text") or chunk.get("delta") or ""
                    if isinstance(delta, str) and delta:
                        text_parts.append(delta)
                elif ctype == "usage" and isinstance(chunk.get("usage"), dict):
                    usage_chunks.append(chunk["usage"])
        elif etype == "assistant/message":
            message = data.get("message") or {}
            if isinstance(message, dict):
                for part in message.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text")
                        if isinstance(text, str):
                            text_parts.append(text)
            if isinstance(data.get("usage"), dict):
                final_usage = data["usage"]
            step = data.get("step")
            if isinstance(step, int):
                final_step = step
    projections = final_history.projections
    token_usage = projections.values.tokenUsage.model_dump() if projections else {}
    session_stats = projections.values.sessionStats.model_dump() if projections else {}
    return {
        "ok": True,
        "sessionId": session_id,
        "turn": last_turn,
        "step": final_step,
        "assistantText": "".join(text_parts),
        "usage": final_usage,
        "usageChunks": usage_chunks,
        "tokenUsage": token_usage,
        "sessionStats": session_stats,
        "title": projections.values.title if projections else None,
    }


def _approval_dict(rec: dict[str, Any]) -> dict[str, Any]:
    """Normalize one raw ``approval/requested`` frame into the public shape."""
    cleaned = {k: v for k, v in rec.items() if v is not None and k != "type"}
    return ApprovalRequest.model_validate(cleaned).model_dump(exclude_none=True)


async def _post_respond(client: DshClient, rec: dict[str, Any], outcome: str) -> dict[str, Any]:
    """Send one client-response answering ``rec``; returns the RpcReceipt dict."""
    message = {
        "type": "client-response",
        "rpcId": rec.get("rpcId") or "",
        "result": {
            "ok": True,
            "value": {
                "sessionId": rec.get("sessionId"),
                "approvalId": rec.get("approvalId"),
                "outcome": outcome,
            },
        },
    }
    receipt = await client.respond(message)
    return RespondReceipt.model_validate(receipt).model_dump(exclude_none=True)


async def _decide_approval_sampling(
    deps: HandlerDeps,
    rec: dict[str, Any],
    timeout_s: float,
) -> Optional[str]:
    """Ask the MCP client (Hermes) to decide one approval via sampling.

    Uses ``sampling/createMessage`` (server→client) with an exact-vocabulary
    prompt; returns ``allowed-once`` / ``rejected`` or ``None`` when sampling
    is unavailable (no request context), unsupported, or produced no decision.
    """
    if deps.server is None:
        return None
    try:
        session = deps.server.request_context.session
    except LookupError:
        return None
    tool_name = rec.get("toolName") or "?"
    reason = rec.get("reason")
    why = f" Reason from DSH: {reason}" if reason else ""
    call_note = f" callId={rec.get('callId')}" if rec.get("callId") else ""
    prompt = (
        "A DeepSeek Harness (DSH) agent session requests permission for a tool "
        f"call: tool '{tool_name}'{call_note}.{why} "
        "Decide whether to allow this exact call (one-time grant) or reject it. "
        "Reply with exactly one token, nothing else: 'allowed-once' or 'rejected'."
    )
    try:
        result = await asyncio.wait_for(
            session.create_message(
                messages=[
                    types.SamplingMessage(
                        role="user",
                        content=types.TextContent(type="text", text=prompt),
                    )
                ],
                max_tokens=8,
                temperature=0.0,
                system_prompt=(
                    "You are the approval gate for a DeepSeek Harness agent. "
                    "Grant one-time permission (allowed-once) or reject."
                ),
            ),
            timeout=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 - sampling is best-effort
        log.info("sampling decision unavailable for approval %s: %s",
                 rec.get("approvalId"), exc)
        return None
    text = ""
    content = getattr(result, "content", None)
    if isinstance(content, dict) and content.get("type") == "text":
        text = str(content.get("text") or "")
    elif hasattr(content, "type") and getattr(content, "type") == "text":
        text = str(getattr(content, "text") or "")
    lowered = text.strip().lower()
    if "allowed-once" in lowered:
        return "allowed-once"
    if "rejected" in lowered:
        return "rejected"
    log.warning("sampling returned an unrecognized decision for %s: %r",
                rec.get("approvalId"), text[:80])
    return None


async def _wait_for_turn(
    client: DshClient,
    deps: HandlerDeps,
    session_id: str,
    max_seq: int,
    timeout_s: float,
    interval: float,
    auto_respond: bool,
    approval_timeout_s: float,
) -> dict[str, Any]:
    """Wait for a turn to finish while watching for approval requests.

    The mux watch runs in its own task and only *collects* approvals; all
    decisions/responds happen in this (request-context) task so that the
    sampling callback can reach the MCP client. Returns the normal assembled
    result, an ``awaitingApproval`` result, or a timeout result.
    """
    approvals: list[dict[str, Any]] = []
    stop = asyncio.Event()
    handled_count = 0

    async def on_frame(_rpc_id: str, frame: dict[str, Any]) -> None:
        if frame.get("type") != "approval/requested":
            return
        if frame.get("sessionId") != session_id:
            return
        rec = {"rpcId": _rpc_id, **frame}
        deps.registry.put(rec)
        approvals.append(rec)

    mux_task = asyncio.create_task(watch_mux(client, on_frame, stop))
    try:
        deadline = time.monotonic() + timeout_s
        last_turn = 0
        final_history = None
        while time.monotonic() < deadline:
            # Handle any approvals collected since the last tick. Decisions must
            # run in this task (sampling needs the request context); the watch
            # task only feeds the list.
            while approvals:
                rec = approvals.pop(0)
                if auto_respond:
                    outcome = await _decide_approval_sampling(deps, rec, approval_timeout_s)
                    if outcome is not None:
                        try:
                            receipt = await _post_respond(client, rec, outcome)
                        except (DshConnectionError, DshRpcError) as exc:
                            log.warning("respond failed for %s: %s", rec.get("approvalId"), exc)
                            return _assemble_awaiting_result(
                                session_id, approvals, rec, "respond failed: " + str(exc))
                        if receipt.get("accepted"):
                            handled_count += 1
                            deps.registry.pop(rec.get("sessionId"), rec.get("approvalId"))
                        else:
                            return _assemble_awaiting_result(
                                session_id, approvals, rec,
                                "respond not accepted: " + str(receipt.get("reason")))
                        continue
                    # Sampling unavailable or undecided -> fall through to awaiting.
                return _assemble_awaiting_result(
                    session_id, approvals, rec, "turn is awaiting approval")
            await asyncio.sleep(interval)
            try:
                current = SessionHistoryValue.model_validate(
                    await client.call("session.history", {"sessionId": session_id})
                )
            except DshRpcError as exc:
                log.debug("history poll failed: %s", exc)
                continue
            ended = False
            for entry in current.events:
                event_dict = entry.model_dump()
                seq = _event_seq(event_dict)
                if seq <= max_seq:
                    continue
                if _event_type(event_dict) == "turn/end":
                    data = event_dict["event"].get("data") or {}
                    try:
                        last_turn = int(data.get("turn") or 0)
                    except Exception:
                        last_turn = 0
                    ended = True
            if ended:
                final_history = current
                break
        if final_history is None:
            pending = [deps.registry.get(session_id, a["approvalId"]) or a for a in approvals]
            return {
                "ok": False,
                "awaitingApproval": bool(approvals),
                "sessionId": session_id,
                "error": "timeout after " + str(timeout_s) + "s waiting for turn completion",
                "pendingApprovals": [_approval_dict(a) for a in pending],
            }
        result = _assemble_send_result(final_history, max_seq, session_id, last_turn)
        result["approvalsHandled"] = handled_count
        return result
    finally:
        stop.set()
        mux_task.cancel()
        try:
            await mux_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


def _assemble_awaiting_result(
    session_id: str,
    approvals: list[dict[str, Any]],
    rec: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    """Result returned when a turn is parked on approval(s) and not auto-handled."""
    pending = [rec, *approvals]
    return {
        "ok": False,
        "awaitingApproval": True,
        "turnPending": True,
        "sessionId": session_id,
        "turn": 0,
        "error": message,
        "pendingApprovals": [_approval_dict(p) for p in pending],
    }


async def get_session_stats(client, args, deps=None):
    session_id = args.get("session_id")
    if not session_id or not isinstance(session_id, str):
        raise ValueError("session_id (string) is required")
    history = SessionHistoryValue.model_validate(
        await client.call("session.history", {"sessionId": session_id})
    )
    projections = history.projections
    if projections is None:
        return {
            "ok": True,
            "sessionId": session_id,
            "projections": None,
            "tokenUsage": {},
            "sessionStats": {},
            "messageCount": 0,
            "hasMore": history.hasMore,
        }
    message_count = sum(
        1 for entry in history.events
        if _event_type(entry.model_dump()) in ("user/message", "assistant/message")
    )
    return {
        "ok": True,
        "sessionId": session_id,
        "messageCount": message_count,
        "hasMore": history.hasMore,
        "title": projections.values.title,
        "tokenUsage": projections.values.tokenUsage.model_dump(),
        "sessionStats": projections.values.sessionStats.model_dump(),
        "contextPressure": projections.values.contextPressure,
        "projections": {k: v for k, v in projections.values.model_dump().items()
                        if k not in ("tokenUsage", "sessionStats")},
    }


async def resume_session(client, args, deps=None):
    session_id = args.get("session_id")
    if not session_id or not isinstance(session_id, str):
        raise ValueError("session_id (string) is required")
    history = SessionHistoryValue.model_validate(
        await client.call("session.history", {"sessionId": session_id})
    )
    models_resp = None
    try:
        models_resp = await client.call("session.models", {"sessionId": session_id})
    except DshRpcError as exc:
        log.debug("session.models failed during resume: %s", exc)
    projections = history.projections
    selected = None
    if isinstance(models_resp, dict):
        selected = models_resp.get("current")
    return {
        "ok": True,
        "sessionId": session_id,
        "title": projections.values.title if projections else None,
        "model": selected,
        "selectedModel": selected,
        "tokenUsage": projections.values.tokenUsage.model_dump() if projections else {},
        "sessionStats": projections.values.sessionStats.model_dump() if projections else {},
        "messageCount": sum(
            1 for entry in history.events
            if _event_type(entry.model_dump()) in ("user/message", "assistant/message")
        ),
        "hasMore": history.hasMore,
        "note": "Continuing this session with session.prompt will reuse the cached prompt prefix.",
    }



SERVER_INSTRUCTIONS = """DSH MCP exposes a DeepSeek Harness (dsh) web UI as MCP tools.

Available tools:
- dsh_list_workspaces(): list every known workspace (path -> workspaceId mapping).
- dsh_create_session(workspace_path, model?, provider?, reasoning_effort?): create a new
  DSH session bound to a directory; auto-creates the workspace if missing. Returns sessionId.
- dsh_send_message(session_id, prompt, timeout_s?, poll_interval_s?,
  auto_respond_approvals?, approval_timeout_s?): send a prompt and block until the turn
  completes. Returns the assistant text plus token usage
  (incl. cacheReadTokens / cacheWriteTokens - the metric DSH prefix caching makes meaningful).
  When the agent requests permission (approval/asked), the server tries a sampling callback
  to the MCP client (Hermes) to decide, then answers DSH so the turn keeps running. If
  sampling is unavailable or auto_respond_approvals=false, the call returns immediately with
  awaitingApproval=true + pendingApprovals so the caller can decide and call
  dsh_respond_approval / dsh_wait_turn.
- dsh_wait_turn(session_id, timeout_s?, poll_interval_s?, auto_respond_approvals?,
  approval_timeout_s?): wait for the in-flight turn to finish without sending a new prompt.
  Use it after answering a pending approval.
- dsh_list_pending_approvals(session_id?): list still-pending approval requests
  (approvalId, toolName, callId?, reason?, rpcId) from the DSH event stream.
- dsh_respond_approval(session_id, approval_id, outcome, rpc_id?): answer one pending
  approval ('allowed-once' or 'rejected'); returns the DSH receipt (accepted:true when the
  answer was consumed).
- dsh_get_session_stats(session_id): fetch the cached projections (tokenUsage, sessionStats,
  contextPressure) for an existing session.
- dsh_resume_session(session_id): verify a session is resumable and surface its current
  model + last cached state so subsequent prompts reuse the prefix cache.

Approval flow: when dsh_send_message returns awaitingApproval=true, the turn is parked on a
permission request. Decide (or have the user decide) and call dsh_respond_approval with the
approvalId from pendingApprovals, then call dsh_wait_turn to continue the turn.

Wire: DSH web runs locally on http://127.0.0.1:3080 (override with DSH_BASE_URL). The
approval event stream is a WebSocket at /api/events.mux (requires the websockets package).
"""


def _tool(name, description, schema):
    return types.Tool(
        name=name,
        description=description,
        inputSchema={
            "type": "object",
            "properties": schema,
            "additionalProperties": False,
        },
    )


def build_server(client):
    server = Server("dsh-mcp")

    def _list_tool_defs():
        return [
            _tool(
                "dsh_list_workspaces",
                "List every DSH workspace known to the local dsh web UI.",
                {},
            ),
            _tool(
                "dsh_create_session",
                "Create a new DSH session bound to a directory; auto-creates the workspace if missing. Returns the sessionId for follow-up tool calls.",
                {
                    "workspace_path": {
                        "type": "string",
                        "description": "Absolute directory path the session should be rooted at (e.g. C:\\\\Users\\\\chenty\\\\code).",
                    },
                    "model": {
                        "type": "string",
                        "description": "Model id (defaults to deepseek-v4-flash).",
                        "default": "deepseek-v4-flash",
                    },
                    "provider": {
                        "type": "string",
                        "description": "Provider id (defaults to the bundled deepseek-official).",
                        "default": "deepseek-official",
                    },
                    "reasoning_effort": {
                        "type": "string",
                        "description": "Optional reasoning effort id (off/high/max).",
                    },
                },
            ),
            _tool(
                "dsh_send_message",
                "Send a prompt to a DSH session and block until the assistant turn completes. Result includes assistant text and token usage (cacheReadTokens / cacheWriteTokens). If the agent requests permission (approval/asked), the server answers it via an MCP sampling callback (auto_respond_approvals=true) so the turn keeps running; otherwise the call returns immediately with awaitingApproval=true and pendingApprovals for the caller to handle via dsh_respond_approval + dsh_wait_turn.",
                {
                    "session_id": {"type": "string"},
                    "prompt": {"type": "string"},
                    "timeout_s": {"type": "number", "default": DEFAULT_TIMEOUT},
                    "poll_interval_s": {"type": "number", "default": POLL_INTERVAL},
                    "auto_respond_approvals": {
                        "type": "boolean",
                        "default": True,
                        "description": "Answer approval requests automatically via a sampling callback to the MCP client (Hermes). Set false to return the pending approvals instead.",
                    },
                    "approval_timeout_s": {
                        "type": "number",
                        "default": APPROVAL_SAMPLING_TIMEOUT,
                        "description": "Seconds to wait for one sampling decision before falling back to awaitingApproval.",
                    },
                },
            ),
            _tool(
                "dsh_wait_turn",
                "Wait for the in-flight DSH turn to finish without sending a new prompt. Use after answering a pending approval with dsh_respond_approval. Same approval handling as dsh_send_message.",
                {
                    "session_id": {"type": "string"},
                    "timeout_s": {"type": "number", "default": DEFAULT_TIMEOUT},
                    "poll_interval_s": {"type": "number", "default": POLL_INTERVAL},
                    "auto_respond_approvals": {
                        "type": "boolean",
                        "default": True,
                    },
                    "approval_timeout_s": {
                        "type": "number",
                        "default": APPROVAL_SAMPLING_TIMEOUT,
                    },
                },
            ),
            _tool(
                "dsh_list_pending_approvals",
                "List still-pending approval requests from the DSH event stream (approvalId, toolName, callId?, reason?, rpcId). Optionally filter by session_id. Pending approvals are replayed on connect, so this is deterministic.",
                {
                    "session_id": {"type": "string"},
                    "scan_timeout_s": {"type": "number", "default": MUX_SCAN_TIMEOUT},
                },
            ),
            _tool(
                "dsh_respond_approval",
                "Answer one pending approval request: outcome is 'allowed-once' (grant one-time) or 'rejected'. Returns the DSH receipt; accepted:true means the answer was consumed and the turn can continue.",
                {
                    "session_id": {"type": "string"},
                    "approval_id": {"type": "string"},
                    "outcome": {
                        "type": "string",
                        "enum": list(APPROVAL_OUTCOMES),
                        "description": "allowed-once grants the single tool call; rejected denies it.",
                    },
                    "rpc_id": {
                        "type": "string",
                        "description": "Optional echo token from dsh_list_pending_approvals / dsh_send_message (rpcId). Resolved automatically when omitted.",
                    },
                },
            ),
            _tool(
                "dsh_get_session_stats",
                "Fetch cached projections (tokenUsage, sessionStats, contextPressure) for a DSH session.",
                {"session_id": {"type": "string"}},
            ),
            _tool(
                "dsh_resume_session",
                "Re-load an existing DSH session and verify its cached state. Subsequent dsh_send_message calls reuse the prompt prefix.",
                {"session_id": {"type": "string"}},
            ),
        ]

    list_tools_decorator = server.list_tools()
    call_tool_decorator = server.call_tool()

    @list_tools_decorator
    async def _list_tools():
        return _list_tool_defs()

    deps = HandlerDeps(server=server)
    handlers = {
        "dsh_list_workspaces": list_workspaces,
        "dsh_create_session": create_session,
        "dsh_send_message": send_message,
        "dsh_wait_turn": wait_turn,
        "dsh_list_pending_approvals": list_pending_approvals,
        "dsh_respond_approval": respond_approval,
        "dsh_get_session_stats": get_session_stats,
        "dsh_resume_session": resume_session,
    }

    @call_tool_decorator
    async def _call_tool(name, arguments):
        arguments = arguments or {}
        handler = handlers.get(name)
        if handler is None:
            return _fail("unknown tool: " + name)
        try:
            result = await handler(client, arguments, deps)
        except DshConnectionError as exc:
            return _fail(str(exc))
        except DshRpcError as exc:
            return _fail("dsh returned " + exc.code + ": " + exc.message, endpoint=exc.endpoint)
        except (ValueError, KeyError, TypeError) as exc:
            return _fail("invalid arguments: " + str(exc))
        except Exception as exc:
            log.exception("tool %s raised", name)
            return _fail("unexpected error: " + str(exc))
        return _ok(result)

    return server



def _configure_logging():
    level = os.environ.get("DSH_MCP_LOG", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="dsh-mcp", description="MCP server wrapping DSH web cordis RPC.")
    parser.add_argument("--base-url", default=os.environ.get("DSH_BASE_URL", "http://127.0.0.1:3080"),
                        help="DSH web base URL (default: $DSH_BASE_URL or http://127.0.0.1:3080)")
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("DSH_TIMEOUT_S", "60")),
                        help="Per-request timeout in seconds (default: 60).")
    parser.add_argument("--check", action="store_true",
                        help="Probe DSH web and exit (used by probe.py).")
    return parser.parse_args(argv)


async def _amain(args):
    client = DshClient(args.base_url, timeout_s=args.timeout)
    if args.check:
        try:
            await client.start()
            value = await client.call("workspace.list", {})
            print(json.dumps({"ok": True, "value": value}, indent=2, ensure_ascii=False, default=str))
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            return 1
        finally:
            await client.close()
        return 0
    await client.start()
    server = build_server(client)
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="dsh-mcp",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
                instructions=SERVER_INSTRUCTIONS,
            ),
        )
    return 0


def main(argv=None):
    _configure_logging()
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return anyio.run(_amain, args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
