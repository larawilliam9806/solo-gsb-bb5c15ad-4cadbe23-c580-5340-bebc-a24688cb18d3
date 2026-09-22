"""请求 / 响应模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

EventKind = Literal["add", "update", "remove"]


class ProcessCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120, examples=["粒子加速实验"])
    version: str = Field(min_length=1, max_length=64, examples=["1.0.0"])
    note: str = Field(default="", max_length=500)


class EventCreate(BaseModel):
    kind: EventKind
    step_key: str = Field(min_length=1, max_length=120, examples=["inject"])
    title: str = Field(default="", max_length=200)
    data: dict[str, Any] = Field(default_factory=dict)
    ts: str | None = Field(default=None, description="ISO8601 时间戳；缺省取服务器当前 UTC 时间，可乱序/晚到")


class EventRevoke(BaseModel):
    revoked: bool
