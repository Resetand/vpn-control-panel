from __future__ import annotations

import secrets

from fastapi import APIRouter, Header, HTTPException, Response, status
from fastapi.responses import RedirectResponse

from vpn_control_plane.backup import DATA_BACKUP_FILE_NAME, build_data_backup
from vpn_control_plane.config import Settings
from vpn_control_plane.data import ControlPlaneStore
from vpn_control_plane.subscription import (
    SubscriptionService,
    UnknownSubscriptionClientError,
    render_subscription_by_accept,
)


def create_router(settings: Settings, store: ControlPlaneStore) -> APIRouter:
    router = APIRouter()
    subscription_service = SubscriptionService(
        store,
        public_base_url=settings.public_subscription_base_url,
    )

    @router.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/backup")
    async def backup(authorization: str | None = Header(default=None)) -> Response:
        _authorize_backup(settings, authorization)
        return Response(
            content=build_data_backup(store.data_file),
            media_type="application/gzip",
            headers={"content-disposition": f'attachment; filename="{DATA_BACKUP_FILE_NAME}"'},
        )

    @router.get(f"{settings.subscription_route}{{sub_id:path}}")
    async def subscription(sub_id: str, accept: str | None = Header(default=None)) -> Response:
        try:
            built_subscription = await subscription_service.build(sub_id)
        except UnknownSubscriptionClientError as exc:
            raise HTTPException(status_code=404, detail="subscription not found") from exc
        if sub_id.strip().strip("/") != built_subscription.client.effective_sub_id:
            return RedirectResponse(built_subscription.public_url, status_code=status.HTTP_302_FOUND)
        return render_subscription_by_accept(built_subscription, accept)

    return router


def _authorize_backup(settings: Settings, authorization: str | None) -> None:
    if settings.backup_http_token is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="backup token is not configured")
    scheme, separator, token = (authorization or "").partition(" ")
    expected_token = settings.backup_http_token.get_secret_value()
    if separator != " " or scheme.lower() != "bearer" or not secrets.compare_digest(token, expected_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid backup token")
