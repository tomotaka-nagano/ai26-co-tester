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
from src.api.models import FailureClassification, ResultAnalysis, Verdict
from src.api.repository import VerificationRepository
from src.api.service import VerificationService


class FakePipeline:
    def generate(self, _request, work_dir: Path) -> dict[str, str]:
        work_dir.mkdir(parents=True, exist_ok=True)
        scenarios = work_dir / 'scenarios.json'
        testcases = work_dir / 'testcases.json'
        judgments = work_dir / 'llm-judgments.json'
        scenarios.write_text(
            '{"items": [{"summary": "scenario-1", "viewpoints": ["view-1"]}]}',
            encoding='utf-8',
        )
        testcases.write_text('[{"name": "case-1"}]', encoding='utf-8')
        judgments.write_text(
            '[{"scenario": "one", "reason": ["requirement"]}]', encoding='utf-8'
        )
        return {
            'scenarios_path': str(scenarios),
            'testcases_path': str(testcases),
            'llm_judgment_log_path': str(judgments),
        }

    def generate_scenarios(self, _request, work_dir: Path) -> dict[str, str]:
        work_dir.mkdir(parents=True, exist_ok=True)
        scenarios = work_dir / 'scenarios.json'
        judgments = work_dir / 'llm-judgments.json'
        scenarios.write_text(
            '{"items": [{"summary": "scenario-1", "viewpoints": ["view-1"]}]}',
            encoding='utf-8',
        )
        judgments.write_text('[]', encoding='utf-8')
        return {
            'scenarios_path': str(scenarios),
            'llm_judgment_log_path': str(judgments),
        }

    def generate_testcases(
        self, _request, work_dir: Path, _scenario_path: Path
    ) -> dict[str, str]:
        testcases = work_dir / 'testcases.json'
        testcases.write_text('[{"name": "case-1"}]', encoding='utf-8')
        return {'testcases_path': str(testcases)}

    def refine_scenarios(
        self, _request, scenarios: list[dict], _history: list[dict], message: str
    ) -> dict:
        return {
            'reply': f'{message}を反映した案です',
            'reason': 'レビュー指摘を反映するため',
            'scenarios': [
                {**scenario, 'summary': f'{scenario["summary"]}-revised'}
                for scenario in scenarios
            ],
        }

    def append_failure_interpretation(
        self, _execution_path: Path, _judgment_path: Path
    ) -> None:
        pass

    def analyze_failure(self, _request, _testcases, _results) -> ResultAnalysis:
        return ResultAnalysis(
            classification=FailureClassification.TESTCASE_DEFECT,
            summary='期待値の誤り',
            facts=['case-1が失敗した'],
            inferences=['期待値がI/F仕様と不一致'],
            affected_testcases=['case-1'],
            confidence=0.9,
            recommendation='case-1を修正する',
        )

    def repair_testcases(self, _request, testcases, _analysis) -> list[dict]:
        return [{**testcase, 'repaired': True} for testcase in testcases]


class FakeRunner:
    def run(self, _config, _product, _testcases_path, work_dir, progress, _canceled):
        progress(1, 1)
        log_path = work_dir / 'execution-log.json'
        log_path.write_text('[{"name": "case-1", "status": "pass"}]', encoding='utf-8')
        result = {'verdict': Verdict.PASS, 'counts': Counts(passed=1).model_dump()}
        return result, log_path


class RepairingRunner:
    def __init__(self):
        self.run_count = 0

    def run(self, _config, _product, testcases_path, work_dir, progress, _canceled):
        self.run_count += 1
        testcases = json.loads(testcases_path.read_text(encoding='utf-8'))
        status = 'pass' if testcases[0].get('repaired') else 'fail'
        progress(1, 1)
        log_path = work_dir / 'execution-log.json'
        log_path.write_text(
            json.dumps([{'name': 'case-1', 'status': status}]), encoding='utf-8'
        )
        counts = Counts(passed=1) if status == 'pass' else Counts(failed=1)
        verdict = Verdict.PASS if status == 'pass' else Verdict.FAIL
        return {'verdict': verdict, 'counts': counts.model_dump()}, log_path


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
        self.root = root
        environment_path = root / 'environments.toml'
        environment_path.write_text(
            "[[environments]]\nid = 'local'\nname = 'Local'\ncommand = ['unused']\n",
            encoding='utf-8',
        )
        repository = VerificationRepository(root / 'jobs.db')
        environments = EnvironmentRegistry(environment_path, repository)
        self.environments = environments
        service = VerificationService(
            repository, FakePipeline(), FakeRunner(), environments, root / 'jobs'
        )
        self.service = service
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
        self.service.close()
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

    def test_verification_accepts_typed_source_documents(self):
        payload = {
            key: value
            for key, value in self.payload.items()
            if key not in {'requirements', 'design'}
        }
        payload['source_documents'] = [
            {
                'type': 'product_spec',
                'title': '製品仕様',
                'artifact': {'path': 'product-spec.pdf'},
            },
            {
                'type': 'change_spec',
                'title': '変更仕様',
                'artifact': {'path': 'change-spec.md'},
            },
        ]
        payload['interface_spec'] = {'path': 'simulator-api'}

        response = self.client.post('/verifications', json=payload)
        self.assertEqual(202, response.status_code)
        self._wait_for(response.json()['verification_id'], 'completed')

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

    def test_scenario_review_then_execution(self):
        self.payload['review_scenarios'] = True
        verification_id = self.client.post('/verifications', json=self.payload).json()[
            'verification_id'
        ]
        self._wait_for(verification_id, 'reviewing_scenarios')

        response = self.client.get(f'/verifications/{verification_id}/scenarios')
        self.assertEqual(200, response.status_code)
        self.assertEqual('scenario-1', response.json()['scenarios'][0]['summary'])

        response = self.client.post(
            f'/verifications/{verification_id}/scenarios/approve'
        )
        self.assertEqual(202, response.status_code)
        self._wait_for(verification_id, 'completed')

        artifacts = self.client.get(
            f'/verifications/{verification_id}/artifacts'
        ).json()
        self.assertEqual('scenario-1', artifacts['scenarios'][0]['summary'])

    def test_scenario_proposal_acceptance_and_revision_conflict(self):
        verification_id = self._create_scenario_review()
        proposal = self.client.post(
            f'/verifications/{verification_id}/scenarios/messages',
            json={'message': '異常系を明確にする', 'base_revision': 1},
        ).json()
        self.assertEqual('update', proposal['changes'][0]['operation'])
        self.assertEqual('scenario-1', proposal['changes'][0]['before']['summary'])
        self.assertEqual(
            'scenario-1-revised', proposal['changes'][0]['after']['summary']
        )

        current = self.client.get(
            f'/verifications/{verification_id}/scenarios'
        ).json()
        self.assertEqual(1, current['revision'])
        self.assertEqual('scenario-1', current['scenarios'][0]['summary'])
        self.assertEqual(
            409,
            self.client.post(
                f'/verifications/{verification_id}/scenarios/approve'
            ).status_code,
        )

        accepted = self.client.post(
            f'/verifications/{verification_id}/scenarios/proposals/'
            f'{proposal["proposal_id"]}/accept'
        ).json()
        self.assertEqual(2, accepted['revision'])
        self.assertEqual('scenario-1-revised', accepted['scenarios'][0]['summary'])
        self.assertEqual('accepted', accepted['proposals'][0]['status'])
        self.assertEqual(2, len(accepted['messages']))

        stale = self.client.post(
            f'/verifications/{verification_id}/scenarios/messages',
            json={'message': '古い版への依頼', 'base_revision': 1},
        )
        self.assertEqual(409, stale.status_code)

        self.client.post(f'/verifications/{verification_id}/scenarios/approve')
        self._wait_for(verification_id, 'completed')
        artifacts = self.client.get(
            f'/verifications/{verification_id}/artifacts'
        ).json()
        self.assertEqual('scenario-1-revised', artifacts['scenarios'][0]['summary'])

    def test_scenario_proposal_rejection_keeps_revision(self):
        verification_id = self._create_scenario_review()
        proposal = self.client.post(
            f'/verifications/{verification_id}/scenarios/messages',
            json={'message': '変更案', 'base_revision': 1},
        ).json()

        rejected = self.client.post(
            f'/verifications/{verification_id}/scenarios/proposals/'
            f'{proposal["proposal_id"]}/reject'
        ).json()
        self.assertEqual('rejected', rejected['status'])
        current = self.client.get(
            f'/verifications/{verification_id}/scenarios'
        ).json()
        self.assertEqual(1, current['revision'])
        self.assertEqual('scenario-1', current['scenarios'][0]['summary'])

    def test_result_approval_completes_with_ok(self):
        self.payload['require_result_approval'] = True
        verification_id = self.client.post('/verifications', json=self.payload).json()[
            'verification_id'
        ]
        self._wait_for(verification_id, 'awaiting_result_approval')
        self.assertEqual(
            'Pass',
            self.client.get(f'/verifications/{verification_id}/result').json()[
                'verdict'
            ],
        )

        response = self.client.post(
            f'/verifications/{verification_id}/result-approval',
            json={'action': 'complete_ok'},
        )
        self.assertEqual(202, response.status_code)
        self.assertEqual('completed_ok', response.json()['status'])

    def test_result_approval_requires_reason_for_override(self):
        self.payload['require_result_approval'] = True
        verification_id = self.client.post('/verifications', json=self.payload).json()[
            'verification_id'
        ]
        self._wait_for(verification_id, 'awaiting_result_approval')

        path = f'/verifications/{verification_id}/result-approval'
        self.assertEqual(
            422,
            self.client.post(path, json={'action': 'complete_ng'}).status_code,
        )
        response = self.client.post(
            path,
            json={'action': 'complete_ng', 'reason': '要求解釈を優先するため'},
        )
        self.assertEqual('completed_ng', response.json()['status'])

    def test_result_approval_restarts_from_scenarios(self):
        self.payload['require_result_approval'] = True
        verification_id = self.client.post('/verifications', json=self.payload).json()[
            'verification_id'
        ]
        self._wait_for(verification_id, 'awaiting_result_approval')

        response = self.client.post(
            f'/verifications/{verification_id}/result-approval',
            json={'action': 'restart_from_scenarios', 'reason': '観点を見直す'},
        )
        self.assertEqual(202, response.status_code)
        review = self._wait_for(verification_id, 'reviewing_scenarios')
        self.assertEqual('reviewing_scenarios', review['status'])
        scenarios = self.client.get(
            f'/verifications/{verification_id}/scenarios'
        ).json()
        self.assertEqual(2, scenarios['revision'])

    def test_testcase_defect_is_repaired_and_rerun(self):
        runner = RepairingRunner()
        repository = VerificationRepository(self.root / 'repair-jobs.db')
        service = VerificationService(
            repository,
            FakePipeline(),
            runner,
            self.environments,
            self.root / 'repair-jobs',
        )
        client = TestClient(create_app(service))
        payload = {
            **self.payload,
            'idempotency_key': 'repair-key',
            'max_repair_rounds': 1,
            'require_result_approval': True,
        }
        verification_id = client.post('/verifications', json=payload).json()[
            'verification_id'
        ]
        state = self._wait_for_client(
            client, verification_id, 'awaiting_result_approval'
        )

        self.assertEqual(1, state['repair_count'])
        self.assertEqual(2, runner.run_count)
        artifacts = client.get(f'/verifications/{verification_id}/artifacts').json()
        self.assertEqual(2, len(artifacts['execution_log_paths']))
        self.assertEqual(1, len(artifacts['result_analyses']))
        self.assertTrue(artifacts['testcases'][0]['repaired'])
        result = client.get(f'/verifications/{verification_id}/result').json()
        self.assertEqual('Pass', result['verdict'])

    def _create_scenario_review(self) -> str:
        self.payload['review_scenarios'] = True
        verification_id = self.client.post('/verifications', json=self.payload).json()[
            'verification_id'
        ]
        self._wait_for(verification_id, 'reviewing_scenarios')
        return verification_id

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
