"""HTTP client for DSH cordis RPC API.

Wire format discovered by reading dsh source:

    POST /api/<namespace>.<method>
    Content-Type: application/json
    Body: {"type":"client-request","rpcId":"<uuid>","method":"<endpoint>","payload":{...}}
    Response: {"type":"server-response","rpcId":"<uuid>",
               "result":{"ok":true,"value":{...}} | {"ok":false,"error":{...}}}

The Host runs at :3080 by default; sessions stream completion events on
WebSocket /api/events.host, but read-side answers are always available via
session.history polling — that is the right synchronisation point.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx


log = logging.getLogger("dsh-mcp.client")


class DshRpcError(RuntimeError):
    """Wire-level RPC failure returned by DSH."""

    def __init__(self, code: str, message: str, endpoint: str):
        super().__init__(f"[{endpoint}] {code}: {message}")
        self.code = code
        self.message = message
        self.endpoint = endpoint


class DshConnectionError(RuntimeError):
    """HTTP/transport-level failure talking to the DSH web service."""

    def __init__(self, message: str, cause: Exception | None = None):
        super().__init__(message)
        self.__cause__ = cause


class DshClient:
    """Async JSON-RPC client targeting a DSH web instance.

    Holds a single httpx.AsyncClient with HTTP/1.1 keep-alive; DSH is local-only,
    so retries are limited and timeouts generous (single tool calls may take
    60+ seconds on a slow LLM round trip).
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:3080",
        *,
        timeout_s: float = 60.0,
        connect_timeout_s: float = 5.0,
        user_agent: str = "dsh-mcp/0.1.0",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_s, connect=connect_timeout_s)
        self._headers = {
            "user-agent": user_agent,
            "content-type": "application/json",
            "accept": "application/json",
        }
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self._timeout,
                headers=self._headers,
                http2=False,
            )
            log.debug("dsh client started at %s", self.base_url)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "DshClient":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise DshConnectionError("client not started; call start() first")
        return self._client

    async def call(self, endpoint: str, payload: dict[str, Any] | None = None) -> Any:
        """Invoke a single RPC endpoint; return the business `value` on success.

        Raises DshRpcError for business-side errors and DshConnectionError for transport-level failures.
        """
        if self._client is None:
            raise DshConnectionError("client not started; call start() first")

        rpc_id = str(uuid.uuid4())
        body = {
            "type": "client-request",
            "rpcId": rpc_id,
            "method": endpoint,
            "payload": payload or {},
        }
        url = f"/api/{endpoint}"

        try:
            resp = await self._client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise DshConnectionError(
                f"DSH web not reachable at {self.base_url}: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise DshConnectionError(
                f"DSH {endpoint} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )

        try:
            envelope = resp.json()
        except Exception as exc:
            raise DshConnectionError(
                f"DSH {endpoint} returned non-JSON response: {exc}"
            ) from exc

        # Guard against rpcId echoing mismatch (would indicate a buggy proxy).
        if isinstance(envelope, dict) and envelope.get("rpcId") != rpc_id:
            log.warning("rpcId mismatch on %s: sent=%s got=%s",
                        endpoint, rpc_id, envelope.get("rpcId"))

        if not isinstance(envelope, dict) or envelope.get("type") != "server-response":
            raise DshConnectionError(
                f"DSH {endpoint} returned unexpected envelope: {str(envelope)[:300]}"
            )

        result = envelope.get("result") or {}
        if result.get("ok"):
            return result.get("value")
        err = result.get("error") or {}
        raise DshRpcError(
            code=str(err.get("code") or "unknown"),
            message=str(err.get("message") or "no message"),
            endpoint=endpoint,
        )

    async def respond(self, message: dict[str, Any]) -> dict[str, Any]:
        """Answer a pending server-request via ``POST /api/respond``.

        ``/api/respond`` is a client-response carrier (not in the rpc-map, no
        new id minted): the body is the full client-response envelope echoing
        the server-request's ``rpcId``, and the HTTP body is an RpcReceipt
        (``{"accepted":true}`` or ``{"accepted":false,"reason":"..."}``) rather
        than a ``server-response`` envelope — so this method does not share the
        ``call()`` envelope parsing.

        Returns the parsed receipt dict. Raises DshConnectionError for
        transport-level failures.
        """
        if self._client is None:
            raise DshConnectionError("client not started; call start() first")
        try:
            resp = await self._client.post("/api/respond", json=message)
        except httpx.HTTPError as exc:
            raise DshConnectionError(
                f"DSH web not reachable at {self.base_url}: {exc}"
            ) from exc
        if resp.status_code != 200:
            raise DshConnectionError(
                f"DSH respond returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            receipt = resp.json()
        except Exception as exc:
            raise DshConnectionError(
                f"DSH respond returned non-JSON response: {exc}"
            ) from exc
        if not isinstance(receipt, dict) or "accepted" not in receipt:
            raise DshConnectionError(
                f"DSH respond returned unexpected receipt: {str(receipt)[:300]}"
            )
        return receipt

