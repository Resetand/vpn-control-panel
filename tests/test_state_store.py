from __future__ import annotations

import json
from pathlib import Path

import pytest

from vpn_control_plane.data import ClientRecord, JsonStateStore, StateValidationError, SubscriptionMetadata


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def valid_nodes() -> list[dict[str, object]]:
    return [
        {
            "id": 1,
            "host": "eu.example.test",
            "port": 2053,
            "webBasePath": "panel",
            "username": "${{ env.EU_USERNAME }}",
            "password": "${{ env.EU_PASSWORD }}",
        }
    ]


def valid_clients() -> list[dict[str, object]]:
    return [{"id": "123456789", "comment": "Kirill", "subId": "legacy-sub-id"}]


def valid_inbounds() -> list[dict[str, object]]:
    return [
        {
            "type": "node-inbound",
            "label": "EU",
            "nodeId": 1,
            "inboundId": 1,
            "permanentClientEmail": "shared-client",
        },
        {"type": "external-inbound", "label": "Extra", "uri": "vless://example#Extra"},
    ]


def test_loads_valid_state_and_normalizes_fields(tmp_path: Path) -> None:
    write_json(tmp_path / "nodes.json", valid_nodes())
    write_json(tmp_path / "clients.json", valid_clients())
    write_json(tmp_path / "inbounds.json", valid_inbounds())
    write_json(tmp_path / "subscription.json", {"profile-title": "base64:VGVzdA==", "routing-enable": True})

    state = JsonStateStore(tmp_path).load_state()

    assert state.nodes[0].web_base_path == "/panel/"
    assert state.clients[0].effective_sub_id == "legacy-sub-id"
    assert state.inbounds[0].type == "node-inbound"
    assert state.inbounds[0].permanent_client_email == "shared-client"
    assert state.subscription.profile_title == "base64:VGVzdA=="
    assert state.subscription.routing_enable is True


def test_resolves_env_templates_in_any_json_string_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODE_HOST", "resolved.example.test")
    monkeypatch.setenv("NODE_PORT", "443")
    monkeypatch.setenv("NODE_USERNAME", "admin")
    monkeypatch.setenv("NODE_PASSWORD", "secret")
    monkeypatch.setenv("PROFILE_TITLE", "base64:UmVzb2x2ZWQ=")
    write_json(
        tmp_path / "nodes.json",
        [
            {
                "id": 1,
                "host": "${{ env.NODE_HOST }}",
                "port": "${{ env.NODE_PORT }}",
                "webBasePath": "panel",
                "username": "${{ env.NODE_USERNAME }}",
                "password": "${{ env.NODE_PASSWORD }}",
            }
        ],
    )
    write_json(tmp_path / "clients.json", valid_clients())
    write_json(tmp_path / "inbounds.json", valid_inbounds())
    write_json(tmp_path / "subscription.json", {"profile-title": "${{ env.PROFILE_TITLE }}"})

    state = JsonStateStore(tmp_path).load_state()

    assert state.nodes[0].host == "resolved.example.test"
    assert state.nodes[0].port == 443
    assert state.nodes[0].username == "admin"
    assert state.nodes[0].password == "secret"
    assert state.subscription.profile_title == "base64:UmVzb2x2ZWQ="


def test_keeps_env_template_when_variable_is_absent(tmp_path: Path) -> None:
    write_json(tmp_path / "nodes.json", valid_nodes())
    write_json(tmp_path / "clients.json", valid_clients())
    write_json(tmp_path / "inbounds.json", valid_inbounds())
    write_json(tmp_path / "subscription.json", {})

    state = JsonStateStore(tmp_path).load_state()

    assert state.nodes[0].username == "${{ env.EU_USERNAME }}"
    assert state.nodes[0].password == "${{ env.EU_PASSWORD }}"


def test_rejects_unknown_inbound_type_with_file_and_field(tmp_path: Path) -> None:
    write_json(tmp_path / "inbounds.json", [{"type": "extra-inbound", "label": "Extra", "uri": "vless://example"}])

    with pytest.raises(StateValidationError) as error:
        JsonStateStore(tmp_path).load_inbounds()

    message = str(error.value)
    assert "inbounds.json" in message
    assert "union_tag_invalid" in message or "Input tag" in message


def test_missing_optional_files_load_empty_defaults(tmp_path: Path) -> None:
    state = JsonStateStore(tmp_path).load_state()

    assert state.nodes == []
    assert state.clients == []
    assert state.inbounds == []
    assert state.subscription == SubscriptionMetadata()


def test_verify_ready_rejects_missing_data_directory(tmp_path: Path) -> None:
    missing_dir = tmp_path / "missing"

    with pytest.raises(StateValidationError) as error:
        JsonStateStore(missing_dir).verify_ready()

    assert "data directory does not exist" in str(error.value)


def test_verify_ready_rejects_missing_required_file(tmp_path: Path) -> None:
    write_json(tmp_path / "nodes.json", valid_nodes())
    write_json(tmp_path / "clients.json", valid_clients())
    write_json(tmp_path / "subscription.json", {})

    with pytest.raises(StateValidationError) as error:
        JsonStateStore(tmp_path).verify_ready()

    message = str(error.value)
    assert "inbounds.json" in message
    assert "required data file is missing" in message


def test_verify_ready_accepts_complete_data_directory(tmp_path: Path) -> None:
    write_json(tmp_path / "nodes.json", valid_nodes())
    write_json(tmp_path / "clients.json", valid_clients())
    write_json(tmp_path / "inbounds.json", valid_inbounds())
    write_json(tmp_path / "subscription.json", {})

    JsonStateStore(tmp_path).verify_ready()


def test_atomic_client_write_replaces_file_with_valid_json(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)

    store.save_clients([ClientRecord(id="123", comment="Test", subId="legacy")])

    saved = json.loads((tmp_path / "clients.json").read_text(encoding="utf-8"))
    assert saved == [{"id": "123", "comment": "Test", "subId": "legacy"}]
    assert list(tmp_path.glob(".clients.json.*.tmp")) == []


def test_invalid_serialized_client_is_not_written(tmp_path: Path) -> None:
    clients_path = tmp_path / "clients.json"
    clients_path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(StateValidationError):
        JsonStateStore(tmp_path).save_clients([{"id": "", "comment": "bad"}])  # type: ignore[list-item]

    assert clients_path.read_text(encoding="utf-8") == "[]\n"
