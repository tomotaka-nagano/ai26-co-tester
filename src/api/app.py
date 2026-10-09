import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.staticfiles import StaticFiles

from src.api.execution import (
    ArtifactResolver,
    CommandTestRunner,
    EnvironmentRegistry,
    GenerationPipeline,
)
from src.api.ai_gen_test import AgenticTestRunner
from src.api.models import (
    ArtifactBundle,
    Health,
    ResultApprovalRequest,
    ScenarioProposal,
    ScenarioRefineRequest,
    ScenarioReview,
    TestEnvironment,
    VerificationAccepted,
    VerificationCreate,
    VerificationResult,
    VerificationRerun,
    VerificationState,
)
from src.api.repository import VerificationRepository
from src.api.service import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    VerificationService,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def create_service() -> VerificationService:
    data_root = Path(os.getenv('CO_TEST_DRIVER_DATA_DIR', PROJECT_ROOT / 'data'))
    artifact_root = Path(os.getenv('CO_TEST_DRIVER_ARTIFACT_ROOT', PROJECT_ROOT))
    environment_path = Path(
        os.getenv(
            'CO_TEST_DRIVER_ENVIRONMENTS', PROJECT_ROOT / 'test-environments.toml'
        )
    )
    repository = VerificationRepository(data_root / 'verifications.db')
    resolver = ArtifactResolver(artifact_root)
    environments = EnvironmentRegistry(environment_path, repository)
    return VerificationService(
        repository,
        GenerationPipeline(resolver),
        CommandTestRunner(resolver),
        environments,
        data_root / 'verifications',
        AgenticTestRunner(resolver),
    )


def create_app(service: VerificationService | None = None) -> FastAPI:
    api = FastAPI(title='Co-Test Driver API', version='0.1.0')
    api.state.verifications = service or create_service()
    api.router.add_event_handler('shutdown', api.state.verifications.close)
    _register_routes(api)
    _mount_web_ui(api)
    return api


def _mount_web_ui(api: FastAPI) -> None:
    web_root = PROJECT_ROOT / 'web' / 'dist'
    if web_root.is_dir():
        api.mount('/ui', StaticFiles(directory=web_root, html=True), name='ui')


def _service(api: FastAPI) -> VerificationService:
    return api.state.verifications


def _register_routes(api: FastAPI) -> None:

    @api.get('/health', response_model=Health)
    def health() -> Health:
        return Health()

    @api.get('/test-environments', response_model=list[TestEnvironment])
    def test_environments() -> list[TestEnvironment]:
        return _service(api).environments.list()

    @api.post(
        '/verifications',
        response_model=VerificationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_verification(request: VerificationCreate) -> VerificationAccepted:
        return _call(_service(api).create, request)

    @api.get('/verifications/{verification_id}', response_model=VerificationState)
    def get_verification(verification_id: str) -> VerificationState:
        return _call(_service(api).get, verification_id)

    @api.get(
        '/verifications/{verification_id}/result', response_model=VerificationResult
    )
    def get_result(verification_id: str) -> VerificationResult:
        return _call(_service(api).result, verification_id)

    @api.get(
        '/verifications/{verification_id}/artifacts', response_model=ArtifactBundle
    )
    def get_artifacts(verification_id: str) -> ArtifactBundle:
        return _call(_service(api).artifacts, verification_id)

    @api.get(
        '/verifications/{verification_id}/scenarios', response_model=ScenarioReview
    )
    def get_scenarios(verification_id: str) -> ScenarioReview:
        return _call(_service(api).scenarios, verification_id)

    @api.post(
        '/verifications/{verification_id}/scenarios/messages',
        response_model=ScenarioProposal,
    )
    def refine_scenarios(
        verification_id: str, request: ScenarioRefineRequest
    ) -> ScenarioProposal:
        return _call(_service(api).refine_scenarios, verification_id, request)

    @api.post(
        '/verifications/{verification_id}/scenarios/proposals/{proposal_id}/accept',
        response_model=ScenarioReview,
    )
    def accept_scenario_proposal(
        verification_id: str, proposal_id: str
    ) -> ScenarioReview:
        return _call(
            _service(api).accept_scenario_proposal, verification_id, proposal_id
        )

    @api.post(
        '/verifications/{verification_id}/scenarios/proposals/{proposal_id}/reject',
        response_model=ScenarioProposal,
    )
    def reject_scenario_proposal(
        verification_id: str, proposal_id: str
    ) -> ScenarioProposal:
        return _call(
            _service(api).reject_scenario_proposal, verification_id, proposal_id
        )

    @api.post(
        '/verifications/{verification_id}/cancel',
        response_model=VerificationState,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def cancel_verification(verification_id: str) -> VerificationState:
        return _call(_service(api).cancel, verification_id)

    @api.post(
        '/verifications/{verification_id}/approve',
        response_model=VerificationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def approve_verification(verification_id: str) -> VerificationAccepted:
        return _call(_service(api).approve, verification_id)

    @api.post(
        '/verifications/{verification_id}/scenarios/approve',
        response_model=VerificationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def approve_scenarios(verification_id: str) -> VerificationAccepted:
        return _call(_service(api).approve_scenarios, verification_id)

    @api.post(
        '/verifications/{verification_id}/result-approval',
        response_model=VerificationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def approve_result(
        verification_id: str, request: ResultApprovalRequest
    ) -> VerificationAccepted:
        return _call(_service(api).approve_result, verification_id, request)

    @api.post(
        '/verifications/{verification_id}/rerun',
        response_model=VerificationAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def rerun_verification(
        verification_id: str, request: VerificationRerun
    ) -> VerificationAccepted:
        return _call(_service(api).rerun, verification_id, request)


def _call(function, *args):
    try:
        return function(*args)
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except InvalidRequestError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


app = create_app()
