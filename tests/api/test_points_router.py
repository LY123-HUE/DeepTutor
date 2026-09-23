from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx

from deeptutor.api.routers import points
from deeptutor.api.routers.auth import require_auth
from deeptutor.services.edubuddy_points import (
    EduBuddyPointsClient,
    PointsServiceError,
)


class StubPointsClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int | None]] = []

    async def get_points(self, access_token: str) -> dict:
        self.calls.append(("summary", access_token, None))
        return {"checked_in_today": False}

    async def checkin(self, access_token: str) -> dict:
        self.calls.append(("checkin", access_token, None))
        return {"already_checked_in": False}

    async def get_rewards(self, access_token: str, limit: int = 50) -> dict:
        self.calls.append(("rewards", access_token, limit))
        return {"items": []}


def _client(stub: StubPointsClient) -> TestClient:
    app = FastAPI()
    app.include_router(points.router, prefix="/api/points")
    app.dependency_overrides[require_auth] = lambda: None
    app.dependency_overrides[points.get_points_client] = lambda: stub
    return TestClient(app)


def test_points_endpoints_forward_native_tokengine_access_token() -> None:
    stub = StubPointsClient()
    with _client(stub) as client:
        summary = client.get(
            "/api/points/summary", headers={"X-Tokengine-Access-Token": "tokengine-access"}
        )
        checkin = client.post(
            "/api/points/checkin", headers={"X-Tokengine-Access-Token": "tokengine-access"}
        )
        rewards = client.get(
            "/api/points/rewards?limit=12",
            headers={"X-Tokengine-Access-Token": "tokengine-access"},
        )

    assert summary.status_code == 200
    assert checkin.status_code == 200
    assert rewards.status_code == 200
    assert stub.calls == [
        ("summary", "tokengine-access", None),
        ("checkin", "tokengine-access", None),
        ("rewards", "tokengine-access", 12),
    ]


def test_points_endpoints_require_tokengine_token() -> None:
    with _client(StubPointsClient()) as client:
        response = client.get("/api/points/summary")

    assert response.status_code == 401


def test_points_service_auth_failure_is_returned_as_401() -> None:
    class ExpiredTokenClient(StubPointsClient):
        async def get_points(self, access_token: str) -> dict:
            raise PointsServiceError(401, "Tokengine login has expired")

    with _client(ExpiredTokenClient()) as client:
        response = client.get(
            "/api/points/summary", headers={"X-Tokengine-Access-Token": "expired"}
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Tokengine login has expired"


def test_client_sends_bearer_token_to_points_service() -> None:
    captured: dict[str, str] = {}

    async def handler(request):
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        return httpx.Response(200, json={"items": []})

    client = EduBuddyPointsClient(
        "http://points.test", transport=httpx.MockTransport(handler)
    )
    result = asyncio.run(client.get_rewards("tokengine-access", 7))
    assert result == {"items": []}
    assert captured == {
        "url": "http://points.test/api/v1/rewards?limit=7",
        "authorization": "Bearer tokengine-access",
    }
