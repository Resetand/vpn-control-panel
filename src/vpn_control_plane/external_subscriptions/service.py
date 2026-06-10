from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx

from vpn_control_plane.config import Settings
from vpn_control_plane.crons.base import interval_delays, run_iterations_forever
from vpn_control_plane.data import ControlPlaneStore, ExternalSubscriptionRecord
from vpn_control_plane.external_subscriptions.cache import (
    ResolvedExternalInbound,
    ResolvedInbounds,
    ResolvedInboundsStore,
)
from vpn_control_plane.external_subscriptions.parser import (
    ParsedSubscriptionEntry,
    parse_subscription_body,
)
from vpn_control_plane.external_subscriptions.slug import assign_slugs

logger = logging.getLogger(__name__)

Fetcher = Callable[[str], Awaitable[str]]

_FETCH_TIMEOUT_SECONDS = 30.0
# The loop wakes every minute and each subscription decides for itself (by updatedAt vs. its
# updateInterval) whether it is time to refetch — so all cadence config lives in data.json.
_TICK_SECONDS = 60


def build_resolved_inbounds(
    subscription: ExternalSubscriptionRecord,
    entries: list[ParsedSubscriptionEntry],
    *,
    now: str,
) -> list[ResolvedExternalInbound]:
    """Filter feed entries (by fragment) and assign each a stable slug, stamping `now` as the
    refresh time. `updatedAt` is the marker the loop uses to decide when to refetch."""
    if subscription.inbound_filter is not None:
        pattern = re.compile(subscription.inbound_filter)
        entries = [entry for entry in entries if pattern.search(entry.label)]
    return [
        ResolvedExternalInbound(slug=slug, label=entry.label, uri=entry.uri, updatedAt=now)
        for slug, entry in assign_slugs(entries)
    ]


class ExternalSubscriptionService:
    def __init__(
        self,
        settings: Settings,
        store: ControlPlaneStore,
        *,
        fetcher: Fetcher | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._cache = ResolvedInboundsStore(settings.external_subscriptions_resolved_path)
        self._fetch = fetcher or _http_fetch
        self._now = now or (lambda: datetime.now(UTC))

    async def run_forever(self) -> None:
        await run_iterations_forever(
            "External subscriptions refresh",
            self.refresh_due,
            delay_until_next_run=interval_delays(_TICK_SECONDS),
        )

    async def refresh_due(self) -> None:
        """Refresh only subscriptions whose updateInterval has elapsed (the loop's per-tick work)."""
        subscriptions = self._store.load_state().external_subscriptions
        if not subscriptions:
            return
        resolved = self._cache.load()
        now_dt = self._now()
        due = [subscription for subscription in subscriptions if self._is_due(subscription, resolved, now_dt)]
        await self._refresh(subscriptions, due, resolved, now_dt)

    async def refresh_all(self) -> ResolvedInbounds:
        """Force-refresh every subscription regardless of interval (manual one-shot run)."""
        subscriptions = self._store.load_state().external_subscriptions
        resolved = self._cache.load()
        if not subscriptions:
            return resolved
        await self._refresh(subscriptions, subscriptions, resolved, self._now())
        return self._cache.load()

    async def _refresh(
        self,
        subscriptions: list[ExternalSubscriptionRecord],
        to_refresh: list[ExternalSubscriptionRecord],
        resolved: ResolvedInbounds,
        now_dt: datetime,
    ) -> None:
        if not to_refresh:
            return
        now_iso = now_dt.isoformat()

        # Fetch each distinct url at most once per cycle (subscriptions may share a url).
        bodies = await self._fetch_bodies({subscription.url for subscription in to_refresh})

        # Keep entries of subscriptions we are not refreshing now; drop entries of subscriptions
        # removed from data.json (orphan cleanup).
        updated: ResolvedInbounds = {
            subscription.name: resolved.get(subscription.name, []) for subscription in subscriptions
        }
        changed = False
        for subscription in to_refresh:
            body = bodies.get(subscription.url)
            if body is None:
                continue  # fetch failed -> keep last-known entries and retry next tick
            updated[subscription.name] = build_resolved_inbounds(
                subscription, parse_subscription_body(body), now=now_iso
            )
            changed = True

        if changed:
            self._cache.save(updated)

    async def _fetch_bodies(self, urls: set[str]) -> dict[str, str | None]:
        bodies: dict[str, str | None] = {}
        for url in urls:
            try:
                bodies[url] = await self._fetch(url)
            except Exception as exc:  # noqa: BLE001 - one feed failing must not abort the others.
                logger.warning("External subscription fetch failed", extra={"url": url, "error": str(exc)})
                bodies[url] = None
        return bodies

    def _is_due(self, subscription: ExternalSubscriptionRecord, resolved: ResolvedInbounds, now_dt: datetime) -> bool:
        entries = resolved.get(subscription.name)
        if not entries:
            return True
        try:
            oldest = min(datetime.fromisoformat(entry.updated_at) for entry in entries)
        except (ValueError, TypeError):
            return True
        return now_dt - oldest >= timedelta(minutes=subscription.update_interval)


async def _http_fetch(url: str) -> str:
    async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": "vpn-control-plane"})
        response.raise_for_status()
        return response.text


async def run_external_subscription_refresh(settings: Settings, store: ControlPlaneStore) -> None:
    service = ExternalSubscriptionService(settings, store)
    await service.run_forever()
