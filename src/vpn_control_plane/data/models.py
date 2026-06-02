from __future__ import annotations

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
    uri: Annotated[str, Field(min_length=1)]

    @field_validator("tag")
    @classmethod
    def strip_tag(cls, value: str) -> str:
        return _strip_nonempty(value, "inbound tag")


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
        for tag in [inbound.tag for node in self.nodes for inbound in node.inbounds]:
            if tag in known_tags:
                raise ValueError(f"duplicate inbound tag: {tag}")
            known_tags.add(tag)
        for inbound in self.external_inbounds:
            if inbound.tag in known_tags:
                raise ValueError(f"duplicate inbound tag: {inbound.tag}")
            known_tags.add(inbound.tag)

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


def _strip_nonempty(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{label} must not be empty")
    return value
