from __future__ import annotations

from datetime import datetime, timedelta

CRON_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


class CronScheduleError(ValueError):
    pass


def next_cron_time(expression: str, now: datetime) -> datetime:
    fields = _parse_cron_expression(expression)
    candidate = (now.replace(second=0, microsecond=0) + timedelta(minutes=1))
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
