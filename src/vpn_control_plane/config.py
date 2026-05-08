from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: str | list[str] | tuple[str, ...] | set[str] | None) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    return {str(item).strip() for item in value if str(item).strip()}


def _split_csv_or_wildcard(value: str | list[str] | tuple[str, ...] | set[str] | None) -> set[str] | None:
    if isinstance(value, str) and value.strip() == "*":
        return None
    return _split_csv(value)


def normalize_subscription_route(value: str) -> str:
    value = value.strip().strip("/")
    if not value:
        raise ValueError("subscription route must not be empty")
    return f"/{value}/"


def build_public_subscription_base_url(domain: str, port: int, route: str) -> str:
    host = domain.strip().rstrip("/")
    if not host:
        raise ValueError("subscription domain must not be empty")
    port_suffix = "" if port == 443 else f":{port}"
    return f"https://{host}{port_suffix}{route.rstrip('/')}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="VPN_",
        extra="ignore",
    )

    data_dir: Path = Field(default=Path("data"), validation_alias="VPN_DATA_DIR")
    http_host: str = Field(default="0.0.0.0", validation_alias="VPN_HTTP_HOST")
    http_port: Annotated[int, Field(ge=1, le=65535)] = Field(default=8080, validation_alias="VPN_HTTP_PORT")
    subscription_route: str = Field(default="/sub/", validation_alias="VPN_SUBSCRIPTION_ROUTE")
    subscription_domain: str = Field(default="example.com", validation_alias="VPN_SUBSCRIPTION_DOMAIN")
    subscription_port: Annotated[int, Field(ge=1, le=65535)] = Field(
        default=443,
        validation_alias="VPN_SUBSCRIPTION_PORT",
    )
    subscription_cert_path: Path = Field(default=Path("./certs"), validation_alias="VPN_SUBSCRIPTION_CERT_PATH")
    telegram_bot_token: SecretStr = Field(validation_alias="VPN_TELEGRAM_BOT_TOKEN")
    telegram_allowed_user_ids: set[str] | None = Field(
        default_factory=set,
        validation_alias="VPN_TELEGRAM_ALLOWED_USER_IDS",
    )
    telegram_allowed_chat_id: int | None = Field(default=None, validation_alias="VPN_TELEGRAM_ALLOWED_CHAT_ID")
    admin_telegram_ids: set[str] = Field(validation_alias="VPN_TELEGRAM_ADMIN_IDS")
    default_vless_flow: str = Field(default="xtls-rprx-vision", validation_alias="VPN_DEFAULT_VLESS_FLOW")
    backup_http_token: SecretStr | None = Field(default=None, validation_alias="BACKUP_HTTP_TOKEN")
    backup_secrets_ssh_key: str | None = Field(default=None, validation_alias="BACKUP_SECRETS_SSH_KEY")
    backup_secrets_env_file: Path = Field(default=Path(".env"), validation_alias="BACKUP_SECRETS_ENV_FILE")

    @field_validator("backup_secrets_ssh_key")
    @classmethod
    def normalize_backup_secrets_ssh_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def parse_allowed_user_ids(cls, value: object) -> set[str] | None:
        return _split_csv_or_wildcard(value)  # type: ignore[arg-type]

    @field_validator("admin_telegram_ids", mode="before")
    @classmethod
    def parse_id_set(cls, value: object) -> set[str]:
        return _split_csv(value)  # type: ignore[arg-type]

    @field_validator("telegram_allowed_chat_id", mode="before")
    @classmethod
    def parse_optional_chat_id(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("default_vless_flow")
    @classmethod
    def strip_default_vless_flow(cls, value: str) -> str:
        return value.strip()

    @field_validator("subscription_domain")
    @classmethod
    def strip_subscription_domain(cls, value: str) -> str:
        value = value.strip().removeprefix("https://").removeprefix("http://").strip("/")
        if not value:
            raise ValueError("subscription domain must not be empty")
        return value

    @field_validator("subscription_route")
    @classmethod
    def normalize_subscription_route_value(cls, value: str) -> str:
        return normalize_subscription_route(value)

    @property
    def public_subscription_base_url(self) -> str:
        return build_public_subscription_base_url(
            self.subscription_domain,
            self.subscription_port,
            self.subscription_route,
        )

