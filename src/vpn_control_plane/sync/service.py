from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from vpn_control_plane.data import (
    ClientRecord,
    ControlPlaneStore,
    NodeCatalogInbound,
    NodeRecord,
    build_inbound_catalog,
    effective_inbound_tags,
)
from vpn_control_plane.provisioning import (
    build_client_payload,
    client_email,
    compute_node_flow,
    effective_telegram_id,
)
from vpn_control_plane.xui import XuiClientInfo, XuiInbound, XuiNodeClient

logger = logging.getLogger(__name__)

JsonObject = dict[str, Any]


@dataclass
class ClientSyncReport:
    created: int = 0
    updated: int = 0
    attached: int = 0
    node_errors: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        line = f"sync: created={self.created}, updated={self.updated}, attached={self.attached}"
        if self.node_errors:
            line += f", node_errors={len(self.node_errors)}"
            for error in self.node_errors:
                line += f"\n  - {error}"
        return line


@dataclass
class _NodePlan:
    node: NodeRecord
    targets: list[tuple[ClientRecord, list[int]]]


class ClientSyncService:
    """Push ``data.json`` clients onto the 3x-ui nodes.

    For every client, ensure it exists on each node that hosts one of its inbounds with
    the ``comment``/``subId``/``tgId`` recorded in ``data.json``; create missing clients
    and attach missing inbounds. Clients present on a panel but absent from ``data.json``
    are never touched, and nothing is ever deleted.
    """

    def __init__(
        self,
        store: ControlPlaneStore,
        *,
        default_vless_flow: str = "xtls-rprx-vision",
        node_client_factory: Callable[[NodeRecord], XuiNodeClient] | None = None,
        dry_run: bool = False,
    ) -> None:
        self._store = store
        self._default_vless_flow = default_vless_flow.strip()
        self._node_client_factory = node_client_factory or XuiNodeClient
        self._dry_run = dry_run

    async def sync(self) -> ClientSyncReport:
        state = self._store.load_state()
        catalog = build_inbound_catalog(state)
        report = ClientSyncReport()
        for plan in self._build_node_plans(state, catalog):
            await self._sync_node(plan, report)
        return report

    def _build_node_plans(self, state: Any, catalog: dict[str, Any]) -> list[_NodePlan]:
        plans: dict[int, _NodePlan] = {}
        for client in state.clients:
            per_node: dict[int, list[int]] = {}
            nodes_by_id: dict[int, NodeRecord] = {}
            for tag in effective_inbound_tags(state, client):
                catalog_inbound = catalog.get(tag)
                if not isinstance(catalog_inbound, NodeCatalogInbound):
                    continue
                node = catalog_inbound.node
                nodes_by_id[node.id] = node
                per_node.setdefault(node.id, []).append(catalog_inbound.inbound.xui_inbound_id)
            for node_id, inbound_ids in per_node.items():
                plan = plans.get(node_id)
                if plan is None:
                    plan = _NodePlan(node=nodes_by_id[node_id], targets=[])
                    plans[node_id] = plan
                plan.targets.append((client, inbound_ids))
        return list(plans.values())

    async def _sync_node(self, plan: _NodePlan, report: ClientSyncReport) -> None:
        client = self._node_client_factory(plan.node)
        try:
            try:
                inbounds_by_id = {ib.id: ib for ib in await client.list_inbounds()}
                existing = {info.client["email"]: info for info in await client.list_clients()}
            except Exception as exc:  # noqa: BLE001 - one dead node must not abort the whole sync.
                logger.warning("Sync skipped node %s: %s", plan.node.id, exc)
                report.node_errors.append(f"node {plan.node.id}: {exc}")
                return
            for record, target_ids in plan.targets:
                try:
                    await self._reconcile_client(
                        client, plan.node, record, target_ids, inbounds_by_id, existing, report
                    )
                except Exception as exc:  # noqa: BLE001 - one bad client must not abort the rest.
                    logger.warning("Sync failed for client %s on node %s: %s", record.id, plan.node.id, exc)
                    report.node_errors.append(f"node {plan.node.id}: client {record.id}: {exc}")
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                await close()

    async def _reconcile_client(
        self,
        client: XuiNodeClient,
        node: NodeRecord,
        record: ClientRecord,
        target_ids: list[int],
        inbounds_by_id: dict[int, XuiInbound],
        existing: dict[str, XuiClientInfo],
        report: ClientSyncReport,
    ) -> None:
        missing_inbounds = [inbound_id for inbound_id in target_ids if inbound_id not in inbounds_by_id]
        if missing_inbounds:
            report.node_errors.append(f"node {node.id}: inbound(s) {missing_inbounds} for client {record.id} not found")
            return

        email = client_email(record.id)
        comment = record.comment
        sub_id = record.effective_sub_id
        telegram_id = effective_telegram_id(record)
        info = existing.get(email)

        if info is None:
            flow = compute_node_flow([inbounds_by_id[i] for i in target_ids], self._default_vless_flow)
            payload = build_client_payload(
                client_id=record.id,
                sub_id=sub_id,
                comment=comment,
                telegram_id=telegram_id,
                flow=flow,
            )
            await self._create_client(client, node, payload, target_ids, report)
            return

        await self._update_existing(client, node, email, comment, sub_id, telegram_id, target_ids, info, report)

    async def _create_client(
        self,
        client: XuiNodeClient,
        node: NodeRecord,
        payload: JsonObject,
        target_ids: list[int],
        report: ClientSyncReport,
    ) -> None:
        if self._dry_run:
            logger.info("[dry-run] create client %s on node %s with inbounds %s", payload["email"], node.id, target_ids)
        else:
            await client.add_client(payload, target_ids)
        report.created += 1

    async def _update_existing(
        self,
        client: XuiNodeClient,
        node: NodeRecord,
        email: str,
        comment: str,
        sub_id: str,
        telegram_id: int | None,
        target_ids: list[int],
        info: XuiClientInfo,
        report: ClientSyncReport,
    ) -> None:
        if _needs_update(info.client, comment, sub_id, telegram_id):
            payload = {**info.client, "comment": comment, "subId": sub_id}
            if telegram_id is not None:
                payload["tgId"] = telegram_id
            if self._dry_run:
                logger.info("[dry-run] update client %s on node %s", email, node.id)
            else:
                await client.update_client(email, payload)
            report.updated += 1

        attached_ids = set(info.inbound_ids)
        missing = [inbound_id for inbound_id in target_ids if inbound_id not in attached_ids]
        if missing:
            if self._dry_run:
                logger.info("[dry-run] attach client %s on node %s to inbounds %s", email, node.id, missing)
            else:
                await client.attach_client(email, missing)
            report.attached += len(missing)


def _needs_update(current: JsonObject, comment: str, sub_id: str, telegram_id: int | None) -> bool:
    return (
        str(current.get("comment") or "") != comment
        or str(current.get("subId") or "") != sub_id
        or (telegram_id is not None and _as_int(current.get("tgId")) != telegram_id)
    )


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdecimal():
        return int(value)
    return None
