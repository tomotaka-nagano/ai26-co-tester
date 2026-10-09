import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from src.api.execution import (
    CommandTestRunner,
    EnvironmentRegistry,
    ExecutionCanceled,
    GenerationPipeline,
)
from src.api.ai_gen_test import AgenticTestRunner, RemoteTestCanceled
from src.api.models import (
    ArtifactBundle,
    Progress,
    VerificationAccepted,
    VerificationCreate,
    VerificationResult,
    VerificationRerun,
    VerificationState,
    VerificationStatus,
)
from src.api.repository import TERMINAL_STATUSES, VerificationRepository


class ServiceError(Exception):
    pass


class NotFoundError(ServiceError):
    pass


class ConflictError(ServiceError):
    pass


class InvalidRequestError(ServiceError):
    pass


class VerificationService:
    def __init__(
        self,
        repository: VerificationRepository,
        pipeline: GenerationPipeline,
        runner: CommandTestRunner,
        environments: EnvironmentRegistry,
        work_root: Path,
        agentic_runner: AgenticTestRunner | None = None,
    ):
        self.repository = repository
        self.pipeline = pipeline
        self.runner = runner
        self.environments = environments
        self.work_root = work_root
        self.agentic_runner = agentic_runner
        self.executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix='verification'
        )
        self.repository.recover_incomplete()

    def create(self, request: VerificationCreate) -> VerificationAccepted:
        self._validate_environment(request.test_environment_id)
        existing = self.repository.find_by_idempotency_key(request.idempotency_key)
        if existing:
            return self._idempotent_response(existing, request)
        verification_id = str(uuid4())
        row = self.repository.create(verification_id, request)
        self.executor.submit(self._generate_and_run, verification_id)
        return self._accepted(row)

    def get(self, verification_id: str) -> VerificationState:
        row = self._get_row(verification_id)
        return VerificationState(
            verification_id=row['id'],
            workflow_step_id=row['workflow_step_id'],
            status=row['status'],
            progress=Progress(
                total=row['progress_total'], completed=row['progress_completed']
            ),
            remote_status=row['remote_status'],
            remote_phase=row['remote_phase'],
            agent_round=row['agent_round'],
            max_rounds=row['max_rounds'],
            error=row['error'],
            created_at=row['created_at'],
            updated_at=row['updated_at'],
        )

    def result(self, verification_id: str) -> VerificationResult:
        row = self._get_row(verification_id)
        if row['status'] != VerificationStatus.COMPLETED:
            raise ConflictError('検証はまだ完了していません')
        return VerificationResult.model_validate_json(row['result_json'])

    def artifacts(self, verification_id: str) -> ArtifactBundle:
        row = self._get_row(verification_id)
        if not row['artifacts_json']:
            raise ConflictError('成果物はまだ生成されていません')
        paths = json.loads(row['artifacts_json'])
        testcase_path = Path(paths['testcases_path'])
        judgment_path = Path(paths['llm_judgment_log_path'])
        execution_path = (
            Path(paths['execution_log_path'])
            if paths.get('execution_log_path')
            else None
        )
        return ArtifactBundle(
            verification_id=verification_id,
            testcases_path=str(testcase_path),
            testcases=json.loads(testcase_path.read_text(encoding='utf-8')),
            execution_log_path=str(execution_path) if execution_path else None,
            execution_log=execution_path.read_text(encoding='utf-8')
            if execution_path
            else None,
            llm_judgment_log_path=str(judgment_path),
            llm_judgments=json.loads(judgment_path.read_text(encoding='utf-8')),
            agent_decision_log_path=paths.get('agent_decision_log_path'),
            agent_decisions=self._read_optional_json(paths.get('agent_decision_log_path')),
        )

    @staticmethod
    def _read_optional_json(path: str | None) -> list[dict[str, Any]]:
        return json.loads(Path(path).read_text(encoding='utf-8')) if path else []

    def cancel(self, verification_id: str) -> VerificationState:
        row = self._get_row(verification_id)
        if VerificationStatus(row['status']) in TERMINAL_STATUSES:
            return self.get(verification_id)
        self.repository.request_cancel(verification_id)
        if row['status'] in {
            VerificationStatus.ACCEPTED,
            VerificationStatus.AWAITING_APPROVAL,
        }:
            self.repository.update(verification_id, status=VerificationStatus.CANCELED)
        return self.get(verification_id)

    def approve(self, verification_id: str) -> VerificationAccepted:
        row = self._get_row(verification_id)
        if row['status'] != VerificationStatus.AWAITING_APPROVAL:
            raise ConflictError('承認待ちの検証ではありません')
        self.repository.update(verification_id, status=VerificationStatus.ACCEPTED)
        self.executor.submit(self._execute, verification_id)
        return self._accepted(self._get_row(verification_id))

    def rerun(
        self, verification_id: str, rerun: VerificationRerun
    ) -> VerificationAccepted:
        source = self._get_row(verification_id)
        if not source['artifacts_json']:
            raise ConflictError('再利用できるテストケースがありません')
        original = VerificationCreate.model_validate_json(source['request_json'])
        request = original.model_copy(
            update={
                'product': rerun.product,
                'idempotency_key': rerun.idempotency_key,
                'callback_url': rerun.callback_url,
                'require_approval': False,
            }
        )
        return self._create_rerun(request, source)

    def _create_rerun(
        self, request: VerificationCreate, source: dict[str, Any]
    ) -> VerificationAccepted:
        existing = self.repository.find_by_idempotency_key(request.idempotency_key)
        if existing:
            return self._idempotent_response(existing, request)
        verification_id = str(uuid4())
        row = self.repository.create(verification_id, request, source_id=source['id'])
        artifacts = self._copy_test_artifacts(
            verification_id, json.loads(source['artifacts_json'])
        )
        self.repository.update(verification_id, artifacts_json=artifacts)
        self.executor.submit(self._execute, verification_id)
        return self._accepted(row)

    def _copy_test_artifacts(
        self, verification_id: str, source: dict[str, str]
    ) -> dict[str, str]:
        work_dir = self.work_root / verification_id
        work_dir.mkdir(parents=True, exist_ok=True)
        testcases = shutil.copy2(source['testcases_path'], work_dir / 'testcases.json')
        judgments = shutil.copy2(
            source['llm_judgment_log_path'], work_dir / 'llm-judgments.json'
        )
        return {
            'testcases_path': str(testcases),
            'llm_judgment_log_path': str(judgments),
        }

    def _generate_and_run(self, verification_id: str) -> None:
        try:
            self._check_canceled(verification_id)
            row = self.repository.update(
                verification_id, status=VerificationStatus.GENERATING_TESTCASES
            )
            request = VerificationCreate.model_validate_json(row['request_json'])
            artifacts = self.pipeline.generate(
                request, self.work_root / verification_id
            )
            testcases = json.loads(
                Path(artifacts['testcases_path']).read_text(encoding='utf-8')
            )
            self.repository.update(
                verification_id, artifacts_json=artifacts, progress_total=len(testcases)
            )
            self._check_canceled(verification_id)
            if request.require_approval:
                self.repository.update(
                    verification_id, status=VerificationStatus.AWAITING_APPROVAL
                )
                return
            self._execute(verification_id)
        except ExecutionCanceled:
            self.repository.update(verification_id, status=VerificationStatus.CANCELED)
        except Exception as error:
            self._fail(verification_id, error)

    def _execute(self, verification_id: str) -> None:
        try:
            self._check_canceled(verification_id)
            row = self._get_row(verification_id)
            request = VerificationCreate.model_validate_json(row['request_json'])
            config = self.environments.get(request.test_environment_id)
            if config.backend == 'ai-gen-test':
                self._execute_agentic(verification_id, request, config, row)
                return
            self.repository.update(
                verification_id, status=VerificationStatus.EXECUTING_TESTS
            )
            artifacts = json.loads(row['artifacts_json'])
            result, log_path = self.runner.run(
                config,
                request.product,
                Path(artifacts['testcases_path']),
                self.work_root / verification_id,
                lambda completed, total: self._progress(
                    verification_id, completed, total
                ),
                lambda: bool(self._get_row(verification_id)['cancel_requested']),
            )
            self._complete(verification_id, result, log_path)
        except (ExecutionCanceled, RemoteTestCanceled):
            self.repository.update(verification_id, status=VerificationStatus.CANCELED)
        except Exception as error:
            self._fail(verification_id, error)

    def _execute_agentic(self, verification_id, request, config, row) -> None:
        if self.agentic_runner is None:
            raise RuntimeError('ai-gen-test runner が構成されていません')
        artifacts = json.loads(row['artifacts_json'])
        self.repository.update(
            verification_id,
            status=VerificationStatus.UPLOADING_PRODUCT,
            max_rounds=config.max_rounds,
        )
        result, log_path, decision_path = self.agentic_runner.run(
            config,
            request.product,
            Path(artifacts['testcases_path']),
            self.work_root / verification_id,
            lambda completed, total: self._progress(verification_id, completed, total),
            lambda: bool(self._get_row(verification_id)['cancel_requested']),
            lambda status: self._remote_status(verification_id, status),
            lambda source_set_id: self._source_uploaded(
                verification_id, source_set_id
            ),
            lambda rules, testcases, results: self._decide(
                verification_id, request, rules, testcases, results
            ),
        )
        self._complete(verification_id, result, log_path, decision_path)

    def _source_uploaded(self, verification_id: str, source_set_id: str) -> None:
        self.repository.update(
            verification_id,
            source_set_id=source_set_id,
            status=VerificationStatus.EXECUTING_TESTS,
        )

    def _remote_status(self, verification_id: str, status: dict[str, Any]) -> None:
        row = self._get_row(verification_id)
        test_id = status.get('test_id')
        agent_round = row['agent_round']
        if test_id and test_id != row['remote_test_id']:
            agent_round += 1
        self.repository.update(
            verification_id,
            status=VerificationStatus.EXECUTING_TESTS,
            remote_test_id=test_id,
            remote_status=status.get('status'),
            remote_phase=status.get('phase'),
            agent_round=agent_round,
        )

    def _decide(self, verification_id, request, rules, testcases, results):
        self.repository.update(
            verification_id, status=VerificationStatus.ANALYZING_RESULTS
        )
        decision = self.pipeline.decide_additional_tests(
            request, rules, testcases, results
        )
        self._check_canceled(verification_id)
        self.repository.update(
            verification_id, status=VerificationStatus.EXECUTING_TESTS
        )
        return decision

    def _complete(
        self,
        verification_id: str,
        result: dict[str, Any],
        log_path: Path,
        decision_path: Path | None = None,
    ) -> None:
        row = self._get_row(verification_id)
        artifacts = json.loads(row['artifacts_json'])
        artifacts['execution_log_path'] = str(log_path)
        if decision_path:
            artifacts['agent_decision_log_path'] = str(decision_path)
        self._interpret_failures(log_path, Path(artifacts['llm_judgment_log_path']))
        report_path = self.work_root / verification_id / 'report.json'
        payload = {
            'verification_id': verification_id,
            'report_path': str(report_path),
            **result,
        }
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding='utf-8',
        )
        self.repository.update(
            verification_id,
            status=VerificationStatus.COMPLETED,
            result_json=payload,
            artifacts_json=artifacts,
        )
        self._send_webhook(verification_id)

    def _fail(self, verification_id: str, error: Exception) -> None:
        self.repository.update(
            verification_id, status=VerificationStatus.ERROR, error=str(error)
        )
        self._send_webhook(verification_id)

    def _send_webhook(self, verification_id: str) -> None:
        row = self._get_row(verification_id)
        request = VerificationCreate.model_validate_json(row['request_json'])
        if not request.callback_url:
            return
        payload = {
            'verification_id': verification_id,
            'status': row['status'],
            'error': row['error'],
        }
        if row['result_json']:
            payload['result'] = json.loads(row['result_json'])
        for _ in range(3):
            try:
                response = httpx.post(
                    str(request.callback_url), json=payload, timeout=10.0
                )
                response.raise_for_status()
                return
            except httpx.HTTPError:
                continue

    def _interpret_failures(self, execution_path: Path, judgment_path: Path) -> None:
        try:
            self.pipeline.append_failure_interpretation(execution_path, judgment_path)
        except Exception as error:
            judgments = json.loads(judgment_path.read_text(encoding='utf-8'))
            judgments.append({'failure_interpretation_error': str(error)})
            judgment_path.write_text(
                json.dumps(judgments, ensure_ascii=False, indent=2), encoding='utf-8'
            )

    def _idempotent_response(
        self, row: dict[str, Any], request: VerificationCreate
    ) -> VerificationAccepted:
        if json.loads(row['request_json']) != request.model_dump(mode='json'):
            raise ConflictError('同じ冪等キーが異なるリクエストで使用されています')
        return self._accepted(row)

    def _get_row(self, verification_id: str) -> dict[str, Any]:
        try:
            return self.repository.get(verification_id)
        except KeyError as error:
            raise NotFoundError('検証ジョブが見つかりません') from error

    def _validate_environment(self, environment_id: str) -> None:
        try:
            self.environments.get(environment_id)
        except KeyError as error:
            raise InvalidRequestError('利用できないテスト環境です') from error

    def _check_canceled(self, verification_id: str) -> None:
        if self._get_row(verification_id)['cancel_requested']:
            raise ExecutionCanceled()

    def _progress(self, verification_id: str, completed: int, total: int) -> None:
        self.repository.update(
            verification_id, progress_completed=completed, progress_total=total
        )

    @staticmethod
    def _accepted(row: dict[str, Any]) -> VerificationAccepted:
        return VerificationAccepted(verification_id=row['id'], status=row['status'])
