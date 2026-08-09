"""Shared data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Job:
    uid: str
    title: str
    url: str
    source_id: str
    source_name: str
    ebene: str
    adapter: str
    location: str | None = None
    posted_at: str | None = None
    deadline: str | None = None
    land: str | None = None
    raw: dict[str, Any] | None = None


@dataclass
class SourceConfig:
    id: str
    name: str
    ebene: str
    type: str
    params: dict[str, Any] = field(default_factory=dict)
    land: str | None = None
    enabled: bool = True
    notes: str | None = None
