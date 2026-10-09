import json
from exmgai import Client
from src.schemas.scenario import Scenarios


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

    def refine(self):
        pass

    def _create_viewpoints_table(self) -> str:
        table_body = ""
        with open('src/static/vp_scenario.json', 'r', encoding='utf-8') as fp:
            vp_spec = json.load(fp)
            for category in vp_spec['categories']:
                id_prefix = f'{category["id"]}.'
                for i, vp in enumerate(category['checks'], start=1):
                    table_body += f'|{id_prefix}{i}|{vp}|\n'
        return table_body
