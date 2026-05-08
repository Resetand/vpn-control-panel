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
from vpn_control_plane.xui import XuiNodeClient, build_xui_share_links


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
    node_errors: tuple[str, ...] = field(default_factory=tuple)


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
        node_errors: list[str] = []
        links: list[str] = []

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
                links.extend(await self._build_node_inbound_links(node_client, node, inbound, client, node_errors))
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
            node_errors=tuple(node_errors),
        )

    async def _build_node_inbound_links(
        self,
        xui_client: XuiNodeClient,
        node: NodeRecord,
        inbound: NodeInboundRecord,
        client: ClientRecord,
        node_errors: list[str],
    ) -> list[str]:
        try:
            xui_inbound = await xui_client.get_inbound(inbound.inbound_id)
            if xui_inbound is None:
                raise SubscriptionError(f"inbound {inbound.inbound_id} was not found")
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
        headers=subscription_metadata_headers(subscription.metadata, subscription.public_url),
    )


def subscription_metadata_headers(metadata: SubscriptionMetadata, public_url: str) -> dict[str, str]:
    headers: dict[str, str] = {"content-disposition": 'attachment; filename="subscription.txt"'}
    if metadata.profile_title:
        headers["profile-title"] = _base64_header(metadata.profile_title)
    if metadata.profile_update_interval is not None:
        headers["profile-update-interval"] = str(metadata.profile_update_interval)
    headers["profile-web-page-url"] = metadata.profile_web_page_url or public_url
    if metadata.subscription_userinfo:
        headers["subscription-userinfo"] = metadata.subscription_userinfo
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
