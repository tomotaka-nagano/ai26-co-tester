import hashlib
import json
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


MAX_BLOB_BYTES = 1024 * 1024
TERMINAL_STATUSES = {'completed', 'failed', 'cancelled', 'expired'}
SOURCE_SUFFIXES = {'.cpp', '.h', '.hpp'}


class AiGenTestError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        details: Any = None,
    ):
        super().__init__(f'{code}: {message}')
        self.code = code
        self.retryable = retryable
        self.details = details


class RemoteTestCanceled(Exception):
    pass


@dataclass(frozen=True)
class SourceBundle:
    root: Path
    manifest: dict[str, Any]
    files_by_hash: dict[str, Path]


def build_source_bundle(root: Path, base_revision: str) -> SourceBundle:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f'製品ソースルートではありません: {root}')
    candidates = _source_paths(root)
    if not candidates:
        raise ValueError('アップロード対象の製品ソースがありません')
    entries = [_source_entry(root, path) for path in candidates]
    oversized = [entry['path'] for entry in entries if entry['size_bytes'] > MAX_BLOB_BYTES]
    if oversized:
        raise ValueError(f'1 MB を超えるソースファイルです: {", ".join(oversized)}')
    files_by_hash = {entry['sha256']: root / entry['path'] for entry in entries}
    manifest = {
        'base_revision': base_revision,
        'files': entries,
        'deleted_paths': [],
    }
    return SourceBundle(root, manifest, files_by_hash)


def _source_paths(root: Path) -> list[Path]:
    paths = [root / 'CMakeLists.txt'] if (root / 'CMakeLists.txt').is_file() else []
    source_root = root / 'src_cpp'
    if source_root.is_dir():
        paths.extend(
            path for path in source_root.rglob('*')
            if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES
        )
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def _source_entry(root: Path, path: Path) -> dict[str, Any]:
    content = path.read_bytes()
    return {
        'path': path.relative_to(root).as_posix(),
        'sha256': hashlib.sha256(content).hexdigest(),
        'size_bytes': len(content),
    }


class AiGenTestClient:
    def __init__(self, url: str, timeout_seconds: float = 300.0):
        self.url = url
        self.timeout_seconds = timeout_seconds
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> 'AiGenTestClient':
        self._stack = AsyncExitStack()
        streams = await self._stack.enter_async_context(streamable_http_client(self.url))
        self._session = await self._stack.enter_async_context(
            ClientSession(*streams, read_timeout_seconds=self.timeout_seconds)
        )
        await self._session.initialize()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        if self._stack:
            await self._stack.aclose()

    async def read_rules(self) -> str:
        result = await self._require_session().read_resource(
            'pot-sim://docs/test-case-rules'
        )
        return '\n'.join(
            str(getattr(content, 'text', '')) for content in result.contents
        )

    async def upload_source(self, bundle: SourceBundle) -> str:
        prepared = await self.call('prepare_source_upload', {'manifest': bundle.manifest})
        for missing in prepared.get('missing_blobs', []):
            await self._upload_blob(missing['sha256'], bundle.files_by_hash)
        source_set_id = str(prepared['source_set_id'])
        await self.call('finalize_source_upload', {'source_set_id': source_set_id})
        return source_set_id

    async def run_test(
        self,
        test_case: dict[str, Any],
        source_set_id: str,
        poll_interval_seconds: float,
        canceled: Callable[[], bool],
        status_changed: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        validated = await self.call('validate_test_case', {'test_case': test_case})
        normalized = validated.get('normalized_test_case') or test_case
        submitted = await self.call(
            'submit_test',
            {'test_case': normalized, 'source_set_id': source_set_id},
        )
        test_id = str(submitted['test_id'])
        cancel_sent = False
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            status = await self.call('get_test_status', {'test_id': test_id})
            status_changed(status)
            if status['status'] in TERMINAL_STATUSES:
                if status['status'] == 'cancelled':
                    raise RemoteTestCanceled()
                return await self.call('get_test_result', {'test_id': test_id})
            if canceled() and not cancel_sent:
                await self.call('cancel_test', {'test_id': test_id})
                cancel_sent = True
            await anyio.sleep(poll_interval_seconds)
        raise AiGenTestError('REMOTE_TIMEOUT', 'ai-gen-test の完了待機がタイムアウトしました')

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._require_session().call_tool(name, arguments)
        payload = result.structured_content
        if payload is None and result.content:
            payload = json.loads(str(getattr(result.content[0], 'text', '{}')))
        if not isinstance(payload, dict):
            raise AiGenTestError('INVALID_RESPONSE', f'{name} の応答が辞書ではありません')
        if result.is_error or payload.get('ok') is False:
            error = payload.get('error') or {}
            raise AiGenTestError(
                str(error.get('code', 'MCP_ERROR')),
                str(error.get('message', f'{name} に失敗しました')),
                bool(error.get('retryable', False)),
                error.get('details'),
            )
        return payload

    async def _upload_blob(self, sha256: str, files_by_hash: dict[str, Path]) -> None:
        path = files_by_hash.get(sha256)
        if path is None:
            raise AiGenTestError('UNDECLARED_BLOB', f'要求された blob がありません: {sha256}')
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.put(self._blob_url(sha256), content=path.read_bytes())
            response.raise_for_status()

    def _blob_url(self, sha256: str) -> str:
        parsed = urlsplit(self.url)
        return urlunsplit((parsed.scheme, parsed.netloc, f'/blobs/{sha256}', '', ''))

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError('MCP セッションが開始されていません')
        return self._session


class AgenticTestRunner:
    def __init__(self, resolver: Any):
        self.resolver = resolver

    def run(
        self,
        config: Any,
        product: Any,
        testcases_path: Path,
        work_dir: Path,
        progress: Callable[[int, int], None],
        canceled: Callable[[], bool],
        status_changed: Callable[[dict[str, Any]], None],
        source_uploaded: Callable[[str], None],
        decide: Callable[[str, list[dict[str, Any]], list[dict[str, Any]]], Any],
    ) -> tuple[dict[str, Any], Path, Path]:
        return anyio.run(
            self._run,
            config,
            product,
            testcases_path,
            work_dir,
            progress,
            canceled,
            status_changed,
            source_uploaded,
            decide,
        )

    async def _run(
        self,
        config: Any,
        product: Any,
        testcases_path: Path,
        work_dir: Path,
        progress: Callable[[int, int], None],
        canceled: Callable[[], bool],
        status_changed: Callable[[dict[str, Any]], None],
        source_uploaded: Callable[[str], None],
        decide: Callable[[str, list[dict[str, Any]], list[dict[str, Any]]], Any],
    ) -> tuple[dict[str, Any], Path, Path]:
        product_root = self.resolver.resolve(product)
        bundle = build_source_bundle(product_root, config.base_revision)
        all_testcases = json.loads(testcases_path.read_text(encoding='utf-8'))
        current_testcases = all_testcases
        results: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        async with AiGenTestClient(config.url, config.timeout_seconds) as client:
            rules = await client.read_rules()
            source_set_id = await client.upload_source(bundle)
            source_uploaded(source_set_id)
            for round_number in range(1, config.max_rounds + 1):
                result = await client.run_test(
                    {'scenarios': current_testcases},
                    source_set_id,
                    config.poll_interval_seconds,
                    canceled,
                    status_changed,
                )
                result['round'] = round_number
                results.append(result)
                progress(len(all_testcases), len(all_testcases))
                if round_number == config.max_rounds:
                    decisions.append(self._limit_decision(round_number))
                    break
                decision = decide(rules, all_testcases, results)
                decision_data = decision.model_dump(mode='json')
                decision_data['round'] = round_number
                decisions.append(decision_data)
                if not decision.continue_verification or not decision.testcases:
                    break
                current_testcases = decision.testcases
                all_testcases.extend(current_testcases)
                progress(len(all_testcases) - len(current_testcases), len(all_testcases))
        testcases_path.write_text(
            json.dumps(all_testcases, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        log_path = work_dir / 'execution-log.json'
        decision_path = work_dir / 'agent-decisions.json'
        log_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        decision_path.write_text(
            json.dumps(decisions, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        return self._summarize(results), log_path, decision_path

    @staticmethod
    def _limit_decision(round_number: int) -> dict[str, Any]:
        return {
            'round': round_number,
            'continue_verification': False,
            'reason': '最大ラウンド数に到達しました',
            'testcases': [],
        }

    @staticmethod
    def _summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
        statuses = [
            str(scenario.get('status', 'unable')).lower()
            for response in results
            for scenario in (response.get('result') or {}).get('scenarios', [])
        ]
        passed = statuses.count('passed') + statuses.count('pass')
        failed = statuses.count('failed') + statuses.count('fail') + statuses.count('timeout')
        unable = len(statuses) - passed - failed
        verdict = 'Pass' if failed == 0 and unable == 0 else 'Fail'
        return {
            'verdict': verdict,
            'counts': {'passed': passed, 'failed': failed, 'unable': unable},
        }
