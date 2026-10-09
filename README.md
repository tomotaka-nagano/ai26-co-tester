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
Set-Location web
npm install
npm run build
Set-Location ..
uv run uvicorn src.api.app:app --host 0.0.0.0 --port 8000
```

API 仕様は起動後に `http://localhost:8000/docs` で確認できます。
検証ワークスペースは `http://localhost:8000/ui/` で利用できます。

## 検証の開始

```http
POST /verifications
Content-Type: application/json

{
	"workflow_step_id": "system-test-01",
	"source_documents": [
		{
			"type": "requirement_spec",
			"title": "要求仕様書",
			"artifact": {"path": "C:/dev/ai26/resource/input/pot-spec-v6.pdf"}
		}
	],
	"interface_spec": {"path": "C:/dev/ai26/ai26-pot-test-env/doc/api"},
	"product": {
		"path": "C:/dev/ai26/ai26-pot-application"
	},
	"test_environment_id": "ai-gen-test",
	"idempotency_key": "workflow-123-system-test-01-v1",
	"callback_url": "https://workflow.example.com/hooks/verification",
	"require_approval": false,
	"review_scenarios": true,
	"require_result_approval": true,
	"max_repair_rounds": 2
}
```

`source_documents`の`type`は`product_spec`、`requirement_spec`、`change_spec`、`other`を指定できます。
複数資料は種別・タイトル・出典を保持したままAIへ渡されます。従来の`requirements`と`design`も互換入力として利用できます。

成果物参照には `path`、または `CO_TEST_DRIVER_ARTIFACT_ROOT` を基準とする `version` を指定します。
`ai-gen-test` 環境の `product` は `CMakeLists.txt` と `src_cpp/` を含む製品ソースルートを指します。
対象の C/C++ ソースは content-addressed blob として MCP へ送信され、ビルド後にテストされます。
ディレクトリに `sha256` は指定できません。

状態値は `accepted`、`generating_scenarios`、`reviewing_scenarios`、`generating_testcases`、
`awaiting_approval`、`uploading_product`、
`executing_tests`、`analyzing_results`、`repairing_testcases`、`awaiting_result_approval`、
`completed`、`completed_ok`、`completed_ng`、`error`、`canceled` です。
状態レスポンスには MCP の `remote_status`、`remote_phase`、現在の `agent_round`、`max_rounds`、
`repair_count`も含まれます。
同一の冪等キーと同一入力は既存ジョブを返し、異なる入力には `409 Conflict` を返します。

Agent は初期テストの結果と要求上の未検証観点を分析し、必要なら追加テストを生成します。既定では最大3ラウンドで、
追加観点がなくなれば早期終了します。`require_approval=true` の場合、承認対象は初期テストだけです。
`POST /verifications/{id}/approve` の後に生成される追加テストは自動実行されます。

`review_scenarios=true` の場合、AIが初期シナリオを生成した後に `reviewing_scenarios` で停止します。
`GET /verifications/{id}/scenarios` でシナリオを取得し、レビュー後に
`POST /verifications/{id}/scenarios/approve` を呼ぶとテストケース生成へ進みます。
既存クライアントとの互換性のため、既定値は `false` です。

レビュー中は、現在の `revision` を指定してAIへ改訂を依頼できます。

```http
POST /verifications/{id}/scenarios/messages
Content-Type: application/json

{
	"message": "停電復旧時のシナリオを追加してください",
	"base_revision": 1
}
```

応答にはAIの回答、変更理由、変更後の完全なシナリオ、`add`・`update`・`remove`形式の差分、
`proposal_id`が含まれます。提案時点では現在版は変更されません。内容を確認して次のいずれかを呼びます。

```http
POST /verifications/{id}/scenarios/proposals/{proposal_id}/accept
POST /verifications/{id}/scenarios/proposals/{proposal_id}/reject
```

採用すると新しいrevisionが作成されます。古いrevisionを元にした依頼や提案の採用は`409 Conflict`になります。
未処理の提案が残っている間は、シナリオ全体を承認してテストケース生成へ進むことはできません。

## NG分析と自動修復

Failまたは実行不能の結果は、AIが`testcase_defect`、`product_or_spec_defect`、
`environment_defect`、`unknown`に分類します。分析には事実、推測、対象ケース、信頼度、推奨アクションが含まれます。

`testcase_defect`かつ`max_repair_rounds`未満の場合だけ、I/F仕様に基づいてケースを修正し再実行します。
修正版が元と同一の場合は循環を避けるため停止します。各ケース版、実行ラウンド、分析結果は上書きせず成果物として保持されます。

## テスト結果の承認

`require_result_approval=true`の場合、実行後は`awaiting_result_approval`で停止します。

```http
POST /verifications/{id}/result-approval
Content-Type: application/json

{
	"action": "complete_ok",
	"reason": null
}
```

`action`には`restart_from_scenarios`、`complete_ng`、`complete_ok`を指定できます。
実行判定と異なる完了判定には理由が必要です。`restart_from_scenarios`は過去成果物を保持したまま新しいcycleを開始します。

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
uv run flake8 src/api src/creators src/schemas tests --max-line-length=120

Set-Location web
npm run build
npm run test:e2e
```