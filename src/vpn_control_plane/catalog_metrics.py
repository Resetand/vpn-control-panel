"""The inbound catalog as Prometheus metrics: monitoring reads what each inbound is from here
instead of inferring it from addresses or marks in names."""

from __future__ import annotations

from vpn_control_plane.data import (
    ControlPlaneState,
    ExternalCatalogInbound,
    build_inbound_catalog,
    parse_external_subscription_ref,
)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def render_inbound_info(state: ControlPlaneState) -> str:
    """One `vpn_inbound_info` series per inbound. `name` is the label the probe client receives,
    so monitoring joins its measurements on it; `node` is set for own nodes, `provider` (the
    external subscription name) for inbounds taken from a provider."""
    lines = [
        "# HELP vpn_inbound_info Inbounds declared in the control plane; value is always 1.",
        "# TYPE vpn_inbound_info gauge",
    ]
    for tag, entry in build_inbound_catalog(state).items():
        if isinstance(entry, ExternalCatalogInbound):
            ref = parse_external_subscription_ref(entry.inbound.uri)
            node, provider = "", ref.name if ref else ""
        else:
            node, provider = entry.node.host, ""
        inbound = entry.inbound
        labels = {
            "tag": tag,
            "name": inbound.label,
            "node": node,
            "provider": provider,
            "critical": _bool(inbound.critical),
            "outage": _bool(inbound.outage),
        }
        rendered = ",".join(f'{key}="{_escape(value)}"' for key, value in labels.items())
        lines.append(f"vpn_inbound_info{{{rendered}}} 1")
    return "\n".join(lines) + "\n"


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
