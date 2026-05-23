from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from typing import Any, TypeVar

from pydantic import TypeAdapter, ValidationError

from vpn_control_plane.data.models import (
    ClientRecord,
    ControlPlaneState,
    InboundRecord,
    NodeRecord,
    SubscriptionMetadata,
)

T = TypeVar("T")
ENV_TEMPLATE_RE = re.compile(r"^\$\{\{\s*env\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")


def resolve_env_templates(value: Any) -> Any:
    if isinstance(value, str):
        match = ENV_TEMPLATE_RE.fullmatch(value)
        if match is None:
            return value
        env_name = match.group(1)
        if env_name not in os.environ:
            raise ValueError(f"environment variable {env_name} is not set")
        return os.environ[env_name]
    if isinstance(value, list):
        return [resolve_env_templates(item) for item in value]
    if isinstance(value, dict):
        return {key: resolve_env_templates(item) for key, item in value.items()}
    return value


class StateValidationError(ValueError):
    def __init__(self, file_path: Path, message: str) -> None:
        self.file_path = file_path
        super().__init__(f"{file_path}: {message}")


class JsonStateStore:
    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)
        self._lock = RLock()
        self._nodes = self.data_dir / "nodes.json"
        self._clients = self.data_dir / "clients.json"
        self._inbounds = self.data_dir / "inbounds.json"
        self._subscription = self.data_dir / "subscription.json"

    def verify_ready(self) -> None:
        if not self.data_dir.exists():
            raise StateValidationError(self.data_dir, "data directory does not exist")
        if not self.data_dir.is_dir():
            raise StateValidationError(self.data_dir, "data path is not a directory")

        for file_path in (self._nodes, self._clients, self._inbounds, self._subscription):
            if not file_path.exists():
                raise StateValidationError(file_path, "required data file is missing")
            if not file_path.is_file():
                raise StateValidationError(file_path, "required data path is not a file")
            try:
                with file_path.open("r", encoding="utf-8"):
                    pass
            except OSError as exc:
                raise StateValidationError(file_path, f"required data file is not readable: {exc}") from exc

    def load_state(self) -> ControlPlaneState:
        with self._lock:
            return ControlPlaneState(
                nodes=self.load_nodes(),
                clients=self.load_clients(),
                inbounds=self.load_inbounds(),
                subscription=self.load_subscription(),
            )

    def load_nodes(self) -> list[NodeRecord]:
        return self._load_model_list(self._nodes, TypeAdapter(list[NodeRecord]), default=[])

    def load_clients(self) -> list[ClientRecord]:
        return self._load_model_list(self._clients, TypeAdapter(list[ClientRecord]), default=[])

    def load_inbounds(self) -> list[InboundRecord]:
        return self._load_model_list(self._inbounds, TypeAdapter(list[InboundRecord]), default=[])

    def load_subscription(self) -> SubscriptionMetadata:
        return self._load_model(
            self._subscription,
            TypeAdapter(SubscriptionMetadata),
            default_factory=SubscriptionMetadata,
        )

    def save_clients(self, clients: list[ClientRecord]) -> None:
        self._save_model(self._clients, TypeAdapter(list[ClientRecord]), clients, exclude_none=True)

    def save_subscription(self, subscription: SubscriptionMetadata) -> None:
        self._save_model(self._subscription, TypeAdapter(SubscriptionMetadata), subscription)

    def _read_json(self, file_path: Path, default: T) -> Any | T:
        if not file_path.exists():
            return default
        try:
            raw = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StateValidationError(file_path, str(exc)) from exc
        if not raw.strip():
            return default
        try:
            return resolve_env_templates(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise StateValidationError(
                file_path,
                f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
            ) from exc
        except ValueError as exc:
            raise StateValidationError(file_path, str(exc)) from exc

    def _load_model_list(self, file_path: Path, adapter: TypeAdapter[T], default: list[Any]) -> T:
        data = self._read_json(file_path, default)
        return self._validate(file_path, adapter, data)

    def _load_model(self, file_path: Path, adapter: TypeAdapter[T], default_factory: Callable[[], Any]) -> T:
        data = self._read_json(file_path, default_factory().model_dump(by_alias=True, mode="json"))
        return self._validate(file_path, adapter, data)

    def _validate(self, file_path: Path, adapter: TypeAdapter[T], data: Any) -> T:
        try:
            return adapter.validate_python(data)
        except ValidationError as exc:
            first_error = exc.errors()[0]
            location = ".".join(str(part) for part in first_error.get("loc", ())) or "<root>"
            message = first_error.get("msg", "validation failed")
            raise StateValidationError(file_path, f"{location}: {message}") from exc

    def _save_model(
        self,
        file_path: Path,
        adapter: TypeAdapter[T],
        value: T,
        *,
        exclude_none: bool = False,
    ) -> None:
        with self._lock:
            validated = self._validate(file_path, adapter, value)
            data = adapter.dump_python(validated, by_alias=True, mode="json", exclude_none=exclude_none)
            self._write_json_atomic(file_path, data)

    def _write_json_atomic(self, file_path: Path, data: Any) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=file_path.parent,
                prefix=f".{file_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                temp_file.write(serialized)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, file_path)
            temp_path = None
            self._fsync_directory(file_path.parent)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if not hasattr(os, "O_DIRECTORY"):
            return
        try:
            directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
