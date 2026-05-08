from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class NodeRecord(StateModel):
    id: Annotated[int, Field(ge=1)]
    host: Annotated[str, Field(min_length=1)]
    port: Annotated[int, Field(ge=1, le=65535)]
    web_base_path: str = Field(default="/", alias="webBasePath")
    username: Annotated[str, Field(min_length=1)]
    password: Annotated[str, Field(min_length=1)]
    two_factor_code: str | None = Field(default=None, alias="twoFactorCode")
    scheme: Literal["http", "https"] = "https"
    label: str | None = None

    @field_validator("web_base_path")
    @classmethod
    def normalize_web_base_path(cls, value: str) -> str:
        value = value.strip() or "/"
        if not value.startswith("/"):
            value = f"/{value}"
        if not value.endswith("/"):
            value = f"{value}/"
        return value

    @field_validator("two_factor_code")
    @classmethod
    def normalize_two_factor_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class ClientRecord(StateModel):
    id: Annotated[str, Field(min_length=1)]
    comment: str = ""
    sub_id: str | None = Field(default=None, alias="subId")

    @field_validator("id", "sub_id")
    @classmethod
    def strip_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("identifier must not be empty")
        return value

    @property
    def effective_sub_id(self) -> str:
        return self.sub_id or self.id


class NodeInboundRecord(StateModel):
    type: Literal["node-inbound"]
    label: Annotated[str, Field(min_length=1)]
    node_id: Annotated[int, Field(ge=1)] = Field(alias="nodeId")
    inbound_id: Annotated[int, Field(ge=1)] = Field(alias="inboundId")
    permanent_client_email: str | None = Field(default=None, alias="permanentClientEmail")

    @field_validator("permanent_client_email")
    @classmethod
    def normalize_permanent_client_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class ExternalInboundRecord(StateModel):
    type: Literal["external-inbound"]
    label: Annotated[str, Field(min_length=1)]
    uri: Annotated[str, Field(min_length=1)]


InboundRecord = Annotated[NodeInboundRecord | ExternalInboundRecord, Field(discriminator="type")]


class SubscriptionMetadata(StateModel):
    profile_title: str | None = Field(default=None, alias="profile-title")
    profile_update_interval: int | None = Field(default=None, alias="profile-update-interval")
    profile_web_page_url: str | None = Field(default=None, alias="profile-web-page-url")
    subscription_userinfo: str | None = Field(default=None, alias="subscription-userinfo")
    support_url: str | None = Field(default=None, alias="support-url")
    announce: str | None = None
    routing: str | None = None
    routing_enable: bool | None = Field(default=None, alias="routing-enable")


class ControlPlaneState(StateModel):
    nodes: list[NodeRecord]
    clients: list[ClientRecord]
    inbounds: list[InboundRecord]
    subscription: SubscriptionMetadata
