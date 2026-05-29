from __future__ import annotations

import base64
import json
from collections.abc import Iterator

import httpx
import pytest

from vpn_control_plane.data import NodeRecord
from vpn_control_plane.provisioning import client_email
from vpn_control_plane.xui import XuiApiError, XuiClientInfo, XuiNodeClient, XuiNodeEndpoint, decode_subscription_lines


def node(**overrides: object) -> NodeRecord:
    values: dict[str, object] = {
        "id": 1,
        "host": "panel.example.test",
        "port": 2053,
        "basePath": "secret-panel",
        "apiToken": "token-123",
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


def status_payload(
    *,
    cpu: float = 3.0,
    mem_current: int = 50,
    mem_total: int = 200,
    xray_state: str = "running",
) -> dict[str, object]:
    return {
        "cpu": cpu,
        "mem": {"current": mem_current, "total": mem_total},
        "xray": {"state": xray_state, "errorMsg": "", "version": "26.4.25"},
    }


@pytest.mark.asyncio
async def test_builds_node_base_url_with_normalized_base_path() -> None:
    endpoint = XuiNodeEndpoint.from_node(node(basePath="panel"))

    assert endpoint.base_url == "https://panel.example.test:2053/panel/"


@pytest.mark.asyncio
async def test_authorizes_api_requests_with_node_api_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response({"success": True, "obj": [inbound_payload()]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        inbounds = await XuiNodeClient(node(apiToken="token-456"), http_client=http_client).list_inbounds()

    assert [request.url.path for request in requests] == ["/secret-panel/panel/api/inbounds/list"]
    assert requests[0].headers["authorization"] == "Bearer token-456"
    assert inbounds[0].id == 1


@pytest.mark.asyncio
async def test_api_auth_error_is_not_retried_with_login() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response({"success": False, "msg": "not found"}, status_code=404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(XuiApiError, match="HTTP 404"):
            await XuiNodeClient(node(apiToken="wrong-token"), http_client=http_client).list_inbounds()

    assert [request.url.path for request in requests] == ["/secret-panel/panel/api/inbounds/list"]


@pytest.mark.asyncio
async def test_list_inbounds_logs_in_and_parses_json_string_fields() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return json_response({"success": True, "obj": [inbound_payload(clients=[{"email": "123"}])]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        inbounds = await XuiNodeClient(node(), http_client=http_client).list_inbounds()

    assert paths == ["/secret-panel/panel/api/inbounds/list"]
    assert inbounds[0].settings == {"clients": [{"email": client_email("123")}]}
    assert inbounds[0].stream_settings == {"network": "tcp"}


@pytest.mark.asyncio
async def test_get_inbound_returns_none_when_api_reports_missing() -> None:
    responses: Iterator[httpx.Response] = iter([json_response({"success": False})])

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
        if request.url.path.endswith("/getDb"):
            return httpx.Response(200, content=b"sqlite-db")
        return httpx.Response(200, content=b'{"xray":"config"}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = XuiNodeClient(node(), http_client=http_client)
        database = await client.get_database_backup()
        config = await client.get_config_json_backup()

    assert paths == [
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
        "/secret-panel/panel/api/server/updateGeofile",
        "/secret-panel/panel/api/custom-geo/update-all",
    ]


@pytest.mark.asyncio
async def test_update_geofiles_raises_when_builtin_update_fails() -> None:
    responses: Iterator[httpx.Response] = iter(
        [
            json_response({"success": False, "msg": "update failed"}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(XuiApiError, match="update failed"):
            await XuiNodeClient(node(), http_client=http_client).update_geofiles()


@pytest.mark.asyncio
async def test_get_status_parses_cpu_memory_and_xray_state() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response({"success": True, "obj": status_payload(cpu=12.5, xray_state="running")})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        status = await XuiNodeClient(node(), http_client=http_client).get_status()

    assert [request.url.path for request in requests] == ["/secret-panel/panel/api/server/status"]
    assert requests[0].headers["authorization"] == "Bearer token-123"
    assert status.cpu_percent == 12.5
    assert status.memory.current == 50
    assert status.memory.total == 200
    assert status.memory.usage_percent == 25.0
    assert status.xray.state == "running"


@pytest.mark.asyncio
async def test_get_status_raises_on_failed_status_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return json_response({"success": False, "msg": "status failed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(XuiApiError, match="node 1: status failed"):
            await XuiNodeClient(node(), http_client=http_client).get_status()


@pytest.mark.asyncio
async def test_get_status_raises_on_malformed_status_payload() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return json_response({"success": True, "obj": {"cpu": 1, "mem": {}, "xray": {"state": "running"}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(XuiApiError, match="malformed payload for node 1"):
            await XuiNodeClient(node(), http_client=http_client).get_status()


@pytest.mark.asyncio
async def test_server_backup_download_raises_on_http_error() -> None:
    responses: Iterator[httpx.Response] = iter([httpx.Response(500)])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(Exception, match="HTTP 500"):
            await XuiNodeClient(node(), http_client=http_client).get_database_backup()


@pytest.mark.asyncio
async def test_get_client_returns_client_info_with_inbound_ids() -> None:
    email = client_email("123")
    client_obj = {"email": email, "uuid": "some-uuid", "subId": "abc", "flow": "xtls-rprx-vision"}
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response({"success": True, "obj": {"client": client_obj, "inboundIds": [1, 3]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        info = await XuiNodeClient(node(), http_client=http_client).get_client(email)

    assert requests[0].url.path == f"/secret-panel/panel/api/clients/get/{email}"
    assert isinstance(info, XuiClientInfo)
    assert info.client["uuid"] == "some-uuid"
    assert info.inbound_ids == [1, 3]


@pytest.mark.asyncio
async def test_get_client_returns_none_when_not_found() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        info = await XuiNodeClient(node(), http_client=http_client).get_client("missing@example.test")

    assert info is None


@pytest.mark.asyncio
async def test_add_client_posts_json_payload_with_inbound_ids() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response({"success": True, "msg": "ok"})

    client_payload = {"email": client_email("123"), "flow": "xtls-rprx-vision", "subId": "abc"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        await XuiNodeClient(node(), http_client=http_client).add_client(client_payload, [1, 2])

    assert requests[0].url.path == "/secret-panel/panel/api/clients/add"
    assert requests[0].method == "POST"
    body = json.loads(requests[0].content)
    assert body == {"client": client_payload, "inboundIds": [1, 2]}


@pytest.mark.asyncio
async def test_add_client_raises_on_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return json_response({"success": False, "msg": "email already in use"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(XuiApiError, match="email already in use"):
            await XuiNodeClient(node(), http_client=http_client).add_client({"email": "x"}, [1])


@pytest.mark.asyncio
async def test_attach_client_posts_inbound_ids_to_email_route() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response({"success": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        await XuiNodeClient(node(), http_client=http_client).attach_client("alice", [3, 4])

    assert requests[0].url.path == "/secret-panel/panel/api/clients/alice/attach"
    assert requests[0].method == "POST"
    assert json.loads(requests[0].content) == {"inboundIds": [3, 4]}


@pytest.mark.asyncio
async def test_list_clients_parses_flat_rows_stripping_inbound_ids_and_traffic() -> None:
    rows = [
        {
            "email": client_email("123"),
            "subId": "abc",
            "comment": "Alice",
            "tgId": 123,
            "inboundIds": [1, 3],
            "traffic": {"up": 10, "down": 20},
        },
        {"missing-email": True},
        "not-a-dict",
    ]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response({"success": True, "obj": rows})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        clients = await XuiNodeClient(node(), http_client=http_client).list_clients()

    assert requests[0].url.path == "/secret-panel/panel/api/clients/list"
    assert len(clients) == 1
    assert clients[0].inbound_ids == [1, 3]
    assert clients[0].client == {"email": client_email("123"), "subId": "abc", "comment": "Alice", "tgId": 123}


@pytest.mark.asyncio
async def test_list_clients_raises_on_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return json_response({"success": False, "msg": "list failed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(XuiApiError, match="list failed"):
            await XuiNodeClient(node(), http_client=http_client).list_clients()


@pytest.mark.asyncio
async def test_update_client_posts_raw_client_body_to_email_route() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response({"success": True})

    email = client_email("123")
    client_payload = {"email": email, "subId": "new", "comment": "Updated", "tgId": 123}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        await XuiNodeClient(node(), http_client=http_client).update_client(email, client_payload)

    assert requests[0].url.path == f"/secret-panel/panel/api/clients/update/{email}"
    assert requests[0].method == "POST"
    assert json.loads(requests[0].content) == client_payload


@pytest.mark.asyncio
async def test_update_client_maps_uuid_to_id_and_drops_read_only_fields() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response({"success": True})

    # A row as returned by list/get: numeric primary-key id, the real secret in uuid,
    # plus denormalized inboundIds/traffic the update endpoint must not receive.
    row = {
        "id": 4,
        "email": client_email("123"),
        "uuid": "d92334e7-7f6a-4bd0-b46c-dcdf46b3cffa",
        "subId": "abc",
        "comment": "Alice",
        "tgId": 123,
        "inboundIds": [1, 5],
        "traffic": {"up": 1, "down": 2},
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        await XuiNodeClient(node(), http_client=http_client).update_client(client_email("123"), row)

    body = json.loads(requests[0].content)
    assert body["id"] == "d92334e7-7f6a-4bd0-b46c-dcdf46b3cffa"
    assert "inboundIds" not in body and "traffic" not in body
    assert body["subId"] == "abc" and body["comment"] == "Alice" and body["tgId"] == 123


@pytest.mark.asyncio
async def test_update_client_raises_on_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return json_response({"success": False, "msg": "update rejected"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(XuiApiError, match="update rejected"):
            await XuiNodeClient(node(), http_client=http_client).update_client("alice", {"email": "alice"})


@pytest.mark.asyncio
async def test_get_client_links_returns_link_list() -> None:
    email = client_email("123")
    links = ["vless://uuid@host:443?type=tcp#Remark", "trojan://pwd@host:443?type=tcp#Two"]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response({"success": True, "obj": links})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await XuiNodeClient(node(), http_client=http_client).get_client_links(email)

    assert requests[0].url.path == f"/secret-panel/panel/api/clients/links/{email}"
    assert result == links


@pytest.mark.asyncio
async def test_get_client_links_returns_empty_list_when_not_found() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await XuiNodeClient(node(), http_client=http_client).get_client_links("ghost")

    assert result == []


@pytest.mark.asyncio
async def test_get_client_traffic_returns_traffic_object() -> None:
    email = client_email("123")
    traffic = {"email": email, "up": 1000, "down": 2000, "total": 0, "expiryTime": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        return json_response({"success": True, "obj": traffic})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await XuiNodeClient(node(), http_client=http_client).get_client_traffic(email)

    assert result == traffic


@pytest.mark.asyncio
async def test_get_client_traffic_returns_none_when_not_found() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await XuiNodeClient(node(), http_client=http_client).get_client_traffic("ghost")

    assert result is None


@pytest.mark.asyncio
async def test_decode_subscription_lines_accepts_base64_and_plain_text() -> None:
    encoded = base64.b64encode(b"vless://one#One\ntrojan://two#Two\n").decode()

    assert decode_subscription_lines(encoded) == ["vless://one#One", "trojan://two#Two"]
    assert decode_subscription_lines("vless://plain#Plain\n") == ["vless://plain#Plain"]
