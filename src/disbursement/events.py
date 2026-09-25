"""只增事件存储：所有结论以追加事实落账，不提供改写。

- event_id 全局唯一，重复提交即重放；
- ``try_append`` 在 event_id 已存在时返回原记录，用于幂等资金动作；
- seq 为全局顺序，供检查点恢复与审计按序回放。
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..culture_finance_progress import EVENT_KINDS


class DuplicateEventError(ValueError):
    """event_id 已存在：同一事实被重复提交。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventStore:
    """追加式事件日志。"""

    def __init__(self) -> None:
        self._events: list[dict] = []
        self._by_id: dict[str, dict] = {}

    def append(
        self,
        kind: str,
        subject_id: str,
        payload: dict,
        *,
        event_id: str | None = None,
        occurred_at: str | None = None,
    ) -> dict:
        if kind not in EVENT_KINDS:
            raise ValueError(f"未知事件种类: {kind}")
        event_id = event_id or f"evt-{len(self._events) + 1:06d}"
        if event_id in self._by_id:
            raise DuplicateEventError(f"事件已存在: {event_id}")
        record = {
            "seq": len(self._events) + 1,
            "event_id": event_id,
            "kind": kind,
            "occurred_at": occurred_at or _utc_now(),
            "subject_id": subject_id,
            "payload": dict(payload),
        }
        self._events.append(record)
        self._by_id[event_id] = record
        return record

    def try_append(
        self,
        kind: str,
        subject_id: str,
        payload: dict,
        *,
        event_id: str,
        occurred_at: str | None = None,
    ) -> tuple[dict, bool]:
        """幂等追加：event_id 已存在时返回 ``(原记录, False)``。"""
        existing = self._by_id.get(event_id)
        if existing is not None:
            if existing["kind"] != kind:
                raise DuplicateEventError(
                    f"{event_id} 已以 {existing['kind']} 存在，不能改写为 {kind}"
                )
            return existing, False
        return self.append(kind, subject_id, payload, event_id=event_id, occurred_at=occurred_at), True

    def get(self, event_id: str) -> dict | None:
        return self._by_id.get(event_id)

    def all(self) -> list[dict]:
        return list(self._events)

    def find(self, kind: str | None = None, subject_id: str | None = None) -> list[dict]:
        return [
            event
            for event in self._events
            if (kind is None or event["kind"] == kind)
            and (subject_id is None or event["subject_id"] == subject_id)
        ]
