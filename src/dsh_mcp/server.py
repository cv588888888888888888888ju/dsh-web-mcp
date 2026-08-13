"""MCP server entry point for DSH cordis RPC."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

import anyio
import mcp.server.stdio
import mcp.types as types
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions

from .client import DshClient, DshConnectionError, DshRpcError
from .models import (
    SessionCreateValue,
    SessionHistoryValue,
    WorkspaceListValue,
)


log = logging.getLogger("dsh-mcp.server")


DEFAULT_TIMEOUT = 120.0
POLL_INTERVAL = 0.75


def _ok(value):
    text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    return [types.TextContent(type="text", text=text)]


def _fail(message, **extra):
    payload = {"ok": False, "error": message, **extra}
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False))]


async def list_workspaces(client, args):
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


async def create_session(client, args):
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


async def send_message(client, args):
    session_id = args.get("session_id")
    prompt = args.get("prompt")
    if not session_id or not isinstance(session_id, str):
        raise ValueError("session_id (string) is required")
    if not prompt or not isinstance(prompt, str):
        raise ValueError("prompt (string) is required")
    timeout_s = float(args.get("timeout_s") or DEFAULT_TIMEOUT)
    interval = float(args.get("poll_interval_s") or POLL_INTERVAL)
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
    deadline = time.monotonic() + timeout_s
    final_history = None
    last_turn = 0
    while time.monotonic() < deadline:
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
        return {
            "ok": False,
            "error": "timeout after " + str(timeout_s) + "s waiting for turn completion",
            "sessionId": session_id,
        }
    return _assemble_send_result(final_history, max_seq, session_id, last_turn)


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


async def get_session_stats(client, args):
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


async def resume_session(client, args):
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
- dsh_send_message(session_id, prompt, timeout_s?, poll_interval_s?): send a prompt and
  block until the turn completes. Returns the assistant text plus token usage
  (incl. cacheReadTokens / cacheWriteTokens - the metric DSH prefix caching makes meaningful).
- dsh_get_session_stats(session_id): fetch the cached projections (tokenUsage, sessionStats,
  contextPressure) for an existing session.
- dsh_resume_session(session_id): verify a session is resumable and surface its current
  model + last cached state so subsequent prompts reuse the prefix cache.

Wire: DSH web runs locally on http://127.0.0.1:3080 (override with DSH_BASE_URL).
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
                "Send a prompt to a DSH session and block until the assistant turn completes. Result includes assistant text and token usage (cacheReadTokens / cacheWriteTokens).",
                {
                    "session_id": {"type": "string"},
                    "prompt": {"type": "string"},
                    "timeout_s": {"type": "number", "default": DEFAULT_TIMEOUT},
                    "poll_interval_s": {"type": "number", "default": POLL_INTERVAL},
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

    handlers = {
        "dsh_list_workspaces": list_workspaces,
        "dsh_create_session": create_session,
        "dsh_send_message": send_message,
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
            result = await handler(client, arguments)
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
