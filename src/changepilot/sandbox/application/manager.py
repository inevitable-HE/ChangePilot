from __future__ import annotations

import json
import re
from pathlib import Path

from changepilot.sandbox.adapters.sqlite import SQLiteSandboxDatabase
from changepilot.sandbox.domain.models import (
    SandboxState,
    ServiceVersion,
)


_SANDBOX_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_STATE_FILE = "sandbox.json"
_DATABASE_FILE = "orders.db"


class SandboxBoundaryError(ValueError):
    """Raised when a sandbox identity or resource escapes its boundary."""


class SandboxManager:
    def __init__(
        self,
        base_directory: str | Path,
        *,
        database: SQLiteSandboxDatabase | None = None,
    ) -> None:
        self._base = Path(base_directory).expanduser().resolve()
        self._base.mkdir(parents=True, exist_ok=True)
        self._database = database or SQLiteSandboxDatabase()

    def create(self, sandbox_id: str) -> SandboxState:
        root = self._sandbox_root(sandbox_id)
        if root.exists() and any(root.iterdir()):
            raise FileExistsError(f"sandbox {sandbox_id!r} already exists")
        root.mkdir(parents=True, exist_ok=True)
        database_path = root / _DATABASE_FILE
        self._database.initialize_v1(database_path, sandbox_id=sandbox_id)
        self._write_service_version(root, ServiceVersion.V1)
        return self.inspect(sandbox_id)

    def reset(self, sandbox_id: str) -> SandboxState:
        root = self._sandbox_root(sandbox_id)
        if root.exists():
            state = self.inspect(sandbox_id)
            if state.sandbox_id != sandbox_id:
                raise SandboxBoundaryError("sandbox identity mismatch")
            for filename in (_DATABASE_FILE, _STATE_FILE, "faults.json"):
                selected = root / filename
                if selected.exists():
                    selected.unlink()
        return self.create(sandbox_id)

    def inspect(self, sandbox_id: str) -> SandboxState:
        root = self._sandbox_root(sandbox_id)
        state_path = root / _STATE_FILE
        if not state_path.is_file():
            raise FileNotFoundError(f"sandbox {sandbox_id!r} does not exist")
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        service_version = ServiceVersion(payload["service_version"])
        database_path = root / _DATABASE_FILE
        stored_id = self._database.sandbox_id(database_path)
        if stored_id != sandbox_id or payload.get("sandbox_id") != sandbox_id:
            raise SandboxBoundaryError("sandbox identity mismatch")
        return SandboxState(
            sandbox_id=sandbox_id,
            root=root.as_posix(),
            service_version=service_version,
            schema_version=self._database.schema_version(database_path),
            migration_ids=self._database.migration_ids(database_path),
            database_fingerprint=self._database.fingerprint(database_path),
        )

    def resolve_resource(
        self,
        sandbox_id: str,
        relative_path: str | Path,
    ) -> Path:
        root = self._sandbox_root(sandbox_id)
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise SandboxBoundaryError("absolute resource paths are forbidden")
        resolved = (root / candidate).resolve()
        if not resolved.is_relative_to(root):
            raise SandboxBoundaryError("resource path escapes the sandbox")
        return resolved

    def database_path(self, sandbox_id: str) -> Path:
        return self.resolve_resource(sandbox_id, _DATABASE_FILE)

    def set_service_version(
        self,
        sandbox_id: str,
        version: ServiceVersion,
    ) -> SandboxState:
        root = self._sandbox_root(sandbox_id)
        self.inspect(sandbox_id)
        self._write_service_version(root, version)
        return self.inspect(sandbox_id)

    def _sandbox_root(self, sandbox_id: str) -> Path:
        if not _SANDBOX_ID.fullmatch(sandbox_id):
            raise SandboxBoundaryError(
                "sandbox id must contain only letters, digits, '_' or '-'"
            )
        root = (self._base / sandbox_id).resolve()
        if not root.is_relative_to(self._base):
            raise SandboxBoundaryError("sandbox path escapes the base directory")
        return root

    @staticmethod
    def _write_service_version(
        root: Path,
        version: ServiceVersion,
    ) -> None:
        state_path = root / _STATE_FILE
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "sandbox_id": root.name,
                    "service_version": version.value,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary.replace(state_path)
