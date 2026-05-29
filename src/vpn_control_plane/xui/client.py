from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from vpn_control_plane.data import NodeRecord

logger = logging.getLogger(__name__)


class XuiError(RuntimeError):
    pass


class XuiApiError(XuiError):
    pass


JsonObject = dict[str, Any]


@dataclass(frozen=True)
class XuiNodeEndpoint:
    base_url: str

    @classmethod
    def from_node(cls, node: NodeRecord) -> XuiNodeEndpoint:
        base_path = normalize_base_path(node.base_path)
        return cls(base_url=f"{node.scheme}://{node.host}:{node.port}{base_path}")


@dataclass(frozen=True)
class XuiInbound:
    id: int
    protocol: str
    settings: JsonObject
    stream_settings: JsonObject
    sniffing: JsonObject
    raw: JsonObject


@dataclass(frozen=True)
class XuiClientInfo:
    client: JsonObject
    inbound_ids: list[int]


@dataclass(frozen=True)
class XuiMemoryStatus:
    current: int
    total: int

    @property
    def usage_percent(self) -> float:
        if self.total <= 0:
            return 0.0
        return self.current / self.total * 100


@dataclass(frozen=True)
class XuiXrayStatus:
    state: str
    error_message: str
    version: str


@dataclass(frozen=True)
class XuiNodeStatus:
    cpu_percent: float
    memory: XuiMemoryStatus
    xray: XuiXrayStatus
    raw: JsonObject


def normalize_base_path(value: str) -> str:
    value = value.strip() or "/"
    if not value.startswith("/"):
        value = f"/{value}"
    if not value.endswith("/"):
        value = f"{value}/"
    return value


class XuiNodeClient:
    def __init__(
        self,
        node: NodeRecord,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        verify: bool = True,
    ) -> None:
        self.node = node
        self.endpoint = XuiNodeEndpoint.from_node(node)
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=timeout, follow_redirects=False, verify=verify)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def list_inbounds(self) -> list[XuiInbound]:
        operation = "xui.list_inbounds"
        body = await self._request_json("GET", "/panel/api/inbounds/list", operation=operation)
        if not body.get("success"):
            logger.warning("3x-UI operation failed", extra={"node_id": self.node.id, "operation": operation})
            raise XuiApiError(f"3x-UI inbound list failed for node {self.node.id}: {_api_message(body)}")
        raw_inbounds = body.get("obj") or []
        if not isinstance(raw_inbounds, list):
            raise XuiApiError(f"3x-UI inbound list returned invalid payload for node {self.node.id}")
        return [_parse_inbound(inbound) for inbound in raw_inbounds if isinstance(inbound, dict)]

    async def get_inbound(self, inbound_id: int) -> XuiInbound | None:
        operation = "xui.get_inbound"
        response = await self._request("GET", f"/panel/api/inbounds/get/{inbound_id}", operation=operation)
        if response.status_code == 404:
            return None
        body = _response_json(response)
        if not body.get("success"):
            return None
        raw_inbound = body.get("obj")
        if not isinstance(raw_inbound, dict):
            return None
        return _parse_inbound(raw_inbound)

    async def get_database_backup(self) -> bytes:
        return await self._download("/panel/api/server/getDb", operation="xui.get_database_backup")

    async def get_config_json_backup(self) -> bytes:
        return await self._download("/panel/api/server/getConfigJson", operation="xui.get_config_json_backup")

    async def update_geofiles(self) -> None:
        await self._post_success("/panel/api/server/updateGeofile", operation="xui.update_geofile")
        await self._post_success(
            "/panel/api/custom-geo/update-all",
            operation="xui.update_custom_geofiles",
            ignore_not_found=True,
        )

    async def get_status(self) -> XuiNodeStatus:
        operation = "xui.get_status"
        body = await self._request_json("GET", "/panel/api/server/status", operation=operation)
        if not body.get("success"):
            logger.warning("3x-UI operation failed", extra={"node_id": self.node.id, "operation": operation})
            raise XuiApiError(f"3x-UI status failed for node {self.node.id}: {_api_message(body)}")
        raw_status = body.get("obj")
        if not isinstance(raw_status, dict):
            raise XuiApiError(f"3x-UI status returned invalid payload for node {self.node.id}")
        try:
            return _parse_status(raw_status)
        except (KeyError, TypeError, ValueError) as exc:
            raise XuiApiError(f"3x-UI status returned malformed payload for node {self.node.id}: {exc}") from exc

    async def get_client(self, email: str) -> XuiClientInfo | None:
        operation = "xui.get_client"
        response = await self._request("GET", f"/panel/api/clients/get/{email}", operation=operation)
        if response.status_code == 404:
            return None
        body = _response_json(response)
        if not body.get("success"):
            return None
        obj = body.get("obj")
        if not isinstance(obj, dict):
            return None
        client = obj.get("client")
        inbound_ids = obj.get("inboundIds")
        if not isinstance(client, dict) or not isinstance(inbound_ids, list):
            return None
        return XuiClientInfo(client=client, inbound_ids=[int(i) for i in inbound_ids])

    async def add_client(self, client: JsonObject, inbound_ids: list[int]) -> None:
        operation = "xui.add_client"
        await self._post_json_success(
            "/panel/api/clients/add",
            body={"client": client, "inboundIds": inbound_ids},
            operation=operation,
        )

    async def attach_client(self, email: str, inbound_ids: list[int]) -> None:
        operation = "xui.attach_client"
        await self._post_json_success(
            f"/panel/api/clients/{email}/attach",
            body={"inboundIds": inbound_ids},
            operation=operation,
        )

    async def list_clients(self) -> list[XuiClientInfo]:
        operation = "xui.list_clients"
        body = await self._request_json("GET", "/panel/api/clients/list", operation=operation)
        if not body.get("success"):
            logger.warning("3x-UI operation failed", extra={"node_id": self.node.id, "operation": operation})
            raise XuiApiError(f"3x-UI client list failed for node {self.node.id}: {_api_message(body)}")
        raw_clients = body.get("obj") or []
        if not isinstance(raw_clients, list):
            raise XuiApiError(f"3x-UI client list returned invalid payload for node {self.node.id}")
        clients: list[XuiClientInfo] = []
        for row in raw_clients:
            if not isinstance(row, dict) or not row.get("email"):
                continue
            client = dict(row)
            raw_inbound_ids = client.pop("inboundIds", None)
            client.pop("traffic", None)
            inbound_ids = [int(i) for i in raw_inbound_ids] if isinstance(raw_inbound_ids, list) else []
            clients.append(XuiClientInfo(client=client, inbound_ids=inbound_ids))
        return clients

    async def update_client(self, email: str, client: JsonObject) -> None:
        operation = "xui.update_client"
        await self._post_json_success(
            f"/panel/api/clients/update/{email}",
            body=_writable_client_payload(client),
            operation=operation,
        )

    async def get_client_links(self, email: str) -> list[str]:
        operation = "xui.get_client_links"
        response = await self._request("GET", f"/panel/api/clients/links/{email}", operation=operation)
        if response.status_code == 404:
            return []
        body = _response_json(response)
        if not body.get("success"):
            return []
        obj = body.get("obj")
        if not isinstance(obj, list):
            return []
        return [str(link) for link in obj if link]

    async def get_client_traffic(self, email: str) -> JsonObject | None:
        operation = "xui.get_client_traffic"
        response = await self._request("GET", f"/panel/api/clients/traffic/{email}", operation=operation)
        if response.status_code == 404:
            return None
        body = _response_json(response)
        if not body.get("success"):
            return None
        obj = body.get("obj")
        return obj if isinstance(obj, dict) else None

    async def _request_json(self, method: str, path: str, *, operation: str, **kwargs: Any) -> JsonObject:
        response = await self._request(method, path, operation=operation, **kwargs)
        body = _response_json(response)
        if response.status_code >= 400:
            logger.warning(
                "3x-UI operation failed",
                extra={"node_id": self.node.id, "operation": operation, "status_code": response.status_code},
            )
            raise XuiApiError(f"3x-UI request failed for node {self.node.id}: HTTP {response.status_code}")
        return body

    async def _download(self, path: str, *, operation: str) -> bytes:
        response = await self._request("GET", path, operation=operation)
        if response.status_code >= 400:
            logger.warning(
                "3x-UI operation failed",
                extra={"node_id": self.node.id, "operation": operation, "status_code": response.status_code},
            )
            raise XuiApiError(f"3x-UI download failed for node {self.node.id}: HTTP {response.status_code}")
        return response.content

    async def _post_success(self, path: str, *, operation: str, ignore_not_found: bool = False) -> None:
        response = await self._request("POST", path, operation=operation)
        if ignore_not_found and response.status_code == 404:
            logger.info("3x-UI operation is not supported", extra={"node_id": self.node.id, "operation": operation})
            return
        body = _response_json(response)
        if response.status_code >= 400 or not body.get("success"):
            logger.warning(
                "3x-UI operation failed",
                extra={"node_id": self.node.id, "operation": operation, "status_code": response.status_code},
            )
            raise XuiApiError(f"3x-UI {operation} failed for node {self.node.id}: {_api_message(body)}")

    async def _post_json_success(self, path: str, *, body: Any, operation: str) -> None:
        response = await self._request("POST", path, operation=operation, json=body)
        resp_body = _response_json(response)
        if response.status_code >= 400 or not resp_body.get("success"):
            logger.warning(
                "3x-UI operation failed",
                extra={"node_id": self.node.id, "operation": operation, "status_code": response.status_code},
            )
            raise XuiApiError(f"3x-UI {operation} failed for node {self.node.id}: {_api_message(resp_body)}")

    async def _request(self, method: str, path: str, *, operation: str, **kwargs: Any) -> httpx.Response:
        logger.info("Starting 3x-UI operation", extra={"node_id": self.node.id, "operation": operation})
        kwargs = self._with_auth_headers(kwargs)
        try:
            response = await self._client.request(method, self._url(path), **kwargs)
        except Exception:
            logger.exception("3x-UI operation failed", extra={"node_id": self.node.id, "operation": operation})
            raise
        logger.info("Finished 3x-UI operation", extra={"node_id": self.node.id, "operation": operation})
        return response

    def _with_auth_headers(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        headers = httpx.Headers(kwargs.pop("headers", None))
        headers["Authorization"] = f"Bearer {self.node.api_token}"
        headers.setdefault("Accept", "application/json")
        return {**kwargs, "headers": headers}

    def _url(self, path: str) -> str:
        return f"{self.endpoint.base_url.rstrip('/')}/{path.lstrip('/')}"


def decode_subscription_lines(text: str) -> list[str]:
    plain_lines = _non_empty_lines(text)
    if any("://" in line for line in plain_lines):
        return plain_lines

    compact = "".join(text.split())
    if compact:
        padding = "=" * (-len(compact) % 4)
        try:
            decoded = base64.b64decode(compact + padding, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return plain_lines
        return _non_empty_lines(decoded)
    return []


def _non_empty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _response_json(response: httpx.Response) -> JsonObject:
    try:
        body = response.json()
    except ValueError as exc:
        raise XuiApiError(f"3x-UI returned non-JSON response: HTTP {response.status_code}") from exc
    if not isinstance(body, dict):
        raise XuiApiError("3x-UI returned invalid JSON response shape")
    return body


def _parse_inbound(raw_inbound: JsonObject) -> XuiInbound:
    parsed = dict(raw_inbound)
    settings = _parse_json_object(parsed.get("settings"))
    stream_settings = _parse_json_object(parsed.get("streamSettings"))
    sniffing = _parse_json_object(parsed.get("sniffing"))
    parsed["settings"] = settings
    parsed["streamSettings"] = stream_settings
    parsed["sniffing"] = sniffing
    return XuiInbound(
        id=int(parsed["id"]),
        protocol=str(parsed.get("protocol") or ""),
        settings=settings,
        stream_settings=stream_settings,
        sniffing=sniffing,
        raw=parsed,
    )


def _parse_json_object(value: Any) -> JsonObject:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parse_status(raw_status: JsonObject) -> XuiNodeStatus:
    memory = raw_status["mem"]
    xray = raw_status["xray"]
    if not isinstance(memory, dict):
        raise TypeError("mem must be an object")
    if not isinstance(xray, dict):
        raise TypeError("xray must be an object")
    return XuiNodeStatus(
        cpu_percent=float(raw_status["cpu"]),
        memory=XuiMemoryStatus(current=int(memory["current"]), total=int(memory["total"])),
        xray=XuiXrayStatus(
            state=str(xray["state"]),
            error_message=str(xray.get("errorMsg") or ""),
            version=str(xray.get("version") or ""),
        ),
        raw=raw_status,
    )


def _writable_client_payload(client: JsonObject) -> JsonObject:
    """Adapt a client row from the read endpoints into the shape the update endpoint accepts.

    The list/get endpoints return ``id`` as the numeric DB primary key plus the protocol
    secret in ``uuid``/``password``/``auth``, but the update endpoint expects ``id`` to be
    that string secret. Read-only/denormalized fields are dropped.
    """
    payload = dict(client)
    payload.pop("traffic", None)
    payload.pop("inboundIds", None)
    identity = payload.get("uuid") or payload.get("password") or payload.get("auth")
    if isinstance(identity, str) and identity:
        payload["id"] = identity
    return payload


def _api_message(body: JsonObject) -> str:
    return str(body.get("msg") or body.get("message") or "unknown error")
