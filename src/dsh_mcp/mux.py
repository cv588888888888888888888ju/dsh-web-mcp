"""Event-mux reader and pending-approval registry for DSH approval callbacks.

Wire facts (verified against @deepseek-ai/dsh-host-apiproxy +
@deepseek-ai/dsh-client-connection source, and by a live probe):

* The approval event stream is the **WebSocket** endpoint ``GET /api/events.mux``
  (a plain GET returns 426 "upgrade required"; SSE is only the in-process
  fallback carrier). Each WS frame is one server-request full form as JSON:
  ``{"type":"server-request","rpcId":"<uuid>","method":"<frame type>",
    "payload":{...}}``.
* An answerable approval arrives as payload ``{type:"approval/requested",
  sessionId, approvalId, toolName, callId?, reason?}`` whose **rpcId** is the
  echo token a ``POST /api/respond`` client-response must reuse. The rpcId is
  minted by the host's pending table — it is *not* the audit ``approvalId`` —
  and is only reachable on this stream.
* Opening the mux **replays every still-pending approval** (and question) with
  its stable rpcId, so a reconnect never loses an answerable approval; missing
  a window is safe because the entry stays pending until answered.
* Client→server traffic on the downlink is a protocol violation (the server
  closes the socket with 1008), so this module is receive-only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional

import websockets

from .client import DshClient


log = logging.getLogger("dsh-mcp.mux")

MUX_PATH = "/api/events.mux"
MUX_MAX_FRAME_BYTES = 2 ** 26  # session/event frames may carry base64 images


def mux_url(base_url: str) -> str:
    """Translate an http(s) base URL into the matching ws(s) mux endpoint."""
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + MUX_PATH
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):] + MUX_PATH
    raise ValueError(f"cannot derive websocket URL from base_url {base_url!r}")


def _parse_frame(raw: str) -> Optional[dict[str, Any]]:
    """Parse one mux frame; returns ``{rpcId, payload}`` for server-requests."""
    try:
        full = json.loads(raw)
    except Exception:
        return None
    if not isinstance(full, dict) or full.get("type") != "server-request":
        return None
    payload = full.get("payload")
    if not isinstance(payload, dict):
        return None
    return {"rpcId": full.get("rpcId"), "payload": payload}


class ApprovalRegistry:
    """In-memory view of pending approvals, keyed ``"{sessionId}:{approvalId}"``.

    Populated by mux scans / watches; consulted by ``dsh_respond_approval`` to
    reuse the rpcId an approval was first seen with. Entries are removed once
    the respond receipt is accepted (or the caller reports a resolution).
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _key(session_id: str, approval_id: str) -> str:
        return f"{session_id}:{approval_id}"

    def put(self, approval: dict[str, Any]) -> None:
        sid = approval.get("sessionId")
        aid = approval.get("approvalId")
        if sid and aid:
            self._entries[self._key(sid, aid)] = dict(approval)

    def get(self, session_id: str, approval_id: str) -> Optional[dict[str, Any]]:
        return self._entries.get(self._key(session_id, approval_id))

    def pop(self, session_id: str, approval_id: str) -> Optional[dict[str, Any]]:
        return self._entries.pop(self._key(session_id, approval_id), None)

    def pending(self, session_id: Optional[str] = None) -> list[dict[str, Any]]:
        items = [dict(v) for v in self._entries.values()]
        if session_id:
            items = [a for a in items if a.get("sessionId") == session_id]
        return sorted(items, key=lambda a: (a.get("sessionId") or "", a.get("approvalId") or ""))


def _is_approval_requested(frame: dict[str, Any]) -> bool:
    return frame.get("payload", {}).get("type") == "approval/requested"


async def scan_pending_approvals(
    client: DshClient,
    session_id: Optional[str] = None,
    timeout_s: float = 6.0,
) -> list[dict[str, Any]]:
    """Open the mux once and collect still-pending ``approval/requested`` frames.

    The mux replays pending approvals on open, so a short-lived scan is the
    deterministic way to list what is answerable right now (used by
    ``dsh_list_pending_approvals`` and as the rpcId fallback for respond).
    """
    found: list[dict[str, Any]] = []
    url = mux_url(client.base_url)
    deadline = time.monotonic() + timeout_s
    try:
        async with websockets.connect(
            url, open_timeout=5.0, close_timeout=1.0, max_size=MUX_MAX_FRAME_BYTES
        ) as ws:
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                parsed = _parse_frame(raw)
                if parsed is None or not _is_approval_requested(parsed):
                    continue
                payload = parsed["payload"]
                if session_id is not None and payload.get("sessionId") != session_id:
                    continue
                found.append({"rpcId": parsed.get("rpcId"), **payload})
    except Exception as exc:  # noqa: BLE001 - a scan must never break the caller
        log.debug("mux approval scan failed: %s", exc)
    return found


async def watch_mux(
    client: DshClient,
    on_frame: Callable[[str, dict[str, Any]], Awaitable[None]],
    stop: asyncio.Event,
    *,
    reconnect_idle_s: float = 30.0,
    connect_timeout_s: float = 5.0,
    backoff_s: float = 0.5,
) -> None:
    """Long-lived mux reader with automatic reconnect (replay covers gaps).

    Calls ``await on_frame(rpc_id, frame_payload)`` for every server-request
    frame. A read-idle timeout or connection drop closes the socket and
    reconnects — still-pending approvals are replayed verbatim on every open,
    so no answerable approval is ever lost. Returns when ``stop`` is set.
    """
    url = mux_url(client.base_url)
    while not stop.is_set():
        try:
            async with websockets.connect(
                url, open_timeout=connect_timeout_s, close_timeout=1.0,
                max_size=MUX_MAX_FRAME_BYTES,
            ) as ws:
                log.debug("mux connected to %s", url)
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=reconnect_idle_s)
                    except asyncio.TimeoutError:
                        log.debug("mux read idle %.0fs; reconnecting (replay covers pending)",
                                  reconnect_idle_s)
                        break
                    parsed = _parse_frame(raw)
                    if parsed is None:
                        continue
                    try:
                        await on_frame(parsed.get("rpcId") or "", parsed["payload"])
                    except Exception:  # noqa: BLE001 - a bad handler must not kill the stream
                        log.exception("mux on_frame handler failed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reconnect loop
            log.debug("mux connection error: %s", exc)
        if not stop.is_set():
            await asyncio.sleep(backoff_s)
