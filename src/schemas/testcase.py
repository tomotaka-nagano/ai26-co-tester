from __future__ import annotations
from pydantic import BaseModel, Field


class Testcases(BaseModel):

    items: list[Testcase]


class Assertion(BaseModel):

    field: str = Field(description='検証対象のフィールド名')
    op: str = Field(description='比較演算子 (==, !=, >, <, >=, <=, in_range)')
    expected: int | float | str | bool | list[float] = Field(description='期待値')


class Action(BaseModel):

    type: str = Field(description='アクション種別')
    value: int | float | None = Field(default=None, description='アクションの値 (該当する場合)')
    temperature: float | None = Field(default=None, description='温度指定 (該当する場合)')
    target: str | None = Field(default=None, description='故障注入対象 (該当する場合)')
    mode: str | None = Field(default=None, description='故障注入モード (該当する場合)')


class Condition(BaseModel):

    field: str = Field(description='監視するフィールド名')
    op: str = Field(description='比較演算子')
    value: int | float | str | bool | list[float] = Field(description='期待値')


class Trigger(BaseModel):

    type: str = Field(description='トリガー種別 (time または wait)')
    seconds: float | None = Field(default=None, description='発火する経過時間 (time トリガーの場合)')
    condition: Condition | None = Field(default=None, description='待機条件 (wait トリガーの場合)')


class Step(BaseModel):

    description: str = Field(description='ステップの説明')
    trigger: Trigger = Field(description='ステップのトリガー')
    actions: list[Action] = Field(default_factory=list, description='実行するアクションのリスト')
    assertions: list[Assertion] = Field(default_factory=list, description='検証するアサーションのリスト')


class Setup(BaseModel):

    water_ml: float = Field(default=0, description='初期水量 (ml)')
    water_temperature: float = Field(default=25.0, description='初期水温 (℃)')
    lid_closed: bool = Field(default=False, description='蓋の初期状態')


class Testcase(BaseModel):

    name: str = Field(description='テストケースの識別名')
    description: str = Field(default='', description='テストケースの説明')
    speed: int = Field(default=5, description='シミュレーション速度倍率')
    timeout: int = Field(default=30, description='タイムアウト秒数')
    setup: Setup = Field(default_factory=Setup, description='初期状態')
    steps: list[Step] = Field(description='テストステップの配列')
