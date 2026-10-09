import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.api.ai_gen_test import (
    MAX_BLOB_BYTES,
    AgenticTestRunner,
    AiGenTestClient,
    build_source_bundle,
)
from src.api.models import AgentDecision
from src.api.repository import VerificationRepository


class SourceBundleTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / 'src_cpp').mkdir()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_builds_manifest_from_allowed_sources(self):
        cmake = b'project(product)'
        source = b'int main() { return 0; }'
        (self.root / 'CMakeLists.txt').write_bytes(cmake)
        (self.root / 'src_cpp' / 'main.cpp').write_bytes(source)
        (self.root / 'src_cpp' / 'ignored.txt').write_text('ignored', encoding='utf-8')

        bundle = build_source_bundle(self.root, 'baseline')

        self.assertEqual('baseline', bundle.manifest['base_revision'])
        self.assertEqual(
            ['CMakeLists.txt', 'src_cpp/main.cpp'],
            [entry['path'] for entry in bundle.manifest['files']],
        )
        self.assertEqual(
            hashlib.sha256(source).hexdigest(),
            bundle.manifest['files'][1]['sha256'],
        )

    def test_rejects_empty_source_root(self):
        with self.assertRaisesRegex(ValueError, 'アップロード対象'):
            build_source_bundle(self.root, 'baseline')

    def test_rejects_oversized_blob(self):
        (self.root / 'src_cpp' / 'large.cpp').write_bytes(b'x' * (MAX_BLOB_BYTES + 1))
        with self.assertRaisesRegex(ValueError, '1 MB'):
            build_source_bundle(self.root, 'baseline')

    def test_blob_url_uses_mcp_origin(self):
        client = AiGenTestClient('http://127.0.0.1:8001/mcp')
        self.assertEqual(
            'http://127.0.0.1:8001/blobs/abc', client._blob_url('abc')
        )

    def test_repository_migrates_existing_database(self):
        database_path = self.root / 'legacy.db'
        connection = sqlite3.connect(database_path)
        try:
            connection.execute(
                'CREATE TABLE verifications ('
                'id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, '
                'source_id TEXT, workflow_step_id TEXT NOT NULL, status TEXT NOT NULL, '
                'request_json TEXT NOT NULL, progress_total INTEGER NOT NULL, '
                'progress_completed INTEGER NOT NULL, result_json TEXT, '
                'artifacts_json TEXT, error TEXT, cancel_requested INTEGER NOT NULL '
                'DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)'
            )
            connection.commit()
        finally:
            connection.close()

        VerificationRepository(database_path)

        connection = sqlite3.connect(database_path)
        try:
            columns = {
                row[1] for row in connection.execute('PRAGMA table_info(verifications)')
            }
        finally:
            connection.close()
        self.assertTrue(
            {'source_set_id', 'remote_test_id', 'remote_phase', 'agent_round'} <= columns
        )

    @patch('src.api.ai_gen_test.AiGenTestClient')
    def test_agentic_runner_executes_additional_round(self, client_class):
        (self.root / 'CMakeLists.txt').write_text('project(product)', encoding='utf-8')
        (self.root / 'src_cpp' / 'main.cpp').write_text('int main() {}', encoding='utf-8')
        testcase_path = self.root / 'testcases.json'
        testcase_path.write_text('[{"name": "initial"}]', encoding='utf-8')
        client = FakeAiGenTestClient()
        client_class.return_value = client
        config = SimpleNamespace(
            url='http://127.0.0.1:8001/mcp',
            timeout_seconds=30,
            base_revision='baseline',
            max_rounds=3,
            poll_interval_seconds=0,
        )
        decisions = iter([
            AgentDecision(
                continue_verification=True,
                reason='境界値を追加',
                testcases=[{'name': 'additional'}],
            ),
            AgentDecision(
                continue_verification=False,
                reason='網羅済み',
                testcases=[],
            ),
        ])
        runner = AgenticTestRunner(SimpleNamespace(resolve=lambda _product: self.root))

        result, log_path, decision_path = runner.run(
            config,
            SimpleNamespace(),
            testcase_path,
            self.root,
            lambda _completed, _total: None,
            lambda: False,
            lambda _status: None,
            lambda _source_set_id: None,
            lambda _rules, _testcases, _results: next(decisions),
        )

        self.assertEqual('Fail', result['verdict'])
        self.assertEqual({'passed': 1, 'failed': 1, 'unable': 0}, result['counts'])
        self.assertEqual(2, len(json.loads(log_path.read_text(encoding='utf-8'))))
        self.assertEqual(2, len(json.loads(decision_path.read_text(encoding='utf-8'))))


class FakeAiGenTestClient:
    def __init__(self):
        self.round = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc_value, _traceback):
        pass

    async def read_rules(self):
        return 'rules'

    async def upload_source(self, _bundle):
        return 'source-set-1'

    async def run_test(self, _test_case, _source_set_id, _interval, _canceled, changed):
        self.round += 1
        changed({'test_id': f'test-{self.round}', 'status': 'completed'})
        status = 'passed' if self.round == 1 else 'failed'
        return {
            'test_id': f'test-{self.round}',
            'status': 'completed',
            'result': {'scenarios': [{'name': f'case-{self.round}', 'status': status}]},
        }


if __name__ == '__main__':
    unittest.main()
