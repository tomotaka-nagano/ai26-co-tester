from __future__ import annotations
from pydantic import BaseModel, Field


class Scenarios(BaseModel):

    items: list[Scenario]


class Scenario(BaseModel):

    summary: str = Field(description='テストシナリオの概要 (事前に設定する条件、操作)')
    viewpoints: list[str] = Field(description='テストシナリオにおいて検証する観点のリスト')


class ScenarioRefinement(BaseModel):

    reply: str = Field(description='ユーザーへの回答')
    reason: str = Field(description='変更案の理由と影響')
    scenarios: list[Scenario] = Field(description='変更後のシナリオ案')
