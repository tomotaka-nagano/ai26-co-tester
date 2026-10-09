import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.execution import EnvironmentRegistry
from src.api.models import TestCounts as Counts
from src.api.models import Verdict
from src.api.repository import VerificationRepository
from src.api.service import VerificationService


class FakePipeline:
    def generate(self, _request, work_dir: Path) -> dict[str, str]:
        work_dir.mkdir(parents=True, exist_ok=True)
        testcases = work_dir / 'testcases.json'
        judgments = work_dir / 'llm-judgments.json'
        testcases.write_text('[{"name": "case-1"}]', encoding='utf-8')
        judgments.write_text(
            '[{"scenario": "one", "reason": ["requirement"]}]', encoding='utf-8'
        )
        return {
            'testcases_path': str(testcases),
            'llm_judgment_log_path': str(judgments),
        }

    def append_failure_interpretation(
        self, _execution_path: Path, _judgment_path: Path
    ) -> None:
        pass


class FakeRunner:
    def run(self, _config, _product, _testcases_path, work_dir, progress, _canceled):
        progress(1, 1)
        log_path = work_dir / 'execution-log.json'
        log_path.write_text('[{"name": "case-1", "status": "pass"}]', encoding='utf-8')
        result = {'verdict': Verdict.PASS, 'counts': Counts(passed=1).model_dump()}
        return result, log_path


class FakeAgenticRunner:
    def run(
        self,
        config,
        _product,
        _testcases_path,
        work_dir,
        progress,
        _canceled,
        status_changed,
        source_uploaded,
        _decide,
    ):
        source_uploaded('source-set-1')
        status_changed(
            {'test_id': 'remote-1', 'status': 'running', 'phase': 'testing'}
        )
        progress(2, 2)
        log_path = work_dir / 'execution-log.json'
        decision_path = work_dir / 'agent-decisions.json'
        log_path.write_text(
            '[{"round": 1, "result": {"scenarios": [{"status": "passed"}]}}]',
            encoding='utf-8',
        )
        decision_path.write_text(
            '[{"round": 1, "continue_verification": false, "reason": "完了"}]',
            encoding='utf-8',
        )
        return {
            'verdict': Verdict.PASS,
            'counts': Counts(passed=1).model_dump(),
        }, log_path, decision_path


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        environment_path = root / 'environments.toml'
        environment_path.write_text(
            "[[environments]]\nid = 'local'\nname = 'Local'\ncommand = ['unused']\n",
            encoding='utf-8',
        )
        repository = VerificationRepository(root / 'jobs.db')
        environments = EnvironmentRegistry(environment_path, repository)
        service = VerificationService(
            repository, FakePipeline(), FakeRunner(), environments, root / 'jobs'
        )
        self.client = TestClient(create_app(service))
        self.payload = {
            'workflow_step_id': 'step-1',
            'requirements': {'path': 'requirements.md'},
            'design': {'path': 'design.md'},
            'product': {'path': 'product.exe'},
            'test_environment_id': 'local',
            'idempotency_key': 'key-1',
        }

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_verification_lifecycle_and_idempotency(self):
        response = self.client.post('/verifications', json=self.payload)
        self.assertEqual(202, response.status_code)
        verification_id = response.json()['verification_id']
        duplicate = self.client.post('/verifications', json=self.payload).json()
        self.assertEqual(verification_id, duplicate['verification_id'])
        state = self._wait_for(verification_id, 'completed')
        self.assertEqual({'total': 1, 'completed': 1}, state['progress'])
        self.assertEqual(
            'Pass',
            self.client.get(f'/verifications/{verification_id}/result').json()[
                'verdict'
            ],
        )
        artifacts = self.client.get(
            f'/verifications/{verification_id}/artifacts'
        ).json()
        self.assertEqual('case-1', artifacts['testcases'][0]['name'])

    def test_approval_and_rerun(self):
        self.payload['require_approval'] = True
        verification_id = self.client.post('/verifications', json=self.payload).json()[
            'verification_id'
        ]
        self._wait_for(verification_id, 'awaiting_approval')
        self.assertEqual(
            202,
            self.client.post(f'/verifications/{verification_id}/approve').status_code,
        )
        self._wait_for(verification_id, 'completed')
        rerun = {'product': {'path': 'fixed.exe'}, 'idempotency_key': 'key-2'}
        rerun_id = self.client.post(
            f'/verifications/{verification_id}/rerun', json=rerun
        ).json()['verification_id']
        self._wait_for(rerun_id, 'completed')

    def test_health_and_environment(self):
        self.assertEqual({'status': 'ok'}, self.client.get('/health').json())
        environments = self.client.get('/test-environments').json()
        self.assertEqual('available', environments[0]['state'])

    def test_agentic_environment_tracks_remote_execution(self):
        root = Path(self.temporary_directory.name)
        environment_path = root / 'agent-environments.toml'
        environment_path.write_text(
            "[[environments]]\nid = 'agent'\nname = 'Agent'\n"
            "backend = 'ai-gen-test'\nurl = 'http://127.0.0.1:8001/mcp'\n",
            encoding='utf-8',
        )
        repository = VerificationRepository(root / 'agent-jobs.db')
        environments = EnvironmentRegistry(environment_path, repository)
        service = VerificationService(
            repository,
            FakePipeline(),
            FakeRunner(),
            environments,
            root / 'agent-jobs',
            FakeAgenticRunner(),
        )
        client = TestClient(create_app(service))
        payload = {**self.payload, 'test_environment_id': 'agent'}

        verification_id = client.post('/verifications', json=payload).json()[
            'verification_id'
        ]
        state = self._wait_for_client(client, verification_id, 'completed')

        self.assertEqual('remote-1', repository.get(verification_id)['remote_test_id'])
        self.assertEqual('testing', state['remote_phase'])
        self.assertEqual(1, state['agent_round'])
        self.assertEqual(3, state['max_rounds'])
        artifacts = client.get(f'/verifications/{verification_id}/artifacts').json()
        self.assertEqual('完了', artifacts['agent_decisions'][0]['reason'])

    def test_cancel_and_idempotency_conflict(self):
        self.payload['require_approval'] = True
        verification_id = self.client.post('/verifications', json=self.payload).json()[
            'verification_id'
        ]
        self._wait_for(verification_id, 'awaiting_approval')
        canceled = self.client.post(f'/verifications/{verification_id}/cancel')
        self.assertEqual('canceled', canceled.json()['status'])
        self.payload['workflow_step_id'] = 'different-step'
        self.assertEqual(
            409, self.client.post('/verifications', json=self.payload).status_code
        )

    @patch('src.api.service.httpx.post')
    def test_completion_webhook(self, post):
        post.return_value.raise_for_status.return_value = None
        self.payload['callback_url'] = 'https://workflow.example.com/callback'
        verification_id = self.client.post('/verifications', json=self.payload).json()[
            'verification_id'
        ]
        self._wait_for(verification_id, 'completed')
        for _ in range(100):
            if post.called:
                break
            time.sleep(0.01)
        self.assertTrue(post.called)
        self.assertEqual(
            verification_id, post.call_args.kwargs['json']['verification_id']
        )

    def _wait_for(self, verification_id: str, expected: str) -> dict:
        return self._wait_for_client(self.client, verification_id, expected)

    def _wait_for_client(
        self, client: TestClient, verification_id: str, expected: str
    ) -> dict:
        for _ in range(100):
            state = client.get(f'/verifications/{verification_id}').json()
            if state['status'] == expected:
                return state
            time.sleep(0.01)
        self.fail(f'{verification_id} did not reach {expected}: {json.dumps(state)}')
