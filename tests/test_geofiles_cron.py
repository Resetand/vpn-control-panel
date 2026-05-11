from __future__ import annotations

import json
from pathlib import Path

import pytest

from vpn_control_plane.crons.geofiles import update_geofiles_on_all_nodes
from vpn_control_plane.data import JsonStateStore, NodeRecord


def write_state(tmp_path: Path, nodes: list[dict[str, object]]) -> None:
    (tmp_path / "nodes.json").write_text(json.dumps(nodes), encoding="utf-8")
    (tmp_path / "clients.json").write_text("[]", encoding="utf-8")
    (tmp_path / "inbounds.json").write_text("[]", encoding="utf-8")
    (tmp_path / "subscription.json").write_text("{}", encoding="utf-8")


@pytest.mark.asyncio
async def test_update_geofiles_on_all_nodes_continues_after_node_failure(tmp_path: Path) -> None:
    write_state(
        tmp_path,
        [
            {"id": 1, "host": "one.example.test", "port": 443, "apiToken": "token-1"},
            {"id": 2, "host": "two.example.test", "port": 443, "apiToken": "token-2"},
        ],
    )
    updated: list[int] = []
    closed: list[int] = []

    class FakeClient:
        def __init__(self, node: NodeRecord) -> None:
            self.node = node

        async def update_geofiles(self) -> None:
            updated.append(self.node.id)
            if self.node.id == 1:
                raise RuntimeError("temporary node failure")

        async def close(self) -> None:
            closed.append(self.node.id)

    await update_geofiles_on_all_nodes(JsonStateStore(tmp_path), client_factory=FakeClient)

    assert updated == [1, 2]
    assert closed == [1, 2]
