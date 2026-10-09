from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, model_validator


class VerificationStatus(StrEnum):
    ACCEPTED = 'accepted'
    GENERATING_TESTCASES = 'generating_testcases'
    AWAITING_APPROVAL = 'awaiting_approval'
    UPLOADING_PRODUCT = 'uploading_product'
    EXECUTING_TESTS = 'executing_tests'
    ANALYZING_RESULTS = 'analyzing_results'
    COMPLETED = 'completed'
    ERROR = 'error'
    CANCELED = 'canceled'


class Verdict(StrEnum):
    PASS = 'Pass'
    FAIL = 'Fail'


class ArtifactReference(BaseModel):
    path: str | None = None
    version: str | None = None
    sha256: str | None = Field(default=None, pattern=r'^[0-9a-fA-F]{64}$')

    @model_validator(mode='after')
    def validate_locator(self) -> ArtifactReference:
        if not self.path and not self.version:
            raise ValueError('path または version のどちらかが必要です')
        return self


class VerificationCreate(BaseModel):
    workflow_step_id: str = Field(min_length=1)
    requirements: ArtifactReference
    design: ArtifactReference
    product: ArtifactReference
    test_environment_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    callback_url: HttpUrl | None = None
    require_approval: bool = False


class VerificationRerun(BaseModel):
    product: ArtifactReference
    idempotency_key: str = Field(min_length=1, max_length=200)
    callback_url: HttpUrl | None = None


class Progress(BaseModel):
    total: int = 0
    completed: int = 0


class VerificationAccepted(BaseModel):
    verification_id: str
    status: VerificationStatus


class VerificationState(VerificationAccepted):
    workflow_step_id: str
    progress: Progress
    remote_status: str | None = None
    remote_phase: str | None = None
    agent_round: int = 0
    max_rounds: int = 1
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class TestCounts(BaseModel):
    passed: int = 0
    failed: int = 0
    unable: int = 0


class AgentDecision(BaseModel):
    continue_verification: bool
    reason: str
    testcases: list[dict[str, Any]] = Field(default_factory=list)


class VerificationResult(BaseModel):
    verification_id: str
    verdict: Verdict
    report_path: str
    counts: TestCounts


class ArtifactBundle(BaseModel):
    verification_id: str
    testcases_path: str
    testcases: list[dict[str, Any]]
    execution_log_path: str | None = None
    execution_log: str | None = None
    llm_judgment_log_path: str
    llm_judgments: list[dict[str, Any]]
    agent_decision_log_path: str | None = None
    agent_decisions: list[dict[str, Any]] = Field(default_factory=list)


class TestEnvironmentState(StrEnum):
    AVAILABLE = 'available'
    IN_USE = 'in_use'
    UNAVAILABLE = 'unavailable'


class TestEnvironment(BaseModel):
    id: str
    name: str
    state: TestEnvironmentState


class Health(BaseModel):
    status: str = 'ok'
