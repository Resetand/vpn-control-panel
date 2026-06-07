from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from threading import RLock

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from vpn_control_plane.data import ExternalSubscriptionRef, parse_external_subscription_ref

logger = logging.getLogger(__name__)


class ResolvedExternalInbound(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    # slug derived from the upstream fragment; this is what "@name:slug" references resolve against.
    slug: str
    label: str
    uri: str
    updated_at: str = Field(alias="updatedAt")


# Generated file shape: {subscription_name: [ResolvedExternalInbound, ...]}.
ResolvedInbounds = dict[str, list[ResolvedExternalInbound]]

_ADAPTER: TypeAdapter[ResolvedInbounds] = TypeAdapter(ResolvedInbounds)


class ResolvedInboundsStore:
    """Reads/writes the generated resolved-inbounds file. Reads never raise — a missing or
    corrupt file yields an empty map so subscription rendering degrades gracefully."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._lock = RLock()

    def load(self) -> ResolvedInbounds:
        with self._lock:
            try:
                raw = self._path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return {}
            except OSError as exc:
                logger.warning(
                    "Could not read resolved inbounds file", extra={"path": str(self._path), "error": str(exc)}
                )
                return {}
            try:
                return _ADAPTER.validate_python(json.loads(raw))
            except (json.JSONDecodeError, ValidationError) as exc:
                logger.warning(
                    "Ignoring malformed resolved inbounds file",
                    extra={"path": str(self._path), "error": str(exc)},
                )
                return {}

    def save(self, resolved: ResolvedInbounds) -> None:
        data = _ADAPTER.dump_python(resolved, by_alias=True, mode="json")
        serialized = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=self._path.parent,
                    prefix=f".{self._path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temp_file:
                    temp_path = Path(temp_file.name)
                    temp_file.write(serialized)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())
                try:
                    os.replace(temp_path, self._path)
                except OSError:
                    # Docker bind mounts may not support atomic rename across the overlay boundary.
                    self._path.write_text(serialized, encoding="utf-8")
                temp_path = None
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)


def resolve_reference(uri: str, resolved: ResolvedInbounds) -> str | None:
    """Resolve an externalInbound uri to a concrete share link.

    Literal uris pass through unchanged. "@name:slug" / "@name:~regex" references look up the
    resolved file and return the matching entry's uri, or None when nothing matches (so the
    caller can simply skip the link).
    """
    try:
        ref = parse_external_subscription_ref(uri)
    except ValueError:
        return None
    if ref is None:
        return uri
    return _resolve_ref(ref, resolved.get(ref.name, []))


def _resolve_ref(ref: ExternalSubscriptionRef, entries: list[ResolvedExternalInbound]) -> str | None:
    if ref.is_regex:
        try:
            pattern = re.compile(ref.query)
        except re.error:
            return None
        for entry in entries:
            if pattern.search(entry.slug):
                return entry.uri
        return None
    for entry in entries:
        if entry.slug == ref.query:
            return entry.uri
    return None
