"""Markdown parsing and semantic chunking for the knowledge corpus."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

REQUIRED_METADATA = frozenset({"document_id", "service", "environment", "document_type", "source"})
FRONT_MATTER_DELIMITER = "---"
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


class KnowledgeDocumentParseError(ValueError):
    """Raised when a knowledge Markdown file cannot be safely ingested."""


@dataclass(frozen=True)
class ParsedKnowledgeDocument:
    """Validated document content and front matter before persistence."""

    source_path: str
    title: str
    metadata: dict[str, str]
    content: str
    content_hash: str


@dataclass(frozen=True)
class Chunk:
    """A heading and paragraph-aligned unit ready for embedding."""

    index: int
    content: str
    section: str


def parse_markdown(path: Path, knowledge_root: Path) -> ParsedKnowledgeDocument:
    """Read a Markdown document, parse its front matter, and validate metadata."""
    try:
        raw_content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise KnowledgeDocumentParseError(f"{path}: cannot read document: {error}") from error

    front_matter, content = _split_front_matter(path, raw_content)
    metadata = _parse_metadata(path, front_matter)
    title = _find_title(path, content)
    try:
        relative_path = path.relative_to(knowledge_root)
    except ValueError as error:
        raise KnowledgeDocumentParseError(
            f"{path}: is outside knowledge root {knowledge_root}"
        ) from error

    return ParsedKnowledgeDocument(
        source_path=f"{knowledge_root.name}/{relative_path.as_posix()}",
        title=title,
        metadata=metadata,
        content=content.strip(),
        content_hash=hashlib.sha256(raw_content.encode("utf-8")).hexdigest(),
    )


def chunk_markdown(
    document: ParsedKnowledgeDocument, *, max_chunk_chars: int = 1_200
) -> list[Chunk]:
    """Split content at headings and paragraph boundaries without character cutting."""
    sections = _sections(document.content)
    chunks: list[Chunk] = []
    for section_title, section_content in sections:
        for content in _group_paragraphs(section_content, max_chunk_chars=max_chunk_chars):
            chunks.append(Chunk(index=len(chunks), content=content, section=section_title))
    if not chunks:
        raise KnowledgeDocumentParseError(f"{document.source_path}: contains no chunkable content")
    return chunks


def _split_front_matter(path: Path, raw_content: str) -> tuple[str, str]:
    lines = raw_content.splitlines()
    if not lines or lines[0].strip() != FRONT_MATTER_DELIMITER:
        raise KnowledgeDocumentParseError(f"{path}: YAML front matter must start with ---")
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == FRONT_MATTER_DELIMITER:
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    raise KnowledgeDocumentParseError(f"{path}: YAML front matter is missing its closing ---")


def _parse_metadata(path: Path, front_matter: str) -> dict[str, str]:
    try:
        raw_metadata = yaml.safe_load(front_matter)
    except yaml.YAMLError as error:
        raise KnowledgeDocumentParseError(f"{path}: invalid YAML front matter: {error}") from error
    if not isinstance(raw_metadata, dict):
        raise KnowledgeDocumentParseError(f"{path}: YAML front matter must be a mapping")

    metadata = {str(key): value for key, value in raw_metadata.items()}
    missing = sorted(REQUIRED_METADATA - metadata.keys())
    if missing:
        raise KnowledgeDocumentParseError(
            f"{path}: missing required metadata: {', '.join(missing)}"
        )

    normalized: dict[str, str] = {}
    for key, value in metadata.items():
        if not isinstance(value, str) or not value.strip():
            raise KnowledgeDocumentParseError(
                f"{path}: metadata {key!r} must be a non-empty string"
            )
        normalized[key] = value.strip()
    return normalized


def _find_title(path: Path, content: str) -> str:
    for line in content.splitlines():
        match = HEADING_PATTERN.match(line)
        if match and len(match.group(1)) == 1:
            return match.group(2)
    raise KnowledgeDocumentParseError(f"{path}: document must contain one level-one Markdown title")


def _sections(content: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, list[str]]] = []
    current_title: str | None = None
    current_lines: list[str] = []

    def append_current() -> None:
        has_body = any(line.strip() for line in current_lines[1:])
        if current_title is not None and has_body:
            sections.append((current_title, current_lines.copy()))

    for line in content.splitlines():
        match = HEADING_PATTERN.match(line)
        if match:
            append_current()
            current_title = match.group(2)
            current_lines = [line]
        elif current_title is not None:
            current_lines.append(line)
    append_current()
    return [(title, "\n".join(lines).strip()) for title, lines in sections]


def _group_paragraphs(section_content: str, *, max_chunk_chars: int) -> list[str]:
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", section_content)
        if paragraph.strip()
    ]
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for paragraph in paragraphs:
        paragraph_length = len(paragraph)
        if current and current_length + paragraph_length + 2 > max_chunk_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_length = 0
        current.append(paragraph)
        current_length += paragraph_length + (2 if current_length else 0)
    if current:
        chunks.append("\n\n".join(current))
    return chunks
