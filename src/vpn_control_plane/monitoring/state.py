from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from vpn_control_plane.data import NodeRecord
from vpn_control_plane.monitoring.detectors import ActiveCondition

EventKey = tuple[int, str]


@dataclass(frozen=True)
class AlertCandidate:
    node: NodeRecord
    condition: ActiveCondition
    active_for: timedelta


@dataclass
class _ActiveRecord:
    first_seen_at: datetime
    last_seen_at: datetime
    condition: ActiveCondition


class MonitoringAlertState:
    def __init__(self) -> None:
        self._active: dict[EventKey, _ActiveRecord] = {}
        self._last_alert_sent_at: dict[EventKey, datetime] = {}

    def update(
        self,
        node: NodeRecord,
        conditions: list[ActiveCondition],
        *,
        now: datetime,
        failure_duration: timedelta,
        alert_cooldown: timedelta,
    ) -> list[AlertCandidate]:
        active_keys = {(node.id, condition.category) for condition in conditions}
        for key in [key for key in self._active if key[0] == node.id and key not in active_keys]:
            del self._active[key]

        candidates: list[AlertCandidate] = []
        for condition in conditions:
            key = (node.id, condition.category)
            record = self._active.get(key)
            if record is None:
                record = _ActiveRecord(first_seen_at=now, last_seen_at=now, condition=condition)
                self._active[key] = record
            else:
                record.last_seen_at = now
                record.condition = condition

            active_for = now - record.first_seen_at
            if active_for < failure_duration:
                continue
            last_alert_sent_at = self._last_alert_sent_at.get(key)
            if last_alert_sent_at is not None and now - last_alert_sent_at < alert_cooldown:
                continue
            candidates.append(AlertCandidate(node=node, condition=condition, active_for=active_for))
        return candidates

    def mark_alert_sent(self, candidate: AlertCandidate, *, now: datetime) -> None:
        self._last_alert_sent_at[(candidate.node.id, candidate.condition.category)] = now
