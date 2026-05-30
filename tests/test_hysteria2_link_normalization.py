from urllib.parse import parse_qs, urlsplit

from vpn_control_plane.subscription.service import _normalize_hysteria2_link


def test_drops_v2rayn_dialect_params() -> None:
    uri = "hysteria2://auth@msk.example:8443?alpn=h3&fp=chrome&security=tls&sni=msk.example#MSK"
    out = _normalize_hysteria2_link(uri)
    parts = urlsplit(out)
    assert parts.scheme == "hysteria2"
    assert parts.netloc == "auth@msk.example:8443"
    assert parts.fragment == "MSK"
    assert parse_qs(parts.query) == {"sni": ["msk.example"]}
    assert "fp" not in out and "security=" not in out and "alpn" not in out


def test_translates_allowinsecure_to_insecure() -> None:
    uri = "hysteria2://auth@h:8443?sni=h&allowInsecure=1&fp=chrome"
    assert parse_qs(urlsplit(_normalize_hysteria2_link(uri)).query) == {"sni": ["h"], "insecure": ["1"]}


def test_keeps_obfs_params_and_hy2_scheme() -> None:
    uri = "hy2://auth@h:8443?sni=h&obfs=salamander&obfs-password=secret&type=hysteria"
    assert parse_qs(urlsplit(_normalize_hysteria2_link(uri)).query) == {
        "sni": ["h"],
        "obfs": ["salamander"],
        "obfs-password": ["secret"],
    }


def test_preserves_auth_and_fragment_verbatim() -> None:
    uri = "hysteria2://417ac5f130a14169a43a6fcce82c2a7c@msk.example:8443?fp=chrome&sni=msk.example#l"
    assert (
        _normalize_hysteria2_link(uri)
        == "hysteria2://417ac5f130a14169a43a6fcce82c2a7c@msk.example:8443?sni=msk.example#l"
    )


def test_leaves_non_hysteria_links_untouched() -> None:
    uri = "vless://uuid@host:443?type=tcp&fp=chrome&security=reality#node"
    assert _normalize_hysteria2_link(uri) == uri
