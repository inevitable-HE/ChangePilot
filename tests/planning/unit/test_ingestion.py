from __future__ import annotations

import pytest

from changepilot.planning.application.ingestion import (
    InMemoryKnowledgeStore,
    RunbookIngestionService,
)
from changepilot.planning.domain.failures import KnowledgeError
from changepilot.planning.domain.knowledge import RunbookDocument
from changepilot.planning.domain.models import TrustLevel


def _runbook(
    *,
    version: str = "1",
    content: str = "# Migration\nRun precheck.\n\n# Rollback\nRestore schema.",
) -> RunbookDocument:
    return RunbookDocument(
        document_id="schema-migration",
        version=version,
        source="runbooks/schema-migration.md",
        trust_level=TrustLevel.AUTHORITATIVE,
        content=content,
        policy_key="schema-migration",
        effective_at="2026-07-24",
    )


def test_identical_document_reuses_existing_chunks() -> None:
    service = RunbookIngestionService(InMemoryKnowledgeStore())

    first = service.ingest(_runbook())
    second = service.ingest(_runbook())

    assert first.created_chunks == 2
    assert second.created_chunks == 0
    assert second.reused is True
    assert second.snapshot.digest == first.snapshot.digest


def test_changed_content_creates_new_knowledge_snapshot() -> None:
    service = RunbookIngestionService(InMemoryKnowledgeStore())
    old = service.ingest(_runbook(version="1"))
    new = service.ingest(
        _runbook(version="2", content="# Rollback\nUse the new restore tool.")
    )

    assert new.snapshot.digest != old.snapshot.digest
    assert new.snapshot.document_count == 2


def test_same_identity_cannot_be_silently_rewritten() -> None:
    service = RunbookIngestionService(InMemoryKnowledgeStore())
    service.ingest(_runbook())

    with pytest.raises(KnowledgeError, match="immutable"):
        service.ingest(_runbook(content="# Migration\nDifferent content."))


def test_chunk_ids_and_locations_are_stable() -> None:
    store = InMemoryKnowledgeStore()
    service = RunbookIngestionService(
        store,
        max_chunk_chars=24,
        overlap_chars=4,
    )
    service.ingest(_runbook())
    first = store.all_chunks()

    second_store = InMemoryKnowledgeStore()
    RunbookIngestionService(
        second_store,
        max_chunk_chars=24,
        overlap_chars=4,
    ).ingest(_runbook())

    assert [chunk.chunk_id for chunk in first] == [
        chunk.chunk_id for chunk in second_store.all_chunks()
    ]
    assert all(chunk.end_offset > chunk.start_offset for chunk in first)
    assert first[0].heading_path == ("Migration",)
