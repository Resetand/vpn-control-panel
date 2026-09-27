from __future__ import annotations

import html
import json
from importlib.resources import files
from typing import cast

PLATFORM_ORDER = ("ios", "android", "windows", "macos")


SUBSCRIPTION_CAPTION = "🔑 Ваш ключ-ссылка — в следующем сообщении, нажмите на нее, чтобы скопировать 👇"


def build_subscription_url_message(subscription_url: str) -> str:
    # The link travels alone so that copying the message copies nothing but the link.
    return f"<code>{html.escape(subscription_url)}</code>"


def build_setup_instructions() -> str:
    clients = _load_recommended_clients()
    parts = [
        "🟢 <b>VPN настроен!</b>",
        "Теперь нужно подключиться — это займёт 1–2 минуты 👇",
        "",
        "📱 <b>Шаг 1. Установите приложение-клиент</b>",
        "",
        *_build_client_lines(clients),
        "",
        "🔗 <b>Шаг 2. Добавьте VPN</b>",
        "",
        "1. Скопируйте ключ-ссылку (из сообщения выше 🔼)",
        "2. Откройте приложение-клиент",
        "3. Импортируйте конфигурацию — отсканируйте QR или вставьте ссылку",
        "",
        "▶️ <b>Шаг 3. Подключитесь</b>",
        "",
        "1. Выберите сервер (рекомендованный отмечен звездочкой – ⭐)",
        "2. Нажмите «Подключиться» и разрешите добавление VPN-конфигурации",
        "",
        "✅ <b>Готово!</b> Теперь интернет работает через VPN.",
    ]
    return "\n".join(parts)


def build_abroad_instructions() -> str:
    parts = [
        "🌍 <b>Ссылка для жизни за границей</b>",
        "",
        "В ней выключена маршрутизация: весь трафик идет через выбранный сервер, включая российские сайты.",
        "",
        "Установите ее вместо текущей подписки:",
        "1. Удалите старую подписку в приложении-клиенте",
        "2. Добавьте эту ссылку — отсканируйте QR или вставьте ее",
    ]
    return "\n".join(parts)


def _load_recommended_clients() -> dict[str, dict[str, str]]:
    content = files(__package__).joinpath("clients_recommended.json").read_text(encoding="utf-8")
    return cast(dict[str, dict[str, str]], json.loads(content))


def _build_client_lines(clients: dict[str, dict[str, str]]) -> list[str]:
    lines: list[str] = []
    for key in PLATFORM_ORDER:
        client = clients.get(key)
        if client is None:
            continue

        platform = html.escape(client["platform"])
        name = html.escape(client["name"])
        url = html.escape(client["url"], quote=True)
        lines.append(f'⋅ {platform}: <a href="{url}">{name}</a>')
    return lines
