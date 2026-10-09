from __future__ import annotations
from pydantic import BaseModel, Field


class Requirements(BaseModel):

    items: list[Requirement]


class Requirement(BaseModel):

    text: str = Field(description='要件の記述')
    sub_requirements: list[Requirement] = Field(description='下位の要件リスト')
