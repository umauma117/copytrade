# -*- coding: utf-8 -*-
"""
运行时开关与事件流水（供网页控制台读写）。
None = 跟随 .env；True/False = 强制覆盖。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

import config

_lock = threading.Lock()
_execute_copy_override: Optional[bool] = None
_copy_sell_override: Optional[bool] = None
_events: Deque[Dict[str, Any]] = deque(maxlen=500)


def effective_execute_copy() -> bool:
    with _lock:
        if _execute_copy_override is not None:
            return _execute_copy_override
    return config.EXECUTE_COPY


def effective_copy_sell() -> bool:
    with _lock:
        if _copy_sell_override is not None:
            return _copy_sell_override
    return config.COPY_SELL_ACTIONS


def set_execute_copy_override(v: Optional[bool]) -> None:
    global _execute_copy_override
    with _lock:
        _execute_copy_override = v


def set_copy_sell_override(v: Optional[bool]) -> None:
    global _copy_sell_override
    with _lock:
        _copy_sell_override = v


def get_overrides() -> Dict[str, Optional[bool]]:
    with _lock:
        return {
            "execute_copy": _execute_copy_override,
            "copy_sell": _copy_sell_override,
        }


def record_event(kind: str, message: str, **extra: Any) -> None:
    with _lock:
        _events.appendleft(
            {
                "ts": time.time(),
                "kind": kind,
                "message": message,
                **{k: v for k, v in extra.items() if v is not None},
            }
        )


def get_events(limit: int = 200) -> List[Dict[str, Any]]:
    from datetime import datetime

    with _lock:
        rows = list(_events)[:limit]
    out = []
    for e in rows:
        d = dict(e)
        d["ts_local"] = datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        out.append(d)
    return out
