from __future__ import annotations

import pytest

from arkray.pipeline.models import Pipeline, Stage
from tests.helpers import signed_in


def opportunities_url(workspace: str = "me", suffix: str = "") -> str:
    return f"/api/v1/workspaces/{workspace}/opportunities{suffix}"


def opportunity_url(opportunity_id, workspace: str = "me", action: str = "") -> str:
    base = f"/api/v1/workspaces/{workspace}/opportunities/{opportunity_id}"
    return f"{base}/{action}" if action else base


def board_url(workspace: str = "me") -> str:
    return f"/api/v1/workspaces/{workspace}/pipeline-board"


def summary_url(workspace: str = "me") -> str:
    return f"/api/v1/workspaces/{workspace}/pipeline-summary"


def convert_url(lead_id, workspace: str = "me") -> str:
    return f"/api/v1/workspaces/{workspace}/leads/{lead_id}/convert"


@pytest.fixture
def pipeline(db) -> Pipeline:
    return Pipeline.objects.get(is_default=True)


@pytest.fixture
def stages(pipeline) -> dict[str, Stage]:
    """The default pipeline's stages by key: new, qualified, proposal, negotiation, won,
    lost."""
    return {stage.key: stage for stage in pipeline.stages.all()}


@pytest.fixture
def user_b_client(user_b):
    return signed_in(user_b)
