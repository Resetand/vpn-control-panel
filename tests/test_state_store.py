from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from vpn_control_plane.data import (
    ClientRecord,
    ControlPlaneStore,
    NodeRecord,
    StateValidationError,
    SubscriptionMetadata,
    effective_inbound_tags,
)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def valid_state() -> dict[str, object]:
    return {
        "nodes": [
            {
                "id": 1,
                "host": "eu.example.test",
                "port": 2053,
                "basePath": "panel",
                "apiToken": "${{ env.EU_API_TOKEN }}",
                "xuiFallbackClientEmail": "node-fallback@example.test",
                "inbounds": [
                    {
                        "tag": "eu",
                        "label": "EU",
                        "xuiInboundId": 1,
                        "xuiFallbackClientEmail": "inbound-fallback@example.test",
                    }
                ],
            }
        ],
        "externalInbounds": [{"tag": "extra", "label": "Extra", "uri": "vless://example#Extra"}],
        "clients": [
            {
                "id": "123456789",
                "comment": "Kirill",
                "subId": "personal-token",
                "legacySubId": "123456789",
            }
        ],
        "defaultClientInboundTags": ["eu", "extra"],
        "subscription": {"profileTitle": "base64:VGVzdA==", "routingEnable": True},
    }


def test_loads_valid_state_and_normalizes_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EU_API_TOKEN", "eu-token")
    write_json(tmp_path / "data.json", valid_state())

    state = ControlPlaneStore(tmp_path / "data.json").load_state()

    assert state.nodes[0].base_path == "/panel/"
    assert state.nodes[0].api_token == "eu-token"
    assert state.nodes[0].xui_fallback_client_email == "node-fallback@example.test"
    assert state.nodes[0].inbounds[0].xui_inbound_id == 1
    assert state.nodes[0].inbounds[0].xui_fallback_client_email == "inbound-fallback@example.test"
    assert state.external_inbounds[0].tag == "extra"
    assert state.clients[0].effective_sub_id == "personal-token"
    assert state.clients[0].legacy_subscription_ids == {"123456789"}
    assert effective_inbound_tags(state, state.clients[0]) == ["eu", "extra"]
    assert state.subscription.profile_title == "base64:VGVzdA=="
    assert state.subscription.routing_enable is True


def test_load_state_picks_up_atomic_data_file_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EU_API_TOKEN", "eu-token")
    data_file = tmp_path / "data.json"
    write_json(data_file, valid_state())
    store = ControlPlaneStore(data_file)

    assert store.load_state().clients[0].comment == "Kirill"

    replacement = tmp_path / "data.json.new"
    state = valid_state()
    state["clients"] = [{"id": "123", "comment": "Updated", "subId": "updated-token"}]
    write_json(replacement, state)
    os.replace(replacement, data_file)

    assert store.load_state().clients[0].comment == "Updated"


def test_client_inbound_tags_override_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EU_API_TOKEN", "eu-token")
    state = valid_state()
    state["clients"] = [{"id": "123", "comment": "Override", "inboundTags": ["extra"]}]
    write_json(tmp_path / "data.json", state)

    loaded = ControlPlaneStore(tmp_path / "data.json").load_state()

    assert effective_inbound_tags(loaded, loaded.clients[0]) == ["extra"]


def test_node_monitoring_defaults_to_enabled() -> None:
    node = NodeRecord.model_validate({"id": 1, "host": "node.example.test", "port": 443, "apiToken": "token"})

    assert node.monitoring is True


def test_node_monitoring_can_be_disabled() -> None:
    node = NodeRecord.model_validate(
        {"id": 1, "host": "node.example.test", "port": 443, "apiToken": "token", "monitoring": False}
    )

    assert node.monitoring is False


def test_legacy_client_without_subscription_id_keeps_client_id_alias() -> None:
    client = ClientRecord.model_validate({"id": "123", "comment": "Existing"})

    assert client.effective_sub_id == "123"
    assert client.legacy_subscription_ids == {"123"}


def test_resolves_env_templates_in_any_json_string_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODE_HOST", "resolved.example.test")
    monkeypatch.setenv("NODE_PORT", "443")
    monkeypatch.setenv("NODE_API_TOKEN", "api-token")
    monkeypatch.setenv("PROFILE_TITLE", "base64:UmVzb2x2ZWQ=")
    state = valid_state()
    state["nodes"] = [
        {
            "id": 1,
            "host": "${{ env.NODE_HOST }}",
            "port": "${{ env.NODE_PORT }}",
            "basePath": "panel",
            "apiToken": "${{ env.NODE_API_TOKEN }}",
            "inbounds": [{"tag": "eu", "label": "EU", "xuiInboundId": 1}],
        }
    ]
    state["subscription"] = {"profileTitle": "${{ env.PROFILE_TITLE }}"}
    write_json(tmp_path / "data.json", state)

    loaded = ControlPlaneStore(tmp_path / "data.json").load_state()

    assert loaded.nodes[0].host == "resolved.example.test"
    assert loaded.nodes[0].port == 443
    assert loaded.nodes[0].base_path == "/panel/"
    assert loaded.nodes[0].api_token == "api-token"
    assert loaded.subscription.profile_title == "base64:UmVzb2x2ZWQ="


def test_rejects_unresolved_env_template_with_file_context(tmp_path: Path) -> None:
    write_json(tmp_path / "data.json", valid_state())

    with pytest.raises(StateValidationError) as error:
        ControlPlaneStore(tmp_path / "data.json").load_state()

    message = str(error.value)
    assert "data.json" in message
    assert "EU_API_TOKEN" in message


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda state: state["nodes"][0]["inbounds"].append(
                {"tag": "extra", "label": "Duplicate", "xuiInboundId": 2}
            ),
            "duplicate inbound tag",
        ),
        (lambda state: state.update({"defaultClientInboundTags": ["missing"]}), "unknown inbound tag"),
        (
            lambda state: state.update({"clients": [{"id": "123", "comment": "Bad", "inboundTags": ["missing"]}]}),
            "unknown inbound tag",
        ),
        (
            lambda state: state.update({"clients": [{"id": "123", "comment": "Bad", "inboundTags": ["eu", "eu"]}]}),
            "duplicate inbound tag",
        ),
    ],
)
def test_rejects_invalid_tag_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: object,
    expected: str,
) -> None:
    monkeypatch.setenv("EU_API_TOKEN", "eu-token")
    state = valid_state()
    mutate(state)  # type: ignore[operator]
    write_json(tmp_path / "data.json", state)

    with pytest.raises(StateValidationError) as error:
        ControlPlaneStore(tmp_path / "data.json").load_state()

    assert expected in str(error.value)


def test_rejects_old_persisted_field_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EU_API_TOKEN", "eu-token")
    state = valid_state()
    state["subscription"] = {"profile-title": "base64:VGVzdA=="}
    write_json(tmp_path / "data.json", state)

    with pytest.raises(StateValidationError) as error:
        ControlPlaneStore(tmp_path / "data.json").load_state()

    assert "profile-title" in str(error.value)


def test_verify_ready_rejects_missing_data_file_parent_directory(tmp_path: Path) -> None:
    missing_file = tmp_path / "missing" / "data.json"

    with pytest.raises(StateValidationError) as error:
        ControlPlaneStore(missing_file).verify_ready()

    assert "data file parent directory does not exist" in str(error.value)


def test_verify_ready_rejects_missing_required_file_even_when_old_files_exist(tmp_path: Path) -> None:
    write_json(tmp_path / "nodes.json", [])
    write_json(tmp_path / "clients.json", [])
    write_json(tmp_path / "inbounds.json", [])
    write_json(tmp_path / "subscription.json", {})

    with pytest.raises(StateValidationError) as error:
        ControlPlaneStore(tmp_path / "data.json").verify_ready()

    message = str(error.value)
    assert "data.json" in message
    assert "required data file is missing" in message


def test_verify_ready_accepts_existing_data_file(tmp_path: Path) -> None:
    write_json(tmp_path / "data.json", valid_state())

    ControlPlaneStore(tmp_path / "data.json").verify_ready()


def test_atomic_client_write_replaces_full_state_with_valid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EU_API_TOKEN", "eu-token")
    write_json(tmp_path / "data.json", valid_state())
    store = ControlPlaneStore(tmp_path / "data.json")

    store.save_clients([ClientRecord(id="123", comment="Test", subId="legacy")])

    saved = json.loads((tmp_path / "data.json").read_text(encoding="utf-8"))
    assert saved["clients"] == [{"id": "123", "comment": "Test", "subId": "legacy"}]
    assert saved["nodes"][0]["inbounds"][0]["xuiInboundId"] == 1
    assert list(tmp_path.glob(".data.json.*.tmp")) == []


def test_invalid_serialized_client_is_not_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EU_API_TOKEN", "eu-token")
    original = valid_state()
    write_json(tmp_path / "data.json", original)

    with pytest.raises(StateValidationError):
        ControlPlaneStore(tmp_path / "data.json").save_clients([{"id": "", "comment": "bad"}])  # type: ignore[list-item]

    assert json.loads((tmp_path / "data.json").read_text(encoding="utf-8")) == original


def test_subscription_metadata_defaults_are_valid() -> None:
    assert SubscriptionMetadata() == SubscriptionMetadata.model_validate({})
