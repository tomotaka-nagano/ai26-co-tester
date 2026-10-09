import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Generator
from typing import Any

from src.api.models import VerificationCreate, VerificationStatus


TERMINAL_STATUSES = {
    VerificationStatus.COMPLETED,
    VerificationStatus.ERROR,
    VerificationStatus.CANCELED,
}


class VerificationRepository:
    def __init__(self, database_path: Path):
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path = database_path
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(_CREATE_TABLE)
            columns = {
                row['name']
                for row in connection.execute(
                    'PRAGMA table_info(verifications)'
                ).fetchall()
            }
            for name, definition in _MIGRATION_COLUMNS.items():
                if name not in columns:
                    connection.execute(
                        f'ALTER TABLE verifications ADD COLUMN {name} {definition}'
                    )

    def create(
        self,
        verification_id: str,
        request: VerificationCreate,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        values = (
            verification_id,
            request.idempotency_key,
            source_id,
            request.workflow_step_id,
            VerificationStatus.ACCEPTED,
            request.model_dump_json(),
            0,
            0,
            now,
            now,
        )
        with self._lock, self._connection() as connection:
            connection.execute(_INSERT_JOB, values)
        return self.get(verification_id)

    def get(self, verification_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                'SELECT * FROM verifications WHERE id = ?', (verification_id,)
            ).fetchone()
        if row is None:
            raise KeyError(verification_id)
        return dict(row)

    def find_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                'SELECT * FROM verifications WHERE idempotency_key = ?', (key,)
            ).fetchone()
        return dict(row) if row else None

    def update(self, verification_id: str, **fields: Any) -> dict[str, Any]:
        fields['updated_at'] = datetime.now(UTC).isoformat()
        assignments = ', '.join(f'{name} = ?' for name in fields)
        values = [self._serialize(value) for value in fields.values()]
        with self._lock, self._connection() as connection:
            connection.execute(
                f'UPDATE verifications SET {assignments} WHERE id = ?',
                [*values, verification_id],
            )
        return self.get(verification_id)

    def request_cancel(self, verification_id: str) -> dict[str, Any]:
        return self.update(verification_id, cancel_requested=1)

    def recover_incomplete(self) -> None:
        terminal_values = [status.value for status in TERMINAL_STATUSES]
        placeholders = ', '.join('?' for _ in terminal_values)
        query = f'UPDATE verifications SET status = ?, error = ?, updated_at = ? WHERE status NOT IN ({placeholders})'
        values = [
            VerificationStatus.ERROR.value,
            'API プロセスの再起動により処理が中断されました',
            datetime.now(UTC).isoformat(),
            *terminal_values,
        ]
        with self._lock, self._connection() as connection:
            connection.execute(query, values)

    def environment_in_use(self, environment_id: str) -> bool:
        with self._connection() as connection:
            rows = connection.execute(
                'SELECT request_json FROM verifications WHERE status = ?',
                (VerificationStatus.EXECUTING_TESTS,),
            ).fetchall()
        return any(
            json.loads(row['request_json'])['test_environment_id'] == environment_id
            for row in rows
        )

    @staticmethod
    def _serialize(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, VerificationStatus):
            return value.value
        return value


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS verifications (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    source_id TEXT,
    workflow_step_id TEXT NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    progress_total INTEGER NOT NULL,
    progress_completed INTEGER NOT NULL,
    result_json TEXT,
    artifacts_json TEXT,
    error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    source_set_id TEXT,
    remote_test_id TEXT,
    remote_status TEXT,
    remote_phase TEXT,
    agent_round INTEGER NOT NULL DEFAULT 0,
    max_rounds INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_INSERT_JOB = """
INSERT INTO verifications (
    id, idempotency_key, source_id, workflow_step_id, status, request_json,
    progress_total, progress_completed, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


_MIGRATION_COLUMNS = {
    'source_set_id': 'TEXT',
    'remote_test_id': 'TEXT',
    'remote_status': 'TEXT',
    'remote_phase': 'TEXT',
    'agent_round': 'INTEGER NOT NULL DEFAULT 0',
    'max_rounds': 'INTEGER NOT NULL DEFAULT 1',
}
