from datetime import UTC, datetime, timedelta

from order_service.log_buffer import LogBuffer


def test_log_buffer_discards_oldest_events_at_capacity() -> None:
    buffer = LogBuffer(capacity=2)
    now = datetime.now(UTC)
    for index in range(3):
        buffer.append(
            {
                "timestamp": (now + timedelta(seconds=index)).isoformat(),
                "level": "info",
                "message": str(index),
            }
        )

    match_count, events = buffer.query(
        time_range_start=now - timedelta(seconds=1),
        time_range_end=now + timedelta(seconds=5),
        level=None,
        query=None,
        limit=10,
    )

    assert match_count == 2
    assert [event["message"] for event in events] == ["1", "2"]


def test_log_buffer_matches_case_insensitive_or_query_terms() -> None:
    buffer = LogBuffer()
    now = datetime.now(UTC)
    buffer.append(
        {
            "timestamp": now.isoformat(),
            "level": "error",
            "message": "required runtime configuration is missing",
            "error_type": "MissingRequiredConfiguration",
        }
    )

    match_count, events = buffer.query(
        time_range_start=now - timedelta(seconds=1),
        time_range_end=now + timedelta(seconds=1),
        level="error",
        query="error OR 500 OR payment-service",
        limit=10,
    )

    assert match_count == 1
    assert events[0]["error_type"] == "MissingRequiredConfiguration"
