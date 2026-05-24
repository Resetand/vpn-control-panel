from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from vpn_control_plane.crons.base import validate_cron_expression


def _strip_wrapping_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1].strip()
    return value


def _split_csv(value: object) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        normalized = _strip_wrapping_quotes(value)
        return {_strip_wrapping_quotes(item) for item in normalized.split(",") if _strip_wrapping_quotes(item)}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(item).strip() for item in value if str(item).strip()}
    if isinstance(value, (int, float, bool)):
        text = str(value).strip()
        return {text} if text else set()
    text = str(value).strip()
    return {text} if text else set()


def _split_csv_or_wildcard(value: object) -> set[str] | None:
    if isinstance(value, str) and _strip_wrapping_quotes(value) == "*":
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

    data_file: Path = Field(default=Path("data.json"), validation_alias="VPN_DATA_FILE")
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
    telegram_allowed_user_ids: Annotated[set[str] | None, NoDecode] = Field(
        default_factory=set,
        validation_alias="VPN_TELEGRAM_ALLOWED_USER_IDS",
    )
    telegram_allowed_chat_id: int | None = Field(default=None, validation_alias="VPN_TELEGRAM_ALLOWED_CHAT_ID")
    admin_telegram_ids: Annotated[set[str], NoDecode] = Field(validation_alias="VPN_TELEGRAM_ADMIN_IDS")
    default_vless_flow: str = Field(default="xtls-rprx-vision", validation_alias="VPN_DEFAULT_VLESS_FLOW")
    backup_http_token: SecretStr | None = Field(default=None, validation_alias="BACKUP_HTTP_TOKEN")
    backup_secrets_ssh_key: str | None = Field(default=None, validation_alias="BACKUP_SECRETS_SSH_KEY")
    backup_secrets_env_file: Path = Field(default=Path(".env"), validation_alias="BACKUP_SECRETS_ENV_FILE")
    report_telegram_enabled: bool = Field(default=False, validation_alias="REPORT_TELEGRAM_ENABLED")
    report_telegram_schedule: str = Field(default="0 3 * * *", validation_alias="REPORT_TELEGRAM_SCHEDULE")
    geofiles_update_enabled: bool = Field(default=False, validation_alias="GEOFILES_UPDATE_ENABLED")
    geofiles_update_schedule: str = Field(default="0 3 * * *", validation_alias="GEOFILES_UPDATE_SCHEDULE")
    monitoring_alerts_enabled: bool = Field(default=False, validation_alias="VPN_MONITORING_ALERTS_ENABLED")
    monitoring_poll_interval_seconds: Annotated[int, Field(ge=1)] = Field(
        default=30,
        validation_alias="VPN_MONITORING_POLL_INTERVAL_SECONDS",
    )
    monitoring_failure_duration_seconds: Annotated[int, Field(ge=1)] = Field(
        default=60,
        validation_alias="VPN_MONITORING_FAILURE_DURATION_SECONDS",
    )
    monitoring_cpu_threshold_percent: Annotated[float, Field(ge=0, le=100)] = Field(
        default=90.0,
        validation_alias="VPN_MONITORING_CPU_THRESHOLD_PERCENT",
    )
    monitoring_ram_threshold_percent: Annotated[float, Field(ge=0, le=100)] = Field(
        default=90.0,
        validation_alias="VPN_MONITORING_RAM_THRESHOLD_PERCENT",
    )
    monitoring_alert_cooldown_seconds: Annotated[int, Field(ge=1)] = Field(
        default=3600,
        validation_alias="VPN_MONITORING_ALERT_COOLDOWN_SECONDS",
    )

    @field_validator("backup_secrets_ssh_key")
    @classmethod
    def normalize_backup_secrets_ssh_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = _strip_wrapping_quotes(value)
        return value or None

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def parse_allowed_user_ids(cls, value: object) -> set[str] | None:
        return _split_csv_or_wildcard(value)

    @field_validator("admin_telegram_ids", mode="before")
    @classmethod
    def parse_id_set(cls, value: object) -> set[str]:
        return _split_csv(value)

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

    @field_validator("report_telegram_schedule", "geofiles_update_schedule")
    @classmethod
    def validate_cron_schedule(cls, value: str) -> str:
        return validate_cron_expression(value)

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
