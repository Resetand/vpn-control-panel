from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any, TypeVar

from pydantic import TypeAdapter, ValidationError

from vpn_control_plane.data.models import ClientRecord, ControlPlaneState, NodeRecord, SubscriptionMetadata

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


class ControlPlaneStore:
    def __init__(self, data_file: Path | str) -> None:
        self._lock = RLock()
        self._data_file = Path(data_file)

    @property
    def data_file(self) -> Path:
        return self._data_file

    def verify_ready(self) -> None:
        if not self._data_file.parent.exists():
            raise StateValidationError(self._data_file.parent, "data file parent directory does not exist")
        if not self._data_file.exists():
            raise StateValidationError(self._data_file, "required data file is missing")
        if not self._data_file.is_file():
            raise StateValidationError(self._data_file, "required data path is not a file")
        try:
            with self._data_file.open("r", encoding="utf-8"):
                pass
        except OSError as exc:
            raise StateValidationError(self._data_file, f"required data file is not readable: {exc}") from exc

    def load_state(self) -> ControlPlaneState:
        with self._lock:
            data = self._read_json()
            return self._validate(TypeAdapter(ControlPlaneState), data)

    def load_nodes(self) -> list[NodeRecord]:
        return self.load_state().nodes

    def load_clients(self) -> list[ClientRecord]:
        return self.load_state().clients

    def load_subscription(self) -> SubscriptionMetadata:
        return self.load_state().subscription

    def save_clients(self, clients: list[ClientRecord]) -> None:
        with self._lock:
            validated_clients = self._validate(TypeAdapter(list[ClientRecord]), clients)
            state = self.load_state().model_copy(update={"clients": validated_clients})
            self._save_state(state)

    def save_subscription(self, subscription: SubscriptionMetadata) -> None:
        with self._lock:
            state = self.load_state().model_copy(update={"subscription": subscription})
            self._save_state(state)

    def _save_state(self, state: ControlPlaneState) -> None:
        validated = self._validate(TypeAdapter(ControlPlaneState), state.model_dump(by_alias=True, mode="json"))
        data = validated.model_dump(by_alias=True, mode="json", exclude_none=True)
        self._write_json_atomic(self._data_file, data)

    def _read_json(self) -> Any:
        try:
            raw = self._data_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise StateValidationError(self._data_file, str(exc)) from exc
        try:
            return resolve_env_templates(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise StateValidationError(
                self._data_file,
                f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
            ) from exc
        except ValueError as exc:
            raise StateValidationError(self._data_file, str(exc)) from exc

    def _validate(self, adapter: TypeAdapter[T], data: Any) -> T:
        try:
            return adapter.validate_python(data)
        except ValidationError as exc:
            first_error = exc.errors()[0]
            location = ".".join(str(part) for part in first_error.get("loc", ())) or "<root>"
            message = first_error.get("msg", "validation failed")
            raise StateValidationError(self._data_file, f"{location}: {message}") from exc

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
