from __future__ import annotations

import html
import json
from importlib.resources import files
from typing import cast

PLATFORM_ORDER = ("ios", "android", "windows", "macos")


def build_subscription_caption(subscription_url: str) -> str:
    return "\n".join(["🔑 Ваш ключ-ссылка:", f"<code>{html.escape(subscription_url)}</code>"])


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
