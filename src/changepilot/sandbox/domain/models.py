from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SandboxBoundaryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ServiceVersion(StrEnum):
    V1 = "v1"
    V2 = "v2"


class SchemaVersion(StrEnum):
    V1 = "v1"
    V2 = "v2"


class SandboxState(SandboxBoundaryModel):
    sandbox_id: str
    root: str
    service_version: ServiceVersion
    schema_version: SchemaVersion
    migration_ids: tuple[str, ...] = ()
    database_fingerprint: str

    @field_validator("sandbox_id", "root", "database_fingerprint")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must be non-empty")
        return value


class OrderRecord(SandboxBoundaryModel):
    order_id: str
    customer: str
    total_cents: int = Field(ge=0)
    priority: str | None = None


class ServiceHealth(SandboxBoundaryModel):
    healthy: bool
    service_version: ServiceVersion
    schema_version: SchemaVersion
    detail: str


class MigrationResult(SandboxBoundaryModel):
    migration_id: str
    from_version: SchemaVersion
    to_version: SchemaVersion
    database_fingerprint: str
    already_applied: bool
