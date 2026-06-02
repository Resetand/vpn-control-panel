from __future__ import annotations

import base64
import json
import tarfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.enums import ChatMemberStatus

import vpn_control_plane.backup.secrets as backup_secrets
from vpn_control_plane.backup import build_data_backup
from vpn_control_plane.config import Settings
from vpn_control_plane.data import ClientRecord, ControlPlaneStore
from vpn_control_plane.provisioning import ProvisioningResult
from vpn_control_plane.subscription import SubscriptionService, build_public_subscription_token
from vpn_control_plane.telegram.bot import (
    DEFAULT_MANUAL_CLIENT_COMMENT,
    HELP_TEXT,
    TelegramBotServices,
    command_argument,
    configure_bot_commands,
    configure_chat_commands,
    generate_qr_png,
    handle_announce,
    handle_backup,
    handle_help,
    handle_id,
    handle_issue,
    handle_plain_text,
    handle_set_routing,
    handle_start,
    handle_status,
    handle_unannounce,
    is_admin,
    is_allowed_user,
    is_valid_happ_routing,
)

JsonObject = dict[str, Any]


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def prepare_store(tmp_path: Path, *, subscription: JsonObject | None = None) -> ControlPlaneStore:
    write_json(
        tmp_path / "data.json",
        {
            "nodes": [],
            "externalInbounds": [],
            "clients": [],
            "defaultClientInboundTags": [],
            "subscription": subscription or {},
        },
    )
    return ControlPlaneStore(tmp_path / "data.json")


def settings(
    tmp_path: Path,
    *,
    backup_secrets_ssh_key: str | None = None,
    backup_secrets_env_file: Path | None = None,
) -> Settings:
    values = {
        "VPN_DATA_FILE": str(tmp_path / "data.json"),
        "VPN_SUBSCRIPTION_ROUTE": "/s/",
        "VPN_SUBSCRIPTION_DOMAIN": "example.test",
        "VPN_SUBSCRIPTION_PORT": "443",
        "VPN_SUBSCRIPTION_TOKEN_SALT": "global-salt",
        "VPN_TELEGRAM_BOT_TOKEN": "token",
        "VPN_TELEGRAM_ALLOWED_USER_IDS": "100,200",
        "VPN_TELEGRAM_ADMIN_IDS": "1",
    }
    if backup_secrets_ssh_key is not None:
        values["BACKUP_SECRETS_SSH_KEY"] = backup_secrets_ssh_key
    if backup_secrets_env_file is not None:
        values["BACKUP_SECRETS_ENV_FILE"] = str(backup_secrets_env_file)
    return Settings.model_validate(values)


class FakeUser:
    def __init__(self, user_id: int, *, first_name: str = "Kirill", username: str | None = "resetand") -> None:
        self.id = user_id
        self.first_name = first_name
        self.username = username


class FakeChat:
    def __init__(self, chat_type: str = "private") -> None:
        self.type = chat_type


class FakeMessage:
    def __init__(self, text: str, user_id: int, *, chat_type: str = "private") -> None:
        self.text = text
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat(chat_type)
        self.answers: list[dict[str, Any]] = []
        self.photos: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append({"text": text, "kwargs": kwargs})

    async def answer_photo(self, photo: object, **kwargs: Any) -> None:
        self.photos.append({"photo": photo, "kwargs": kwargs})

    async def answer_document(self, document: object, **kwargs: Any) -> None:
        self.documents.append({"document": document, "kwargs": kwargs})


class FakeProvisioning:
    def __init__(self) -> None:
        self.telegram_calls: list[dict[str, Any]] = []
        self.issue_calls: list[str] = []

    async def ensure_telegram_user(
        self,
        telegram_user_id: int,
        *,
        comment: str,
        username: str | None,
    ) -> ProvisioningResult:
        self.telegram_calls.append({"id": telegram_user_id, "comment": comment, "username": username})
        return ProvisioningResult(client=ClientRecord(id=str(telegram_user_id), comment=comment), created=1, reused=0)

    async def issue_manual_client(self, *, comment: str) -> ProvisioningResult:
        self.issue_calls.append(comment)
        return ProvisioningResult(client=ClientRecord(id="manual-1", comment=comment), created=1, reused=0)


class FakeBot:
    def __init__(self, status: ChatMemberStatus | Exception) -> None:
        self.status = status
        self.calls: list[dict[str, int]] = []

    async def get_chat_member(self, *, chat_id: int, user_id: int) -> object:
        self.calls.append({"chat_id": chat_id, "user_id": user_id})
        if isinstance(self.status, Exception):
            raise self.status
        return SimpleNamespace(status=self.status)


class FakeCommandBot:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def set_my_commands(self, commands: object, *, scope: object) -> None:
        self.calls.append({"commands": commands, "scope": scope})


def services(
    tmp_path: Path,
    provisioning: FakeProvisioning | None = None,
    *,
    app_settings: Settings | None = None,
) -> TelegramBotServices:
    store = prepare_store(tmp_path)
    app_settings = app_settings or settings(tmp_path)
    return TelegramBotServices(
        settings=app_settings,
        provisioning=cast(Any, provisioning or FakeProvisioning()),
        subscription=SubscriptionService(
            store,
            public_base_url=app_settings.public_subscription_base_url,
            token_salt=app_settings.subscription_token_salt.get_secret_value()
            if app_settings.subscription_token_salt
            else None,
        ),
        store=store,
    )


def test_access_control_helpers(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)

    assert is_admin(app_settings, 1)
    assert is_allowed_user(app_settings, 100)
    assert not is_allowed_user(app_settings, 999)


def test_start_access_policy_denies_all_when_allowed_users_are_unset(tmp_path: Path) -> None:
    app_settings = Settings.model_validate(
        {
            "VPN_DATA_FILE": str(tmp_path / "data.json"),
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_SUBSCRIPTION_PORT": "443",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ALLOWED_USER_IDS": "",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )

    assert not is_allowed_user(app_settings, 1)
    assert not is_allowed_user(app_settings, 100)


def test_start_access_policy_supports_wildcard_allowed_users(tmp_path: Path) -> None:
    app_settings = Settings.model_validate(
        {
            "VPN_DATA_FILE": str(tmp_path / "data.json"),
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ALLOWED_USER_IDS": "*",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )

    assert is_allowed_user(app_settings, 999)


def test_command_argument_parsing() -> None:
    assert command_argument("/issue Router kitchen") == "Router kitchen"
    assert command_argument("/issue") == ""
    assert command_argument(None) == ""


@pytest.mark.asyncio
async def test_configure_bot_commands_sets_default_and_admin_scoped_menus(tmp_path: Path) -> None:
    bot = FakeCommandBot()

    await configure_bot_commands(cast(Any, bot), settings(tmp_path))

    assert [command.command for command in bot.calls[0]["commands"]] == ["start", "help", "status", "id"]
    assert bot.calls[0]["scope"].type == "default"
    assert [command.command for command in bot.calls[1]["commands"]] == ["start", "help", "status", "id"]
    assert bot.calls[1]["scope"].type == "all_private_chats"
    assert [command.command for command in bot.calls[2]["commands"]] == [
        "start",
        "help",
        "status",
        "id",
        "issue",
        "announce",
        "unannounce",
        "backup",
        "set_routing",
    ]
    assert bot.calls[2]["scope"].type == "chat"
    assert bot.calls[2]["scope"].chat_id == 1


@pytest.mark.asyncio
async def test_configure_chat_commands_sets_user_specific_scope(tmp_path: Path) -> None:
    bot = FakeCommandBot()
    app_settings = settings(tmp_path)

    await configure_chat_commands(cast(Any, bot), app_settings, 100)
    await configure_chat_commands(cast(Any, bot), app_settings, 1)

    assert [command.command for command in bot.calls[0]["commands"]] == ["start", "help", "status", "id"]
    assert bot.calls[0]["scope"].type == "chat"
    assert bot.calls[0]["scope"].chat_id == 100
    assert [command.command for command in bot.calls[1]["commands"]] == [
        "start",
        "help",
        "status",
        "id",
        "issue",
        "announce",
        "unannounce",
        "backup",
        "set_routing",
    ]
    assert bot.calls[1]["scope"].type == "chat"
    assert bot.calls[1]["scope"].chat_id == 1


def test_qr_png_is_generated() -> None:
    png = generate_qr_png("https://example.test/sub/100")

    assert png.startswith(b"\x89PNG")


def valid_routing() -> str:
    payload = base64.b64encode(b'{"Name":"RU Direct"}').decode("ascii").rstrip("=")
    return f"happ://routing/onadd/{payload}"


def test_routing_validation_accepts_happ_routing_with_base64_json() -> None:
    assert is_valid_happ_routing(valid_routing())
    assert is_valid_happ_routing(valid_routing().replace("onadd", "add"))


def test_routing_validation_rejects_wrong_prefix_or_invalid_payload() -> None:
    assert not is_valid_happ_routing("https://example.test/routing")
    assert not is_valid_happ_routing("happ://routing/onadd/not-json")
    assert not is_valid_happ_routing("happ://routing/onadd/eyJicm9rZW4i")


def test_build_data_backup_contains_only_control_plane_json_files(tmp_path: Path) -> None:
    store = prepare_store(tmp_path, subscription={"announce": "Maintenance"})
    write_json(tmp_path / "runtime-cache.json", {"ignored": True})

    backup = build_data_backup(store.data_file)

    with tarfile.open(fileobj=BytesIO(backup), mode="r:gz") as archive:
        assert sorted(archive.getnames()) == ["data.json"]
        data_file = archive.extractfile("data.json")
        assert data_file is not None
        assert json.loads(data_file.read().decode("utf-8"))["subscription"] == {"announce": "Maintenance"}


@pytest.mark.asyncio
async def test_start_denies_unauthorized_user_without_provisioning(tmp_path: Path) -> None:
    provisioning = FakeProvisioning()
    message = FakeMessage("/start", 999)

    await handle_start(cast(Any, message), services(tmp_path, provisioning))

    assert provisioning.telegram_calls == []
    assert message.photos == []
    assert message.answers[-1]["text"] == "Доступ запрещен. Обратитесь к администратору."


@pytest.mark.asyncio
async def test_start_provisions_allowed_private_user_and_sends_url_qr_and_instructions(tmp_path: Path) -> None:
    provisioning = FakeProvisioning()
    message = FakeMessage("/start", 100)

    await handle_start(cast(Any, message), services(tmp_path, provisioning))

    assert provisioning.telegram_calls == [{"id": 100, "comment": "Kirill", "username": "resetand"}]
    assert len(message.photos) == 1
    token = build_public_subscription_token("100", "global-salt")
    assert f"https://example.test/s/{token}" in message.photos[0]["kwargs"]["caption"]
    assert message.answers[-1]["kwargs"] == {"parse_mode": "HTML", "disable_web_page_preview": True}


@pytest.mark.asyncio
async def test_help_status_id_and_plain_text_have_minimal_responses(tmp_path: Path) -> None:
    bot_services = services(tmp_path)
    help_message = FakeMessage("/help", 100)
    status_message = FakeMessage("/status", 100)
    id_message = FakeMessage("/id", 100)
    plain_message = FakeMessage("hello", 100)

    await handle_help(cast(Any, help_message), bot_services)
    await handle_status(cast(Any, status_message), bot_services)
    await handle_id(cast(Any, id_message), bot_services)
    await handle_plain_text(cast(Any, plain_message), bot_services)

    assert help_message.answers == [{"text": HELP_TEXT, "kwargs": {}}]
    assert status_message.answers == [{"text": "Бот работает.", "kwargs": {}}]
    assert id_message.answers == [{"text": "Ваш Telegram ID: 100", "kwargs": {}}]
    assert plain_message.answers == [{"text": HELP_TEXT, "kwargs": {}}]


@pytest.mark.asyncio
async def test_start_uses_allowed_chat_membership_when_configured(tmp_path: Path) -> None:
    app_settings = Settings.model_validate(
        {
            "VPN_DATA_FILE": str(tmp_path / "data.json"),
            "VPN_SUBSCRIPTION_ROUTE": "/sub/",
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ALLOWED_CHAT_ID": "-100123",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )
    provisioning = FakeProvisioning()
    message = FakeMessage("/start", 100)
    bot = FakeBot(ChatMemberStatus.MEMBER)

    await handle_start(cast(Any, message), services(tmp_path, provisioning, app_settings=app_settings), cast(Any, bot))

    assert bot.calls == [{"chat_id": -100123, "user_id": 100}]
    assert provisioning.telegram_calls == [{"id": 100, "comment": "Kirill", "username": "resetand"}]


@pytest.mark.asyncio
async def test_start_denies_non_member_when_allowed_chat_is_configured(tmp_path: Path) -> None:
    app_settings = Settings.model_validate(
        {
            "VPN_DATA_FILE": str(tmp_path / "data.json"),
            "VPN_SUBSCRIPTION_ROUTE": "/sub/",
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ALLOWED_CHAT_ID": "-100123",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )
    provisioning = FakeProvisioning()
    message = FakeMessage("/start", 100)
    bot = FakeBot(ChatMemberStatus.LEFT)

    await handle_start(cast(Any, message), services(tmp_path, provisioning, app_settings=app_settings), cast(Any, bot))

    assert provisioning.telegram_calls == []
    assert message.photos == []
    assert message.answers[-1]["text"] == "Доступ запрещен. Обратитесь к администратору."


@pytest.mark.asyncio
async def test_start_in_group_chat_does_not_send_subscription_material(tmp_path: Path) -> None:
    provisioning = FakeProvisioning()
    message = FakeMessage("/start", 100, chat_type="group")

    await handle_start(cast(Any, message), services(tmp_path, provisioning))

    assert provisioning.telegram_calls == []
    assert message.photos == []
    assert message.answers == [{"text": "Напишите мне в личные сообщения, чтобы получить VPN-доступ.", "kwargs": {}}]


@pytest.mark.asyncio
async def test_issue_is_admin_only_and_does_not_mutate_for_non_admin(tmp_path: Path) -> None:
    provisioning = FakeProvisioning()
    message = FakeMessage("/issue Router kitchen", 100)

    await handle_issue(cast(Any, message), services(tmp_path, provisioning))

    assert provisioning.issue_calls == []
    assert message.photos == []
    assert message.answers == [{"text": "Доступ запрещен.", "kwargs": {}}]


@pytest.mark.asyncio
async def test_issue_creates_manual_client_with_comment_and_sends_material(tmp_path: Path) -> None:
    provisioning = FakeProvisioning()
    message = FakeMessage("/issue Router kitchen", 1)

    await handle_issue(cast(Any, message), services(tmp_path, provisioning))

    assert provisioning.issue_calls == ["Router kitchen"]
    token = build_public_subscription_token("manual-1", "global-salt")
    assert f"https://example.test/s/{token}" in message.photos[0]["kwargs"]["caption"]


@pytest.mark.asyncio
async def test_issue_without_comment_uses_safe_default(tmp_path: Path) -> None:
    provisioning = FakeProvisioning()
    message = FakeMessage("/issue", 1)

    await handle_issue(cast(Any, message), services(tmp_path, provisioning))

    assert provisioning.issue_calls == [DEFAULT_MANUAL_CLIENT_COMMENT]


@pytest.mark.asyncio
async def test_announce_and_unannounce_mutate_subscription_metadata(tmp_path: Path) -> None:
    bot_services = services(tmp_path)

    await handle_announce(cast(Any, FakeMessage("/announce Maintenance tonight", 1)), bot_services)
    assert bot_services.store.load_subscription().announce == "Maintenance tonight"

    await handle_unannounce(cast(Any, FakeMessage("/unannounce", 1)), bot_services)
    assert bot_services.store.load_subscription().announce is None


@pytest.mark.asyncio
async def test_announce_is_admin_only_and_requires_text(tmp_path: Path) -> None:
    bot_services = services(tmp_path)
    non_admin_message = FakeMessage("/announce No", 100)

    await handle_announce(cast(Any, non_admin_message), bot_services)
    assert bot_services.store.load_subscription().announce is None
    assert non_admin_message.answers == [{"text": "Доступ запрещен.", "kwargs": {}}]

    admin_message = FakeMessage("/announce", 1)
    await handle_announce(cast(Any, admin_message), bot_services)
    assert admin_message.answers == [{"text": "Укажите текст объявления: /announce <text>", "kwargs": {}}]


@pytest.mark.asyncio
async def test_backup_is_admin_only_private_and_sends_archive(tmp_path: Path) -> None:
    bot_services = services(tmp_path)
    non_admin_message = FakeMessage("/backup", 100)
    group_message = FakeMessage("/backup", 1, chat_type="group")
    admin_message = FakeMessage("/backup", 1)

    await handle_backup(cast(Any, non_admin_message), bot_services)
    await handle_backup(cast(Any, group_message), bot_services)
    await handle_backup(cast(Any, admin_message), bot_services)

    assert non_admin_message.documents == []
    assert non_admin_message.answers == [{"text": "Доступ запрещен.", "kwargs": {}}]
    assert group_message.documents == []
    assert group_message.answers == [
        {"text": "Напишите мне в личные сообщения, чтобы получить VPN-доступ.", "kwargs": {}}
    ]
    assert len(admin_message.documents) == 1
    assert admin_message.documents[0]["document"].filename == "vpn-control-plane-backup.tar.gz"
    assert admin_message.documents[0]["kwargs"] == {"caption": "Бекап control-plane, секретов и 3x-UI нод."}


@pytest.mark.asyncio
async def test_backup_sends_encrypted_secrets_archive_when_ssh_key_is_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("VPN_TELEGRAM_BOT_TOKEN=secret\n", encoding="utf-8")
    monkeypatch.setattr(backup_secrets, "encrypt_for_ssh_public_key", lambda _plaintext, _key: b"encrypted-secrets")
    app_settings = settings(
        tmp_path,
        backup_secrets_ssh_key="ssh-ed25519 AAAATEST backup",
        backup_secrets_env_file=env_file,
    )
    bot_services = services(tmp_path, app_settings=app_settings)
    admin_message = FakeMessage("/backup", 1)

    await handle_backup(cast(Any, admin_message), bot_services)

    assert len(admin_message.documents) == 1
    assert admin_message.documents[0]["document"].filename == "vpn-control-plane-backup.tar.gz"
    assert admin_message.documents[0]["kwargs"] == {"caption": "Бекап control-plane, секретов и 3x-UI нод."}


@pytest.mark.asyncio
async def test_set_routing_is_admin_only_and_persists_valid_routing(tmp_path: Path) -> None:
    bot_services = services(tmp_path)
    routing = valid_routing()
    non_admin_message = FakeMessage(f"/set-routing {routing}", 100)
    admin_message = FakeMessage(f"/set-routing {routing}", 1)

    await handle_set_routing(cast(Any, non_admin_message), bot_services)
    assert bot_services.store.load_subscription().routing is None
    assert non_admin_message.answers == [{"text": "Доступ запрещен.", "kwargs": {}}]

    await handle_set_routing(cast(Any, admin_message), bot_services)
    assert bot_services.store.load_subscription().routing == routing
    assert admin_message.answers == [{"text": "Routing обновлен.", "kwargs": {}}]


@pytest.mark.asyncio
async def test_set_routing_rejects_missing_or_invalid_routing_without_mutating_state(tmp_path: Path) -> None:
    bot_services = services(tmp_path)
    missing_message = FakeMessage("/set-routing", 1)
    invalid_message = FakeMessage("/set-routing happ://routing/onadd/not-json", 1)

    await handle_set_routing(cast(Any, missing_message), bot_services)
    await handle_set_routing(cast(Any, invalid_message), bot_services)

    assert bot_services.store.load_subscription().routing is None
    assert missing_message.answers == [
        {"text": "Укажите routing строку: /set_routing happ://routing/onadd/<base64-json>", "kwargs": {}}
    ]
    assert invalid_message.answers == [
        {
            "text": "Routing строка невалидна: ожидается happ://routing/onadd или happ://routing/add с base64 JSON.",
            "kwargs": {},
        }
    ]
