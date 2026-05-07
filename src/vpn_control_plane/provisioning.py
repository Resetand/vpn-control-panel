from __future__ import annotations

import asyncio
import base64
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from vpn_control_plane.data import ClientRecord, JsonStateStore, NodeInboundRecord, NodeRecord
from vpn_control_plane.xui import XuiInbound, XuiNodeClient, find_client_by_email


class ProvisioningError(RuntimeError):
    pass


JsonObject = dict[str, Any]


@dataclass(frozen=True)
class ProvisioningResult:
    client: ClientRecord
    created: int
    reused: int


def legacy_client_email(inbound_id: int, client_id: str) -> str:
    return f"{inbound_id}_{client_id}"


def telegram_client_id(telegram_user_id: int | str) -> str:
    return str(telegram_user_id).strip()


def generate_manual_client_id(existing_client_ids: set[str] | None = None) -> str:
    existing_client_ids = existing_client_ids or set()
    while True:
        candidate = secrets.token_hex(4)
        if candidate not in existing_client_ids:
            return candidate


class ProvisioningService:
    def __init__(
        self,
        store: JsonStateStore,
        *,
        node_client_factory: Callable[[NodeRecord], XuiNodeClient] | None = None,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._store = store
        self._node_client_factory = node_client_factory or XuiNodeClient
        self._uuid_factory = uuid_factory
        self._random_bytes = random_bytes
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
        telegram_id = int(client_id) if client_id.isdecimal() else None
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
        node_inbounds = [inbound for inbound in state.inbounds if isinstance(inbound, NodeInboundRecord)]
        if not node_inbounds:
            raise ProvisioningError("no node-inbound entries are configured")

        nodes_by_id = {node.id: node for node in state.nodes}
        required_node_ids = {inbound.node_id for inbound in node_inbounds}
        clients_by_node = {
            node.id: self._node_client_factory(node) for node in state.nodes if node.id in required_node_ids
        }
        inbound_cache: dict[tuple[int, int], XuiInbound] = {}
        existing_by_inbound: dict[tuple[int, int], JsonObject] = {}
        discovered_sub_id: str | None = existing_record.sub_id if existing_record else None

        try:
            for meta_inbound in node_inbounds:
                if meta_inbound.node_id not in nodes_by_id:
                    raise ProvisioningError(
                        f"node {meta_inbound.node_id} is referenced by an inbound but is not configured"
                    )
                xui_client = clients_by_node[meta_inbound.node_id]
                inbound = await self._load_inbound(xui_client, meta_inbound)
                inbound_cache[(meta_inbound.node_id, meta_inbound.inbound_id)] = inbound
                existing_client = find_client_by_email(inbound, legacy_client_email(meta_inbound.inbound_id, client_id))
                if existing_client is not None:
                    existing_by_inbound[(meta_inbound.node_id, meta_inbound.inbound_id)] = existing_client
                    if discovered_sub_id is None and existing_client.get("subId"):
                        discovered_sub_id = str(existing_client["subId"])

            final_sub_id = discovered_sub_id or client_id
            created = 0
            reused = len(existing_by_inbound)

            for meta_inbound in node_inbounds:
                inbound_key = (meta_inbound.node_id, meta_inbound.inbound_id)
                if inbound_key in existing_by_inbound:
                    continue
                inbound = inbound_cache[inbound_key]
                payload = self.build_client_payload(
                    inbound,
                    client_id=client_id,
                    sub_id=final_sub_id,
                    comment=comment,
                    telegram_id=telegram_id,
                )
                add_result = await clients_by_node[meta_inbound.node_id].add_client(meta_inbound.inbound_id, payload)
                if add_result.created:
                    created += 1
                else:
                    reused += 1

            client_record = self._persist_client_record(
                state.clients,
                existing_record=existing_record,
                client_id=client_id,
                comment=comment,
                sub_id=final_sub_id,
            )
            return ProvisioningResult(client=client_record, created=created, reused=reused)
        finally:
            for xui_client in clients_by_node.values():
                close = getattr(xui_client, "close", None)
                if close is not None:
                    await close()

    async def _load_inbound(self, xui_client: XuiNodeClient, meta_inbound: NodeInboundRecord) -> XuiInbound:
        inbounds = await xui_client.list_inbounds()
        inbound = next((candidate for candidate in inbounds if candidate.id == meta_inbound.inbound_id), None)
        if inbound is None:
            raise ProvisioningError(f"inbound {meta_inbound.inbound_id} was not found on node {meta_inbound.node_id}")
        return inbound

    def build_client_payload(
        self,
        inbound: XuiInbound,
        *,
        client_id: str,
        sub_id: str,
        comment: str,
        telegram_id: int | None = None,
    ) -> JsonObject:
        payload: JsonObject = {
            "email": legacy_client_email(inbound.id, client_id),
            "limitIp": 0,
            "totalGB": 0,
            "expiryTime": 0,
            "enable": True,
            "subId": sub_id,
            "comment": comment,
            "reset": 0,
        }
        if telegram_id is not None:
            payload["tgId"] = telegram_id

        protocol = inbound.protocol.lower()
        if protocol == "trojan":
            payload["password"] = str(self._uuid_factory())
        elif protocol == "shadowsocks":
            method = str(inbound.settings.get("method") or "chacha20-ietf-poly1305")
            payload["password"] = self._shadowsocks_password(method)
            payload["method"] = "" if method.startswith("2022-") else method
        else:
            payload["id"] = str(self._uuid_factory())
            payload["flow"] = self._vless_flow(inbound) if protocol == "vless" else ""
        return payload

    def _shadowsocks_password(self, method: str) -> str:
        if method.startswith("2022-"):
            key_length = 32 if "256" in method else 16
            return base64.b64encode(self._random_bytes(key_length)).decode("ascii")
        return str(self._uuid_factory())

    @staticmethod
    def _vless_flow(inbound: XuiInbound) -> str:
        network = str(inbound.stream_settings.get("network") or "tcp")
        if network != "tcp":
            return ""
        clients = inbound.settings.get("clients", [])
        if isinstance(clients, list):
            for client in clients:
                if isinstance(client, dict) and client.get("flow"):
                    return str(client["flow"])
        return ""

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
            subId=sub_id if sub_id != client_id else None,
        )
        next_clients = [client for client in clients if client.id != client_id]
        next_clients.append(record)
        self._store.save_clients(next_clients)
        return record
