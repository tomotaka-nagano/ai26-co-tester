import json
from exmgai import Client
from src.schemas.scenario import ScenarioRefinement, Scenarios


CREATE_PROMPT = """\
あなたはテストエンジニアです。
以下の要求リストとテストシナリオ導出観点に基づき、テストシナリオを網羅的に導出してください。

## 指示
- 要求リストの各項目に対して、該当するテストシナリオ導出観点を適用し、テストシナリオを作成してください。
- 各テストシナリオには、事前条件・操作の概要 (summary) と、そのシナリオの中で検証したいことのリスト (viewpoints) を含めてください。
- viewpoints には導出観点のIDではなく、そのシナリオで具体的に何を確認・検証するかを自然言語で記述してください。
- 要求に関連しない観点は無理に適用しないでください。
- 重複するシナリオは統合してください。

# 要求リスト
{requirements}

# テストシナリオ導出観点
|ID|観点|
|--|--|
{table_body}
"""


REFINE_PROMPT = """\
あなたはテストシナリオを人間と共同レビューするテストエンジニアです。
要求、現在のシナリオ、これまでの会話、最新の依頼を踏まえて改訂案を作成してください。

## 指示
- 最新の依頼に必要な変更だけを行い、無関係なシナリオは維持してください。
- 要求に反する変更は行わず、依頼を反映できない場合は理由を回答してください。
- replyには依頼への簡潔な回答、reasonには変更点と根拠を記述してください。
- scenariosには変更後の完全なシナリオ一覧を返してください。

# 要求
{requirements}

# 現在のシナリオ
{scenarios}

# 会話履歴
{history}

# 最新の依頼
{message}
"""


class ScenarioCreator:

    def __init__(self, requirements: str):
        self.requirements = requirements

    def create(self) -> Scenarios:
        prompt = CREATE_PROMPT.format(
            requirements=self.requirements,
            table_body=self._create_viewpoints_table()
        )

        client = Client('gpt-5.4')
        response = client.chat.create(
            prompt,
            response_format=Scenarios,
        )
        return Scenarios.model_validate(response.content)

    def refine(
        self,
        scenarios: list[dict],
        history: list[dict],
        message: str,
    ) -> ScenarioRefinement:
        prompt = REFINE_PROMPT.format(
            requirements=self.requirements,
            scenarios=json.dumps(scenarios, ensure_ascii=False, indent=2),
            history=json.dumps(history, ensure_ascii=False, indent=2),
            message=message,
        )
        response = Client('gpt-5.4').chat.create(
            prompt,
            response_format=ScenarioRefinement,
        )
        return ScenarioRefinement.model_validate(response.content)

    def _create_viewpoints_table(self) -> str:
        table_body = ""
        with open('src/static/vp_scenario.json', 'r', encoding='utf-8') as fp:
            vp_spec = json.load(fp)
            for category in vp_spec['categories']:
                id_prefix = f'{category["id"]}.'
                for i, vp in enumerate(category['checks'], start=1):
                    table_body += f'|{id_prefix}{i}|{vp}|\n'
        return table_body
