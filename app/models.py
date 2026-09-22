"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

EVENT_TYPES = ("started", "completed", "failed", "updated", "undone")


def parse_time(value: str) -> datetime:
    """解析 ISO 8601 时间；支持 'Z' 后缀，朴素时间按 UTC 处理。"""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FlowCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    version: str = Field(..., min_length=1, max_length=50)
    description: str = ""


class EventCreate(BaseModel):
    step_id: str = Field(..., min_length=1, max_length=200)
    event_type: Literal["started", "completed", "failed", "updated", "undone"]
    event_time: Optional[str] = None          # 缺省为服务端当前时间
    seq: Optional[int] = None                 # 业务序号，用于同时间戳下的排序
    payload: dict[str, Any] = Field(default_factory=dict)
    target_event_id: Optional[int] = None     # event_type=undone 时必填

    @field_validator("event_time")
    @classmethod
    def _check_time(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            parse_time(v)  # 非法格式直接抛 ValueError -> 422
        return v
