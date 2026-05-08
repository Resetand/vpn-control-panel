from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass
from io import BytesIO

import qrcode  # type: ignore[import-untyped]
from aiogram import Bot, Dispatcher
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, Message

from vpn_control_plane.config import Settings
from vpn_control_plane.data import (
    BACKUP_FILE_NAME,
    SECRETS_BACKUP_FILE_NAME,
    JsonStateStore,
    SecretsBackupError,
    SubscriptionMetadata,
    build_data_backup,
    build_secrets_backup,
)
from vpn_control_plane.provisioning import ProvisioningError, ProvisioningResult, ProvisioningService
from vpn_control_plane.subscription import SubscriptionService
from vpn_control_plane.telegram.setup_messages import build_setup_instructions, build_subscription_caption

logger = logging.getLogger(__name__)

DEFAULT_MANUAL_CLIENT_COMMENT = "manual"
ROUTING_PREFIXES = ("happ://routing/onadd/", "happ://routing/add/")
ALLOWED_CHAT_MEMBER_STATUSES = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}


@dataclass(frozen=True)
class TelegramBotServices:
    settings: Settings
    provisioning: ProvisioningService
    subscription: SubscriptionService
    store: JsonStateStore


def create_services(settings: Settings, store: JsonStateStore) -> TelegramBotServices:
    return TelegramBotServices(
        settings=settings,
        provisioning=ProvisioningService(store, default_vless_flow=settings.default_vless_flow),
        subscription=SubscriptionService(store, public_base_url=settings.public_subscription_base_url),
        store=store,
    )


def create_dispatcher(services: TelegramBotServices) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher["services"] = services
    dispatcher.message.register(handle_start, CommandStart())
    dispatcher.message.register(handle_issue, Command("issue"))
    dispatcher.message.register(handle_announce, Command("announce"))
    dispatcher.message.register(handle_unannounce, Command("unannounce"))
    dispatcher.message.register(handle_backup, Command("backup"))
    dispatcher.message.register(handle_set_routing, Command("set-routing"))
    return dispatcher


def create_bot(settings: Settings) -> Bot:
    return Bot(token=settings.telegram_bot_token.get_secret_value())


async def run_telegram_bot(settings: Settings, store: JsonStateStore) -> None:
    bot = create_bot(settings)
    dispatcher = create_dispatcher(create_services(settings, store))
    try:
        logger.info("Starting Telegram bot polling")
        await dispatcher.start_polling(bot)
    finally:
        logger.info("Stopping Telegram bot polling")
        await bot.session.close()


def is_admin(settings: Settings, user_id: int | str) -> bool:
    return str(user_id) in settings.admin_telegram_ids


def is_allowed_user(settings: Settings, user_id: int | str) -> bool:
    if settings.telegram_allowed_user_ids is None:
        return True
    user_id_text = str(user_id)
    return user_id_text in settings.telegram_allowed_user_ids


async def handle_start(message: Message, services: TelegramBotServices, bot: Bot | None = None) -> None:
    user = message.from_user
    if user is None:
        return
    if not _is_private_chat(message):
        await message.answer("Напишите мне в личные сообщения, чтобы получить VPN-доступ.")
        return
    if not await _is_user_allowed_for_start(services.settings, bot, user.id):
        logger.info("Access denied for /start: user %s is not allowed", user.id)
        await message.answer("Доступ запрещен. Обратитесь к администратору.")
        return

    await message.answer("Настраиваю VPN-доступ, подождите...")
    try:
        result = await services.provisioning.ensure_telegram_user(
            user.id,
            comment=user.first_name or "Telegram user",
            username=user.username,
        )
    except ProvisioningError:
        logger.exception("Provisioning failed for Telegram user %s", user.id)
        await message.answer("Не удалось настроить VPN. Попробуйте позже или обратитесь к администратору.")
        return

    await send_subscription_material(message, services, result)


async def handle_issue(message: Message, services: TelegramBotServices) -> None:
    user = message.from_user
    if user is None:
        return
    if not await _ensure_private(message):
        return
    if not await _ensure_admin(message, services.settings, user.id, command="/issue"):
        return

    comment = command_argument(message.text) or DEFAULT_MANUAL_CLIENT_COMMENT
    await message.answer("Создаю VPN-клиента...")
    try:
        result = await services.provisioning.issue_manual_client(comment=comment)
    except ProvisioningError:
        logger.exception("Manual provisioning failed for admin %s", user.id)
        await message.answer("Не удалось создать VPN-клиента. Попробуйте позже.")
        return

    await send_subscription_material(message, services, result)


async def handle_announce(message: Message, services: TelegramBotServices) -> None:
    user = message.from_user
    if user is None:
        return
    if not await _ensure_admin(message, services.settings, user.id, command="/announce"):
        return

    announcement = command_argument(message.text)
    if not announcement:
        await message.answer("Укажите текст объявления: /announce <text>")
        return

    current = services.store.load_subscription()
    services.store.save_subscription(current.model_copy(update={"announce": announcement}))
    await message.answer("Объявление обновлено.")


async def handle_unannounce(message: Message, services: TelegramBotServices) -> None:
    user = message.from_user
    if user is None:
        return
    if not await _ensure_admin(message, services.settings, user.id, command="/unannounce"):
        return

    current = services.store.load_subscription()
    services.store.save_subscription(current.model_copy(update={"announce": None}))
    await message.answer("Объявление очищено.")


async def handle_backup(message: Message, services: TelegramBotServices) -> None:
    user = message.from_user
    if user is None:
        return
    if not await _ensure_private(message):
        return
    if not await _ensure_admin(message, services.settings, user.id, command="/backup"):
        return

    backup = build_data_backup(services.store.data_dir)
    await message.answer_document(
        BufferedInputFile(backup, filename=BACKUP_FILE_NAME),
        caption="Бекап данных control-plane.",
    )
    if services.settings.backup_secrets_ssh_key is None:
        return

    try:
        secrets_backup = build_secrets_backup(
            services.settings.backup_secrets_env_file,
            services.settings.backup_secrets_ssh_key,
        )
    except SecretsBackupError:
        logger.exception("Secrets backup failed")
        await message.answer("Бекап данных создан, но не удалось зашифровать бекап секретов.")
        return

    await message.answer_document(
        BufferedInputFile(secrets_backup, filename=SECRETS_BACKUP_FILE_NAME),
        caption="Зашифрованный бекап секретов.",
    )


async def handle_set_routing(message: Message, services: TelegramBotServices) -> None:
    user = message.from_user
    if user is None:
        return
    if not await _ensure_admin(message, services.settings, user.id, command="/set-routing"):
        return

    routing = command_argument(message.text)
    if not routing:
        await message.answer("Укажите routing строку: /set-routing happ://routing/onadd/<base64-json>")
        return
    if not is_valid_happ_routing(routing):
        await message.answer(
            "Routing строка невалидна: ожидается happ://routing/onadd или happ://routing/add с base64 JSON."
        )
        return

    current = services.store.load_subscription()
    services.store.save_subscription(current.model_copy(update={"routing": routing}))
    await message.answer("Routing обновлен.")


async def send_subscription_material(
    message: Message,
    services: TelegramBotServices,
    result: ProvisioningResult,
) -> None:
    subscription_url = services.subscription.public_url_for_client(result.client)
    await message.answer_photo(
        BufferedInputFile(generate_qr_png(subscription_url), filename="subscription_qr.png"),
        caption=build_subscription_caption(subscription_url),
        parse_mode="HTML",
    )
    await message.answer(build_setup_instructions(), parse_mode="HTML", disable_web_page_preview=True)


def generate_qr_png(data: str) -> bytes:
    image = qrcode.make(data)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def is_valid_happ_routing(value: str) -> bool:
    payload = ""
    for prefix in ROUTING_PREFIXES:
        if value.startswith(prefix):
            payload = value.removeprefix(prefix)
            break
    if not payload:
        return False

    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.b64decode(payload + padding, validate=True).decode("utf-8")
        json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


def command_argument(text: str | None) -> str:
    if not text:
        return ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def update_subscription_announcement(current: SubscriptionMetadata, announcement: str | None) -> SubscriptionMetadata:
    return current.model_copy(update={"announce": announcement})


async def _is_user_allowed_for_start(settings: Settings, bot: Bot | None, user_id: int) -> bool:
    if settings.telegram_allowed_chat_id is not None:
        return await _is_chat_member(bot, user_id, settings.telegram_allowed_chat_id)
    return is_allowed_user(settings, user_id)


async def _is_chat_member(bot: Bot | None, user_id: int, chat_id: int) -> bool:
    if bot is None:
        logger.warning("Access denied for user %s: Telegram bot instance is unavailable", user_id)
        return False
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception:
        logger.exception("Failed to check membership for user %s in chat %s", user_id, chat_id)
        return False
    if member.status in ALLOWED_CHAT_MEMBER_STATUSES:
        return True
    logger.info("Access denied for user %s: status=%s in chat %s", user_id, member.status, chat_id)
    return False


async def _ensure_admin(message: Message, settings: Settings, user_id: int | str, *, command: str) -> bool:
    if is_admin(settings, user_id):
        return True
    logger.info("Access denied for %s: user %s is not admin", command, user_id)
    await message.answer("Доступ запрещен.")
    return False


async def _ensure_private(message: Message) -> bool:
    if _is_private_chat(message):
        return True
    await message.answer("Напишите мне в личные сообщения, чтобы получить VPN-доступ.")
    return False


def _is_private_chat(message: Message) -> bool:
    return getattr(message.chat, "type", None) == "private"
