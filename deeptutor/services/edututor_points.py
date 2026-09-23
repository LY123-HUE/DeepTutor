"""Client for the standalone EduTutor points service."""

from __future__ import annotations

import os
from typing import Any

import httpx


class PointsServiceError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class EduTutorPointsClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("EDUTUTOR_POINTS_SERVICE_URL", "http://127.0.0.1:8080")
        ).rstrip("/")
        self.timeout = timeout
        self.transport = transport

    async def get_points(self, access_token: str) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/points", access_token)

    async def checkin(self, access_token: str) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/checkin", access_token)

    async def get_rewards(self, access_token: str, limit: int = 50) -> dict[str, Any]:
        return await self._request(
            "GET", f"/api/v1/rewards?limit={max(1, min(limit, 100))}", access_token
        )

    async def _request(
        self, method: str, path: str, access_token: str
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self.transport
            ) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            raise PointsServiceError(502, "Points service is unavailable") from exc

        if response.status_code in {401, 403}:
            raise PointsServiceError(401, "Tokengine login has expired")
        if response.status_code >= 500:
            raise PointsServiceError(502, "Points service request failed")
        if not response.is_success:
            raise PointsServiceError(502, "Points service rejected the request")
        try:
            payload = response.json()
        except ValueError as exc:
            raise PointsServiceError(502, "Points service returned invalid data") from exc
        if not isinstance(payload, dict):
            raise PointsServiceError(502, "Points service returned invalid data")
        return payload


_client: EduTutorPointsClient | None = None


def get_points_client() -> EduTutorPointsClient:
    global _client
    if _client is None:
        _client = EduTutorPointsClient()
    return _client
