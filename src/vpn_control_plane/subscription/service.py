from __future__ import annotations

import base64
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

from fastapi import Response

from vpn_control_plane.data import (
    ClientRecord,
    ExternalInboundRecord,
    JsonStateStore,
    NodeInboundRecord,
    NodeRecord,
    SubscriptionMetadata,
)
from vpn_control_plane.xui import XuiNodeClient


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
        node_links: dict[int, list[str]] = {}
        used_node_indexes: dict[int, set[int]] = {}
        node_errors: list[str] = []
        links: list[str] = []

        for inbound in state.inbounds:
            if isinstance(inbound, ExternalInboundRecord):
                if inbound.uri.strip():
                    links.append(inbound.uri.strip())
                continue

            node = nodes_by_id.get(inbound.node_id)
            if node is None:
                node_errors.append(f"node {inbound.node_id} is not configured")
                continue
            fetched_links = await self._fetch_node_links(node, client.effective_sub_id, node_links, node_errors)
            selected = _select_node_link(fetched_links, inbound, used_node_indexes.setdefault(inbound.node_id, set()))
            if selected is not None:
                links.append(selected)

        return BuiltSubscription(
            client=client,
            links=links,
            metadata=state.subscription,
            public_url=self.public_url_for_client(client),
            node_errors=tuple(node_errors),
        )

    async def _fetch_node_links(
        self,
        node: NodeRecord,
        sub_id: str,
        cache: dict[int, list[str]],
        node_errors: list[str],
    ) -> list[str]:
        if node.id in cache:
            return cache[node.id]
        xui_client = self._node_client_factory(node)
        try:
            links = await xui_client.fetch_subscription_links(sub_id)
        except Exception as exc:  # noqa: BLE001 - keep partial subscriptions available when one node is down.
            node_errors.append(f"node {node.id}: {exc}")
            links = []
        finally:
            close = getattr(xui_client, "close", None)
            if close is not None:
                await close()
        cache[node.id] = links
        return links

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


def _select_node_link(links: Sequence[str], inbound: NodeInboundRecord, used_indexes: set[int]) -> str | None:
    for index, link in enumerate(links):
        if index not in used_indexes and _fragment_label(link) == inbound.label:
            used_indexes.add(index)
            return link
    for index, link in enumerate(links):
        if index not in used_indexes:
            used_indexes.add(index)
            return link
    return None


def _fragment_label(uri: str) -> str:
    _prefix, separator, fragment = uri.partition("#")
    if not separator:
        return ""
    return unquote(fragment)


def _base64_header(value: str) -> str:
    if value.startswith("base64:"):
        return value
    return "base64:" + base64.b64encode(value.encode("utf-8")).decode("ascii")
