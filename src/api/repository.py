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
    VerificationStatus.COMPLETED_OK,
    VerificationStatus.COMPLETED_NG,
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
            connection.execute(_CREATE_SCENARIO_REVISIONS)
            connection.execute(_CREATE_SCENARIO_MESSAGES)
            connection.execute(_CREATE_SCENARIO_PROPOSALS)
            connection.execute(_CREATE_APPROVAL_EVENTS)
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

    def create_approval_event(
        self,
        verification_id: str,
        action: str,
        reason: str | None,
        final_verdict: str | None,
    ) -> dict[str, Any]:
        created_at = datetime.now(UTC).isoformat()
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                _INSERT_APPROVAL_EVENT,
                (verification_id, action, reason, final_verdict, created_at),
            )
            event_id = cursor.lastrowid
            row = connection.execute(
                'SELECT * FROM approval_events WHERE id = ?', (event_id,)
            ).fetchone()
        return dict(row)

    def approval_events(self, verification_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                'SELECT action, reason, final_verdict, created_at '
                'FROM approval_events WHERE verification_id = ? ORDER BY id',
                (verification_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_scenario_revision(
        self, verification_id: str, scenarios: list[dict[str, Any]]
    ) -> int:
        with self._lock, self._connection() as connection:
            revision = self._next_scenario_revision(connection, verification_id)
            connection.execute(
                _INSERT_SCENARIO_REVISION,
                (
                    verification_id,
                    revision,
                    json.dumps(scenarios, ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return revision

    def latest_scenario_revision(self, verification_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                'SELECT * FROM scenario_revisions WHERE verification_id = ? '
                'ORDER BY revision DESC LIMIT 1',
                (verification_id,),
            ).fetchone()
        return self._scenario_revision(row) if row else None

    def get_scenario_revision(
        self, verification_id: str, revision: int
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                'SELECT * FROM scenario_revisions '
                'WHERE verification_id = ? AND revision = ?',
                (verification_id, revision),
            ).fetchone()
        return self._scenario_revision(row) if row else None

    def add_scenario_message(
        self, verification_id: str, role: str, content: str
    ) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                _INSERT_SCENARIO_MESSAGE,
                (verification_id, role, content, datetime.now(UTC).isoformat()),
            )

    def scenario_messages(self, verification_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                'SELECT role, content, created_at FROM scenario_messages '
                'WHERE verification_id = ? ORDER BY id',
                (verification_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_scenario_proposal(
        self,
        proposal_id: str,
        verification_id: str,
        base_revision: int,
        reply: str,
        reason: str,
        scenarios: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connection() as connection:
            connection.execute(
                _INSERT_SCENARIO_PROPOSAL,
                (
                    proposal_id,
                    verification_id,
                    base_revision,
                    reply,
                    reason,
                    json.dumps(scenarios, ensure_ascii=False),
                    'pending',
                    now,
                    now,
                ),
            )
        return self.get_scenario_proposal(proposal_id)

    def get_scenario_proposal(self, proposal_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                'SELECT * FROM scenario_proposals WHERE id = ?', (proposal_id,)
            ).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        return self._scenario_proposal(row)

    def scenario_proposals(self, verification_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                'SELECT * FROM scenario_proposals WHERE verification_id = ? '
                'ORDER BY created_at',
                (verification_id,),
            ).fetchall()
        return [self._scenario_proposal(row) for row in rows]

    def accept_scenario_proposal(self, proposal_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            proposal = connection.execute(
                'SELECT * FROM scenario_proposals WHERE id = ?', (proposal_id,)
            ).fetchone()
            if proposal is None:
                raise KeyError(proposal_id)
            if proposal['status'] != 'pending':
                raise ValueError('提案は既に処理されています')
            revision = self._next_scenario_revision(
                connection, proposal['verification_id']
            )
            if revision - 1 != proposal['base_revision']:
                raise ValueError('提案元のシナリオ版が最新ではありません')
            connection.execute(
                _INSERT_SCENARIO_REVISION,
                (
                    proposal['verification_id'],
                    revision,
                    proposal['scenarios_json'],
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.execute(
                'UPDATE scenario_proposals SET status = ?, updated_at = ? WHERE id = ?',
                ('accepted', datetime.now(UTC).isoformat(), proposal_id),
            )
        return self.latest_scenario_revision(proposal['verification_id']) or {}

    def reject_scenario_proposal(self, proposal_id: str) -> dict[str, Any]:
        proposal = self.get_scenario_proposal(proposal_id)
        if proposal['status'] != 'pending':
            raise ValueError('提案は既に処理されています')
        with self._lock, self._connection() as connection:
            connection.execute(
                'UPDATE scenario_proposals SET status = ?, updated_at = ? WHERE id = ?',
                ('rejected', datetime.now(UTC).isoformat(), proposal_id),
            )
        return self.get_scenario_proposal(proposal_id)

    @staticmethod
    def _next_scenario_revision(
        connection: sqlite3.Connection, verification_id: str
    ) -> int:
        row = connection.execute(
            'SELECT COALESCE(MAX(revision), 0) AS revision FROM scenario_revisions '
            'WHERE verification_id = ?',
            (verification_id,),
        ).fetchone()
        return int(row['revision']) + 1

    @staticmethod
    def _scenario_revision(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data['scenarios'] = json.loads(data.pop('scenarios_json'))
        return data

    @staticmethod
    def _scenario_proposal(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data['proposal_id'] = data.pop('id')
        data['scenarios'] = json.loads(data.pop('scenarios_json'))
        return data

    def recover_incomplete(self) -> None:
        preserved = [
            *TERMINAL_STATUSES,
            VerificationStatus.REVIEWING_SCENARIOS,
            VerificationStatus.AWAITING_APPROVAL,
            VerificationStatus.AWAITING_RESULT_APPROVAL,
        ]
        preserved_values = [status.value for status in preserved]
        placeholders = ', '.join('?' for _ in preserved_values)
        query = f'UPDATE verifications SET status = ?, error = ?, updated_at = ? WHERE status NOT IN ({placeholders})'
        values = [
            VerificationStatus.ERROR.value,
            'API プロセスの再起動により処理が中断されました',
            datetime.now(UTC).isoformat(),
            *preserved_values,
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
    'repair_count': 'INTEGER NOT NULL DEFAULT 0',
    'cycle_number': 'INTEGER NOT NULL DEFAULT 1',
}


_CREATE_SCENARIO_REVISIONS = """
CREATE TABLE IF NOT EXISTS scenario_revisions (
    verification_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    scenarios_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (verification_id, revision),
    FOREIGN KEY (verification_id) REFERENCES verifications(id)
)
"""


_CREATE_SCENARIO_MESSAGES = """
CREATE TABLE IF NOT EXISTS scenario_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    verification_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (verification_id) REFERENCES verifications(id)
)
"""


_CREATE_SCENARIO_PROPOSALS = """
CREATE TABLE IF NOT EXISTS scenario_proposals (
    id TEXT PRIMARY KEY,
    verification_id TEXT NOT NULL,
    base_revision INTEGER NOT NULL,
    reply TEXT NOT NULL,
    reason TEXT NOT NULL,
    scenarios_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (verification_id) REFERENCES verifications(id)
)
"""


_INSERT_SCENARIO_REVISION = """
INSERT INTO scenario_revisions (
    verification_id, revision, scenarios_json, created_at
) VALUES (?, ?, ?, ?)
"""


_INSERT_SCENARIO_MESSAGE = """
INSERT INTO scenario_messages (
    verification_id, role, content, created_at
) VALUES (?, ?, ?, ?)
"""


_INSERT_SCENARIO_PROPOSAL = """
INSERT INTO scenario_proposals (
    id, verification_id, base_revision, reply, reason, scenarios_json,
    status, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


_CREATE_APPROVAL_EVENTS = """
CREATE TABLE IF NOT EXISTS approval_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    verification_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT,
    final_verdict TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (verification_id) REFERENCES verifications(id)
)
"""


_INSERT_APPROVAL_EVENT = """
INSERT INTO approval_events (
    verification_id, action, reason, final_verdict, created_at
) VALUES (?, ?, ?, ?, ?)
"""
