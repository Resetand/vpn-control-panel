from __future__ import annotations

import asyncio
import contextlib

from vpn_control_plane.config import Settings
from vpn_control_plane.crons.base import App, interval_delays, run_iterations_forever, wire_cron_jobs


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "VPN_TELEGRAM_BOT_TOKEN": "token",
        "VPN_TELEGRAM_ADMIN_IDS": "1",
    }
    values.update(overrides)
    return Settings.model_validate(values)


def test_wire_cron_jobs_registers_enabled_jobs() -> None:
    app = App()

    wire_cron_jobs(
        app,
        settings(
            REPORT_TELEGRAM_ENABLED="true",
            REPORT_TELEGRAM_SCHEDULE="5 3 * * *",
            GEOFILES_UPDATE_ENABLED="true",
            GEOFILES_UPDATE_SCHEDULE="10 4 * * *",
        ),
    )

    assert [(job.name, job.schedule) for job in app.cron_jobs] == [
        ("Telegram report", "5 3 * * *"),
        ("geofiles update", "10 4 * * *"),
    ]


def test_wire_cron_jobs_skips_disabled_jobs() -> None:
    app = App()

    wire_cron_jobs(app, settings())

    assert app.cron_jobs == []


async def _wait_for(condition, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.001)


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_failing_iteration_does_not_stop_the_loop() -> None:
    runs: list[int] = []

    async def iteration() -> None:
        runs.append(len(runs))
        if len(runs) == 1:
            raise RuntimeError("boom")

    task = asyncio.create_task(run_iterations_forever("test", iteration, delay_until_next_run=lambda: 0.001))
    try:
        await _wait_for(lambda: len(runs) >= 3)
    finally:
        await _cancel(task)


async def test_iterations_never_overlap() -> None:
    active = 0
    max_active = 0
    finished = 0

    async def iteration() -> None:
        nonlocal active, max_active, finished
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        finished += 1

    # Zero delay between runs: any overlap would be visible immediately.
    task = asyncio.create_task(run_iterations_forever("test", iteration, delay_until_next_run=lambda: 0.0))
    try:
        await _wait_for(lambda: finished >= 3)
        assert max_active == 1
    finally:
        await _cancel(task)


async def test_hung_iteration_is_cancelled_after_timeout_and_loop_continues() -> None:
    runs = 0
    hung_iteration_cancelled = False

    async def iteration() -> None:
        nonlocal runs, hung_iteration_cancelled
        runs += 1
        if runs == 1:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                hung_iteration_cancelled = True
                raise

    task = asyncio.create_task(
        run_iterations_forever(
            "test",
            iteration,
            delay_until_next_run=lambda: 0.001,
            iteration_timeout_seconds=0.01,
        )
    )
    try:
        await _wait_for(lambda: runs >= 2)
        assert hung_iteration_cancelled
    finally:
        await _cancel(task)


async def test_cancelling_the_task_stops_the_loop() -> None:
    started = asyncio.Event()

    async def iteration() -> None:
        started.set()

    task = asyncio.create_task(run_iterations_forever("test", iteration, delay_until_next_run=lambda: 0.001))
    await started.wait()
    await _cancel(task)
    assert task.cancelled()


def test_interval_delays_first_run_is_immediate() -> None:
    delays = interval_delays(60.0)

    assert delays() == 0.0
    assert delays() == 60.0
    assert delays() == 60.0
