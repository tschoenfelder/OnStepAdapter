"""Structured public results for the OnStep hardware adapter."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OnStepConnectionResult:
    connected: bool
    mount_connected: bool
    focuser_available: bool
    port: str


@dataclass(frozen=True)
class FocuserStatus:
    available: bool
    position: int
    max_position: int
    moving: bool


@dataclass(frozen=True)
class FocuserMoveResult:
    accepted: bool
    target_position: int
    start_position: int
    onstep_reply: str
