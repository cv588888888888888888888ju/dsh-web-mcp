# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-08-14

### Added

- **Approval callback dual-track**: `dsh_send_message` / `dsh_wait_turn` now
  capture `approval/asked` events while polling `session.history`, delegate the
  decision to the MCP client via `sampling/createMessage`, and answer DSH
  through `POST /api/respond` (rpcId obtained from the WebSocket `events.mux`
  replay stream, verified against DSH source + live probe).
- New fallback tools: `dsh_list_pending_approvals` and `dsh_respond_approval`
  (MCP-standard tool polling; works even when the client has no sampling
  handler).
- `mux.py`: WebSocket event-mux reader + pending-approval registry
  (replay-safe — reconnect replays every still-pending approval).
- `client.respond()`: client-response carrier returning an `RpcReceipt`
  (`{accepted: true}` / `{accepted: false, reason}`).
- Models: `ApprovalRequest`, `RespondReceipt`.
- New dependency: `websockets>=12.0` (pure-Python WS client for the mux).
- `probe.py` extended with an end-to-end approval-chain smoke test
  (respond returns `accepted: true`).

### Changed

- `dsh_send_message` return shape stays backward compatible; new optional
  fields (`awaitingApproval`, `pendingApprovals`) are only present when an
  approval is pending.

## [0.1.0] - 2026-08-14

### Added

- Initial release: MCP bridge for [DeepSeek Harness (DSH)](https://github.com/deepseek-ai/deepseek-harness)
  web UI, exposing the cordis RPC API (`/api/<endpoint>`) as stdio MCP tools.
- Five MCP tools: `dsh_list_workspaces`, `dsh_create_session`,
  `dsh_send_message`, `dsh_get_session_stats`, `dsh_resume_session`.
- Prompt-prefix cache reuse: DSH cacheReadTokens = ~100% across turns.
- Reverse-engineered from DSH 0.1.0-rc.6 source (see README for details).
- `probe.py` end-to-end smoke test; MIT license; bilingual README (EN/ZH).
