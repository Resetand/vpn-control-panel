from __future__ import annotations

import base64
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import quote

from fastapi import Response

from vpn_control_plane.data import (
    ClientRecord,
    ExternalInboundRecord,
    JsonStateStore,
    NodeInboundRecord,
    NodeRecord,
    SubscriptionMetadata,
)
from vpn_control_plane.xui import XuiInbound, XuiNodeClient, build_xui_share_links


class SubscriptionError(RuntimeError):
    pass


class UnknownSubscriptionClientError(SubscriptionError):
    pass


@dataclass(frozen=True)
class BuiltSubscription:
    client: ClientRecord
    links: list[str]
    metadata: SubscriptionMetadata
    public_url: str
    subscription_userinfo: str | None = None
    node_errors: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class SubscriptionTraffic:
    matched: bool = False
    upload: int = 0
    download: int = 0
    total: int = 0
    expire: int | None = None

    def add(self, stat: object) -> None:
        if not isinstance(stat, dict):
            return
        self.matched = True
        self.upload += _nonnegative_int(stat.get("up"))
        self.download += _nonnegative_int(stat.get("down"))
        total = _nonnegative_int(stat.get("total"))
        if total > 0:
            self.total += total
        expire = _timestamp_seconds(stat.get("expiryTime"))
        if expire is not None:
            self.expire = max(self.expire or 0, expire)


def normalize_subscription_base_url(value: str) -> str:
    return value.rstrip("/")


def build_public_subscription_url(public_base_url: str, sub_id: str) -> str:
    return f"{normalize_subscription_base_url(public_base_url)}/{quote(sub_id.strip('/'), safe='')}"


class SubscriptionService:
    def __init__(
        self,
        store: JsonStateStore,
        *,
        public_base_url: str,
        node_client_factory: Callable[[NodeRecord], XuiNodeClient] | None = None,
    ) -> None:
        self._store = store
        self._public_base_url = normalize_subscription_base_url(public_base_url)
        self._node_client_factory = node_client_factory or XuiNodeClient

    def public_url_for_client(self, client: ClientRecord) -> str:
        return build_public_subscription_url(self._public_base_url, client.effective_sub_id)

    async def build(self, requested_sub_id: str) -> BuiltSubscription:
        requested_sub_id = requested_sub_id.strip().strip("/")
        state = self._store.load_state()
        client = self._find_client(state.clients, requested_sub_id)
        if client is None:
            raise UnknownSubscriptionClientError("unknown subscription client")

        nodes_by_id = {node.id: node for node in state.nodes}
        node_clients: dict[int, XuiNodeClient] = {}
        node_inbounds_by_id: dict[int, dict[int, XuiInbound]] = {}
        node_inbound_load_failures: set[int] = set()
        node_errors: list[str] = []
        links: list[str] = []
        traffic = SubscriptionTraffic()

        try:
            for inbound in state.inbounds:
                if isinstance(inbound, ExternalInboundRecord):
                    if inbound.uri.strip():
                        links.append(_ensure_fragment_label(inbound.uri.strip(), inbound.label))
                    continue

                node = nodes_by_id.get(inbound.node_id)
                if node is None:
                    node_errors.append(f"node {inbound.node_id} is not configured")
                    continue
                node_client = node_clients.get(node.id)
                if node_client is None:
                    node_client = self._node_client_factory(node)
                    node_clients[node.id] = node_client
                if node.id in node_inbound_load_failures:
                    continue
                listed_inbounds = node_inbounds_by_id.get(node.id)
                if listed_inbounds is None:
                    try:
                        listed_inbounds = {item.id: item for item in await node_client.list_inbounds()}
                    except Exception as exc:  # noqa: BLE001 - keep partial subscriptions available when one node is down.
                        node_errors.append(f"node {node.id}: {exc}")
                        node_inbound_load_failures.add(node.id)
                        continue
                    node_inbounds_by_id[node.id] = listed_inbounds
                links.extend(
                    await self._build_node_inbound_links(
                        listed_inbounds.get(inbound.inbound_id),
                        node,
                        inbound,
                        client,
                        node_errors,
                        traffic,
                    )
                )
        finally:
            for node_client in node_clients.values():
                close = getattr(node_client, "close", None)
                if close is not None:
                    await close()

        return BuiltSubscription(
            client=client,
            links=links,
            metadata=state.subscription,
            public_url=self.public_url_for_client(client),
            subscription_userinfo=_build_subscription_userinfo(state.subscription.subscription_userinfo, traffic),
            node_errors=tuple(node_errors),
        )

    async def _build_node_inbound_links(
        self,
        xui_inbound: XuiInbound | None,
        node: NodeRecord,
        inbound: NodeInboundRecord,
        client: ClientRecord,
        node_errors: list[str],
        traffic: SubscriptionTraffic,
    ) -> list[str]:
        try:
            if xui_inbound is None:
                raise SubscriptionError(f"inbound {inbound.inbound_id} was not found")
            _add_client_traffic(traffic, xui_inbound, inbound, client)
            if not _xui_inbound_is_enabled(xui_inbound.raw.get("enable", True)):
                return []
            links = build_xui_share_links(
                xui_inbound,
                fallback_address=node.host,
                sub_id=client.effective_sub_id,
                client_email=inbound.permanent_client_email,
                fallback_email=None if inbound.permanent_client_email else f"{inbound.inbound_id}_{client.id}",
                remark=inbound.label,
            )
            if not links:
                return []
            return [_ensure_fragment_label(link, inbound.label) for link in links]
        except Exception as exc:  # noqa: BLE001 - keep partial subscriptions available when one node is down.
            node_errors.append(f"node {node.id}: {exc}")
            return []

    @staticmethod
    def _find_client(clients: Sequence[ClientRecord], requested_sub_id: str) -> ClientRecord | None:
        for client in clients:
            if client.effective_sub_id == requested_sub_id:
                return client
        for client in clients:
            if client.id == requested_sub_id:
                return client
        return None


def render_subscription_response(subscription: BuiltSubscription) -> Response:
    decoded_body = "\n".join(subscription.links)
    if decoded_body:
        decoded_body = f"{decoded_body}\n"
    encoded_body = base64.b64encode(decoded_body.encode("utf-8")).decode("ascii")
    return Response(
        content=encoded_body,
        media_type="text/plain; charset=utf-8",
        headers=subscription_metadata_headers(
            subscription.metadata,
            subscription.public_url,
            subscription_userinfo=subscription.subscription_userinfo,
        ),
    )


def subscription_metadata_headers(
    metadata: SubscriptionMetadata,
    public_url: str,
    *,
    subscription_userinfo: str | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {"content-disposition": 'attachment; filename="subscription.txt"'}
    if metadata.profile_title:
        headers["profile-title"] = _base64_header(metadata.profile_title)
    if metadata.profile_update_interval is not None:
        headers["profile-update-interval"] = str(metadata.profile_update_interval)
    headers["profile-web-page-url"] = metadata.profile_web_page_url or public_url
    userinfo = subscription_userinfo or metadata.subscription_userinfo
    if userinfo:
        headers["subscription-userinfo"] = userinfo
    if metadata.support_url:
        headers["support-url"] = metadata.support_url
    if metadata.announce:
        headers["announce"] = _base64_header(metadata.announce)
    if metadata.routing:
        routing_enable = metadata.routing_enable if metadata.routing_enable is not None else True
        headers["routing-enable"] = str(routing_enable).lower()
        headers["routing"] = metadata.routing
    elif metadata.routing_enable is not None:
        headers["routing-enable"] = str(metadata.routing_enable).lower()
    return headers


def _build_subscription_userinfo(configured_userinfo: str | None, traffic: SubscriptionTraffic) -> str | None:
    if not traffic.matched:
        return configured_userinfo

    configured = _parse_subscription_userinfo(configured_userinfo)
    total = traffic.total if traffic.total > 0 else _nonnegative_int(configured.get("total"))
    values = {
        "upload": str(traffic.upload),
        "download": str(traffic.download),
        "total": str(total),
    }
    expire = traffic.expire or _timestamp_seconds(configured.get("expire"))
    if expire is not None:
        values["expire"] = str(expire)
    return "; ".join(f"{key}={value}" for key, value in values.items())


def _parse_subscription_userinfo(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    parsed: dict[str, str] = {}
    for part in value.split(";"):
        key, separator, raw_value = part.strip().partition("=")
        if separator and key:
            parsed[key.strip()] = raw_value.strip()
    return parsed


def _add_client_traffic(
    traffic: SubscriptionTraffic,
    xui_inbound: object,
    inbound: NodeInboundRecord,
    client: ClientRecord,
) -> None:
    raw = getattr(xui_inbound, "raw", {})
    stats = raw.get("clientStats") if isinstance(raw, dict) else None
    if not isinstance(stats, list):
        return

    client_email = inbound.permanent_client_email
    fallback_email = None if client_email else f"{inbound.inbound_id}_{client.id}"
    for stat in stats:
        if _traffic_stat_matches_client(
            stat,
            sub_id=client.effective_sub_id,
            client_email=client_email,
            fallback_email=fallback_email,
        ):
            traffic.add(stat)


def _traffic_stat_matches_client(
    stat: object,
    *,
    sub_id: str,
    client_email: str | None,
    fallback_email: str | None,
) -> bool:
    if not isinstance(stat, dict):
        return False
    if client_email:
        return _text(stat.get("email")) == client_email
    return _text(stat.get("subId")) == sub_id or bool(fallback_email and _text(stat.get("email")) == fallback_email)


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        parsed = int(value)
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return 0
    else:
        return 0
    return max(parsed, 0)


def _timestamp_seconds(value: object) -> int | None:
    timestamp = _nonnegative_int(value)
    if timestamp <= 0:
        return None
    if timestamp >= 10_000_000_000:
        return timestamp // 1000
    return timestamp


def _ensure_fragment_label(uri: str, label: str) -> str:
    prefix, separator, fragment = uri.partition("#")
    if separator and fragment:
        return uri
    return f"{prefix}#{quote(label, safe='')}"


def _xui_inbound_is_enabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _base64_header(value: str) -> str:
    if value.startswith("base64:"):
        return value
    return "base64:" + base64.b64encode(value.encode("utf-8")).decode("ascii")
