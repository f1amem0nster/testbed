"""Durable outbox/inbox for whole-sample retry and idempotent receipt."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from .wire import WireError


TRANSFER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SpoolError(RuntimeError):
    """A transfer cannot be accepted or committed."""


class SpoolStore:
    def __init__(self, root: str | Path, max_artifact_bytes: int, chunk_bytes: int) -> None:
        self.root = Path(root)
        self.outbox = self.root / "outbox"
        self.inbox = self.root / "inbox"
        self.tmp = self.root / "tmp"
        self.max_artifact_bytes = max_artifact_bytes
        self.chunk_bytes = chunk_bytes
        self._lock = threading.RLock()
        for directory in (self.outbox, self.inbox, self.tmp):
            directory.mkdir(parents=True, exist_ok=True)
        self._recover_outbox()

    @staticmethod
    def validate_transfer_id(transfer_id: str) -> str:
        if not TRANSFER_ID.fullmatch(transfer_id):
            raise SpoolError("transfer_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}")
        return transfer_id

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SpoolError(f"cannot read spool metadata {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise SpoolError(f"spool metadata is not an object: {path}")
        return value

    @staticmethod
    def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    def _recover_outbox(self) -> None:
        for directory in self.outbox.iterdir():
            metadata_path = directory / "metadata.json"
            if not directory.is_dir() or not metadata_path.exists():
                continue
            metadata = self._read_json(metadata_path)
            if metadata.get("state") not in {"DELIVERED", "QUEUED"}:
                metadata["state"] = "QUEUED"
                metadata["last_error"] = "recovered_after_process_restart"
                self._write_json_atomic(metadata_path, metadata)

    def accept_outgoing(
        self,
        transfer_id: str,
        stream: BinaryIO,
        length: int,
        metadata: dict[str, Any],
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        self.validate_transfer_id(transfer_id)
        if length < 0 or length > self.max_artifact_bytes:
            raise SpoolError(f"artifact size must be between 0 and {self.max_artifact_bytes} bytes")
        destination = self.outbox / transfer_id
        with self._lock:
            if destination.exists():
                existing = self._read_json(destination / "metadata.json")
                submitted_sha256 = self._hash_exact(stream, length)
                if submitted_sha256 != existing.get("sha256"):
                    raise SpoolError("transfer_id already exists with different content")
                if expected_sha256 and submitted_sha256 != expected_sha256.lower():
                    raise SpoolError("transfer_id already exists with a different sha256")
                return existing

            staging = Path(tempfile.mkdtemp(prefix=f"out-{transfer_id}-", dir=self.tmp))
            try:
                digest, written = self._copy_exact(stream, staging / "payload.bin", length)
                if expected_sha256 and digest != expected_sha256.lower():
                    raise SpoolError("payload sha256 does not match X-Content-SHA256")
                record = {
                    **metadata,
                    "transfer_id": transfer_id,
                    "payload_bytes": written,
                    "sha256": digest,
                    "state": "QUEUED",
                    "attempts": 0,
                    "accepted_at": utc_now(),
                    "last_error": None,
                }
                self._write_json_atomic(staging / "metadata.json", record)
                os.replace(staging, destination)
                return record
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise

    def _copy_exact(self, source: BinaryIO, destination: Path, length: int) -> tuple[str, int]:
        digest = hashlib.sha256()
        remaining = length
        written = 0
        with destination.open("wb") as output:
            while remaining:
                chunk = source.read(min(self.chunk_bytes, remaining))
                if not chunk:
                    raise SpoolError(f"request ended with {remaining} bytes remaining")
                output.write(chunk)
                digest.update(chunk)
                written += len(chunk)
                remaining -= len(chunk)
            output.flush()
            os.fsync(output.fileno())
        return digest.hexdigest(), written

    def _hash_exact(self, source: BinaryIO, length: int) -> str:
        digest = hashlib.sha256()
        remaining = length
        while remaining:
            chunk = source.read(min(self.chunk_bytes, remaining))
            if not chunk:
                raise SpoolError(f"request ended with {remaining} bytes remaining")
            digest.update(chunk)
            remaining -= len(chunk)
        return digest.hexdigest()

    def list_outgoing(self) -> list[dict[str, Any]]:
        with self._lock:
            values = []
            for directory in self.outbox.iterdir():
                if directory.is_dir() and (directory / "metadata.json").exists():
                    values.append(self._read_json(directory / "metadata.json"))
            return sorted(values, key=lambda value: (value.get("accepted_at", ""), value["transfer_id"]))

    def next_pending(self) -> dict[str, Any] | None:
        for record in self.list_outgoing():
            if record.get("state") != "DELIVERED":
                return record
        return None

    def outgoing(self, transfer_id: str) -> dict[str, Any] | None:
        self.validate_transfer_id(transfer_id)
        with self._lock:
            path = self.outbox / transfer_id / "metadata.json"
            return self._read_json(path) if path.exists() else None

    def outgoing_payload(self, transfer_id: str) -> Path:
        self.validate_transfer_id(transfer_id)
        path = self.outbox / transfer_id / "payload.bin"
        if not path.exists():
            raise SpoolError("outgoing payload does not exist")
        return path

    def update_outgoing(self, transfer_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            path = self.outbox / self.validate_transfer_id(transfer_id) / "metadata.json"
            if not path.exists():
                raise SpoolError("outgoing transfer does not exist")
            record = self._read_json(path)
            record.update(changes)
            self._write_json_atomic(path, record)
            return record

    def receive_from_socket(self, sock: socket.socket, metadata: dict[str, Any]) -> dict[str, Any]:
        transfer_id = metadata.get("transfer_id")
        if not isinstance(transfer_id, str):
            raise WireError("transfer_id is required")
        self.validate_transfer_id(transfer_id)
        payload_bytes = metadata.get("payload_bytes")
        sha256 = metadata.get("sha256")
        if not isinstance(payload_bytes, int) or not 0 <= payload_bytes <= self.max_artifact_bytes:
            raise WireError("invalid payload_bytes")
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise WireError("invalid sha256")
        destination = self.inbox / transfer_id
        with self._lock:
            if destination.exists():
                existing = self._read_json(destination / "metadata.json")
                if existing.get("sha256") != sha256:
                    raise WireError("transfer_id conflicts with an existing payload")
                self._discard_exact(sock, payload_bytes)
                return {"ok": True, "transfer_id": transfer_id, "status": "already_present", "sha256": sha256}

            staging = Path(tempfile.mkdtemp(prefix=f"in-{transfer_id}-", dir=self.tmp))
            try:
                digest, received = self._copy_exact(sock.makefile("rb", buffering=0), staging / "payload.bin", payload_bytes)
                if digest != sha256:
                    raise WireError("received payload sha256 mismatch")
                record = {
                    **{key: value for key, value in metadata.items() if key != "auth_token"},
                    "payload_bytes": received,
                    "sha256": digest,
                    "state": "RECEIVED_VERIFIED",
                    "received_at": utc_now(),
                }
                self._write_json_atomic(staging / "metadata.json", record)
                os.replace(staging, destination)
                return {"ok": True, "transfer_id": transfer_id, "status": "received_verified", "sha256": sha256}
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise

    def _discard_exact(self, sock: socket.socket, length: int) -> None:
        remaining = length
        while remaining:
            chunk = sock.recv(min(self.chunk_bytes, remaining))
            if not chunk:
                raise WireError(f"duplicate payload ended with {remaining} bytes remaining")
            remaining -= len(chunk)

    def list_incoming(self) -> list[dict[str, Any]]:
        values = []
        with self._lock:
            for directory in self.inbox.iterdir():
                if directory.is_dir() and (directory / "metadata.json").exists():
                    values.append(self._read_json(directory / "metadata.json"))
        return sorted(values, key=lambda value: (value.get("received_at", ""), value["transfer_id"]))

    def incoming(self, transfer_id: str) -> dict[str, Any] | None:
        self.validate_transfer_id(transfer_id)
        with self._lock:
            path = self.inbox / transfer_id / "metadata.json"
            return self._read_json(path) if path.exists() else None

    def incoming_payload(self, transfer_id: str) -> Path:
        self.validate_transfer_id(transfer_id)
        path = self.inbox / transfer_id / "payload.bin"
        if not path.exists():
            raise SpoolError("incoming payload does not exist")
        return path
