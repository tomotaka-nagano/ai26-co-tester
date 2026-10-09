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
    FailureClassification,
    Progress,
    ResultApprovalAction,
    ResultApprovalRequest,
    ResultAnalysis,
    ScenarioProposal,
    ScenarioRefineRequest,
    ScenarioReview,
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

    def close(self) -> None:
        self.executor.shutdown(wait=True)

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
            repair_count=row['repair_count'],
            error=row['error'],
            created_at=row['created_at'],
            updated_at=row['updated_at'],
        )

    def result(self, verification_id: str) -> VerificationResult:
        row = self._get_row(verification_id)
        available = {
            VerificationStatus.AWAITING_RESULT_APPROVAL,
            VerificationStatus.COMPLETED,
            VerificationStatus.COMPLETED_OK,
            VerificationStatus.COMPLETED_NG,
        }
        if row['status'] not in available:
            raise ConflictError('検証はまだ完了していません')
        return VerificationResult.model_validate_json(row['result_json'])

    def artifacts(self, verification_id: str) -> ArtifactBundle:
        row = self._get_row(verification_id)
        if not row['artifacts_json']:
            raise ConflictError('成果物はまだ生成されていません')
        paths = json.loads(row['artifacts_json'])
        scenario_path = (
            Path(paths['scenarios_path']) if paths.get('scenarios_path') else None
        )
        testcase_path = Path(paths['testcases_path'])
        judgment_path = Path(paths['llm_judgment_log_path'])
        execution_path = (
            Path(paths['execution_log_path'])
            if paths.get('execution_log_path')
            else None
        )
        analysis_path = (
            Path(paths['result_analysis_log_path'])
            if paths.get('result_analysis_log_path')
            else None
        )
        return ArtifactBundle(
            verification_id=verification_id,
            scenarios_path=str(scenario_path) if scenario_path else None,
            scenarios=self._read_scenarios(scenario_path),
            testcases_path=str(testcase_path),
            testcases=json.loads(testcase_path.read_text(encoding='utf-8')),
            execution_log_path=str(execution_path) if execution_path else None,
            execution_log=execution_path.read_text(encoding='utf-8')
            if execution_path
            else None,
            execution_log_paths=paths.get('execution_log_paths', []),
            result_analysis_log_path=str(analysis_path) if analysis_path else None,
            result_analyses=self._read_optional_json(
                str(analysis_path) if analysis_path else None
            ),
            llm_judgment_log_path=str(judgment_path),
            llm_judgments=json.loads(judgment_path.read_text(encoding='utf-8')),
            agent_decision_log_path=paths.get('agent_decision_log_path'),
            agent_decisions=self._read_optional_json(paths.get('agent_decision_log_path')),
        )

    def scenarios(self, verification_id: str) -> ScenarioReview:
        row = self._get_row(verification_id)
        paths = json.loads(row['artifacts_json'] or '{}')
        path = Path(paths['scenarios_path']) if paths.get('scenarios_path') else None
        if path is None:
            raise ConflictError('テストシナリオはまだ生成されていません')
        revision = self._latest_scenario_revision(verification_id, path)
        return ScenarioReview(
            verification_id=verification_id,
            revision=revision['revision'],
            scenarios=revision['scenarios'],
            messages=self.repository.scenario_messages(verification_id),
            proposals=[
                self._scenario_proposal(proposal)
                for proposal in self.repository.scenario_proposals(verification_id)
            ],
        )

    def refine_scenarios(
        self, verification_id: str, request: ScenarioRefineRequest
    ) -> ScenarioProposal:
        row = self._reviewing_scenarios(verification_id)
        paths = json.loads(row['artifacts_json'])
        revision = self._latest_scenario_revision(
            verification_id, Path(paths['scenarios_path'])
        )
        if revision['revision'] != request.base_revision:
            raise ConflictError('指定されたシナリオ版は最新ではありません')
        self.repository.add_scenario_message(
            verification_id, 'user', request.message
        )
        history = self.repository.scenario_messages(verification_id)
        verification = VerificationCreate.model_validate_json(row['request_json'])
        refinement = self.pipeline.refine_scenarios(
            verification,
            revision['scenarios'],
            history,
            request.message,
        )
        self.repository.add_scenario_message(
            verification_id, 'assistant', refinement['reply']
        )
        proposal = self.repository.create_scenario_proposal(
            str(uuid4()),
            verification_id,
            request.base_revision,
            refinement['reply'],
            refinement['reason'],
            refinement['scenarios'],
        )
        return self._scenario_proposal(proposal)

    def accept_scenario_proposal(
        self, verification_id: str, proposal_id: str
    ) -> ScenarioReview:
        self._reviewing_scenarios(verification_id)
        self._validate_proposal_owner(verification_id, proposal_id)
        try:
            revision = self.repository.accept_scenario_proposal(proposal_id)
        except ValueError as error:
            raise ConflictError(str(error)) from error
        self._write_scenario_revision(verification_id, revision)
        return self.scenarios(verification_id)

    def reject_scenario_proposal(
        self, verification_id: str, proposal_id: str
    ) -> ScenarioProposal:
        self._reviewing_scenarios(verification_id)
        self._validate_proposal_owner(verification_id, proposal_id)
        try:
            proposal = self.repository.reject_scenario_proposal(proposal_id)
        except ValueError as error:
            raise ConflictError(str(error)) from error
        return self._scenario_proposal(proposal)

    def _scenario_proposal(self, proposal: dict[str, Any]) -> ScenarioProposal:
        base = self.repository.get_scenario_revision(
            proposal['verification_id'], proposal['base_revision']
        )
        before = base['scenarios'] if base else []
        return ScenarioProposal.model_validate(
            {
                **proposal,
                'changes': self._scenario_changes(before, proposal['scenarios']),
            }
        )

    @staticmethod
    def _scenario_changes(
        before: list[dict[str, Any]], after: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        changes = []
        for index in range(max(len(before), len(after))):
            old = before[index] if index < len(before) else None
            new = after[index] if index < len(after) else None
            if old == new:
                continue
            operation = 'add' if old is None else 'remove' if new is None else 'update'
            changes.append(
                {'operation': operation, 'index': index, 'before': old, 'after': new}
            )
        return changes

    def _latest_scenario_revision(
        self, verification_id: str, path: Path
    ) -> dict[str, Any]:
        revision = self.repository.latest_scenario_revision(verification_id)
        if revision:
            return revision
        self.repository.create_scenario_revision(
            verification_id, self._read_scenarios(path)
        )
        return self.repository.latest_scenario_revision(verification_id) or {}

    def _write_scenario_revision(
        self, verification_id: str, revision: dict[str, Any]
    ) -> None:
        path = self.work_root / verification_id / (
            f'scenarios-revision-{revision["revision"]}.json'
        )
        path.write_text(
            json.dumps(
                {'items': revision['scenarios']}, ensure_ascii=False, indent=2
            ),
            encoding='utf-8',
        )
        row = self._get_row(verification_id)
        artifacts = json.loads(row['artifacts_json'])
        artifacts['scenarios_path'] = str(path)
        self.repository.update(verification_id, artifacts_json=artifacts)

    def _reviewing_scenarios(self, verification_id: str) -> dict[str, Any]:
        row = self._get_row(verification_id)
        if row['status'] != VerificationStatus.REVIEWING_SCENARIOS:
            raise ConflictError('テストシナリオのレビュー待ちではありません')
        return row

    def _validate_proposal_owner(
        self, verification_id: str, proposal_id: str
    ) -> None:
        try:
            proposal = self.repository.get_scenario_proposal(proposal_id)
        except KeyError as error:
            raise NotFoundError('シナリオ提案が見つかりません') from error
        if proposal['verification_id'] != verification_id:
            raise NotFoundError('シナリオ提案が見つかりません')

    @staticmethod
    def _read_scenarios(path: Path | None) -> list[dict[str, Any]]:
        if path is None:
            return []
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, list) else data.get('items', [])

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
            VerificationStatus.REVIEWING_SCENARIOS,
            VerificationStatus.AWAITING_APPROVAL,
            VerificationStatus.AWAITING_RESULT_APPROVAL,
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

    def approve_scenarios(self, verification_id: str) -> VerificationAccepted:
        self._reviewing_scenarios(verification_id)
        proposals = self.repository.scenario_proposals(verification_id)
        if any(proposal['status'] == 'pending' for proposal in proposals):
            raise ConflictError('未処理のシナリオ提案があります')
        self.repository.update(
            verification_id, status=VerificationStatus.GENERATING_TESTCASES
        )
        self.executor.submit(self._generate_testcases_and_run, verification_id)
        return self._accepted(self._get_row(verification_id))

    def approve_result(
        self, verification_id: str, request: ResultApprovalRequest
    ) -> VerificationAccepted:
        row = self._get_row(verification_id)
        if row['status'] != VerificationStatus.AWAITING_RESULT_APPROVAL:
            raise ConflictError('テスト結果の承認待ちではありません')
        if request.action == ResultApprovalAction.RESTART_FROM_SCENARIOS:
            return self._restart_from_scenarios(verification_id, request)
        result = json.loads(row['result_json'])
        expected = self._approval_verdict(request.action)
        if result['verdict'] != expected and not (request.reason or '').strip():
            raise InvalidRequestError('実行判定と異なる承認には理由が必要です')
        event = self.repository.create_approval_event(
            verification_id, request.action, request.reason, expected
        )
        result['approval'] = event
        Path(result['report_path']).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        status = (
            VerificationStatus.COMPLETED_OK
            if expected == 'Pass'
            else VerificationStatus.COMPLETED_NG
        )
        self.repository.update(
            verification_id, status=status, result_json=result
        )
        self._send_webhook(verification_id)
        return self._accepted(self._get_row(verification_id))

    def _restart_from_scenarios(
        self, verification_id: str, request: ResultApprovalRequest
    ) -> VerificationAccepted:
        row = self._get_row(verification_id)
        self.repository.create_approval_event(
            verification_id, request.action, request.reason, None
        )
        self.repository.update(
            verification_id,
            status=VerificationStatus.GENERATING_SCENARIOS,
            result_json=None,
            progress_total=0,
            progress_completed=0,
            repair_count=0,
            cycle_number=row['cycle_number'] + 1,
        )
        self.executor.submit(self._regenerate_scenarios, verification_id)
        return self._accepted(self._get_row(verification_id))

    @staticmethod
    def _approval_verdict(action: ResultApprovalAction) -> str:
        return 'Pass' if action == ResultApprovalAction.COMPLETE_OK else 'Fail'

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
            row = self._get_row(verification_id)
            request = VerificationCreate.model_validate_json(row['request_json'])
            if request.review_scenarios:
                self.repository.update(
                    verification_id, status=VerificationStatus.GENERATING_SCENARIOS
                )
                artifacts = self.pipeline.generate_scenarios(
                    request, self.work_root / verification_id
                )
                self._check_canceled(verification_id)
                scenarios = self._read_scenarios(Path(artifacts['scenarios_path']))
                self.repository.create_scenario_revision(
                    verification_id, scenarios
                )
                self.repository.update(
                    verification_id,
                    status=VerificationStatus.REVIEWING_SCENARIOS,
                    artifacts_json=artifacts,
                )
                return
            self.repository.update(
                verification_id, status=VerificationStatus.GENERATING_TESTCASES
            )
            artifacts = self.pipeline.generate(
                request, self.work_root / verification_id
            )
            if artifacts.get('scenarios_path'):
                scenarios = self._read_scenarios(Path(artifacts['scenarios_path']))
                self.repository.create_scenario_revision(
                    verification_id, scenarios
                )
            self._store_testcases_and_continue(verification_id, request, artifacts)
        except ExecutionCanceled:
            self.repository.update(verification_id, status=VerificationStatus.CANCELED)
        except Exception as error:
            self._fail(verification_id, error)

    def _generate_testcases_and_run(self, verification_id: str) -> None:
        try:
            self._check_canceled(verification_id)
            row = self._get_row(verification_id)
            request = VerificationCreate.model_validate_json(row['request_json'])
            artifacts = json.loads(row['artifacts_json'])
            generated = self.pipeline.generate_testcases(
                request,
                self.work_root / verification_id,
                Path(artifacts['scenarios_path']),
            )
            artifacts.update(generated)
            self._store_testcases_and_continue(verification_id, request, artifacts)
        except ExecutionCanceled:
            self.repository.update(verification_id, status=VerificationStatus.CANCELED)
        except Exception as error:
            self._fail(verification_id, error)

    def _regenerate_scenarios(self, verification_id: str) -> None:
        try:
            row = self._get_row(verification_id)
            request = VerificationCreate.model_validate_json(row['request_json'])
            work_dir = self.work_root / verification_id / f'cycle-{row["cycle_number"]}'
            artifacts = self.pipeline.generate_scenarios(request, work_dir)
            scenarios = self._read_scenarios(Path(artifacts['scenarios_path']))
            self.repository.create_scenario_revision(verification_id, scenarios)
            self.repository.update(
                verification_id,
                status=VerificationStatus.REVIEWING_SCENARIOS,
                artifacts_json=artifacts,
            )
        except Exception as error:
            self._fail(verification_id, error)

    def _store_testcases_and_continue(
        self,
        verification_id: str,
        request: VerificationCreate,
        artifacts: dict[str, str],
    ) -> None:
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
                Path(artifacts['testcases_path']).parent,
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
            Path(artifacts['testcases_path']).parent,
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
        archived_log = self._archive_execution_log(row, log_path, artifacts)
        artifacts['execution_log_path'] = str(archived_log)
        if decision_path:
            artifacts['agent_decision_log_path'] = str(decision_path)
        analysis = self._analyze_failure(row, artifacts, archived_log, result)
        if analysis:
            self._record_analysis(artifacts, analysis)
            if self._repair_and_rerun(verification_id, row, artifacts, analysis):
                return
        report_path = Path(artifacts['testcases_path']).parent / 'report.json'
        payload = {
            'verification_id': verification_id,
            'report_path': str(report_path),
            **result,
        }
        if analysis:
            payload['analysis'] = analysis.model_dump(mode='json')
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding='utf-8',
        )
        self.repository.update(
            verification_id,
            status=self._completion_status(row),
            result_json=payload,
            artifacts_json=artifacts,
        )
        if not self._requires_result_approval(row):
            self._send_webhook(verification_id)

    def _archive_execution_log(
        self, row: dict[str, Any], log_path: Path, artifacts: dict[str, Any]
    ) -> Path:
        round_number = row['repair_count'] + 1
        archived = log_path.parent / f'execution-log-round-{round_number}.json'
        if log_path != archived:
            shutil.copy2(log_path, archived)
        paths = artifacts.setdefault('execution_log_paths', [])
        if str(archived) not in paths:
            paths.append(str(archived))
        return archived

    def _analyze_failure(
        self,
        row: dict[str, Any],
        artifacts: dict[str, Any],
        log_path: Path,
        result: dict[str, Any],
    ) -> ResultAnalysis | None:
        if result['verdict'] != 'Fail':
            return None
        self.repository.update(row['id'], status=VerificationStatus.ANALYZING_RESULTS)
        request = VerificationCreate.model_validate_json(row['request_json'])
        testcases = json.loads(
            Path(artifacts['testcases_path']).read_text(encoding='utf-8')
        )
        results = json.loads(log_path.read_text(encoding='utf-8'))
        try:
            return self.pipeline.analyze_failure(request, testcases, results)
        except Exception as error:
            return ResultAnalysis(
                classification=FailureClassification.UNKNOWN,
                summary='AIによる原因分析に失敗しました',
                facts=[str(error)],
                confidence=0,
                recommendation='人間が実行ログを確認してください',
            )

    @staticmethod
    def _record_analysis(
        artifacts: dict[str, Any], analysis: ResultAnalysis
    ) -> None:
        testcase_path = Path(artifacts['testcases_path'])
        path = testcase_path.parent / 'result-analyses.json'
        analyses = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
        analyses.append(analysis.model_dump(mode='json'))
        path.write_text(
            json.dumps(analyses, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        artifacts['result_analysis_log_path'] = str(path)

    def _repair_and_rerun(
        self,
        verification_id: str,
        row: dict[str, Any],
        artifacts: dict[str, Any],
        analysis: ResultAnalysis,
    ) -> bool:
        request = VerificationCreate.model_validate_json(row['request_json'])
        if analysis.classification != FailureClassification.TESTCASE_DEFECT:
            return False
        if row['repair_count'] >= request.max_repair_rounds:
            return False
        path = Path(artifacts['testcases_path'])
        current = json.loads(path.read_text(encoding='utf-8'))
        repaired = self.pipeline.repair_testcases(request, current, analysis)
        if self._canonical_json(current) == self._canonical_json(repaired):
            return False
        repair_count = row['repair_count'] + 1
        repaired_path = path.parent / f'testcases-revision-{repair_count + 1}.json'
        repaired_path.write_text(
            json.dumps(repaired, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        artifacts['testcases_path'] = str(repaired_path)
        self.repository.update(
            verification_id,
            status=VerificationStatus.REPAIRING_TESTCASES,
            repair_count=repair_count,
            progress_total=len(repaired),
            progress_completed=0,
            artifacts_json=artifacts,
        )
        self._execute(verification_id)
        return True

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    @staticmethod
    def _requires_result_approval(row: dict[str, Any]) -> bool:
        request = VerificationCreate.model_validate_json(row['request_json'])
        return request.require_result_approval

    def _completion_status(self, row: dict[str, Any]) -> VerificationStatus:
        if self._requires_result_approval(row):
            return VerificationStatus.AWAITING_RESULT_APPROVAL
        return VerificationStatus.COMPLETED

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
