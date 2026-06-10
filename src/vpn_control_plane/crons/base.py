from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

from vpn_control_plane.data import ControlPlaneStore

if TYPE_CHECKING:
    from vpn_control_plane.config import Settings

logger = logging.getLogger(__name__)

CronHandler = Callable[["Settings", ControlPlaneStore], Awaitable[None]]
CRON_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
ITERATION_TIMEOUT_SECONDS = 60 * 60


async def run_iterations_forever(
    name: str,
    iteration: Callable[[], Awaitable[None]],
    *,
    delay_until_next_run: Callable[[], float],
    iteration_timeout_seconds: float = ITERATION_TIMEOUT_SECONDS,
) -> None:
    """The single loop every scheduled background job runs on.

    Guarantees:
    - iterations never overlap: the next run is scheduled only after the previous one finishes;
    - a failing iteration never stops the loop or affects later iterations;
    - an iteration stuck past ``iteration_timeout_seconds`` is cancelled and the loop moves on.

    Cancelling the surrounding task is the only way to stop the loop.
    """
    while True:
        await asyncio.sleep(delay_until_next_run())
        try:
            async with asyncio.timeout(iteration_timeout_seconds):
                await iteration()
        except TimeoutError:
            logger.error(
                "%s iteration was cancelled after exceeding the %.0fs timeout", name, iteration_timeout_seconds
            )
        except Exception:
            logger.exception("%s iteration failed", name)


def interval_delays(interval_seconds: float) -> Callable[[], float]:
    """Delay sequence for interval jobs: first run immediately, then wait ``interval_seconds``
    after each iteration completes."""
    first = True

    def delay_until_next_run() -> float:
        nonlocal first
        if first:
            first = False
            return 0.0
        return interval_seconds

    return delay_until_next_run


class CronScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class CronJob:
    name: str
    handler: CronHandler
    schedule: str


class App(FastAPI):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.cron_jobs: list[CronJob] = []

    def add_cron_job(self, handler: CronHandler, schedule: str, *, name: str | None = None) -> None:
        self.cron_jobs.append(CronJob(name=name or handler.__name__, handler=handler, schedule=schedule))


def wire_cron_jobs(app: App, settings: Settings) -> None:
    from vpn_control_plane.crons import geofiles, telegram_report

    telegram_report.register(app, settings)
    geofiles.register(app, settings)


@asynccontextmanager
async def run_registered_cron_jobs(
    app: App,
    settings: Settings,
    store: ControlPlaneStore,
) -> AsyncIterator[None]:
    tasks = [_start_cron_job(job, settings, store) for job in app.cron_jobs]
    try:
        yield
    finally:
        for name, task in reversed(tasks):
            logger.info("Stopping %s cron job", name)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _start_cron_job(job: CronJob, settings: Settings, store: ControlPlaneStore) -> tuple[str, asyncio.Task[None]]:
    logger.info("Starting %s cron job", job.name)
    task = asyncio.create_task(_run_cron_loop(job, settings, store))
    task.add_done_callback(lambda completed_task: _log_cron_task_failure(job.name, completed_task))
    return job.name, task


async def _run_cron_loop(job: CronJob, settings: Settings, store: ControlPlaneStore) -> None:
    def delay_until_next_run() -> float:
        now = datetime.now(UTC)
        next_time = next_cron_time(job.schedule, now)
        logger.info("Next %s cron run is scheduled at %s", job.name, next_time.isoformat())
        return max(0.0, (next_time - now).total_seconds())

    await run_iterations_forever(
        job.name,
        lambda: job.handler(settings, store),
        delay_until_next_run=delay_until_next_run,
    )


def _log_cron_task_failure(name: str, task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exception = task.exception()
    if exception is not None:
        logger.error(
            "%s cron task stopped unexpectedly",
            name,
            exc_info=(type(exception), exception, exception.__traceback__),
        )


def next_cron_time(expression: str, now: datetime) -> datetime:
    fields = _parse_cron_expression(expression)
    candidate = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _minute in range(366 * 24 * 60):
        if _matches(candidate, fields):
            return candidate
        candidate += timedelta(minutes=1)
    raise CronScheduleError("cron expression did not match any time within one year")


def validate_cron_expression(expression: str) -> str:
    _parse_cron_expression(expression)
    return expression.strip()


def _parse_cron_expression(expression: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    parts = expression.strip().split()
    if len(parts) != 5:
        raise CronScheduleError("cron expression must have five fields")
    parsed = [_parse_field(part, *field_range) for part, field_range in zip(parts, CRON_FIELD_RANGES, strict=True)]
    return parsed[0], parsed[1], parsed[2], parsed[3], parsed[4]


def _parse_field(field: str, minimum: int, maximum: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise CronScheduleError("empty cron field part")
        step = 1
        if "/" in part:
            base, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) <= 0:
                raise CronScheduleError("cron step must be a positive integer")
            step = int(step_text)
        else:
            base = part

        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start, end = _parse_int(start_text), _parse_int(end_text)
        else:
            start = end = _parse_int(base)

        if start < minimum or end > maximum or start > end:
            raise CronScheduleError("cron field value is out of range")
        values.update(range(start, end + 1, step))
    if maximum == 7 and 7 in values:
        values.add(0)
        values.remove(7)
    return values


def _parse_int(value: str) -> int:
    if not value.isdigit():
        raise CronScheduleError("cron field value must be an integer")
    return int(value)


def _matches(candidate: datetime, fields: tuple[set[int], set[int], set[int], set[int], set[int]]) -> bool:
    minutes, hours, month_days, months, weekdays = fields
    cron_weekday = (candidate.weekday() + 1) % 7
    return (
        candidate.minute in minutes
        and candidate.hour in hours
        and candidate.day in month_days
        and candidate.month in months
        and cron_weekday in weekdays
    )
