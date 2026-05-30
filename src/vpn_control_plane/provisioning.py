from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from vpn_control_plane.data import (
    ClientRecord,
    ControlPlaneStore,
    NodeCatalogInbound,
    NodeRecord,
    build_inbound_catalog,
    effective_inbound_tags,
)
from vpn_control_plane.xui import XuiInbound, XuiNodeClient


class ProvisioningError(RuntimeError):
    pass


JsonObject = dict[str, Any]
SUBSCRIPTION_ID_BYTES = 18


@dataclass(frozen=True)
class ProvisioningResult:
    client: ClientRecord
    created: int
    reused: int


def client_email(client_id: str) -> str:
    return f"{client_id}"


def telegram_client_id(telegram_user_id: int | str) -> str:
    return str(telegram_user_id).strip()


def telegram_id_for_client_id(client_id: str) -> int | None:
    client_id = client_id.strip()
    return int(client_id) if client_id.isdecimal() else None


def effective_telegram_id(record: ClientRecord) -> int | None:
    if record.telegram_id is not None and record.telegram_id.isdecimal():
        return int(record.telegram_id)
    return telegram_id_for_client_id(record.id)


def compute_node_flow(inbounds: list[XuiInbound], default_vless_flow: str) -> str:
    """Return default_vless_flow when all vless inbounds on this node use TCP; otherwise ""."""
    vless_inbounds = [ib for ib in inbounds if ib.protocol.lower() == "vless"]
    if not vless_inbounds:
        return ""
    if all(str(ib.stream_settings.get("network") or "tcp") == "tcp" for ib in vless_inbounds):
        return default_vless_flow
    return ""


def build_client_payload(
    *,
    client_id: str,
    sub_id: str,
    comment: str,
    telegram_id: int | None,
    flow: str,
) -> JsonObject:
    payload: JsonObject = {
        "email": client_email(client_id),
        "subId": sub_id,
        "comment": comment,
        "enable": True,
        "limitIp": 0,
        "totalGB": 0,
        "expiryTime": 0,
        "reset": 0,
        "flow": flow,
    }
    if telegram_id is not None:
        payload["tgId"] = telegram_id
    return payload


def generate_manual_client_id(existing_client_ids: set[str] | None = None) -> str:
    existing_client_ids = existing_client_ids or set()
    while True:
        candidate = secrets.token_hex(4)
        if candidate not in existing_client_ids:
            return candidate


def generate_subscription_id() -> str:
    return secrets.token_urlsafe(SUBSCRIPTION_ID_BYTES)


class ProvisioningService:
    def __init__(
        self,
        store: ControlPlaneStore,
        *,
        default_vless_flow: str = "xtls-rprx-vision",
        node_client_factory: Callable[[NodeRecord], XuiNodeClient] | None = None,
        subscription_id_factory: Callable[[], str] = generate_subscription_id,
    ) -> None:
        self._store = store
        self._default_vless_flow = default_vless_flow.strip()
        self._node_client_factory = node_client_factory or XuiNodeClient
        self._subscription_id_factory = subscription_id_factory
        self._locks_guard = asyncio.Lock()
        self._client_locks: dict[str, asyncio.Lock] = {}

    async def ensure_telegram_user(
        self,
        telegram_user_id: int | str,
        *,
        comment: str,
        username: str | None = None,
    ) -> ProvisioningResult:
        client_id = telegram_client_id(telegram_user_id)
        telegram_id = telegram_id_for_client_id(client_id)
        display_comment = f"{comment} (@{username})" if username else comment
        return await self.ensure_client(client_id, comment=display_comment, telegram_id=telegram_id)

    async def issue_manual_client(self, *, comment: str) -> ProvisioningResult:
        state = self._store.load_state()
        client_id = generate_manual_client_id({client.id for client in state.clients})
        return await self.ensure_client(client_id, comment=comment, telegram_id=None)

    async def ensure_client(
        self,
        client_id: str,
        *,
        comment: str = "",
        telegram_id: int | None = None,
    ) -> ProvisioningResult:
        client_id = client_id.strip()
        if not client_id:
            raise ProvisioningError("client ID must not be empty")

        async with await self._client_lock(client_id):
            return await self._ensure_client_locked(client_id, comment=comment, telegram_id=telegram_id)

    async def _client_lock(self, client_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._client_locks.get(client_id)
            if lock is None:
                lock = asyncio.Lock()
                self._client_locks[client_id] = lock
            return lock

    async def _ensure_client_locked(
        self,
        client_id: str,
        *,
        comment: str,
        telegram_id: int | None,
    ) -> ProvisioningResult:
        state = self._store.load_state()
        existing_record = next((client for client in state.clients if client.id == client_id), None)
        candidate_record = existing_record or ClientRecord(id=client_id, comment=comment)
        catalog = build_inbound_catalog(state)
        final_sub_id = (existing_record.sub_id if existing_record else None) or self._subscription_id_factory()
        email = client_email(client_id)

        # Group target node-inbounds by node.
        node_inbounds: dict[int, tuple[NodeRecord, list[NodeCatalogInbound]]] = {}
        for tag in effective_inbound_tags(state, candidate_record):
            catalog_inbound = catalog[tag]
            if not isinstance(catalog_inbound, NodeCatalogInbound):
                continue
            node = catalog_inbound.node
            if node.id not in node_inbounds:
                node_inbounds[node.id] = (node, [])
            node_inbounds[node.id][1].append(catalog_inbound)

        clients_by_node = {node_id: self._node_client_factory(node) for node_id, (node, _) in node_inbounds.items()}
        created = 0
        reused = 0

        try:
            for node_id, (node, catalog_inbounds) in node_inbounds.items():
                xui_client = clients_by_node[node_id]

                # Load all inbounds from the node once per node.
                listed_inbounds = await xui_client.list_inbounds()
                inbounds_by_id = {ib.id: ib for ib in listed_inbounds}

                # Validate all target inbounds exist and compute per-node flow.
                target_inbounds: list[XuiInbound] = []
                for ci in catalog_inbounds:
                    inbound = inbounds_by_id.get(ci.inbound.xui_inbound_id)
                    if inbound is None:
                        raise ProvisioningError(f"inbound {ci.inbound.xui_inbound_id} was not found on node {node.id}")
                    target_inbounds.append(inbound)

                target_ids = [ib.id for ib in target_inbounds]
                node_flow = self._compute_node_flow(target_inbounds)

                # Check whether the client already exists on this node.
                info = await xui_client.get_client(email)
                if info is None:
                    payload = self._build_client_payload(
                        client_id=client_id,
                        sub_id=final_sub_id,
                        comment=comment,
                        telegram_id=telegram_id,
                        flow=node_flow,
                    )
                    await xui_client.add_client(payload, target_ids)
                    created += len(target_ids)
                else:
                    existing_ids = set(info.inbound_ids)
                    missing = [i for i in target_ids if i not in existing_ids]
                    if missing:
                        await xui_client.attach_client(email, missing)
                    created += len(missing)
                    reused += len(target_ids) - len(missing)

        finally:
            for xui_client in clients_by_node.values():
                close = getattr(xui_client, "close", None)
                if close is not None:
                    await close()

        client_record = self._persist_client_record(
            state.clients,
            existing_record=existing_record,
            client_id=client_id,
            comment=comment,
            sub_id=final_sub_id,
        )
        return ProvisioningResult(client=client_record, created=created, reused=reused)

    def _build_client_payload(
        self,
        *,
        client_id: str,
        sub_id: str,
        comment: str,
        telegram_id: int | None,
        flow: str,
    ) -> JsonObject:
        return build_client_payload(
            client_id=client_id,
            sub_id=sub_id,
            comment=comment,
            telegram_id=telegram_id,
            flow=flow,
        )

    def _compute_node_flow(self, inbounds: list[XuiInbound]) -> str:
        return compute_node_flow(inbounds, self._default_vless_flow)

    def _persist_client_record(
        self,
        clients: list[ClientRecord],
        *,
        existing_record: ClientRecord | None,
        client_id: str,
        comment: str,
        sub_id: str,
    ) -> ClientRecord:
        record = ClientRecord(
            id=client_id,
            comment=existing_record.comment if existing_record else comment,
            subId=sub_id,
            legacySubId=_legacy_sub_id(existing_record),
            inboundTags=existing_record.inbound_tags if existing_record else None,
        )
        next_clients = [client for client in clients if client.id != client_id]
        next_clients.append(record)
        self._store.save_clients(next_clients)
        return record


def _legacy_sub_id(existing_record: ClientRecord | None) -> str | None:
    if existing_record is None:
        return None
    if existing_record.legacy_sub_id is not None:
        return existing_record.legacy_sub_id
    if existing_record.sub_id is None:
        return existing_record.id
    return None
