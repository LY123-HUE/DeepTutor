"""Authenticated EduBuddy points endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from deeptutor.api.routers.auth import TokenPayload, require_auth
from deeptutor.services.edubuddy_points import (
    EduBuddyPointsClient,
    PointsServiceError,
    get_points_client,
)

router = APIRouter()


def _client_error(exc: PointsServiceError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _require_tokengine_access_token(value: str | None) -> str:
    token = str(value or "").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Tokengine OAuth access token is required",
        )
    return token


def _access_token_from_request(value: str | None) -> str:
    # The desktop WebView obtains this from its native bridge and forwards it
    # per request; browser storage and the EduBuddy session JWT are not sources.
    return _require_tokengine_access_token(value)


@router.get("/summary")
async def get_summary(
    x_tokengine_access_token: str | None = Header(default=None),
    _: TokenPayload | None = Depends(require_auth),
    client: EduBuddyPointsClient = Depends(get_points_client),
) -> dict:
    token = _access_token_from_request(x_tokengine_access_token)
    try:
        return await client.get_points(token)
    except PointsServiceError as exc:
        raise _client_error(exc) from exc


@router.post("/checkin")
async def do_checkin(
    x_tokengine_access_token: str | None = Header(default=None),
    _: TokenPayload | None = Depends(require_auth),
    client: EduBuddyPointsClient = Depends(get_points_client),
) -> dict:
    token = _access_token_from_request(x_tokengine_access_token)
    try:
        return await client.checkin(token)
    except PointsServiceError as exc:
        raise _client_error(exc) from exc


@router.get("/rewards")
async def get_rewards(
    limit: int = Query(default=50, ge=1, le=100),
    x_tokengine_access_token: str | None = Header(default=None),
    _: TokenPayload | None = Depends(require_auth),
    client: EduBuddyPointsClient = Depends(get_points_client),
) -> dict:
    token = _access_token_from_request(x_tokengine_access_token)
    try:
        return await client.get_rewards(token, limit)
    except PointsServiceError as exc:
        raise _client_error(exc) from exc
