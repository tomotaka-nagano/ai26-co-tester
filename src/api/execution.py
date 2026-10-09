import hashlib
import json
import subprocess
import tomllib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

from fastapi import UploadFile
from exmgai import Client

from src.api.models import (
    AgentDecision,
    ArtifactReference,
    TestCounts,
    TestEnvironment,
    TestEnvironmentState,
    Verdict,
)
from src.api.repository import VerificationRepository
from src.creators.scenario_creator import ScenarioCreator
from src.creators.testcase_creator import TestcaseCreator
from src.parsers.requirement_parser import parse_pdf
from src.schemas.inputs import Requirement


FAILURE_PROMPT = """\
あなたはテストエンジニアです。以下のテスト実行結果について、Fail または実行不能の原因と、要求への影響を簡潔に解釈してください。
事実と推測を分け、追加確認が必要な点を明記してください。

{results}
"""


AGENT_PROMPT = """\
あなたは組込み製品の検証を自律的に進めるテストエージェントです。
要求に対する未検証観点と直前までの実行結果を分析し、追加テストが必要か判断してください。

## 指示
- 成功したという理由だけで終了せず、要求上の未検証観点を確認してください。
- 既存テストと重複するテストは生成しないでください。
- 追加テストはテスト環境ルールに厳密に準拠した JSON オブジェクトとして返してください。
- 追加すべき観点がなければ continue_verification=false、testcases=[] としてください。

# 要求
{requirements}

# テスト環境ルール
{rules}

# 実行済みテスト
{testcases}

# 実行結果
{results}
"""


class ExecutionCanceled(Exception):
    pass


class ArtifactResolver:
    def __init__(self, artifact_root: Path):
        self.artifact_root = artifact_root.resolve()

    def resolve(self, reference: ArtifactReference) -> Path:
        path = (
            Path(reference.path)
            if reference.path
            else self.artifact_root / str(reference.version)
        )
        path = path.expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f'成果物が見つかりません: {path}')
        self._verify_hash(path, reference.sha256)
        return path

    @staticmethod
    def _verify_hash(path: Path, expected: str | None) -> None:
        if not expected:
            return
        if not path.is_file():
            raise ValueError('SHA-256 検証はファイルにのみ対応しています')
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual.lower() != expected.lower():
            raise ValueError(f'SHA-256 が一致しません: {path}')


class GenerationPipeline:
    def __init__(self, resolver: ArtifactResolver):
        self.resolver = resolver

    def generate(self, request: Any, work_dir: Path) -> dict[str, str]:
        work_dir.mkdir(parents=True, exist_ok=True)
        requirements = self._load_requirements(
            self.resolver.resolve(request.requirements)
        )
        scenarios = ScenarioCreator(requirements).create()
        scenario_path = work_dir / 'scenarios.json'
        scenario_path.write_text(scenarios.model_dump_json(indent=2), encoding='utf-8')
        groups = TestcaseCreator(self.resolver.resolve(request.design)).generate(
            scenario_path
        )
        testcases = [testcase for group in groups for testcase in group]
        testcase_path = work_dir / 'testcases.json'
        testcase_path.write_text(
            json.dumps(testcases, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        judgment_path = self._write_judgments(work_dir, scenarios.model_dump()['items'])
        return {
            'testcases_path': str(testcase_path),
            'llm_judgment_log_path': str(judgment_path),
        }

    def decide_additional_tests(
        self,
        request: Any,
        rules: str,
        testcases: list[dict[str, Any]],
        results: list[dict[str, Any]],
    ) -> AgentDecision:
        requirements = self._load_requirements(self.resolver.resolve(request.requirements))
        prompt = AGENT_PROMPT.format(
            requirements=requirements,
            rules=rules,
            testcases=json.dumps(testcases, ensure_ascii=False, indent=2),
            results=json.dumps(results, ensure_ascii=False, indent=2),
        )
        response = Client('gpt-5.4').chat.create(prompt, response_format=AgentDecision)
        decision = AgentDecision.model_validate(response.content)
        decision.testcases = self._remove_duplicates(testcases, decision.testcases)
        if not decision.testcases:
            decision.continue_verification = False
        return decision

    @staticmethod
    def _remove_duplicates(
        existing: list[dict[str, Any]], additional: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        names = {str(testcase.get('name', '')) for testcase in existing}
        return [
            testcase for testcase in additional
            if testcase.get('name') and str(testcase['name']) not in names
        ]

    @staticmethod
    def append_failure_interpretation(
        execution_path: Path, judgment_path: Path
    ) -> None:
        results = json.loads(execution_path.read_text(encoding='utf-8'))
        failures = GenerationPipeline._failure_results(results)
        if not failures:
            return
        prompt = FAILURE_PROMPT.format(
            results=json.dumps(failures, ensure_ascii=False, indent=2)
        )
        response = Client('gpt-5.4').chat.create(prompt)
        judgments = json.loads(judgment_path.read_text(encoding='utf-8'))
        judgments.append({'failure_interpretation': response.content})
        judgment_path.write_text(
            json.dumps(judgments, ensure_ascii=False, indent=2), encoding='utf-8'
        )

    @staticmethod
    def _failure_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        failures = []
        for item in results:
            scenarios = (item.get('result') or {}).get('scenarios')
            if scenarios is not None:
                failures.extend(
                    scenario for scenario in scenarios
                    if str(scenario.get('status')).lower() not in {'pass', 'passed'}
                )
            elif str(item.get('status')).lower() not in {'pass', 'passed'}:
                failures.append(item)
        return failures

    @staticmethod
    def _load_requirements(path: Path) -> str:
        if path.suffix.lower() != '.pdf':
            return path.read_text(encoding='utf-8')
        upload = UploadFile(file=BytesIO(path.read_bytes()), filename=path.name)
        requirements = parse_pdf([upload])
        return '\n'.join(GenerationPipeline._render_requirements(requirements.items))

    @staticmethod
    def _render_requirements(items: list[Requirement], depth: int = 0) -> list[str]:
        lines: list[str] = []
        for requirement in items:
            lines.append(f'{"  " * depth}- {requirement.text}')
            lines.extend(
                GenerationPipeline._render_requirements(
                    requirement.sub_requirements, depth + 1
                )
            )
        return lines

    @staticmethod
    def _write_judgments(work_dir: Path, scenarios: list[dict[str, Any]]) -> Path:
        judgments = [
            {'scenario': item['summary'], 'reason': item['viewpoints']}
            for item in scenarios
        ]
        path = work_dir / 'llm-judgments.json'
        path.write_text(
            json.dumps(judgments, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        return path


@dataclass(frozen=True)
class EnvironmentConfig:
    id: str
    name: str
    backend: str = 'command'
    command: list[str] | None = None
    url: str | None = None
    base_revision: str = 'baseline'
    max_rounds: int = 3
    poll_interval_seconds: float = 0.5
    timeout_seconds: float = 300.0
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.backend == 'command' and not self.command:
            raise ValueError(f'command 環境には command が必要です: {self.id}')
        if self.backend == 'ai-gen-test' and not self.url:
            raise ValueError(f'ai-gen-test 環境には url が必要です: {self.id}')
        if self.backend not in {'command', 'ai-gen-test'}:
            raise ValueError(f'未対応の環境 backend です: {self.backend}')
        if self.max_rounds < 1:
            raise ValueError('max_rounds は1以上である必要があります')


class EnvironmentRegistry:
    def __init__(self, config_path: Path, repository: VerificationRepository):
        self.repository = repository
        self.environments = self._load(config_path)

    @staticmethod
    def _load(path: Path) -> dict[str, EnvironmentConfig]:
        if not path.exists():
            return {}
        data = tomllib.loads(path.read_text(encoding='utf-8'))
        configs = [EnvironmentConfig(**item) for item in data.get('environments', [])]
        return {config.id: config for config in configs}

    def get(self, environment_id: str) -> EnvironmentConfig:
        config = self.environments.get(environment_id)
        if not config or not config.enabled:
            raise KeyError(environment_id)
        return config

    def list(self) -> list[TestEnvironment]:
        return [self._to_model(config) for config in self.environments.values()]

    def _to_model(self, config: EnvironmentConfig) -> TestEnvironment:
        if not config.enabled:
            state = TestEnvironmentState.UNAVAILABLE
        elif self.repository.environment_in_use(config.id):
            state = TestEnvironmentState.IN_USE
        else:
            state = TestEnvironmentState.AVAILABLE
        return TestEnvironment(id=config.id, name=config.name, state=state)


class CommandTestRunner:
    def __init__(self, resolver: ArtifactResolver):
        self.resolver = resolver

    def run(
        self,
        config: EnvironmentConfig,
        product: ArtifactReference,
        testcases_path: Path,
        work_dir: Path,
        progress: Callable[[int, int], None],
        canceled: Callable[[], bool],
    ) -> tuple[dict[str, Any], Path]:
        testcases = json.loads(testcases_path.read_text(encoding='utf-8'))
        product_path = self.resolver.resolve(product)
        results = self._run_all(
            config, product_path, testcases, work_dir, progress, canceled
        )
        log_path = work_dir / 'execution-log.json'
        log_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        counts = self._count(results)
        verdict = (
            Verdict.PASS if counts.failed == 0 and counts.unable == 0 else Verdict.FAIL
        )
        return {'verdict': verdict, 'counts': counts.model_dump()}, log_path

    def _run_all(self, config, product_path, testcases, work_dir, progress, canceled):
        results = []
        total = len(testcases)
        for index, testcase in enumerate(testcases, start=1):
            if canceled():
                raise ExecutionCanceled()
            results.append(
                self._run_one(config, product_path, testcase, work_dir, index, canceled)
            )
            progress(index, total)
        return results

    def _run_one(self, config, product, testcase, work_dir, index, canceled):
        testcase_path = work_dir / f'testcase-{index}.json'
        result_path = work_dir / f'result-{index}.json'
        testcase_path.write_text(
            json.dumps(testcase, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        values = {
            'product_path': product,
            'testcase_path': testcase_path,
            'result_path': result_path,
            'work_dir': work_dir,
        }
        command = [part.format_map(values) for part in config.command or []]
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        stdout, stderr = self._wait(process, canceled)
        if process.returncode != 0 or not result_path.exists():
            return {
                'name': testcase.get('name', str(index)),
                'status': 'unable',
                'detail': stderr or stdout,
            }
        result = json.loads(result_path.read_text(encoding='utf-8'))
        result.setdefault('name', testcase.get('name', str(index)))
        return result

    @staticmethod
    def _wait(
        process: subprocess.Popen, canceled: Callable[[], bool]
    ) -> tuple[str, str]:
        while True:
            try:
                return process.communicate(timeout=0.2)
            except subprocess.TimeoutExpired:
                if canceled():
                    process.terminate()
                    process.communicate()
                    raise ExecutionCanceled()

    @staticmethod
    def _count(results: list[dict[str, Any]]) -> TestCounts:
        statuses = [str(result.get('status', 'unable')).lower() for result in results]
        return TestCounts(
            passed=statuses.count('pass'),
            failed=statuses.count('fail'),
            unable=len(statuses) - statuses.count('pass') - statuses.count('fail'),
        )
