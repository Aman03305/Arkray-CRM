from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

GROUPS = ("leads", "opportunities", "tasks", "meetings", "notes")


def search_url(q: str | None = None, workspace: str = "me", **extra: str) -> str:
    params = {**({"q": q} if q is not None else {}), **extra}
    return f"/api/v1/workspaces/{workspace}/search" + (f"?{urlencode(params)}" if params else "")


def search(client: Any, q: str, workspace: str = "me") -> dict[str, Any]:
    response = client.get(search_url(q, workspace))
    assert response.status_code == 200, response.content
    body: dict[str, Any] = response.json()
    return body


def ids(body: dict[str, Any], group: str) -> list[str]:
    return [row["id"] for row in body[group]["results"]]


def found(body: dict[str, Any]) -> dict[str, list[str]]:
    """Every group's result ids (empty groups included)."""
    return {group: ids(body, group) for group in GROUPS}
