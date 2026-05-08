from __future__ import annotations

import base64
import json
from urllib.parse import ParseResult, parse_qs, unquote, urlparse

from vpn_control_plane.xui import XuiInbound, build_xui_share_links

JsonObject = dict[str, object]


def inbound(
    *,
    protocol: str,
    settings: JsonObject,
    stream_settings: JsonObject,
    listen: str = "",
    port: int = 443,
) -> XuiInbound:
    raw = {
        "id": 7,
        "protocol": protocol,
        "listen": listen,
        "port": port,
        "settings": settings,
        "streamSettings": stream_settings,
        "sniffing": {},
    }
    return XuiInbound(
        id=7,
        protocol=protocol,
        settings=settings,
        stream_settings=stream_settings,
        sniffing={},
        raw=raw,
    )


def parse_single_link(link: str) -> tuple[ParseResult, dict[str, list[str]], str]:
    parsed = urlparse(link)
    return parsed, parse_qs(parsed.query), unquote(parsed.fragment)


def test_builds_vless_reality_tcp_link_from_inbound_and_client() -> None:
    links = build_xui_share_links(
        inbound(
            protocol="vless",
            settings={
                "encryption": "none",
                "clients": [
                    {
                        "id": "11111111-1111-4111-8111-111111111111",
                        "email": "7_telegram-user",
                        "subId": "legacy-sub",
                        "flow": "xtls-rprx-vision",
                    }
                ],
            },
            stream_settings={
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "serverNames": "vpn.example.test,backup.example.test",
                    "shortIds": "abcd,ef01",
                    "settings": {
                        "publicKey": "pub-key",
                        "fingerprint": "chrome",
                        "spiderX": "/",
                        "mldsa65Verify": "verify-key",
                    },
                },
            },
        ),
        fallback_address="node.example.test",
        sub_id="legacy-sub",
        fallback_email="7_telegram-user",
        remark="NL VLESS",
    )

    parsed, query, fragment = parse_single_link(links[0])

    assert parsed.scheme == "vless"
    assert parsed.username == "11111111-1111-4111-8111-111111111111"
    assert parsed.hostname == "node.example.test"
    assert parsed.port == 443
    assert query == {
        "type": ["tcp"],
        "encryption": ["none"],
        "security": ["reality"],
        "pbk": ["pub-key"],
        "fp": ["chrome"],
        "sni": ["vpn.example.test"],
        "sid": ["abcd"],
        "spx": ["/"],
        "pqv": ["verify-key"],
        "flow": ["xtls-rprx-vision"],
    }
    assert fragment == "NL VLESS"


def test_builds_vless_tls_websocket_link_with_listen_address() -> None:
    links = build_xui_share_links(
        inbound(
            protocol="vless",
            listen="edge.example.test",
            settings={"encryption": "none", "clients": [{"id": "uuid", "email": "7_1", "subId": "sub"}]},
            stream_settings={
                "network": "ws",
                "security": "tls",
                "wsSettings": {"path": "/ws", "headers": {"Host": "front.example.test"}},
                "tlsSettings": {
                    "serverName": "sni.example.test",
                    "alpn": ["h2", "http/1.1"],
                    "settings": {"fingerprint": "firefox"},
                },
            },
        ),
        fallback_address="node.example.test",
        sub_id="sub",
        remark="WS TLS",
    )

    parsed, query, fragment = parse_single_link(links[0])

    assert parsed.hostname == "edge.example.test"
    assert query["type"] == ["ws"]
    assert query["path"] == ["/ws"]
    assert query["host"] == ["front.example.test"]
    assert query["security"] == ["tls"]
    assert query["sni"] == ["sni.example.test"]
    assert query["alpn"] == ["h2,http/1.1"]
    assert query["fp"] == ["firefox"]
    assert fragment == "WS TLS"


def test_builds_trojan_grpc_link() -> None:
    links = build_xui_share_links(
        inbound(
            protocol="trojan",
            settings={"clients": [{"password": "secret", "email": "7_1", "subId": "sub"}]},
            stream_settings={
                "network": "grpc",
                "security": "none",
                "grpcSettings": {"serviceName": "svc", "authority": "grpc.example.test", "multiMode": True},
            },
        ),
        fallback_address="node.example.test",
        sub_id="sub",
        remark="Trojan gRPC",
    )

    parsed, query, fragment = parse_single_link(links[0])

    assert parsed.scheme == "trojan"
    assert parsed.username == "secret"
    assert query == {
        "type": ["grpc"],
        "serviceName": ["svc"],
        "authority": ["grpc.example.test"],
        "mode": ["multi"],
        "security": ["none"],
    }
    assert fragment == "Trojan gRPC"


def test_builds_shadowsocks_2022_link_with_inbound_and_client_passwords() -> None:
    links = build_xui_share_links(
        inbound(
            protocol="shadowsocks",
            settings={
                "method": "2022-blake3-aes-128-gcm",
                "password": "server-pass",
                "clients": [{"password": "client-pass", "email": "7_1", "subId": "sub"}],
            },
            stream_settings={"network": "tcp", "security": "none"},
        ),
        fallback_address="node.example.test",
        sub_id="sub",
        remark="SS",
    )

    parsed, query, fragment = parse_single_link(links[0])
    method_password = base64.b64decode(parsed.username or "").decode("utf-8")

    assert parsed.scheme == "ss"
    assert method_password == "2022-blake3-aes-128-gcm:server-pass:client-pass"
    assert query == {"type": ["tcp"]}
    assert fragment == "SS"


def test_builds_vmess_tls_link_payload() -> None:
    links = build_xui_share_links(
        inbound(
            protocol="vmess",
            settings={"clients": [{"id": "vmess-id", "email": "7_1", "subId": "sub", "security": "auto"}]},
            stream_settings={
                "network": "ws",
                "security": "tls",
                "wsSettings": {"path": "/vmess", "headers": {"Host": "host.example.test"}},
                "tlsSettings": {"serverName": "sni.example.test"},
            },
        ),
        fallback_address="node.example.test",
        sub_id="sub",
        remark="VMess",
    )

    payload = json.loads(base64.b64decode(links[0].removeprefix("vmess://")).decode("utf-8"))

    assert payload["ps"] == "VMess"
    assert payload["add"] == "node.example.test"
    assert payload["port"] == 443
    assert payload["id"] == "vmess-id"
    assert payload["net"] == "ws"
    assert payload["path"] == "/vmess"
    assert payload["host"] == "host.example.test"
    assert payload["tls"] == "tls"
    assert payload["sni"] == "sni.example.test"


def test_returns_no_links_when_sub_id_client_is_absent() -> None:
    links = build_xui_share_links(
        inbound(
            protocol="vless",
            settings={"encryption": "none", "clients": [{"id": "uuid", "email": "7_1", "subId": "other"}]},
            stream_settings={"network": "tcp", "security": "none"},
        ),
        fallback_address="node.example.test",
        sub_id="missing",
        fallback_email="7_missing",
        remark="Missing",
    )

    assert links == []


def test_returns_no_links_when_matching_client_is_disabled() -> None:
    links = build_xui_share_links(
        inbound(
            protocol="vless",
            settings={
                "encryption": "none",
                "clients": [{"id": "uuid", "email": "7_1", "subId": "sub", "enable": False}],
            },
            stream_settings={"network": "tcp", "security": "none"},
        ),
        fallback_address="node.example.test",
        sub_id="sub",
        fallback_email="7_1",
        remark="Disabled",
    )

    assert links == []
