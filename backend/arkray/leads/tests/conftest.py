from __future__ import annotations

import pytest

from tests.helpers import signed_in


def leads_url(workspace: str = "me", suffix: str = "") -> str:
    return f"/api/v1/workspaces/{workspace}/leads{suffix}"


def lead_url(lead_id, workspace: str = "me", action: str = "") -> str:
    return f"/api/v1/workspaces/{workspace}/leads/{lead_id}{'/' + action if action else ''}"


@pytest.fixture
def user_b_client(user_b):
    return signed_in(user_b)
