from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, model_validator


class VerificationStatus(StrEnum):
    ACCEPTED = 'accepted'
    GENERATING_SCENARIOS = 'generating_scenarios'
    REVIEWING_SCENARIOS = 'reviewing_scenarios'
    GENERATING_TESTCASES = 'generating_testcases'
    AWAITING_APPROVAL = 'awaiting_approval'
    UPLOADING_PRODUCT = 'uploading_product'
    EXECUTING_TESTS = 'executing_tests'
    ANALYZING_RESULTS = 'analyzing_results'
    REPAIRING_TESTCASES = 'repairing_testcases'
    AWAITING_RESULT_APPROVAL = 'awaiting_result_approval'
    COMPLETED = 'completed'
    COMPLETED_OK = 'completed_ok'
    COMPLETED_NG = 'completed_ng'
    ERROR = 'error'
    CANCELED = 'canceled'


class Verdict(StrEnum):
    PASS = 'Pass'
    FAIL = 'Fail'


class FailureClassification(StrEnum):
    TESTCASE_DEFECT = 'testcase_defect'
    PRODUCT_OR_SPEC_DEFECT = 'product_or_spec_defect'
    ENVIRONMENT_DEFECT = 'environment_defect'
    UNKNOWN = 'unknown'


class ArtifactReference(BaseModel):
    path: str | None = None
    version: str | None = None
    sha256: str | None = Field(default=None, pattern=r'^[0-9a-fA-F]{64}$')

    @model_validator(mode='after')
    def validate_locator(self) -> ArtifactReference:
        if not self.path and not self.version:
            raise ValueError('path または version のどちらかが必要です')
        return self


class SourceDocumentType(StrEnum):
    PRODUCT_SPEC = 'product_spec'
    REQUIREMENT_SPEC = 'requirement_spec'
    CHANGE_SPEC = 'change_spec'
    OTHER = 'other'


class SourceDocument(BaseModel):
    type: SourceDocumentType
    artifact: ArtifactReference
    title: str | None = None


class VerificationCreate(BaseModel):
    workflow_step_id: str = Field(min_length=1)
    requirements: ArtifactReference | None = None
    design: ArtifactReference | None = None
    source_documents: list[SourceDocument] = Field(default_factory=list)
    interface_spec: ArtifactReference | None = None
    product: ArtifactReference
    test_environment_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    callback_url: HttpUrl | None = None
    require_approval: bool = False
    review_scenarios: bool = False
    require_result_approval: bool = False
    max_repair_rounds: int = Field(default=0, ge=0, le=10)

    @model_validator(mode='after')
    def validate_inputs(self) -> VerificationCreate:
        if not self.requirements and not self.source_documents:
            raise ValueError('requirements または source_documents が必要です')
        if not self.design and not self.interface_spec:
            raise ValueError('design または interface_spec が必要です')
        return self


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


class ScenarioProposalStatus(StrEnum):
    PENDING = 'pending'
    ACCEPTED = 'accepted'
    REJECTED = 'rejected'


class ScenarioRefineRequest(BaseModel):
    message: str = Field(min_length=1)
    base_revision: int = Field(ge=1)


class ScenarioMessage(BaseModel):
    role: str
    content: str
    created_at: datetime


class ScenarioChange(BaseModel):
    operation: str
    index: int
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


class ScenarioProposal(BaseModel):
    proposal_id: str
    base_revision: int
    reply: str
    reason: str
    scenarios: list[dict[str, Any]]
    changes: list[ScenarioChange] = Field(default_factory=list)
    status: ScenarioProposalStatus
    created_at: datetime


class ScenarioReview(BaseModel):
    verification_id: str
    revision: int
    scenarios: list[dict[str, Any]]
    messages: list[ScenarioMessage] = Field(default_factory=list)
    proposals: list[ScenarioProposal] = Field(default_factory=list)


class VerificationState(VerificationAccepted):
    workflow_step_id: str
    progress: Progress
    remote_status: str | None = None
    remote_phase: str | None = None
    agent_round: int = 0
    max_rounds: int = 1
    repair_count: int = 0
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


class ResultAnalysis(BaseModel):
    classification: FailureClassification
    summary: str
    facts: list[str] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    affected_testcases: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    recommendation: str


class VerificationResult(BaseModel):
    verification_id: str
    verdict: Verdict
    report_path: str
    counts: TestCounts


class ResultApprovalAction(StrEnum):
    RESTART_FROM_SCENARIOS = 'restart_from_scenarios'
    COMPLETE_NG = 'complete_ng'
    COMPLETE_OK = 'complete_ok'


class ResultApprovalRequest(BaseModel):
    action: ResultApprovalAction
    reason: str | None = None


class ResultApproval(BaseModel):
    action: ResultApprovalAction
    reason: str | None = None
    final_verdict: Verdict | None = None
    created_at: datetime


class ArtifactBundle(BaseModel):
    verification_id: str
    scenarios_path: str | None = None
    scenarios: list[dict[str, Any]] = Field(default_factory=list)
    testcases_path: str
    testcases: list[dict[str, Any]]
    execution_log_path: str | None = None
    execution_log: str | None = None
    execution_log_paths: list[str] = Field(default_factory=list)
    result_analysis_log_path: str | None = None
    result_analyses: list[ResultAnalysis] = Field(default_factory=list)
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
