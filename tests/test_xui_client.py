from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from urllib.parse import parse_qs

import httpx
import pytest

from vpn_control_plane.data import NodeRecord
from vpn_control_plane.xui import XuiApiError, XuiAuthError, XuiNodeClient, XuiNodeEndpoint, decode_subscription_lines


def node(**overrides: object) -> NodeRecord:
    values: dict[str, object] = {
        "id": 1,
        "host": "panel.example.test",
        "port": 2053,
        "webBasePath": "secret-panel",
        "username": "admin",
        "password": "password",
        "scheme": "https",
    }
    values.update(overrides)
    return NodeRecord.model_validate(values)


def json_response(value: object, *, status_code: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status_code, json=value, headers=headers)


def inbound_payload(
    inbound_id: int = 1,
    protocol: str = "vless",
    clients: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "id": inbound_id,
        "protocol": protocol,
        "settings": json.dumps({"clients": clients or []}),
        "streamSettings": json.dumps({"network": "tcp"}),
        "sniffing": "{}",
    }


@pytest.mark.asyncio
async def test_builds_node_base_url_with_normalized_web_base_path() -> None:
    endpoint = XuiNodeEndpoint.from_node(node(webBasePath="panel"))

    assert endpoint.base_url == "https://panel.example.test:2053/panel/"


@pytest.mark.asyncio
async def test_login_posts_credentials_and_two_factor_code() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response({"success": True, "msg": "ok"}, headers={"set-cookie": "session=abc"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = XuiNodeClient(node(twoFactorCode="123456"), http_client=http_client)
        await client.login()

    form = parse_qs(requests[0].content.decode())
    assert str(requests[0].url) == "https://panel.example.test:2053/secret-panel/login/"
    assert form == {"username": ["admin"], "password": ["password"], "twoFactorCode": ["123456"]}


@pytest.mark.asyncio
async def test_login_follows_panel_redirect() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if len(paths) == 1:
            return httpx.Response(307, headers={"location": "/secret-panel/login"})
        return json_response({"success": True, "msg": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        await XuiNodeClient(node(), http_client=http_client).login()

    assert paths == ["/secret-panel/login/", "/secret-panel/login"]


@pytest.mark.asyncio
async def test_login_failure_raises_auth_error_without_password() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"success": False, "msg": "bad credentials"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = XuiNodeClient(node(), http_client=http_client)
        with pytest.raises(XuiAuthError) as error:
            await client.login()

    assert "bad credentials" in str(error.value)
    assert "password" not in str(error.value)


@pytest.mark.asyncio
async def test_list_inbounds_logs_in_and_parses_json_string_fields() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/login/"):
            return json_response({"success": True})
        return json_response({"success": True, "obj": [inbound_payload(clients=[{"email": "1_123"}])]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        inbounds = await XuiNodeClient(node(), http_client=http_client).list_inbounds()

    assert paths == ["/secret-panel/login/", "/secret-panel/panel/api/inbounds/list"]
    assert inbounds[0].settings == {"clients": [{"email": "1_123"}]}
    assert inbounds[0].stream_settings == {"network": "tcp"}


@pytest.mark.asyncio
async def test_relogs_in_and_retries_once_for_expired_session() -> None:
    responses: Iterator[httpx.Response] = iter(
        [
            json_response({"success": True}),
            json_response({"success": False, "msg": "please login"}),
            json_response({"success": True}),
            json_response({"success": True, "obj": []}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        inbounds = await XuiNodeClient(node(), http_client=http_client).list_inbounds()

    assert inbounds == []


@pytest.mark.asyncio
async def test_get_inbound_returns_none_when_api_reports_missing() -> None:
    responses: Iterator[httpx.Response] = iter([json_response({"success": True}), json_response({"success": False})])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        inbound = await XuiNodeClient(node(), http_client=http_client).get_inbound(404)

    assert inbound is None


@pytest.mark.asyncio
async def test_downloads_server_database_and_config_json_backups() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/login/"):
            return json_response({"success": True})
        if request.url.path.endswith("/getDb"):
            return httpx.Response(200, content=b"sqlite-db")
        return httpx.Response(200, content=b'{"xray":"config"}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = XuiNodeClient(node(), http_client=http_client)
        database = await client.get_database_backup()
        config = await client.get_config_json_backup()

    assert paths == [
        "/secret-panel/login/",
        "/secret-panel/panel/api/server/getDb",
        "/secret-panel/panel/api/server/getConfigJson",
    ]
    assert database == b"sqlite-db"
    assert config == b'{"xray":"config"}'


@pytest.mark.asyncio
async def test_update_geofiles_triggers_builtin_and_custom_updates() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return json_response({"success": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        await XuiNodeClient(node(), http_client=http_client).update_geofiles()

    assert requests == [
        ("POST", "/secret-panel/login/"),
        ("POST", "/secret-panel/panel/api/server/updateGeofile"),
        ("POST", "/secret-panel/panel/api/custom-geo/update-all"),
    ]


@pytest.mark.asyncio
async def test_update_geofiles_ignores_missing_custom_geo_endpoint() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/custom-geo/update-all"):
            return httpx.Response(404)
        return json_response({"success": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        await XuiNodeClient(node(), http_client=http_client).update_geofiles()

    assert paths == [
        "/secret-panel/login/",
        "/secret-panel/panel/api/server/updateGeofile",
        "/secret-panel/panel/api/custom-geo/update-all",
    ]


@pytest.mark.asyncio
async def test_update_geofiles_raises_when_builtin_update_fails() -> None:
    responses: Iterator[httpx.Response] = iter(
        [json_response({"success": True}), json_response({"success": False, "msg": "update failed"})]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(XuiApiError, match="update failed"):
            await XuiNodeClient(node(), http_client=http_client).update_geofiles()


@pytest.mark.asyncio
async def test_server_backup_download_raises_on_http_error() -> None:
    responses: Iterator[httpx.Response] = iter([json_response({"success": True}), httpx.Response(500)])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(Exception, match="HTTP 500"):
            await XuiNodeClient(node(), http_client=http_client).get_database_backup()


@pytest.mark.asyncio
async def test_add_client_treats_duplicate_email_as_idempotent_after_reread() -> None:
    existing = {"email": "1_123", "id": "existing-uuid", "subId": "legacy-sub"}
    responses: Iterator[httpx.Response] = iter(
        [
            json_response({"success": True}),
            json_response({"success": False, "msg": "Duplicate email"}),
            json_response({"success": True, "obj": inbound_payload(clients=[dict(existing)])}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await XuiNodeClient(node(), http_client=http_client).add_client(1, {"email": "1_123"})

    assert result.created is False
    assert result.client == existing


@pytest.mark.asyncio
async def test_decode_subscription_lines_accepts_base64_and_plain_text() -> None:
    encoded = base64.b64encode(b"vless://one#One\ntrojan://two#Two\n").decode()

    assert decode_subscription_lines(encoded) == ["vless://one#One", "trojan://two#Two"]
    assert decode_subscription_lines("vless://plain#Plain\n") == ["vless://plain#Plain"]


