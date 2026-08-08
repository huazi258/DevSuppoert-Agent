"""Strict format boundary for JSON returned by structured LLM nodes."""

from __future__ import annotations

import json

from pydantic import JsonValue


class StructuredOutputParseError(ValueError):
    """Raised when LLM output is not one complete accepted JSON representation."""


def parse_structured_json(raw_output: str) -> JsonValue:
    """Parse bare JSON or one complete ``json`` fence without extracting surrounding text."""
    normalized = raw_output.strip()
    if not normalized:
        raise StructuredOutputParseError("structured output must not be blank")
    if normalized.startswith("```") or normalized.endswith("```"):
        return _parse_json_fence(normalized)
    return _decode_json(normalized)


def _parse_json_fence(normalized: str) -> JsonValue:
    """Accept exactly one full response fence explicitly labelled as JSON."""
    if normalized.count("```") != 2:
        raise StructuredOutputParseError("structured output must contain one complete JSON fence")
    opening_line, separator, fenced_content = normalized.partition("\n")
    if not separator or opening_line[3:].strip() != "json" or not normalized.endswith("```"):
        raise StructuredOutputParseError("structured output fence must be a complete json fence")
    return _decode_json(fenced_content[:-3].strip())


def _decode_json(raw_json: str) -> JsonValue:
    """Wrap decoding failures without attempting repairs or object extraction."""
    try:
        return json.loads(raw_json)
    except json.JSONDecodeError as error:
        raise StructuredOutputParseError("structured output is not valid JSON") from error
