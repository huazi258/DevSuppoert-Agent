"""Tests for the strict shared LLM structured-output boundary."""

import pytest

from devsupport_backend.agent.structured_output import (
    StructuredOutputParseError,
    parse_structured_json,
)


@pytest.mark.parametrize(
    "raw_output",
    [
        '{"answer": 1}',
        '```json\n{"answer": 1}\n```',
        '  \n ```json\n\n {"answer": 1} \n\n``` \n ',
    ],
)
def test_parse_structured_json_accepts_bare_or_complete_json_fence(raw_output: str) -> None:
    assert parse_structured_json(raw_output) == {"answer": 1}


@pytest.mark.parametrize(
    "raw_output",
    [
        'Here is the result: {"answer": 1}',
        '{"answer": 1}\nThis is the result.',
        '```json\n{"answer": 1}\n```\n```json\n{"answer": 2}\n```',
        '```yaml\nanswer: 1\n```',
        '```json\n{"answer":}\n```',
    ],
)
def test_parse_structured_json_rejects_non_whole_json_forms(raw_output: str) -> None:
    with pytest.raises(StructuredOutputParseError):
        parse_structured_json(raw_output)
