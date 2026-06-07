from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class NodeInboundRecord(StateModel):
    tag: Annotated[str, Field(min_length=1)]
    label: Annotated[str, Field(min_length=1)]
    xui_inbound_id: Annotated[int, Field(ge=1)] = Field(alias="xuiInboundId")
    xui_fallback_client_email: str | None = Field(default=None, alias="xuiFallbackClientEmail")

    @field_validator("tag")
    @classmethod
    def strip_tag(cls, value: str) -> str:
        return _strip_nonempty(value, "inbound tag")

    @field_validator("xui_fallback_client_email")
    @classmethod
    def strip_xui_fallback_client_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _strip_nonempty(value, "xui fallback client email")


class NodeRecord(StateModel):
    id: Annotated[int, Field(ge=1)]
    host: Annotated[str, Field(min_length=1)]
    port: Annotated[int, Field(ge=1, le=65535)]
    base_path: str = Field(default="/", alias="basePath")
    api_token: Annotated[str, Field(min_length=1)] = Field(alias="apiToken")
    scheme: Literal["http", "https"] = "https"
    label: str | None = None
    monitoring: bool = True
    xui_fallback_client_email: str | None = Field(default=None, alias="xuiFallbackClientEmail")
    inbounds: list[NodeInboundRecord] = Field(default_factory=list)

    @field_validator("base_path")
    @classmethod
    def normalize_base_path(cls, value: str) -> str:
        value = value.strip() or "/"
        if not value.startswith("/"):
            value = f"/{value}"
        if not value.endswith("/"):
            value = f"{value}/"
        return value

    @field_validator("api_token")
    @classmethod
    def normalize_api_token(cls, value: str) -> str:
        return _strip_nonempty(value, "api token")

    @field_validator("xui_fallback_client_email")
    @classmethod
    def strip_xui_fallback_client_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _strip_nonempty(value, "xui fallback client email")


class ExternalInboundRecord(StateModel):
    tag: Annotated[str, Field(min_length=1)]
    label: Annotated[str, Field(min_length=1)]
    # Either a literal share link (vless://..., wireguard://...) or a reference into an
    # external subscription: "@<name>:<slug>" (exact) or "@<name>:~<regex>" (first match).
    uri: Annotated[str, Field(min_length=1)]

    @field_validator("tag")
    @classmethod
    def strip_tag(cls, value: str) -> str:
        return _strip_nonempty(value, "inbound tag")


class ExternalSubscriptionRecord(StateModel):
    """An upstream subscription feed. Inbounds matching `inbound_filter` (by fragment) are
    exported to the resolved-inbounds file; reference them from externalInbounds via
    "@<name>:<slug>"."""

    name: Annotated[str, Field(min_length=1)]
    url: Annotated[str, Field(min_length=1)]
    # Refresh cadence in minutes.
    update_interval: Annotated[int, Field(ge=1)] = Field(alias="updateInterval")
    # Optional regex matched (re.search) against each entry's fragment; None means take all.
    inbound_filter: str | None = Field(default=None, alias="inboundFilter")

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = _strip_nonempty(value, "subscription name")
        if ":" in value or "@" in value:
            raise ValueError("subscription name must not contain ':' or '@'")
        return value

    @field_validator("inbound_filter")
    @classmethod
    def validate_inbound_filter(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid inboundFilter regex: {exc}") from exc
        return value


class ClientRecord(StateModel):
    id: Annotated[str, Field(min_length=1)]
    comment: str = ""
    telegram_id: str | None = Field(default=None, alias="telegramId")
    sub_id: str | None = Field(default=None, alias="subId")
    legacy_sub_id: str | None = Field(default=None, alias="legacySubId")
    inbound_tags: list[str] | None = Field(default=None, alias="inboundTags")

    @field_validator("id", "telegram_id", "sub_id", "legacy_sub_id")
    @classmethod
    def strip_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _strip_nonempty(value, "identifier")

    @field_validator("inbound_tags")
    @classmethod
    def strip_inbound_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [_strip_nonempty(tag, "inbound tag") for tag in value]

    @property
    def effective_sub_id(self) -> str:
        return self.sub_id or self.id

    @property
    def legacy_subscription_ids(self) -> set[str]:
        if self.legacy_sub_id is not None:
            return {self.legacy_sub_id}
        if self.sub_id is not None:
            return set()
        return {self.id}


class SubscriptionMetadata(StateModel):
    profile_title: str | None = Field(default=None, alias="profileTitle")
    profile_update_interval: int | None = Field(default=None, alias="profileUpdateInterval")
    profile_web_page_url: str | None = Field(default=None, alias="profileWebPageUrl")
    subscription_userinfo: str | None = Field(default=None, alias="subscriptionUserinfo")
    support_url: str | None = Field(default=None, alias="supportUrl")
    happ_provider_id: str | None = Field(default=None, alias="happProviderId")
    announce: str | None = None
    routing: str | None = None
    routing_enable: bool | None = Field(default=None, alias="routingEnable")


class ControlPlaneState(StateModel):
    nodes: list[NodeRecord] = Field(default_factory=list)
    external_inbounds: list[ExternalInboundRecord] = Field(default_factory=list, alias="externalInbounds")
    external_subscriptions: list[ExternalSubscriptionRecord] = Field(
        default_factory=list, alias="externalSubscriptions"
    )
    clients: list[ClientRecord] = Field(default_factory=list)
    default_client_inbound_tags: list[str] = Field(default_factory=list, alias="defaultClientInboundTags")
    subscription: SubscriptionMetadata = Field(default_factory=SubscriptionMetadata)

    @field_validator("default_client_inbound_tags")
    @classmethod
    def strip_default_tags(cls, value: list[str]) -> list[str]:
        return [_strip_nonempty(tag, "inbound tag") for tag in value]

    @model_validator(mode="after")
    def validate_inbound_tags(self) -> ControlPlaneState:
        known_tags: set[str] = set()

        def declare(tag: str) -> None:
            if tag in known_tags:
                raise ValueError(f"duplicate inbound tag: {tag}")
            known_tags.add(tag)

        for node in self.nodes:
            for node_inbound in node.inbounds:
                declare(node_inbound.tag)
        for external_inbound in self.external_inbounds:
            declare(external_inbound.tag)

        seen_subscriptions: set[str] = set()
        for subscription in self.external_subscriptions:
            if subscription.name in seen_subscriptions:
                raise ValueError(f"duplicate external subscription name: {subscription.name}")
            seen_subscriptions.add(subscription.name)

        # An externalInbound uri may point into a subscription (@name:slug / @name:~regex). The
        # subscription name must exist (catches typos); the slug/regex resolves lazily at render
        # time, so a missing slug is tolerated (never fatal) rather than validated here.
        for external_inbound in self.external_inbounds:
            ref = parse_external_subscription_ref(external_inbound.uri)
            if ref is None:
                continue
            if ref.name not in seen_subscriptions:
                raise ValueError(
                    f"externalInbounds[{external_inbound.tag}].uri references unknown subscription: {ref.name}"
                )
            if ref.is_regex:
                try:
                    re.compile(ref.query)
                except re.error as exc:
                    raise ValueError(f"externalInbounds[{external_inbound.tag}].uri has invalid regex: {exc}") from exc

        self._validate_tag_list("defaultClientInboundTags", self.default_client_inbound_tags, known_tags)
        for client in self.clients:
            if client.inbound_tags is not None:
                self._validate_tag_list(f"clients[{client.id}].inboundTags", client.inbound_tags, known_tags)
        return self

    @staticmethod
    def _validate_tag_list(label: str, tags: list[str], known_tags: set[str]) -> None:
        seen: set[str] = set()
        for tag in tags:
            if tag in seen:
                raise ValueError(f"{label} contains duplicate inbound tag: {tag}")
            seen.add(tag)
            if tag not in known_tags:
                raise ValueError(f"{label} references unknown inbound tag: {tag}")


@dataclass(frozen=True)
class NodeCatalogInbound:
    node: NodeRecord
    inbound: NodeInboundRecord


@dataclass(frozen=True)
class ExternalCatalogInbound:
    inbound: ExternalInboundRecord


CatalogInbound = NodeCatalogInbound | ExternalCatalogInbound


def build_inbound_catalog(state: ControlPlaneState) -> dict[str, CatalogInbound]:
    catalog: dict[str, CatalogInbound] = {}
    for node in state.nodes:
        for inbound in node.inbounds:
            catalog[inbound.tag] = NodeCatalogInbound(node=node, inbound=inbound)
    for external_inbound in state.external_inbounds:
        catalog[external_inbound.tag] = ExternalCatalogInbound(inbound=external_inbound)
    return catalog


def effective_inbound_tags(state: ControlPlaneState, client: ClientRecord) -> list[str]:
    if client.inbound_tags is not None:
        return list(client.inbound_tags)
    return list(state.default_client_inbound_tags)


@dataclass(frozen=True)
class ExternalSubscriptionRef:
    name: str
    # Exact slug to match (is_regex=False) or a regex searched against slugs (is_regex=True).
    query: str
    is_regex: bool


def parse_external_subscription_ref(uri: str) -> ExternalSubscriptionRef | None:
    """Parse an "@name:slug" / "@name:~regex" reference. Returns None for literal URIs.

    Raises ValueError for a malformed "@" reference so it surfaces during state validation.
    """
    if not uri.startswith("@"):
        return None
    name, separator, rest = uri[1:].partition(":")
    if not separator or not name or not rest:
        raise ValueError(f"invalid external subscription reference: {uri!r} (expected @name:slug or @name:~regex)")
    if rest.startswith("~"):
        pattern = rest[1:]
        if not pattern:
            raise ValueError(f"invalid external subscription reference: {uri!r} (empty regex after '~')")
        return ExternalSubscriptionRef(name=name, query=pattern, is_regex=True)
    return ExternalSubscriptionRef(name=name, query=rest, is_regex=False)


def _strip_nonempty(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{label} must not be empty")
    return value
