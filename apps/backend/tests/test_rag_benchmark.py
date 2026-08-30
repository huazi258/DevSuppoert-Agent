from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from devsupport_backend.rag import benchmark
from devsupport_backend.rag.benchmark import (
    BenchmarkCase,
    RetrievalBenchmarkError,
    document_ranking,
    ensure_corpus_fresh,
    load_suite,
    run,
)
from devsupport_backend.rag.markdown import ParsedKnowledgeDocument
from devsupport_backend.rag.retrieval import Citation, RetrievalResult


@dataclass
class StubSession:
    documents: list[SimpleNamespace]

    def scalars(self, _statement: object) -> list[SimpleNamespace]:
        return self.documents


@dataclass
class StubRAGService:
    results: list[RetrievalResult]

    def search(self, *_args: object) -> list[RetrievalResult]:
        return self.results


def _document(
    document_id: str,
    *,
    source_path: str = "knowledge/test.md",
    content_hash: str = "a" * 64,
    service: str = "order-service",
    environment: str = "local",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        source_path=source_path,
        content_hash=content_hash,
        service=service,
        environment=environment,
        metadata_data={"document_id": document_id},
    )


def _parsed(document: SimpleNamespace) -> ParsedKnowledgeDocument:
    return ParsedKnowledgeDocument(
        source_path=document.source_path,
        title="Test",
        metadata={
            "document_id": document.metadata_data["document_id"],
            "service": document.service,
            "environment": document.environment,
            "document_type": "runbook",
            "source": "test-source",
        },
        content="Test",
        content_hash=document.content_hash,
    )


def _result(document_id: UUID, *, chunk_id: UUID | None = None) -> RetrievalResult:
    chunk_id = chunk_id or uuid4()
    citation = Citation(
        id=f"knowledge:{document_id}:{chunk_id}",
        document_id=document_id,
        chunk_id=chunk_id,
        source="test-source",
        section="Test",
        document_reference="test",
    )
    return RetrievalResult(
        chunk_id=chunk_id,
        document_id=document_id,
        content="test",
        service="order-service",
        environment="local",
        document_type="runbook",
        source="test-source",
        section="Test",
        vector_score=0.9,
        keyword_score=None,
        fusion_score=0.1,
        citation=citation,
    )


def test_load_suite_keeps_stable_and_diagnostic_document_ids(tmp_path: Path) -> None:
    suite = tmp_path / "suite.yaml"
    suite.write_text(
        """cases:
  - id: stable-case
    query: test query
    service: order-service
    environment: local
    top_k: 5
    required_document_id: rb-order-service-500-triage
    diagnostic_relevant_document_ids: [rb-post-deployment-anomaly]
""",
        encoding="utf-8",
    )

    assert load_suite(suite) == [
        BenchmarkCase(
            id="stable-case",
            query="test query",
            service="order-service",
            environment="local",
            top_k=5,
            required_document_id="rb-order-service-500-triage",
            diagnostic_relevant_document_ids=("rb-post-deployment-anomaly",),
        )
    ]


def test_load_suite_rejects_empty_suite(tmp_path: Path) -> None:
    suite = tmp_path / "suite.yaml"
    suite.write_text("cases: []\n", encoding="utf-8")

    with pytest.raises(RetrievalBenchmarkError, match="at least one case"):
        load_suite(suite)


def test_run_rejects_empty_suite() -> None:
    with pytest.raises(RetrievalBenchmarkError, match="at least one case"):
        run(StubSession([]), StubRAGService([]), [])  # type: ignore[arg-type]


def test_document_ranking_deduplicates_chunks_and_keeps_first_document_rank() -> None:
    first_document = uuid4()
    second_document = uuid4()

    ranking = document_ranking(
        [_result(first_document), _result(first_document), _result(second_document)],
        {first_document: "first", second_document: "second"},
    )

    assert [item["document_id"] for item in ranking] == ["first", "second"]
    assert [item["rank"] for item in ranking] == [1, 2]


def test_run_reports_reciprocal_rank_for_required_hit_and_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hit_document = _document("required", source_path="knowledge/hit.md")
    other_document = _document("other", source_path="knowledge/other.md")
    diagnostic_document = _document(
        "platform-runbook",
        source_path="knowledge/platform.md",
        service="platform",
        environment="common",
    )
    session = StubSession([hit_document, other_document, diagnostic_document])
    monkeypatch.setattr(
        benchmark,
        "collect_documents",
        lambda _path: [
            _parsed(hit_document),
            _parsed(other_document),
            _parsed(diagnostic_document),
        ],
    )

    result = run(
        session,
        StubRAGService([_result(hit_document.id)]),  # type: ignore[arg-type]
        [
            BenchmarkCase(
                "hit",
                "query",
                "order-service",
                "local",
                5,
                "required",
                ("platform-runbook",),
            ),
            BenchmarkCase("miss", "query", "order-service", "local", 5, "missing"),
        ],
    )

    assert [case["reciprocal_rank"] for case in result["cases"]] == [1.0, 0]
    assert result["aggregate"] == {"passed": 1, "total": 2, "hit_at_k": 0.5, "mrr": 0.5}
    assert result["cases"][0]["diagnostic_documents"] == [
        {
            "document_id": "platform-runbook",
            "eligible": False,
            "reasons": ["service_mismatch"],
            "requested_service": "order-service",
            "document_service": "platform",
            "requested_environment": "local",
            "document_environment": "common",
        }
    ]


@pytest.mark.parametrize(
    ("documents", "parsed", "match"),
    [
        ([], [_parsed(_document("missing"))], "missing="),
        ([_document("extra")], [], "extra="),
        (
            [_document("drift", content_hash="a" * 64)],
            [_parsed(_document("drift", content_hash="b" * 64))],
            "drift=",
        ),
    ],
)
def test_corpus_freshness_fails_closed_for_missing_extra_and_hash_drift(
    monkeypatch: pytest.MonkeyPatch,
    documents: list[SimpleNamespace],
    parsed: list[ParsedKnowledgeDocument],
    match: str,
) -> None:
    monkeypatch.setattr(benchmark, "collect_documents", lambda _path: parsed)

    with pytest.raises(RetrievalBenchmarkError, match=match):
        ensure_corpus_fresh(StubSession(documents))  # type: ignore[arg-type]


def test_corpus_freshness_accepts_matching_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    document = _document("fresh")
    monkeypatch.setattr(benchmark, "collect_documents", lambda _path: [_parsed(document)])

    inventory = ensure_corpus_fresh(StubSession([document]))  # type: ignore[arg-type]

    assert inventory[0]["document_id"] == "fresh"


def test_run_rejects_persisted_document_without_stable_id(monkeypatch: pytest.MonkeyPatch) -> None:
    document = _document("ignored")
    document.metadata_data = {}
    monkeypatch.setattr(
        benchmark,
        "collect_documents",
        lambda _path: [
            ParsedKnowledgeDocument(
                source_path=document.source_path,
                title="Test",
                metadata={
                    "document_id": "source-document",
                    "service": document.service,
                    "environment": document.environment,
                    "document_type": "runbook",
                    "source": "test-source",
                },
                content="Test",
                content_hash=document.content_hash,
            )
        ],
    )

    with pytest.raises(RetrievalBenchmarkError, match="stable metadata_data"):
        run(
            StubSession([document]),  # type: ignore[arg-type]
            StubRAGService([]),  # type: ignore[arg-type]
            [BenchmarkCase("case", "query", "order-service", "local", 5, "source-document")],
        )
