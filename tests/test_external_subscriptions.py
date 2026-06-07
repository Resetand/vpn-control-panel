from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from vpn_control_plane.config import Settings
from vpn_control_plane.data import (
    ControlPlaneStore,
    ExternalSubscriptionRecord,
    StateValidationError,
    parse_external_subscription_ref,
)
from vpn_control_plane.external_subscriptions import (
    ExternalSubscriptionService,
    ResolvedExternalInbound,
    ResolvedInboundsStore,
    assign_slugs,
    build_resolved_inbounds,
    parse_subscription_body,
    resolve_reference,
    slugify,
)
from vpn_control_plane.subscription import SubscriptionService

JsonObject = dict[str, Any]

NL_GEO = "vless://uuid@1.1.1.1:443?type=tcp#%F0%9F%87%B1%F0%9F%87%B9%20%D0%9B%D0%B8%D1%82%D0%B2%D0%B0"  # 🇱🇹 Литва
FI_WL = "vless://uuid@2.2.2.2:443?type=tcp#%F0%9F%87%AB%F0%9F%87%AE%20Finland%2C%20Extra%20Whitelist%20Bravo"


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


# --------------------------------------------------------------------------- parser


def test_parser_plain_body_strips_fragment_and_decodes_label() -> None:
    entries = parse_subscription_body(f"{NL_GEO}\n{FI_WL}\n# not a link\n")

    assert len(entries) == 2
    assert entries[0].uri == "vless://uuid@1.1.1.1:443?type=tcp"
    assert entries[0].label == "🇱🇹 Литва"
    assert entries[1].label == "🇫🇮 Finland, Extra Whitelist Bravo"


def test_parser_decodes_base64_body() -> None:
    encoded = base64.b64encode(f"{NL_GEO}\n{FI_WL}\n".encode()).decode("ascii")

    assert [entry.label for entry in parse_subscription_body(encoded)] == [
        "🇱🇹 Литва",
        "🇫🇮 Finland, Extra Whitelist Bravo",
    ]


# --------------------------------------------------------------------------- slug


def test_slugify_drops_flags_and_keeps_cyrillic() -> None:
    assert slugify("🇳🇱 Netherlands, Extra Whitelist Delta") == "netherlands-extra-whitelist-delta"
    assert slugify("Амстердам, Нидерланды, Extra") == "амстердам-нидерланды-extra"
    assert slugify("🇸🇪") == ""


def test_assign_slugs_is_deterministic_with_collision_suffixes() -> None:
    entries = parse_subscription_body(
        "\n".join(
            [
                "vless://b#Dup",
                "vless://a#Dup",
                "vless://c",  # no fragment -> untitled
            ]
        )
    )

    slugs = {entry.uri: slug for slug, entry in assign_slugs(entries)}

    # Sorted by uri: a -> dup, b -> dup-2; empty fragment -> untitled.
    assert slugs == {"vless://a": "dup", "vless://b": "dup-2", "vless://c": "untitled"}


# --------------------------------------------------------------------------- reference parsing / resolution


def test_parse_reference_variants() -> None:
    assert parse_external_subscription_ref("vless://literal") is None

    exact = parse_external_subscription_ref("@blanc:стокгольм-швеция-extra")
    assert (
        exact is not None and exact.name == "blanc" and exact.query == "стокгольм-швеция-extra" and not exact.is_regex
    )

    regex = parse_external_subscription_ref("@blanc:~литва")
    assert regex is not None and regex.is_regex and regex.query == "литва"


@pytest.mark.parametrize("bad", ["@blanc", "@blanc:", "@:slug", "@blanc:~"])
def test_parse_reference_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_external_subscription_ref(bad)


def _resolved() -> dict[str, list[ResolvedExternalInbound]]:
    return {
        "blanc": [
            ResolvedExternalInbound(
                slug="вильнюс-литва-extra", label="🇱🇹 Вильнюс, Литва", uri="vless://lt", updated_at="t"
            ),
            ResolvedExternalInbound(slug="осло-extra", label="🇳🇴 Осло", uri="vless://no", updated_at="t"),
        ]
    }


def test_resolve_reference_literal_passthrough() -> None:
    assert resolve_reference("wireguard://x#WG", {}) == "wireguard://x#WG"


def test_resolve_reference_exact_and_regex_and_missing() -> None:
    resolved = _resolved()
    assert resolve_reference("@blanc:вильнюс-литва-extra", resolved) == "vless://lt"
    assert resolve_reference("@blanc:~литва", resolved) == "vless://lt"
    assert resolve_reference("@blanc:does-not-exist", resolved) is None
    assert resolve_reference("@blanc:~непонятно", resolved) is None
    assert resolve_reference("@unknown:~x", resolved) is None


# --------------------------------------------------------------------------- resolved-inbounds file store


def test_store_missing_and_corrupt_file_return_empty(tmp_path: Path) -> None:
    assert ResolvedInboundsStore(tmp_path / "missing.json").load() == {}
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert ResolvedInboundsStore(corrupt).load() == {}


def test_store_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "external_subscriptions_resolved.json"
    store = ResolvedInboundsStore(path)
    store.save(_resolved())

    reloaded = store.load()
    assert reloaded["blanc"][0].slug == "вильнюс-литва-extra"
    assert "updatedAt" in json.loads(path.read_text(encoding="utf-8"))["blanc"][0]


# --------------------------------------------------------------------------- state model validation


def _state(
    *, external_inbounds: list[JsonObject], subscriptions: list[JsonObject], client_tags: list[str]
) -> JsonObject:
    return {
        "nodes": [],
        "externalInbounds": external_inbounds,
        "externalSubscriptions": subscriptions,
        "clients": [{"id": "c1", "comment": "c", "subId": "tok", "inboundTags": client_tags}],
        "defaultClientInboundTags": [],
    }


def _blanc(**overrides: Any) -> JsonObject:
    base = {"name": "blanc", "url": "https://feed.example/sub", "updateInterval": 60}
    base.update(overrides)
    return base


def test_reference_to_known_subscription_loads_even_when_slug_absent(tmp_path: Path) -> None:
    state = _state(
        external_inbounds=[{"tag": "blanc-lt", "label": "🇱🇹 Литва", "uri": "@blanc:~литва"}],
        subscriptions=[_blanc()],
        client_tags=["blanc-lt"],
    )
    write_json(tmp_path / "data.json", state)

    # No resolved file exists yet, but the friendly tag is declared so loading must not fail.
    loaded = ControlPlaneStore(tmp_path / "data.json").load_state()
    assert loaded.external_inbounds[0].uri == "@blanc:~литва"


def test_reference_to_unknown_subscription_is_rejected(tmp_path: Path) -> None:
    state = _state(
        external_inbounds=[{"tag": "x", "label": "X", "uri": "@typo:slug"}],
        subscriptions=[_blanc()],
        client_tags=[],
    )
    write_json(tmp_path / "data.json", state)

    with pytest.raises(StateValidationError, match="unknown subscription"):
        ControlPlaneStore(tmp_path / "data.json").load_state()


def test_reference_with_invalid_regex_is_rejected(tmp_path: Path) -> None:
    state = _state(
        external_inbounds=[{"tag": "x", "label": "X", "uri": "@blanc:~["}],
        subscriptions=[_blanc()],
        client_tags=[],
    )
    write_json(tmp_path / "data.json", state)

    with pytest.raises(StateValidationError, match="invalid regex"):
        ControlPlaneStore(tmp_path / "data.json").load_state()


def test_duplicate_subscription_name_is_rejected(tmp_path: Path) -> None:
    state = _state(external_inbounds=[], subscriptions=[_blanc(), _blanc(url="https://other")], client_tags=[])
    write_json(tmp_path / "data.json", state)

    with pytest.raises(StateValidationError, match="duplicate external subscription name"):
        ControlPlaneStore(tmp_path / "data.json").load_state()


def test_invalid_inbound_filter_regex_is_rejected() -> None:
    with pytest.raises(ValueError):
        ExternalSubscriptionRecord.model_validate(_blanc(inboundFilter="["))


def test_subscription_name_with_colon_is_rejected() -> None:
    with pytest.raises(ValueError):
        ExternalSubscriptionRecord.model_validate(_blanc(name="bl:anc"))


# --------------------------------------------------------------------------- build_resolved_inbounds


def test_build_resolved_inbounds_applies_filter_and_stamps_now() -> None:
    subscription = ExternalSubscriptionRecord.model_validate(_blanc(inboundFilter="Литва"))
    entries = parse_subscription_body(f"{NL_GEO}\n{FI_WL}\n")  # only NL_GEO label contains "Литва"

    resolved = build_resolved_inbounds(subscription, entries, now="t1")
    assert [r.slug for r in resolved] == ["литва"]
    assert resolved[0].uri == "vless://uuid@1.1.1.1:443?type=tcp"
    assert resolved[0].updated_at == "t1"


def test_build_resolved_inbounds_without_filter_takes_all() -> None:
    subscription = ExternalSubscriptionRecord.model_validate(_blanc())
    entries = parse_subscription_body(f"{NL_GEO}\n{FI_WL}\n")

    assert len(build_resolved_inbounds(subscription, entries, now="t")) == 2


# --------------------------------------------------------------------------- refresh service


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        VPN_TELEGRAM_BOT_TOKEN="x",
        VPN_TELEGRAM_ADMIN_IDS="1",
        VPN_DATA_FILE=str(tmp_path / "data.json"),
        VPN_EXTERNAL_SUBSCRIPTIONS_RESOLVED=str(tmp_path / "external_subscriptions_resolved.json"),
    )  # type: ignore[call-arg]


def _service_data(tmp_path: Path) -> ControlPlaneStore:
    write_json(
        tmp_path / "data.json",
        _state(
            external_inbounds=[{"tag": "blanc-fi", "label": "🇫🇮 Финляндия", "uri": "@blanc:~bravo"}],
            subscriptions=[_blanc(inboundFilter="Bravo")],
            client_tags=["blanc-fi"],
        ),
    )
    return ControlPlaneStore(tmp_path / "data.json")


@pytest.mark.asyncio
async def test_refresh_writes_file(tmp_path: Path) -> None:
    store = _service_data(tmp_path)

    async def fetch(url: str) -> str:
        return f"{NL_GEO}\n{FI_WL}\n"

    service = ExternalSubscriptionService(_settings(tmp_path), store, fetcher=fetch)
    await service.refresh_due()

    resolved = ResolvedInboundsStore(tmp_path / "external_subscriptions_resolved.json").load()
    assert [r.slug for r in resolved["blanc"]] == ["finland-extra-whitelist-bravo"]


@pytest.mark.asyncio
async def test_refresh_keeps_last_known_on_fetch_failure(tmp_path: Path) -> None:
    store = _service_data(tmp_path)
    cache = ResolvedInboundsStore(tmp_path / "external_subscriptions_resolved.json")
    cache.save({"blanc": [ResolvedExternalInbound(slug="cached", label="C", uri="vless://cached", updated_at="t")]})

    async def failing(url: str) -> str:
        raise RuntimeError("boom")

    service = ExternalSubscriptionService(_settings(tmp_path), store, fetcher=failing)
    await service.refresh_due()

    assert cache.load()["blanc"][0].uri == "vless://cached"


@pytest.mark.asyncio
async def test_refresh_respects_interval_and_dedupes_url(tmp_path: Path) -> None:
    store = _service_data(tmp_path)
    calls = {"n": 0}

    async def counting(url: str) -> str:
        calls["n"] += 1
        return f"{FI_WL}\n"

    service = ExternalSubscriptionService(_settings(tmp_path), store, fetcher=counting)
    await service.refresh_due()
    await service.refresh_due()  # within 60-minute interval -> no refetch

    assert calls["n"] == 1


# --------------------------------------------------------------------------- end-to-end render resilience


@pytest.mark.asyncio
async def test_subscription_render_resolves_refs_and_skips_missing(tmp_path: Path) -> None:
    write_json(
        tmp_path / "data.json",
        _state(
            external_inbounds=[
                {"tag": "blanc-se", "label": "🇸🇪 Швеция", "uri": "@blanc:~швеция"},
                {"tag": "blanc-missing", "label": "X", "uri": "@blanc:does-not-exist"},
                {"tag": "wg", "label": "WG", "uri": "wireguard://literal#WG"},
            ],
            subscriptions=[_blanc()],
            client_tags=["blanc-se", "blanc-missing", "wg"],
        ),
    )
    store = ControlPlaneStore(tmp_path / "data.json")
    resolved_path = tmp_path / "external_subscriptions_resolved.json"
    ResolvedInboundsStore(resolved_path).save(
        {
            "blanc": [
                ResolvedExternalInbound(
                    slug="стокгольм-швеция-extra", label="🇸🇪 Стокгольм", uri="vless://se", updated_at="t"
                )
            ]
        }
    )

    service = SubscriptionService(store, public_base_url="https://example.test/s", resolved_inbounds_path=resolved_path)
    built = await service.build("tok")

    # Resolved Sweden + literal WG are present; the unresolved reference is silently skipped.
    assert any(link.startswith("vless://se") for link in built.links)
    assert any(link.startswith("wireguard://literal") for link in built.links)
    assert len(built.links) == 2
