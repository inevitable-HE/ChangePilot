from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from changepilot.planning.domain.failures import KnowledgeError
from changepilot.planning.domain.knowledge import (
    IngestionResult,
    KnowledgeChunk,
    KnowledgeSnapshot,
    RunbookDocument,
)
from changepilot.planning.ports.knowledge import KnowledgeStore


_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(slots=True)
class InMemoryKnowledgeStore:
    _documents: dict[tuple[str, str], RunbookDocument] = field(default_factory=dict)
    _chunks: dict[str, KnowledgeChunk] = field(default_factory=dict)

    def get_document(
        self,
        document_id: str,
        version: str,
    ) -> RunbookDocument | None:
        return self._documents.get((document_id, version))

    def add_document(
        self,
        document: RunbookDocument,
        chunks: tuple[KnowledgeChunk, ...],
    ) -> None:
        identity = (document.document_id, document.version)
        if identity in self._documents:
            raise KnowledgeError("document version already exists")
        self._documents[identity] = document
        self._chunks.update({chunk.chunk_id: chunk for chunk in chunks})

    def all_documents(self) -> tuple[RunbookDocument, ...]:
        return tuple(
            self._documents[key]
            for key in sorted(self._documents)
        )

    def all_chunks(self) -> tuple[KnowledgeChunk, ...]:
        return tuple(
            sorted(
                self._chunks.values(),
                key=lambda chunk: (
                    chunk.document_id,
                    chunk.document_version,
                    chunk.position,
                    chunk.chunk_id,
                ),
            )
        )

    def snapshot(self) -> KnowledgeSnapshot:
        return calculate_snapshot(self.all_documents(), self.all_chunks())


class RunbookIngestionService:
    def __init__(
        self,
        store: KnowledgeStore,
        *,
        max_chunk_chars: int = 800,
        overlap_chars: int = 120,
    ) -> None:
        if max_chunk_chars <= 0:
            raise ValueError("max_chunk_chars must be positive")
        if not 0 <= overlap_chars < max_chunk_chars:
            raise ValueError("overlap_chars must be within chunk size")
        self._store = store
        self._max_chunk_chars = max_chunk_chars
        self._overlap_chars = overlap_chars

    def ingest(self, document: RunbookDocument) -> IngestionResult:
        existing = self._store.get_document(
            document.document_id,
            document.version,
        )
        if existing is not None:
            if existing.content_digest != document.content_digest:
                raise KnowledgeError(
                    "document identity is immutable; use a new version"
                )
            return IngestionResult(
                document_id=document.document_id,
                document_version=document.version,
                content_digest=document.content_digest,
                snapshot=self._store.snapshot(),
                created_chunks=0,
                reused=True,
            )

        chunks = self._chunk(document)
        self._store.add_document(document, chunks)
        return IngestionResult(
            document_id=document.document_id,
            document_version=document.version,
            content_digest=document.content_digest,
            snapshot=self._store.snapshot(),
            created_chunks=len(chunks),
            reused=False,
        )

    def _chunk(
        self,
        document: RunbookDocument,
    ) -> tuple[KnowledgeChunk, ...]:
        normalized = document.content.replace("\r\n", "\n").replace("\r", "\n")
        sections = _markdown_sections(normalized)
        chunks: list[KnowledgeChunk] = []
        position = 0
        for heading_path, section_start, section_text in sections:
            cursor = 0
            while cursor < len(section_text):
                end = min(len(section_text), cursor + self._max_chunk_chars)
                content = section_text[cursor:end].strip()
                if content:
                    content_digest = hashlib.sha256(
                        content.encode("utf-8")
                    ).hexdigest()
                    absolute_start = section_start + cursor
                    absolute_end = section_start + end
                    chunk_id = hashlib.sha256(
                        (
                            f"{document.document_id}:{document.version}:"
                            f"{position}:{content_digest}"
                        ).encode("utf-8")
                    ).hexdigest()
                    chunks.append(
                        KnowledgeChunk(
                            chunk_id=chunk_id,
                            document_id=document.document_id,
                            document_version=document.version,
                            source=document.source,
                            trust_level=document.trust_level,
                            policy_key=document.policy_key,
                            rule_digest=(
                                document.rule_digest or document.content_digest
                            ),
                            status=document.status,
                            heading_path=heading_path,
                            position=position,
                            start_offset=absolute_start,
                            end_offset=absolute_end,
                            content=content,
                            content_digest=content_digest,
                        )
                    )
                    position += 1
                if end == len(section_text):
                    break
                cursor = end - self._overlap_chars
        return tuple(chunks)


def load_runbook(path: str | Path) -> RunbookDocument:
    selected = Path(path)
    text = selected.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(text)
    if match is None:
        raise KnowledgeError("Runbook requires YAML frontmatter")
    metadata = yaml.safe_load(match.group(1))
    if not isinstance(metadata, dict):
        raise KnowledgeError("Runbook frontmatter must be a mapping")
    payload = {
        **metadata,
        "source": metadata.get("source", selected.as_posix()),
        "content": text[match.end() :].strip(),
    }
    try:
        return RunbookDocument.model_validate_json(
            json.dumps(payload, ensure_ascii=False)
        )
    except Exception as exc:
        raise KnowledgeError(f"invalid Runbook metadata: {exc}") from exc


def calculate_snapshot(
    documents: tuple[RunbookDocument, ...],
    chunks: tuple[KnowledgeChunk, ...],
) -> KnowledgeSnapshot:
    payload = {
        "documents": [
            {
                "document_id": document.document_id,
                "version": document.version,
                "content_digest": document.content_digest,
                "status": document.status.value,
            }
            for document in documents
        ],
        "chunks": [
            {
                "chunk_id": chunk.chunk_id,
                "content_digest": chunk.content_digest,
            }
            for chunk in chunks
        ],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return KnowledgeSnapshot(
        digest=hashlib.sha256(encoded).hexdigest(),
        document_count=len(documents),
        chunk_count=len(chunks),
    )


def _markdown_sections(
    content: str,
) -> tuple[tuple[tuple[str, ...], int, str], ...]:
    headings: list[str] = []
    sections: list[tuple[tuple[str, ...], int, str]] = []
    current_start = 0
    current_lines: list[str] = []
    offset = 0

    def flush() -> None:
        nonlocal current_lines
        text = "\n".join(current_lines).strip()
        if text:
            sections.append((tuple(headings), current_start, text))
        current_lines = []

    for line in content.split("\n"):
        match = _HEADING.match(line)
        if match:
            flush()
            level = len(match.group(1))
            title = match.group(2).strip()
            del headings[level - 1 :]
            while len(headings) < level - 1:
                headings.append("")
            headings.append(title)
            current_start = offset + len(line) + 1
        else:
            if not current_lines:
                current_start = offset
            current_lines.append(line)
        offset += len(line) + 1
    flush()
    if not sections and content.strip():
        return (((), 0, content.strip()),)
    return tuple(sections)
