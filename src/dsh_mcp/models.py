"""Pydantic models matching DSH wire types."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceView(BaseModel):
    model_config = ConfigDict(extra="allow")

    workspaceId: str
    path: str
    title: str = ""
    sessionIds: list[str] = Field(default_factory=list)
    createdAt: Optional[str] = None
    updatedAt: Optional[str] = None


class WorkspaceListValue(BaseModel):
    model_config = ConfigDict(extra="allow")
    items: list[WorkspaceView] = Field(default_factory=list)
    archivedSessionIds: list[str] = Field(default_factory=list)


class SessionCreateValue(BaseModel):
    model_config = ConfigDict(extra="allow")
    sessionId: str
    agentPreset: Optional[str] = None


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="allow")
    provider: str
    model: str
    reasoningEffort: Optional[str] = None


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="allow")
    uncachedInputTokens: int = 0
    outputTokens: int = 0
    cacheReadTokens: int = 0
    cacheWriteTokens: int = 0


class SessionStats(BaseModel):
    model_config = ConfigDict(extra="allow")
    turns: int = 0
    steps: int = 0
    llmMs: int = 0
    toolMs: int = 0
    ttftMs: int = 0
    ttftSteps: int = 0
    decodeMs: int = 0
    decodeTokens: int = 0


class SessionProjectionsValues(BaseModel):
    """Inner `values` block of the projections projection.

    DSH keeps a denormalised key/value bag; we surface the well-known keys
    (`tokenUsage`, `sessionStats`, `title`, `contextPressure`) and pass the
    rest through `extra` so future additions land unmodified.
    """

    model_config = ConfigDict(extra="allow")

    sessionStats: SessionStats = Field(default_factory=SessionStats)
    tokenUsage: TokenUsage = Field(default_factory=TokenUsage)
    title: Optional[str] = None
    contextPressure: dict[str, Any] = Field(default_factory=dict)


class SessionProjections(BaseModel):
    model_config = ConfigDict(extra="allow")
    asOfSeq: Optional[int] = None
    values: SessionProjectionsValues = Field(default_factory=SessionProjectionsValues)

    @property
    def token_usage(self) -> TokenUsage:
        return self.values.tokenUsage

    @property
    def session_stats(self) -> SessionStats:
        return self.values.sessionStats

    @property
    def title(self) -> Optional[str]:
        return self.values.title


class HistoryEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    event: dict[str, Any]


class SessionHistoryValue(BaseModel):
    model_config = ConfigDict(extra="allow")
    events: list[HistoryEntry] = Field(default_factory=list)
    hasMore: bool = False
    projections: Optional[SessionProjections] = None
