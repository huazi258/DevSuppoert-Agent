"""Bounded in-memory store for the same structured events sent to stdout."""

from __future__ import annotations

import re
from collections import deque
from datetime import datetime
from threading import Lock
from typing import Any

MAX_LOG_EVENTS = 200


class LogBuffer:
    """Keep a bounded, thread-safe history of structured service log events."""

    def __init__(self, capacity: int = MAX_LOG_EVENTS) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = Lock()

    def append(self, event: dict[str, Any]) -> None:
        """Record exactly the event serialized by the JSON stdout formatter."""
        with self._lock:
            self._events.append(dict(event))

    def clear(self) -> None:
        """Clear local history for isolated Fault Lab tests."""
        with self._lock:
            self._events.clear()

    def query(
        self,
        *,
        time_range_start: datetime,
        time_range_end: datetime,
        level: str | None,
        query: str | None,
        limit: int,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Return newest bounded matches and the total count before applying ``limit``."""
        normalized_level = level.lower() if level else None
        normalized_query = query.lower() if query else None
        with self._lock:
            matching_events = [
                event
                for event in self._events
                if _matches(
                    event,
                    time_range_start=time_range_start,
                    time_range_end=time_range_end,
                    level=normalized_level,
                    query=normalized_query,
                )
            ]
        return len(matching_events), matching_events[-limit:]


def _matches(
    event: dict[str, Any],
    *,
    time_range_start: datetime,
    time_range_end: datetime,
    level: str | None,
    query: str | None,
) -> bool:
    timestamp = datetime.fromisoformat(str(event["timestamp"]))
    if not time_range_start <= timestamp <= time_range_end:
        return False
    if level is not None and event.get("level") != level:
        return False
    if query is None:
        return True
    searchable_event = " ".join(
        str(value) for value in event.values() if value is not None
    ).lower()
    return any(term in searchable_event for term in _query_terms(query))


def _query_terms(query: str) -> tuple[str, ...]:
    """Split the Fault Lab's small, case-insensitive OR query syntax."""
    terms = tuple(term.strip() for term in re.split(r"\s+or\s+", query) if term.strip())
    return terms or (query,)
