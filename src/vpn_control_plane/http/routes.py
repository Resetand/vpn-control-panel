from __future__ import annotations

import secrets

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from vpn_control_plane.backup import DATA_BACKUP_FILE_NAME, build_data_backup
from vpn_control_plane.config import Settings
from vpn_control_plane.data import ControlPlaneStore
from vpn_control_plane.subscription import (
    BuiltSubscription,
    SubscriptionService,
    UnknownSubscriptionClientError,
    render_subscription_by_accept,
)

NEW_URL_HEADER = "new-url"
PROVIDER_ID_HEADER = "providerid"


def create_router(settings: Settings, store: ControlPlaneStore) -> APIRouter:
    router = APIRouter()
    subscription_service = _build_subscription_service(settings, store)
    legacy_routes = _legacy_subscription_routes(settings)

    @router.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/backup")
    async def backup(authorization: str | None = Header(default=None)) -> Response:
        _authorize_backup(settings, authorization)
        return _backup_response(store)

    async def subscription(request: Request, sub_id: str, accept: str | None = Header(default=None)) -> Response:
        built_subscription = await _build_subscription_or_404(subscription_service, sub_id)
        should_update_url = _should_update_subscription_url(
            request.url.path,
            sub_id,
            built_subscription,
            subscription_service,
            legacy_routes,
        )
        if should_update_url and _accepts_html(accept):
            return RedirectResponse(built_subscription.public_url, status_code=status.HTTP_302_FOUND)

        response = render_subscription_by_accept(built_subscription, accept)
        _attach_subscription_headers(
            response,
            provider_id=built_subscription.metadata.happ_provider_id,
            new_url=built_subscription.public_url if should_update_url else None,
        )
        return response

    for route in _subscription_routes(settings):
        router.add_api_route(f"{route}{{sub_id:path}}", subscription, methods=["GET"])
    return router


def _build_subscription_service(settings: Settings, store: ControlPlaneStore) -> SubscriptionService:
    token_salt = settings.subscription_token_salt.get_secret_value() if settings.subscription_token_salt else None
    return SubscriptionService(
        store,
        public_base_url=settings.public_subscription_base_url,
        token_salt=token_salt,
    )


def _backup_response(store: ControlPlaneStore) -> Response:
    return Response(
        content=build_data_backup(store.data_file),
        media_type="application/gzip",
        headers={"content-disposition": f'attachment; filename="{DATA_BACKUP_FILE_NAME}"'},
    )


def _attach_subscription_headers(
    response: Response,
    *,
    provider_id: str | None,
    new_url: str | None,
) -> None:
    if provider_id:
        response.headers[PROVIDER_ID_HEADER] = provider_id
    if new_url:
        response.headers[NEW_URL_HEADER] = new_url


async def _build_subscription_or_404(
    subscription_service: SubscriptionService,
    sub_id: str,
) -> BuiltSubscription:
    try:
        return await subscription_service.build(sub_id)
    except UnknownSubscriptionClientError as exc:
        raise HTTPException(status_code=404, detail="subscription not found") from exc


def _should_update_subscription_url(
    path: str,
    requested_sub_id: str,
    subscription: BuiltSubscription,
    subscription_service: SubscriptionService,
    legacy_routes: tuple[str, ...],
) -> bool:
    return _is_legacy_subscription_path(path, legacy_routes) or not subscription_service.is_public_token_for_client(
        requested_sub_id,
        subscription.client,
    )


def _is_legacy_subscription_path(path: str, legacy_routes: tuple[str, ...]) -> bool:
    return any(path.startswith(route) for route in legacy_routes)


def _subscription_routes(settings: Settings) -> list[str]:
    routes = dict.fromkeys([settings.subscription_route, *settings.subscription_legacy_routes])
    return sorted(routes, key=len, reverse=True)


def _legacy_subscription_routes(settings: Settings) -> tuple[str, ...]:
    return tuple(route for route in settings.subscription_legacy_routes if route != settings.subscription_route)


def _accepts_html(accept: str | None) -> bool:
    if not accept:
        return False
    return any(item.split(";", 1)[0].strip() == "text/html" for item in accept.lower().split(","))


def _authorize_backup(settings: Settings, authorization: str | None) -> None:
    if settings.backup_http_token is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="backup token is not configured")
    scheme, separator, token = (authorization or "").partition(" ")
    expected_token = settings.backup_http_token.get_secret_value()
    if separator != " " or scheme.lower() != "bearer" or not secrets.compare_digest(token, expected_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid backup token")
