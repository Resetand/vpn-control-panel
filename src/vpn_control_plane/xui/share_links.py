from __future__ import annotations

import base64
import json
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import quote, urlencode

from vpn_control_plane.xui.client import JsonObject, XuiInbound


def build_xui_share_links(
    inbound: XuiInbound,
    *,
    fallback_address: str,
    sub_id: str,
    remark: str,
    client_email: str | None = None,
    fallback_email: str | None = None,
    fallback_emails: Sequence[str] = (),
) -> list[str]:
    client = _find_client(
        inbound,
        sub_id=sub_id,
        client_email=client_email,
        fallback_emails=[email for email in (fallback_email, *fallback_emails) if email],
    )
    if client is None:
        return []

    protocol = inbound.protocol.lower()
    if protocol == "vmess":
        return _build_vmess_links(inbound, client, fallback_address=fallback_address, remark=remark)
    if protocol == "vless":
        return _build_vless_links(inbound, client, fallback_address=fallback_address, remark=remark)
    if protocol == "trojan":
        return _build_trojan_links(inbound, client, fallback_address=fallback_address, remark=remark)
    if protocol == "shadowsocks":
        return _build_shadowsocks_links(inbound, client, fallback_address=fallback_address, remark=remark)
    if protocol in {"hysteria", "hysteria2"}:
        return _build_hysteria_links(inbound, client, fallback_address=fallback_address, remark=remark)
    return []


def _build_vmess_links(inbound: XuiInbound, client: JsonObject, *, fallback_address: str, remark: str) -> list[str]:
    address, port = _address_and_port(inbound, fallback_address)
    if not address or port <= 0:
        return []

    stream = inbound.stream_settings
    network = _text(stream.get("network"), default="tcp")
    obj: JsonObject = {
        "v": "2",
        "ps": remark,
        "add": address,
        "port": port,
        "id": _text(client.get("id")),
        "aid": 0,
        "scy": _text(client.get("security"), default="auto"),
        "net": network,
        "type": "none",
    }
    _apply_vmess_network(stream, network, obj)
    if isinstance(stream.get("finalmask"), dict):
        _apply_finalmask_obj(stream["finalmask"], obj)

    security = _text(stream.get("security"))
    obj["tls"] = security
    if security == "tls":
        _apply_vmess_tls(stream, obj)

    external_proxies = _external_proxies(stream)
    if external_proxies:
        links: list[str] = []
        for proxy in external_proxies:
            proxy_obj = dict(obj)
            proxy_obj["add"] = _text(proxy.get("dest"), default=address)
            proxy_obj["port"] = _int(proxy.get("port"), default=port)
            proxy_obj["ps"] = _remark(remark, _text(proxy.get("remark")))
            force_tls = _text(proxy.get("forceTls"), default="same")
            if force_tls != "same":
                proxy_obj["tls"] = force_tls
                if force_tls == "none":
                    for key in ("alpn", "sni", "fp"):
                        proxy_obj.pop(key, None)
            links.append(_vmess_url(proxy_obj))
        return links

    return [_vmess_url(obj)]


def _build_vless_links(inbound: XuiInbound, client: JsonObject, *, fallback_address: str, remark: str) -> list[str]:
    address, port = _address_and_port(inbound, fallback_address)
    if not address or port <= 0:
        return []

    stream = inbound.stream_settings
    network = _text(stream.get("network"), default="tcp")
    params: dict[str, str] = {"type": network}
    encryption = _text(inbound.settings.get("encryption"), default="none")
    if encryption:
        params["encryption"] = encryption

    _apply_share_network_params(stream, network, params)
    if isinstance(stream.get("finalmask"), dict):
        _apply_finalmask_params(stream["finalmask"], params)
    security = _apply_share_security_params(stream, params)
    if security in {"tls", "reality"} and network == "tcp":
        flow = _text(client.get("flow"))
        if flow:
            params["flow"] = flow

    uuid = _text(client.get("id"))
    if not uuid:
        return []

    return _build_param_links(
        stream,
        base_security=security,
        make_link=lambda dest, proxy_port: f"vless://{quote(uuid, safe='')}@{_hostport(dest, proxy_port)}",
        params=params,
        remark=remark,
        fallback_address=address,
        fallback_port=port,
    )


def _build_trojan_links(inbound: XuiInbound, client: JsonObject, *, fallback_address: str, remark: str) -> list[str]:
    address, port = _address_and_port(inbound, fallback_address)
    if not address or port <= 0:
        return []

    password = _text(client.get("password"))
    if not password:
        return []

    stream = inbound.stream_settings
    network = _text(stream.get("network"), default="tcp")
    params: dict[str, str] = {"type": network}
    _apply_share_network_params(stream, network, params)
    if isinstance(stream.get("finalmask"), dict):
        _apply_finalmask_params(stream["finalmask"], params)
    security = _apply_share_security_params(stream, params)
    if security == "reality" and network == "tcp":
        flow = _text(client.get("flow"))
        if flow:
            params["flow"] = flow

    return _build_param_links(
        stream,
        base_security=security,
        make_link=lambda dest, proxy_port: f"trojan://{quote(password, safe='')}@{_hostport(dest, proxy_port)}",
        params=params,
        remark=remark,
        fallback_address=address,
        fallback_port=port,
    )


def _build_shadowsocks_links(
    inbound: XuiInbound,
    client: JsonObject,
    *,
    fallback_address: str,
    remark: str,
) -> list[str]:
    address, port = _address_and_port(inbound, fallback_address)
    if not address or port <= 0:
        return []

    method = _text(inbound.settings.get("method"))
    if not method:
        return []
    client_password = _text(client.get("password"))
    inbound_password = _text(inbound.settings.get("password"))
    password_parts = [client_password]
    if method.startswith("2022-") and inbound_password:
        password_parts.insert(0, inbound_password)
    password = ":".join(part for part in password_parts if part)
    if not password:
        return []

    encoded = base64.b64encode(f"{method}:{password}".encode()).decode("ascii")
    stream = inbound.stream_settings
    network = _text(stream.get("network"), default="tcp")
    params: dict[str, str] = {"type": network}
    _apply_share_network_params(stream, network, params)
    if isinstance(stream.get("finalmask"), dict):
        _apply_finalmask_params(stream["finalmask"], params)
    security = _text(stream.get("security"))
    if security == "tls":
        _apply_share_tls_params(stream, params)

    return _build_param_links(
        stream,
        base_security=security,
        make_link=lambda dest, proxy_port: f"ss://{encoded}@{_hostport(dest, proxy_port)}",
        params=params,
        remark=remark,
        fallback_address=address,
        fallback_port=port,
    )


def _build_hysteria_links(inbound: XuiInbound, client: JsonObject, *, fallback_address: str, remark: str) -> list[str]:
    address, port = _address_and_port(inbound, fallback_address)
    if not address or port <= 0:
        return []

    auth = _text(client.get("auth")) or _text(inbound.settings.get("auth"))
    if not auth:
        return []
    protocol = "hysteria" if _int(inbound.settings.get("version"), default=2) == 1 else "hysteria2"
    params: dict[str, str] = {}
    stream = inbound.stream_settings
    tls_settings = _mapping(stream.get("tlsSettings")) or _mapping(stream.get("tls"))
    sni = _text(tls_settings.get("serverName")) or _text(_mapping(tls_settings.get("settings")).get("serverName"))
    if sni:
        params["sni"] = sni
    fingerprint = _text(_mapping(tls_settings.get("settings")).get("fingerprint"))
    if fingerprint:
        params["fp"] = fingerprint
    if _bool(_mapping(tls_settings.get("settings")).get("allowInsecure")):
        params["insecure"] = "1"
    obfs = _text(inbound.settings.get("obfs"))
    obfs_password = _text(inbound.settings.get("obfsPassword"))
    if obfs:
        params["obfs"] = obfs
    if obfs_password:
        params["obfs-password"] = obfs_password

    link = f"{protocol}://{quote(auth, safe='')}@{_hostport(address, port)}"
    return [_build_link_with_params(link, params, remark)]


def _build_param_links(
    stream: JsonObject,
    *,
    base_security: str,
    make_link: Callable[[str, int], str],
    params: dict[str, str],
    remark: str,
    fallback_address: str,
    fallback_port: int,
) -> list[str]:
    external_proxies = _external_proxies(stream)
    if not external_proxies:
        return [_build_link_with_params(make_link(fallback_address, fallback_port), params, remark)]

    links: list[str] = []
    for proxy in external_proxies:
        dest = _text(proxy.get("dest"), default=fallback_address)
        port = _int(proxy.get("port"), default=fallback_port)
        force_tls = _text(proxy.get("forceTls"), default="same")
        proxy_params = dict(params)
        security = base_security if force_tls == "same" else force_tls
        if security:
            proxy_params["security"] = security
        if force_tls == "none":
            for key in ("alpn", "sni", "fp", "pbk", "sid", "spx", "pqv"):
                proxy_params.pop(key, None)
        links.append(
            _build_link_with_params(make_link(dest, port), proxy_params, _remark(remark, _text(proxy.get("remark"))))
        )
    return links


def _find_client(
    inbound: XuiInbound,
    *,
    sub_id: str,
    client_email: str | None,
    fallback_emails: Sequence[str],
) -> JsonObject | None:
    clients = inbound.settings.get("clients", [])
    if not isinstance(clients, list):
        return None
    dict_clients = [client for client in clients if isinstance(client, dict)]
    if client_email:
        return _find_client_by_email(dict_clients, client_email)
    for client in dict_clients:
        if _client_is_enabled(client) and _text(client.get("subId")) == sub_id:
            return client
    for fallback_email in fallback_emails:
        fallback_client = _find_client_by_email(dict_clients, fallback_email)
        if fallback_client is not None:
            return fallback_client
    return None


def _find_client_by_email(clients: list[JsonObject], email: str) -> JsonObject | None:
    for client in clients:
        if _client_is_enabled(client) and _text(client.get("email")) == email:
            return client
    return None


def _client_is_enabled(client: JsonObject) -> bool:
    return _bool(client.get("enable", True))


def _address_and_port(inbound: XuiInbound, fallback_address: str) -> tuple[str, int]:
    listen = _text(inbound.raw.get("listen"))
    address = fallback_address if listen in {"", "0.0.0.0", "::", "::0"} or listen.startswith("@") else listen
    return address, _int(inbound.raw.get("port"))


def _apply_share_network_params(stream: JsonObject, network: str, params: dict[str, str]) -> None:
    if network == "tcp":
        tcp = _mapping(stream.get("tcpSettings"))
        header = _mapping(tcp.get("header"))
        if _text(header.get("type")) == "http":
            request = _mapping(header.get("request"))
            path_list = _string_list(request.get("path"))
            if path_list:
                params["path"] = ",".join(path_list)
            host = _search_header(_list(request.get("headers")), "host")
            if host:
                params["host"] = host
            params["headerType"] = "http"
    elif network == "kcp":
        kcp = _mapping(stream.get("kcpSettings"))
        header = _mapping(kcp.get("header"))
        header_type = _text(header.get("type"))
        if header_type:
            params["headerType"] = header_type
        seed = _text(kcp.get("seed"))
        if seed:
            params["seed"] = seed
    elif network == "ws":
        ws = _mapping(stream.get("wsSettings"))
        _apply_path_and_host_params(ws, params)
    elif network == "http":
        http = _mapping(stream.get("httpSettings"))
        hosts = _string_list(http.get("host"))
        path_text = _text(http.get("path"))
        if hosts:
            params["host"] = ",".join(hosts)
        if path_text:
            params["path"] = path_text
    elif network == "grpc":
        grpc = _mapping(stream.get("grpcSettings"))
        service_name = _text(grpc.get("serviceName"))
        authority = _text(grpc.get("authority"))
        if service_name:
            params["serviceName"] = service_name
        if authority:
            params["authority"] = authority
        if _bool(grpc.get("multiMode")):
            params["mode"] = "multi"
    elif network in {"xhttp", "splithttp"}:
        xhttp = _mapping(stream.get("xhttpSettings")) or _mapping(stream.get("splithttpSettings"))
        _apply_path_and_host_params(xhttp, params)
        mode = _text(xhttp.get("mode"))
        if mode:
            params["mode"] = mode
        extra = _build_xhttp_extra(xhttp)
        if extra:
            params["extra"] = json.dumps(extra, ensure_ascii=False, separators=(",", ":"))
        padding = _text(xhttp.get("xPaddingBytes"))
        if padding:
            params["x_padding_bytes"] = padding


def _apply_vmess_network(stream: JsonObject, network: str, obj: JsonObject) -> None:
    obj["net"] = network
    params: dict[str, str] = {}
    _apply_share_network_params(stream, network, params)
    if "headerType" in params:
        obj["type"] = params["headerType"]
    if "host" in params:
        obj["host"] = params["host"]
    if "path" in params:
        obj["path"] = params["path"]
    if "serviceName" in params:
        obj["path"] = params["serviceName"]
    if "authority" in params:
        obj["host"] = params["authority"]
    if "mode" in params:
        obj["mode"] = params["mode"]
    if "extra" in params:
        obj["extra"] = params["extra"]
    if "x_padding_bytes" in params:
        obj["x_padding_bytes"] = params["x_padding_bytes"]


def _apply_share_security_params(stream: JsonObject, params: dict[str, str]) -> str:
    security = _text(stream.get("security"))
    if security == "tls":
        _apply_share_tls_params(stream, params)
    elif security == "reality":
        _apply_share_reality_params(stream, params)
    else:
        params["security"] = "none"
        return "none"
    return security


def _apply_share_tls_params(stream: JsonObject, params: dict[str, str]) -> None:
    tls = _mapping(stream.get("tlsSettings"))
    params["security"] = "tls"
    server_name = _text(tls.get("serverName"))
    if server_name:
        params["sni"] = server_name
    alpn = _string_list(tls.get("alpn"))
    if alpn:
        params["alpn"] = ",".join(alpn)
    settings = _mapping(tls.get("settings"))
    fingerprint = _text(settings.get("fingerprint"))
    if fingerprint:
        params["fp"] = fingerprint
    if _bool(settings.get("allowInsecure")):
        params["allowInsecure"] = "1"


def _apply_vmess_tls(stream: JsonObject, obj: JsonObject) -> None:
    params: dict[str, str] = {}
    _apply_share_tls_params(stream, params)
    for source, target in (("sni", "sni"), ("alpn", "alpn"), ("fp", "fp")):
        if source in params:
            obj[target] = params[source]


def _apply_share_reality_params(stream: JsonObject, params: dict[str, str]) -> None:
    reality = _mapping(stream.get("realitySettings"))
    settings = _mapping(reality.get("settings"))
    params["security"] = "reality"
    public_key = _text(settings.get("publicKey"))
    fingerprint = _text(settings.get("fingerprint"))
    server_name = _first_csv(reality.get("serverNames"))
    short_id = _first_csv(reality.get("shortIds"))
    spider_x = _text(settings.get("spiderX"))
    mldsa_verify = _text(settings.get("mldsa65Verify"))
    if public_key:
        params["pbk"] = public_key
    if fingerprint:
        params["fp"] = fingerprint
    if server_name:
        params["sni"] = server_name
    if short_id:
        params["sid"] = short_id
    if spider_x:
        params["spx"] = spider_x
    if mldsa_verify:
        params["pqv"] = mldsa_verify


def _apply_path_and_host_params(settings: JsonObject, params: dict[str, str]) -> None:
    path = _text(settings.get("path"))
    if path:
        params["path"] = path
    host = _text(settings.get("host")) or _search_header(list(_mapping(settings.get("headers")).items()), "host")
    if host:
        params["host"] = host


def _apply_finalmask_params(finalmask: JsonObject, params: dict[str, str]) -> None:
    if any(value not in (None, "", [], {}) for value in finalmask.values()):
        params["fm"] = json.dumps(finalmask, ensure_ascii=False, separators=(",", ":"))


def _apply_finalmask_obj(finalmask: JsonObject, obj: JsonObject) -> None:
    if any(value not in (None, "", [], {}) for value in finalmask.values()):
        obj["fm"] = json.dumps(finalmask, ensure_ascii=False, separators=(",", ":"))


def _build_xhttp_extra(xhttp: JsonObject) -> JsonObject | None:
    fields = (
        "noGRPCHeader",
        "scMaxEachPostBytes",
        "scMaxConcurrentPosts",
        "scMinPostsIntervalMs",
        "xPaddingBytes",
        "mode",
        "downloadSettings",
        "extra",
    )
    extra: JsonObject = {}
    for field in fields:
        value = xhttp.get(field)
        if value not in (None, "", [], {}):
            extra[field] = value
    headers = _mapping(xhttp.get("headers"))
    if headers:
        non_host_headers = {key: value for key, value in headers.items() if key.lower() != "host"}
        if non_host_headers:
            extra["headers"] = non_host_headers
    return extra or None


def _external_proxies(stream: JsonObject) -> list[JsonObject]:
    return [item for item in _list(stream.get("externalProxy")) if isinstance(item, dict)]


def _vmess_url(obj: JsonObject) -> str:
    payload = json.dumps(obj, ensure_ascii=False, indent=2)
    return "vmess://" + base64.b64encode(payload.encode("utf-8")).decode("ascii")


def _build_link_with_params(link: str, params: dict[str, str], fragment: str) -> str:
    query = urlencode([(key, value) for key, value in params.items() if value != ""])
    suffix = f"?{query}" if query else ""
    return f"{link}{suffix}#{quote(fragment, safe='')}"


def _hostport(address: str, port: int) -> str:
    host = f"[{address}]" if ":" in address and not address.startswith("[") else address
    return f"{host}:{port}"


def _remark(base: str, extra: str) -> str:
    return f"{base} {extra}" if extra else base


def _mapping(value: Any) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_text(item) for item in value if _text(item)]
    text = _text(value)
    return [text] if text else []


def _search_header(headers: list[Any], name: str) -> str:
    for header in headers:
        if isinstance(header, tuple) and len(header) == 2 and str(header[0]).lower() == name:
            return _text(header[1])
        if isinstance(header, dict) and _text(header.get("name")).lower() == name:
            return _text(header.get("value"))
    return ""


def _first_csv(value: Any) -> str:
    if isinstance(value, list):
        return _text(value[0]) if value else ""
    text = _text(value)
    return next((part.strip() for part in text.split(",") if part.strip()), "")


def _text(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
