from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib.resources import files
from io import BytesIO
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import qrcode  # type: ignore[import-untyped]
from fastapi import Response

from vpn_control_plane.data import (
    ClientRecord,
    ControlPlaneStore,
    ExternalCatalogInbound,
    NodeCatalogInbound,
    NodeInboundRecord,
    NodeRecord,
    SubscriptionMetadata,
    build_inbound_catalog,
    effective_inbound_tags,
)
from vpn_control_plane.external_subscriptions.cache import ResolvedInboundsStore, resolve_reference
from vpn_control_plane.provisioning import client_email
from vpn_control_plane.xui import XuiNodeClient

ABROAD_PATH_SEGMENT = "abroad"
ABROAD_TITLE_SUFFIX = "Abroad"


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


@dataclass(frozen=True)
class _NodeSubscriptionData:
    remark_by_inbound: dict[int, str] = field(default_factory=dict)
    link_by_remark: dict[str, str] = field(default_factory=dict)
    traffic: object = None
    error: str | None = None


def normalize_subscription_base_url(value: str) -> str:
    return value.rstrip("/")


def build_public_subscription_url(public_base_url: str, sub_id: str) -> str:
    return f"{normalize_subscription_base_url(public_base_url)}/{quote(sub_id.strip('/'), safe='')}"


def build_public_subscription_token(sub_id: str, salt: str | None) -> str:
    sub_id = sub_id.strip().strip("/")
    if not salt:
        return sub_id
    digest = hmac.new(salt.encode("utf-8"), sub_id.encode("utf-8"), hashlib.sha256).digest()
    return _base62_encode(int.from_bytes(digest, byteorder="big"))


def _base62_encode(value: int) -> str:
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return alphabet[0]
    encoded = ""
    base = len(alphabet)
    while value:
        value, remainder = divmod(value, base)
        encoded = f"{alphabet[remainder]}{encoded}"
    return encoded


class SubscriptionService:
    def __init__(
        self,
        store: ControlPlaneStore,
        *,
        public_base_url: str,
        token_salt: str | None = None,
        node_client_factory: Callable[[NodeRecord], XuiNodeClient] | None = None,
        resolved_inbounds_path: Path | str | None = None,
        node_fetch_timeout_seconds: float = 4.0,
    ) -> None:
        self._store = store
        self._public_base_url = normalize_subscription_base_url(public_base_url)
        self._token_salt = token_salt or None
        self._node_client_factory = node_client_factory or XuiNodeClient
        self._node_fetch_timeout_seconds = node_fetch_timeout_seconds
        self._resolved_inbounds_store = (
            ResolvedInboundsStore(resolved_inbounds_path) if resolved_inbounds_path is not None else None
        )

    def public_token_for_client(self, client: ClientRecord) -> str:
        return build_public_subscription_token(client.effective_sub_id, self._token_salt)

    def public_url_for_client(self, client: ClientRecord) -> str:
        return build_public_subscription_url(self._public_base_url, self.public_token_for_client(client))

    def abroad_public_url_for_client(self, client: ClientRecord) -> str:
        return build_public_subscription_url(
            f"{self._public_base_url}/{ABROAD_PATH_SEGMENT}", self.public_token_for_client(client)
        )

    def is_public_token_for_client(self, token: str, client: ClientRecord) -> bool:
        return secrets.compare_digest(token.strip().strip("/"), self.public_token_for_client(client))

    async def build(self, requested_sub_id: str, *, abroad: bool = False) -> BuiltSubscription:
        """Build a client's subscription. The abroad variant serves the same links for people
        living outside Russia: Happ routing is switched off so all traffic goes through the chosen
        server, and it lives at its own URL so Happ keeps it apart from the regular subscription."""
        requested_sub_id = requested_sub_id.strip().strip("/")
        state = self._store.load_state()
        client = self._find_client(state.clients, requested_sub_id)
        if client is None:
            raise UnknownSubscriptionClientError("unknown subscription client")

        catalog = build_inbound_catalog(state)
        resolved_inbounds = self._resolved_inbounds_store.load() if self._resolved_inbounds_store is not None else {}
        requested_tags = effective_inbound_tags(state, client)
        requested_nodes: dict[int, tuple[NodeRecord, NodeInboundRecord]] = {}
        for tag in requested_tags:
            catalog_inbound = catalog[tag]
            if isinstance(catalog_inbound, NodeCatalogInbound):
                requested_nodes.setdefault(catalog_inbound.node.id, (catalog_inbound.node, catalog_inbound.inbound))

        node_clients: dict[int, XuiNodeClient] = {}
        node_data: dict[int, _NodeSubscriptionData] = {}
        node_errors: list[str] = []
        links: list[str] = []
        traffic = SubscriptionTraffic()
        email = client_email(client.id)

        try:
            fetch_node_ids: list[int] = []
            fetch_tasks: list[asyncio.Task[_NodeSubscriptionData]] = []
            for node_id, (node, inbound) in requested_nodes.items():
                try:
                    node_client = self._node_client_factory(node)
                except Exception as exc:  # noqa: BLE001 - isolate a broken node client.
                    node_data[node_id] = _NodeSubscriptionData(error=str(exc))
                    continue
                node_clients[node_id] = node_client
                fetch_node_ids.append(node_id)
                fetch_tasks.append(asyncio.create_task(self._fetch_node_data(node, inbound, node_client, email)))
            fetched = await asyncio.gather(*fetch_tasks)
            node_data.update(zip(fetch_node_ids, fetched, strict=True))
            for node_id, data in node_data.items():
                if data.error is not None:
                    node_errors.append(f"node {node_id}: {data.error}")
                elif data.traffic is not None:
                    traffic.add(data.traffic)

            for tag in requested_tags:
                catalog_inbound = catalog[tag]
                if isinstance(catalog_inbound, ExternalCatalogInbound):
                    external_inbound = catalog_inbound.inbound
                    # uri is either a literal link or an "@name:slug" reference resolved from the
                    # external-subscriptions file; an unresolved reference is silently skipped.
                    uri = resolve_reference(external_inbound.uri, resolved_inbounds)
                    if uri and uri.strip():
                        links.append(_normalize_link(_ensure_fragment_label(uri.strip(), external_inbound.label)))
                    continue

                assert isinstance(catalog_inbound, NodeCatalogInbound)
                node = catalog_inbound.node
                node_inbound = catalog_inbound.inbound

                # Emit ONLY this allowed inbound's link, relabelled with our friendly label
                # (the panel fragment is the inbound remark, not our control-plane label).
                data = node_data[node.id]
                remark = data.remark_by_inbound.get(node_inbound.xui_inbound_id)
                link = data.link_by_remark.get(remark) if remark else None
                if link:
                    links.append(_normalize_link(_relabel_fragment(link, node_inbound.label)))
        finally:
            await asyncio.gather(*(self._close_node_client(client) for client in node_clients.values()))

        metadata = state.subscription
        public_url = self.public_url_for_client(client)
        if abroad:
            metadata = _abroad_metadata(metadata)
            public_url = self.abroad_public_url_for_client(client)

        return BuiltSubscription(
            client=client,
            links=links,
            metadata=metadata,
            public_url=public_url,
            subscription_userinfo=_build_subscription_userinfo(state.subscription.subscription_userinfo, traffic),
            node_errors=tuple(node_errors),
        )

    async def _fetch_node_data(
        self,
        node: NodeRecord,
        inbound: NodeInboundRecord,
        client: XuiNodeClient,
        email: str,
    ) -> _NodeSubscriptionData:
        remark_by_inbound: dict[int, str] = {}
        link_by_remark: dict[str, str] = {}
        links_fetched = False
        try:
            async with asyncio.timeout(self._node_fetch_timeout_seconds):
                inbounds = await client.list_inbounds()
                remark_by_inbound = {ib.id: str(ib.raw.get("remark") or "") for ib in inbounds}
                link_list = await client.get_client_links(email)
                if not link_list:
                    for fallback_email in _fallback_client_emails(node, inbound):
                        link_list = await client.get_client_links(fallback_email)
                        if link_list:
                            break
                link_by_remark = {}
                for panel_link in link_list:
                    link_by_remark.setdefault(_panel_link_remark(panel_link), panel_link)
                links_fetched = True
                try:
                    traffic_obj = await client.get_client_traffic(email)
                    if not traffic_obj:
                        for fallback_email in _fallback_client_emails(node, inbound):
                            traffic_obj = await client.get_client_traffic(fallback_email)
                            if traffic_obj:
                                break
                except Exception:  # noqa: BLE001 - traffic is non-fatal
                    traffic_obj = None
                return _NodeSubscriptionData(remark_by_inbound, link_by_remark, traffic_obj)
        except TimeoutError:
            if links_fetched:
                return _NodeSubscriptionData(remark_by_inbound, link_by_remark)
            return _NodeSubscriptionData(error=f"timed out after {self._node_fetch_timeout_seconds:g}s")
        except Exception as exc:  # noqa: BLE001 - keep partial subscriptions available when one node is down.
            return _NodeSubscriptionData(error=str(exc))

    async def _close_node_client(self, client: XuiNodeClient) -> None:
        close = getattr(client, "close", None)
        if close is None:
            return
        try:
            async with asyncio.timeout(self._node_fetch_timeout_seconds):
                await close()
        except Exception:  # noqa: BLE001 - cleanup must not break a ready subscription.
            pass

    def _find_client(self, clients: Sequence[ClientRecord], requested_sub_id: str) -> ClientRecord | None:
        for client in clients:
            if client.effective_sub_id == requested_sub_id:
                return client
        for client in clients:
            if requested_sub_id in client.legacy_subscription_ids:
                return client
        if self._token_salt:
            for client in clients:
                if secrets.compare_digest(requested_sub_id, self.public_token_for_client(client)):
                    return client
        return None


def _abroad_metadata(metadata: SubscriptionMetadata) -> SubscriptionMetadata:
    title = _decode_base64_header(metadata.profile_title) if metadata.profile_title else ""
    title = f"{title} ({ABROAD_TITLE_SUFFIX})" if title else ABROAD_TITLE_SUFFIX
    return metadata.model_copy(update={"routing": None, "routing_enable": False, "profile_title": title})


def _decode_base64_header(value: str) -> str:
    if not value.startswith("base64:"):
        return value
    try:
        return base64.b64decode(value.removeprefix("base64:"), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return value


def _fallback_client_emails(node: NodeRecord, inbound: NodeInboundRecord) -> tuple[str, ...]:
    email = inbound.xui_fallback_client_email or node.xui_fallback_client_email
    if email is None:
        return ()
    return (email,)


def render_subscription_response(subscription: BuiltSubscription) -> Response:
    return render_subscription_text_response(subscription)


def render_subscription_by_accept(subscription: BuiltSubscription, accept: str | None) -> Response:
    if _accepts(accept, "text/html"):
        return render_subscription_html_response(subscription)
    if _accepts(accept, "application/json"):
        return render_subscription_json_response(subscription)
    return render_subscription_text_response(subscription)


def render_subscription_text_response(subscription: BuiltSubscription) -> Response:
    encoded_body = _encoded_subscription_body(subscription)
    return Response(
        content=encoded_body,
        media_type="text/plain; charset=utf-8",
        headers=subscription_metadata_headers(
            subscription.metadata,
            subscription.public_url,
            subscription_userinfo=subscription.subscription_userinfo,
        ),
    )


def render_subscription_json_response(subscription: BuiltSubscription) -> Response:
    payload = _subscription_view_payload(subscription)
    return Response(
        content=json.dumps(payload, ensure_ascii=False),
        media_type="application/json; charset=utf-8",
    )


def render_subscription_html_response(subscription: BuiltSubscription) -> Response:
    payload = _subscription_view_payload(subscription, include_qr=True)
    template = files("vpn_control_plane.subscription").joinpath("assets/subscription.html").read_text(encoding="utf-8")
    title = payload["subscription"]["title"]
    content = template.replace("__SUBSCRIPTION_TITLE__", _html_escape(title)).replace(
        "__SUBSCRIPTION_PAYLOAD__",
        _json_script_escape(json.dumps(payload, ensure_ascii=False)),
    )
    return Response(content=content, media_type="text/html; charset=utf-8")


def _decoded_subscription_body(subscription: BuiltSubscription) -> str:
    decoded_body = "\n".join(subscription.links)
    if decoded_body:
        decoded_body = f"{decoded_body}\n"
    return decoded_body


def _encoded_subscription_body(subscription: BuiltSubscription) -> str:
    return base64.b64encode(_decoded_subscription_body(subscription).encode("utf-8")).decode("ascii")


def _subscription_view_payload(subscription: BuiltSubscription, *, include_qr: bool = False) -> dict[str, Any]:
    profile_title = subscription.metadata.profile_title or ""
    client_title = subscription.client.comment or subscription.client.effective_sub_id
    title = profile_title or client_title
    links = [_subscription_link_payload(link, index) for index, link in enumerate(subscription.links)]
    subscription_info: dict[str, Any] = {
        "id": subscription.client.effective_sub_id,
        "title": title,
        "profile_title": profile_title,
        "client_title": client_title,
        "public_url": subscription.public_url,
        "decoded": _decoded_subscription_body(subscription),
        "encoded": _encoded_subscription_body(subscription),
    }
    if include_qr:
        subscription_info["qr"] = _qr_png_data_uri(subscription.public_url)

    return {
        "subscription": subscription_info,
        "links": links,
        "recommended_clients": _recommended_clients(),
        "metadata": subscription.metadata.model_dump(by_alias=True),
        "subscription_userinfo": subscription.subscription_userinfo,
        "node_errors": list(subscription.node_errors),
    }


def _subscription_link_payload(link: str, index: int) -> dict[str, Any]:
    return {
        "name": _subscription_link_name(link, index),
        "protocol": _subscription_link_protocol(link),
        "url": link,
    }


def _subscription_link_name(link: str, index: int) -> str:
    fragment = urlsplit(link).fragment
    if fragment:
        try:
            decoded = unquote(fragment).strip()
        except Exception:  # noqa: BLE001 - fragments from external links can be arbitrary.
            decoded = fragment.strip()
        if decoded:
            return decoded
    return f"Key {index + 1}"


def _subscription_link_protocol(link: str) -> str:
    scheme, separator, _rest = link.partition("://")
    if separator and scheme:
        return scheme.upper()
    return "LINK"


def _recommended_clients() -> dict[str, dict[str, str]]:
    content = files("vpn_control_plane.telegram").joinpath("clients_recommended.json").read_text(encoding="utf-8")
    return cast(dict[str, dict[str, str]], json.loads(content))


def _qr_png_data_uri(value: str) -> str:
    image = qrcode.make(value)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _accepts(accept: str | None, media_type: str) -> bool:
    if not accept:
        return False
    for item in accept.lower().split(","):
        value = item.split(";", 1)[0].strip()
        if value == media_type:
            return True
    return False


def _html_escape(value: object) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _json_script_escape(value: str) -> str:
    return value.replace("&", r"\u0026").replace("<", r"\u003c").replace(">", r"\u003e")


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


def _relabel_fragment(uri: str, label: str) -> str:
    """Replace the URL fragment with the control-plane label (panel links carry the
    inbound remark as the fragment). If we have no label, keep the panel's fragment."""
    if not label or uri.lower().startswith(_AMNEZIAWG_SCHEME):
        return uri
    prefix = uri.partition("#")[0]
    return f"{prefix}#{quote(label, safe='')}"


# AmneziaWG share links are ``vpn://<base64 wg config>``: the whole payload is the config,
# so a ``#label`` suffix would corrupt the base64 the client decodes, and the inbound remark
# arrives as a ``# <remark>`` comment inside that config instead of as a URL fragment.
_AMNEZIAWG_SCHEME = "vpn://"


def _panel_link_remark(uri: str) -> str:
    """The inbound remark the panel keyed this share link by — its URL fragment, except for
    AmneziaWG links, where it is a comment line inside the encoded WireGuard config."""
    fragment = unquote(urlsplit(uri).fragment)
    if fragment or not uri.lower().startswith(_AMNEZIAWG_SCHEME):
        return fragment
    payload = uri[len(_AMNEZIAWG_SCHEME) :]
    try:
        config = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""
    for line in config.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


# Query parameters defined by the official Hysteria2 URI scheme
# (https://v2.hysteria.network/docs/developers/URI-Scheme/). Anything else is a
# third-party extension and must not be assumed to be understood by other clients.
_HYSTERIA2_SCHEMES = ("hysteria2://", "hy2://")
_HYSTERIA2_SPEC_PARAMS = {"sni", "insecure", "obfs", "obfs-password", "pinsha256", "mport"}


def _normalize_link(uri: str) -> str:
    """Rewrite a panel link into its most interoperable canonical form before serving it."""
    return _normalize_hysteria2_link(uri)


def _normalize_hysteria2_link(uri: str) -> str:
    """3x-ui emits Hysteria2 share links in the v2rayN/Xray dialect -- it appends ``fp``,
    ``security``, ``type`` and ``alpn`` query params and uses ``allowInsecure``. None of those
    belong to the official Hysteria2 URI scheme. Lenient clients (e.g. Happ) silently normalize
    the link, but strict parsers (sing-box via podkop) trip over the extras -- notably ``fp`` --
    and fail to build a working outbound.

    Keep only the spec-defined query params, translating ``allowInsecure`` to ``insecure``, and
    preserve everything else (auth, host, port, fragment label) verbatim."""
    if not uri.lower().startswith(_HYSTERIA2_SCHEMES):
        return uri

    parts = urlsplit(uri)
    kept: list[tuple[str, str]] = []
    seen: set[str] = set()
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        name = key.lower()
        if name == "allowinsecure":
            name, key, value = "insecure", "insecure", ("1" if value in ("1", "true") else "0")
        if name not in _HYSTERIA2_SPEC_PARAMS or name in seen:
            continue
        seen.add(name)
        kept.append((key, value))

    query = urlencode(kept)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _base64_header(value: str) -> str:
    if value.startswith("base64:"):
        return value
    return "base64:" + base64.b64encode(value.encode("utf-8")).decode("ascii")
