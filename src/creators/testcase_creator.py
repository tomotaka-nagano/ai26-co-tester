import json
from pathlib import Path

from exmgai import Client
from src.schemas.scenario import Scenario
from src.schemas.testcase import Testcases


CREATE_PROMPT = """\
あなたはテストエンジニアです。
以下のテストシナリオとインターフェース資料に基づき、シミュレータに投入可能な具体的なテストケースを生成してください。

## 指示
- テストシナリオの summary と viewpoints を網羅するテストケースを作成してください。
- 各 viewpoint に対して、1つ以上のテストケースを作成してください。
- テストケースはインターフェース資料に記載された構造・フィールド・値の仕様に厳密に準拠してください。
- 具体的な値 (温度、水量、時間、状態名など) を設定してください。
- 境界値や異常系も viewpoints に含まれている場合は適切にテストケース化してください。
- 出力は JSON 配列とし、各要素がインターフェース資料で定義された1テストケースに対応するようにしてください。
- JSON 以外のテキストは出力しないでください。

# テストシナリオ
{scenario}

# インターフェース資料
{api_doc}
"""


DESCRIBE_PROMPT = """\
あなたはテストエンジニアです。
以下のテストシナリオ、テストケース (JSON)、およびインターフェース資料に基づき、人間が理解可能なテストケースの記述を Markdown 形式で作成してください。

## 指示
- テストシナリオとの対応関係が明確にわかるように、シナリオ番号・概要を見出しとして含めてください。
- 各テストケースについて以下を記述してください:
  - テストケース名
  - 目的 (どの検証観点に対応するか)
  - 前提条件 (初期状態の内容を自然言語で記述、具体的な数値を含む)
  - 操作手順 (各ステップの操作を自然言語で記述、タイミング・具体的な値を含む)
  - 期待結果 (検証内容を自然言語で記述、定量的な判断基準を含む)
- 定量的な操作・判断基準 (温度、水量、時間、on/off 等) は省略せず明記してください。
- 出力は Markdown テキストのみとし、コードブロックで囲まないでください。

# テストシナリオ
{scenario}

# テストケース (JSON)
{testcases}

# インターフェース資料
{api_doc}
"""


REPAIR_PROMPT = """\
あなたはシミュレーション用テストケースを修正するテストエンジニアです。
原因分析に基づき、誤りのあるテストケースだけを修正してください。

## 指示
- 全テストケースを含む修正後の完全な一覧を返してください。
- 原因分析で影響対象とされていないケースは変更しないでください。
- インターフェース資料に記載された構造、フィールド、値に厳密に準拠してください。
- 製品不具合を隠すために期待値を実行結果へ合わせてはなりません。

# 現在のテストケース
{testcases}

# 原因分析
{analysis}

# インターフェース資料
{api_doc}
"""


class TestcaseCreator:

    def __init__(self, api_doc_path: str | Path):
        self.api_doc_path = Path(api_doc_path)
        self._api_doc = self.api_doc_path.read_text(encoding='utf-8')

    def generate(self, scenario_path: str | Path) -> list[list[dict]]:
        """シナリオごとにテストケースを生成し、シナリオ単位のリストで返す。"""
        scenario_path = Path(scenario_path)
        scenarios = self._load_scenarios(scenario_path)

        all_groups: list[list[dict]] = []
        client = Client('gpt-5.4')

        for i, item in enumerate(scenarios, start=1):
            scenario = Scenario.model_validate(item)
            prompt = CREATE_PROMPT.format(
                scenario=json.dumps(item, ensure_ascii=False, indent=2),
                api_doc=self._api_doc,
            )

            response = client.chat.create(prompt, response_format=Testcases)
            generated = Testcases.model_validate(response.content)
            result = [item.model_dump(mode='json') for item in generated.items]
            all_groups.append(result)

            print(f'シナリオ {i} ({scenario.summary[:30]}...) → {len(result)} テストケース生成')

        return all_groups

    def describe(self, scenario_path: str | Path, testcase_groups: list[list[dict]]) -> str:
        """シナリオとテストケースグループの対応からMarkdown記述を生成する。"""
        scenario_path = Path(scenario_path)
        scenarios = self._load_scenarios(scenario_path)

        md_parts: list[str] = []
        client = Client('gpt-5.4')

        for i, (item, group) in enumerate(zip(scenarios, testcase_groups), start=1):
            scenario = Scenario.model_validate(item)
            if not group:
                continue

            prompt = DESCRIBE_PROMPT.format(
                scenario=json.dumps(item, ensure_ascii=False, indent=2),
                testcases=json.dumps(group, ensure_ascii=False, indent=2),
                api_doc=self._api_doc,
            )

            response = client.chat.create(prompt)
            md_parts.append(response.content)
            print(f'シナリオ {i} ({scenario.summary[:30]}...) → 記述生成完了')

        return '\n\n'.join(md_parts)

    def repair(
        self, testcases: list[dict], analysis: dict
    ) -> list[dict]:
        prompt = REPAIR_PROMPT.format(
            testcases=json.dumps(testcases, ensure_ascii=False, indent=2),
            analysis=json.dumps(analysis, ensure_ascii=False, indent=2),
            api_doc=self._api_doc,
        )
        response = Client('gpt-5.4').chat.create(
            prompt, response_format=Testcases
        )
        repaired = Testcases.model_validate(response.content)
        return [item.model_dump(mode='json') for item in repaired.items]

    @staticmethod
    def _extract_list(data: list | dict) -> list[dict]:
        """JSON データからリスト部分を抽出する。

        配列ならそのまま、オブジェクトなら最初のリスト型の値を返す。
        """
        if isinstance(data, list):
            return data
        for v in data.values():
            if isinstance(v, list):
                return v
        return []

    def _load_scenarios(self, path: Path) -> list[dict]:
        data = json.loads(path.read_text(encoding='utf-8'))
        return self._extract_list(data)
