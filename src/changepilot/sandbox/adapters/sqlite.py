from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from changepilot.sandbox.domain.models import (
    MigrationResult,
    OrderRecord,
    SchemaVersion,
)


_BASELINE_ORDERS = (
    ("order-1001", "alice", 1299),
    ("order-1002", "bob", 2599),
)
_MIGRATION_ID = "orders-v1-to-v2"


class SandboxDatabaseError(RuntimeError):
    """Raised when sandbox database state violates a declared transition."""


class SQLiteSandboxDatabase:
    def initialize_v1(self, path: Path, *, sandbox_id: str) -> None:
        selected = Path(path)
        selected.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(selected)
        try:
            with connection:
                connection.executescript(
                    """
                    PRAGMA foreign_keys = ON;
                    CREATE TABLE sandbox_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE orders (
                        order_id TEXT PRIMARY KEY,
                        customer TEXT NOT NULL,
                        total_cents INTEGER NOT NULL CHECK (total_cents >= 0)
                    );
                CREATE TABLE migration_ledger (
                    migration_id TEXT PRIMARY KEY,
                    from_version TEXT NOT NULL,
                    to_version TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    database_fingerprint TEXT NOT NULL
                );
                CREATE TABLE tool_effects (
                    logical_idempotency_key TEXT PRIMARY KEY,
                    tool_name TEXT NOT NULL,
                    output_json TEXT NOT NULL
                );
                    """
                )
                connection.executemany(
                    "INSERT INTO sandbox_metadata(key, value) VALUES (?, ?)",
                    (("sandbox_id", sandbox_id), ("schema_version", "v1")),
                )
                connection.executemany(
                    """
                    INSERT INTO orders(order_id, customer, total_cents)
                    VALUES (?, ?, ?)
                    """,
                    _BASELINE_ORDERS,
                )
        finally:
            connection.close()

    def schema_version(self, path: Path) -> SchemaVersion:
        with self._connect(path) as connection:
            value = connection.execute(
                "SELECT value FROM sandbox_metadata WHERE key = 'schema_version'"
            ).fetchone()
        if value is None:
            raise SandboxDatabaseError("schema version metadata is missing")
        return SchemaVersion(value[0])

    def sandbox_id(self, path: Path) -> str:
        with self._connect(path) as connection:
            value = connection.execute(
                "SELECT value FROM sandbox_metadata WHERE key = 'sandbox_id'"
            ).fetchone()
        if value is None:
            raise SandboxDatabaseError("sandbox identity metadata is missing")
        return str(value[0])

    def migration_ids(self, path: Path) -> tuple[str, ...]:
        with self._connect(path) as connection:
            rows = connection.execute(
                "SELECT migration_id FROM migration_ledger ORDER BY migration_id"
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def list_orders(self, path: Path) -> tuple[OrderRecord, ...]:
        version = self.schema_version(path)
        priority_column = ", priority" if version is SchemaVersion.V2 else ""
        with self._connect(path) as connection:
            rows = connection.execute(
                "SELECT order_id, customer, total_cents"
                f"{priority_column} FROM orders ORDER BY order_id"
            ).fetchall()
        return tuple(
            OrderRecord(
                order_id=str(row[0]),
                customer=str(row[1]),
                total_cents=int(row[2]),
                priority=(str(row[3]) if version is SchemaVersion.V2 else None),
            )
            for row in rows
        )

    def insert_order(self, path: Path, order: OrderRecord) -> None:
        version = self.schema_version(path)
        with self._connect(path) as connection:
            if version is SchemaVersion.V1:
                if order.priority is not None:
                    raise SandboxDatabaseError(
                        "priority is unavailable in schema v1"
                    )
                connection.execute(
                    """
                    INSERT INTO orders(order_id, customer, total_cents)
                    VALUES (?, ?, ?)
                    """,
                    (order.order_id, order.customer, order.total_cents),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO orders(
                        order_id, customer, total_cents, priority
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        order.order_id,
                        order.customer,
                        order.total_cents,
                        order.priority or "standard",
                    ),
                )

    def migrate_v1_to_v2(
        self,
        path: Path,
        *,
        idempotency_key: str,
        expected_fingerprint: str,
    ) -> MigrationResult:
        with self._connect(path) as connection:
            existing = self._migration_by_key(connection, idempotency_key)
            if existing is not None:
                if existing[0] != _MIGRATION_ID:
                    raise SandboxDatabaseError(
                        "idempotency key belongs to another migration"
                    )
                return MigrationResult(
                    migration_id=str(existing[0]),
                    from_version=SchemaVersion(existing[1]),
                    to_version=SchemaVersion(existing[2]),
                    database_fingerprint=str(existing[3]),
                    already_applied=True,
                )
            current = self._schema_version_in(connection)
            if current is not SchemaVersion.V1:
                raise SandboxDatabaseError(
                    f"migration requires schema v1, found {current.value}"
                )
            actual_fingerprint = self._fingerprint(connection)
            if actual_fingerprint != expected_fingerprint:
                raise SandboxDatabaseError(
                    "database fingerprint does not match migration precondition"
                )
            connection.execute(
                """
                ALTER TABLE orders
                ADD COLUMN priority TEXT NOT NULL DEFAULT 'standard'
                """
            )
            connection.execute(
                """
                UPDATE sandbox_metadata SET value = 'v2'
                WHERE key = 'schema_version'
                """
            )
            final_fingerprint = self._fingerprint(connection)
            connection.execute(
                """
                INSERT INTO migration_ledger(
                    migration_id,
                    from_version,
                    to_version,
                    idempotency_key,
                    database_fingerprint
                ) VALUES (?, 'v1', 'v2', ?, ?)
                """,
                (_MIGRATION_ID, idempotency_key, final_fingerprint),
            )
        return MigrationResult(
            migration_id=_MIGRATION_ID,
            from_version=SchemaVersion.V1,
            to_version=SchemaVersion.V2,
            database_fingerprint=final_fingerprint,
            already_applied=False,
        )

    def rollback_v2_to_v1(
        self,
        path: Path,
        *,
        idempotency_key: str,
    ) -> MigrationResult:
        rollback_id = "orders-v2-to-v1"
        with self._connect(path) as connection:
            existing = self._migration_by_key(connection, idempotency_key)
            if existing is not None:
                if existing[0] != rollback_id:
                    raise SandboxDatabaseError(
                        "idempotency key belongs to another migration"
                    )
                return MigrationResult(
                    migration_id=str(existing[0]),
                    from_version=SchemaVersion(existing[1]),
                    to_version=SchemaVersion(existing[2]),
                    database_fingerprint=str(existing[3]),
                    already_applied=True,
                )
            current = self._schema_version_in(connection)
            if current is not SchemaVersion.V2:
                raise SandboxDatabaseError(
                    f"rollback requires schema v2, found {current.value}"
                )
            unsafe = connection.execute(
                "SELECT COUNT(*) FROM orders WHERE priority <> 'standard'"
            ).fetchone()
            if unsafe is None or int(unsafe[0]) > 0:
                raise SandboxDatabaseError(
                    "schema rollback would discard non-default priority data"
                )
            connection.executescript(
                """
                CREATE TABLE orders_v1 (
                    order_id TEXT PRIMARY KEY,
                    customer TEXT NOT NULL,
                    total_cents INTEGER NOT NULL CHECK (total_cents >= 0)
                );
                INSERT INTO orders_v1(order_id, customer, total_cents)
                SELECT order_id, customer, total_cents FROM orders;
                DROP TABLE orders;
                ALTER TABLE orders_v1 RENAME TO orders;
                UPDATE sandbox_metadata SET value = 'v1'
                WHERE key = 'schema_version';
                """
            )
            final_fingerprint = self._fingerprint(connection)
            connection.execute(
                """
                INSERT INTO migration_ledger(
                    migration_id,
                    from_version,
                    to_version,
                    idempotency_key,
                    database_fingerprint
                ) VALUES (?, 'v2', 'v1', ?, ?)
                """,
                (rollback_id, idempotency_key, final_fingerprint),
            )
        return MigrationResult(
            migration_id=rollback_id,
            from_version=SchemaVersion.V2,
            to_version=SchemaVersion.V1,
            database_fingerprint=final_fingerprint,
            already_applied=False,
        )

    def migration_for_key(
        self,
        path: Path,
        idempotency_key: str,
    ) -> MigrationResult | None:
        with self._connect(path) as connection:
            row = self._migration_by_key(connection, idempotency_key)
        if row is None:
            return None
        return MigrationResult(
            migration_id=str(row[0]),
            from_version=SchemaVersion(row[1]),
            to_version=SchemaVersion(row[2]),
            database_fingerprint=str(row[3]),
            already_applied=True,
        )

    def effect_output(
        self,
        path: Path,
        *,
        logical_idempotency_key: str,
        tool_name: str,
    ) -> dict[str, object] | None:
        with self._connect(path) as connection:
            row = connection.execute(
                """
                SELECT tool_name, output_json
                FROM tool_effects
                WHERE logical_idempotency_key = ?
                """,
                (logical_idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        if row[0] != tool_name:
            raise SandboxDatabaseError(
                "idempotency key belongs to another tool"
            )
        payload = json.loads(row[1])
        if not isinstance(payload, dict):
            raise SandboxDatabaseError("stored tool output is invalid")
        return payload

    def record_effect(
        self,
        path: Path,
        *,
        logical_idempotency_key: str,
        tool_name: str,
        output: dict[str, object],
    ) -> None:
        with self._connect(path) as connection:
            connection.execute(
                """
                INSERT INTO tool_effects(
                    logical_idempotency_key,
                    tool_name,
                    output_json
                ) VALUES (?, ?, ?)
                """,
                (
                    logical_idempotency_key,
                    tool_name,
                    json.dumps(output, sort_keys=True, separators=(",", ":")),
                ),
            )

    def fingerprint(self, path: Path) -> str:
        with self._connect(path) as connection:
            return self._fingerprint(connection)

    @staticmethod
    @contextmanager
    def _connect(path: Path) -> Iterator[sqlite3.Connection]:
        selected = Path(path)
        if not selected.is_file():
            raise SandboxDatabaseError(f"sandbox database does not exist: {selected}")
        connection = sqlite3.connect(selected)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _schema_version_in(
        connection: sqlite3.Connection,
    ) -> SchemaVersion:
        row = connection.execute(
            "SELECT value FROM sandbox_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            raise SandboxDatabaseError("schema version metadata is missing")
        return SchemaVersion(row[0])

    @staticmethod
    def _migration_by_key(
        connection: sqlite3.Connection,
        idempotency_key: str,
    ) -> tuple[object, ...] | None:
        return connection.execute(
            """
            SELECT migration_id, from_version, to_version, database_fingerprint
            FROM migration_ledger
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()

    def _fingerprint(self, connection: sqlite3.Connection) -> str:
        version = self._schema_version_in(connection)
        columns = connection.execute("PRAGMA table_info(orders)").fetchall()
        priority_column = ", priority" if version is SchemaVersion.V2 else ""
        orders = connection.execute(
            "SELECT order_id, customer, total_cents"
            f"{priority_column} FROM orders ORDER BY order_id"
        ).fetchall()
        payload = {
            "schema_version": version.value,
            "columns": [
                {
                    "name": row[1],
                    "type": row[2],
                    "not_null": row[3],
                    "default": row[4],
                    "primary_key": row[5],
                }
                for row in columns
            ],
            "orders": orders,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
