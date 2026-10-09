# co-test-driver

組込み製品向けのテストケース生成・実行を、非同期の検証ジョブとして提供する FastAPI アプリケーションです。

## 起動

Agentic 検証を利用する場合は、先に別ターミナルで ai-gen-test MCP を起動します。

```powershell
Set-Location C:\dev\ai-gen-test
uv sync
uv run python -m mcp_server.server
```

co-test-driver は MCP プロセスを起動・停止しません。既定では `http://127.0.0.1:8001/mcp` へ接続します。

```powershell
uv sync
uv run uvicorn src.api.app:app --host 0.0.0.0 --port 8000
```

API 仕様は起動後に `http://localhost:8000/docs` で確認できます。

## 検証の開始

```http
POST /verifications
Content-Type: application/json

{
	"workflow_step_id": "system-test-01",
	"requirements": {"path": "res/artifact/requirements.md"},
	"design": {"path": "res/artifact/api.md"},
	"product": {
		"path": "C:/dev/product"
	},
	"test_environment_id": "ai-gen-test",
	"idempotency_key": "workflow-123-system-test-01-v1",
	"callback_url": "https://workflow.example.com/hooks/verification",
	"require_approval": false
}
```

成果物参照には `path`、または `CO_TEST_DRIVER_ARTIFACT_ROOT` を基準とする `version` を指定します。
`ai-gen-test` 環境の `product` は `CMakeLists.txt` と `src_cpp/` を含む製品ソースルートを指します。
対象の C/C++ ソースは content-addressed blob として MCP へ送信され、ビルド後にテストされます。
ディレクトリに `sha256` は指定できません。

状態値は `accepted`、`generating_testcases`、`awaiting_approval`、`uploading_product`、
`executing_tests`、`analyzing_results`、`completed`、`error`、`canceled` です。
状態レスポンスには MCP の `remote_status`、`remote_phase`、現在の `agent_round`、`max_rounds` も含まれます。
同一の冪等キーと同一入力は既存ジョブを返し、異なる入力には `409 Conflict` を返します。

Agent は初期テストの結果と要求上の未検証観点を分析し、必要なら追加テストを生成します。既定では最大3ラウンドで、
追加観点がなくなれば早期終了します。`require_approval=true` の場合、承認対象は初期テストだけです。
`POST /verifications/{id}/approve` の後に生成される追加テストは自動実行されます。

## テスト環境

`backend = 'ai-gen-test'` の環境では、次の設定を利用できます。

| キー | 既定値 | 説明 |
|---|---|---|
| `url` | 必須 | Streamable HTTP MCP URL |
| `base_revision` | `baseline` | source manifest の基準リビジョン |
| `max_rounds` | `3` | 初期実行を含む最大ラウンド数 |
| `poll_interval_seconds` | `0.5` | リモート状態の確認間隔 |
| `timeout_seconds` | `300` | MCP 接続・ジョブ待機の上限秒数 |

既存の command runner を利用する場合は、[test-environments.toml](test-environments.toml) の `command` が
テストケースごとに実行されます。以下のプレースホルダーを利用できます。

- `{product_path}`: 製品ソフトウェアのパス
- `{testcase_path}`: 1 件分のテストケース JSON
- `{result_path}`: runner が結果を書き込むパス
- `{work_dir}`: ジョブの作業ディレクトリ

runner は `{result_path}` に次の形式の JSON を書き込み、終了コード `0` で終了してください。

```json
{
	"name": "testcase-name",
	"status": "pass",
	"detail": "optional execution detail"
}
```

`status` は `pass`、`fail`、`unable` のいずれかです。終了コードが非 `0`、または結果ファイルがない場合は `unable` として集計されます。

## 設定

| 環境変数 | 既定値 | 説明 |
|---|---|---|
| `CO_TEST_DRIVER_DATA_DIR` | `data` | SQLite DB とジョブ成果物の保存先 |
| `CO_TEST_DRIVER_ARTIFACT_ROOT` | プロジェクトルート | `version` 参照の基準ディレクトリ |
| `CO_TEST_DRIVER_ENVIRONMENTS` | `test-environments.toml` | テスト環境設定ファイル |

完了またはエラー時の Webhook は最大 3 回送信されます。結果や成果物はそれぞれ
`GET /verifications/{id}/result`、`GET /verifications/{id}/artifacts` から再取得できます。
Agentic 検証では `execution-log.json` に全ラウンドの MCP 結果、`agent-decisions.json` に追加テスト判断と
停止理由が保存されます。

## テスト

```powershell
uv run python -m unittest discover -s tests -v
```