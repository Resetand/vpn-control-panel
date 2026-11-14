from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from vpn_control_plane.catalog_metrics import render_inbound_info
from vpn_control_plane.config import Settings
from vpn_control_plane.data import ControlPlaneState, ControlPlaneStore
from vpn_control_plane.http.routes import create_router

STATE = {
    "nodes": [
        {
            "id": 1,
            "host": "eu.example.test",
            "port": 2053,
            "apiToken": "token",
            "inbounds": [{"tag": "eu", "label": '🇳🇱 "Quoted"', "xuiInboundId": 1, "critical": True}],
        }
    ],
    "externalSubscriptions": [{"name": "blanc", "url": "https://provider.example.test/sub", "updateInterval": 60}],
    "externalInbounds": [
        {"tag": "blanc-de", "label": "🇩🇪 Германия", "uri": "@blanc:germany", "outage": True},
        {"tag": "raw", "label": "Raw", "uri": "vless://raw#Raw"},
    ],
    "clients": [],
}


def test_renders_one_info_series_per_inbound_with_its_declared_facts() -> None:
    text = render_inbound_info(ControlPlaneState.model_validate(STATE))

    assert text.splitlines()[2:] == [
        'vpn_inbound_info{tag="eu",name="🇳🇱 \\"Quoted\\"",node="eu.example.test",provider="",'
        'critical="true",outage="false"} 1',
        'vpn_inbound_info{tag="blanc-de",name="🇩🇪 Германия",node="",provider="blanc",critical="false",outage="true"} 1',
        'vpn_inbound_info{tag="raw",name="Raw",node="",provider="",critical="false",outage="false"} 1',
    ]
    assert "# TYPE vpn_inbound_info gauge" in text


def test_catalog_route_serves_metrics_without_secrets(tmp_path: Path) -> None:
    import json

    (tmp_path / "data.json").write_text(json.dumps(STATE), encoding="utf-8")
    settings = Settings.model_validate(
        {
            "VPN_DATA_FILE": str(tmp_path / "data.json"),
            "VPN_SUBSCRIPTION_DOMAIN": "example.test",
            "VPN_TELEGRAM_BOT_TOKEN": "token",
            "VPN_TELEGRAM_ADMIN_IDS": "1",
        }
    )
    app = FastAPI()
    app.include_router(create_router(settings, ControlPlaneStore(tmp_path / "data.json")))

    response = TestClient(app).get("/catalog/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert 'tag="blanc-de"' in response.text
    assert "token" not in response.text
    assert "provider.example.test" not in response.text
