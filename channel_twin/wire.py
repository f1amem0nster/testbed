"""Small length-prefixed TCP protocol used between twin gateways."""

from __future__ import annotations

import json
import socket
import struct
from typing import Any, BinaryIO


MAGIC = b"SGT1"
HEADER = struct.Struct("!4sI")
LENGTH = struct.Struct("!I")
MAX_METADATA_BYTES = 64 * 1024


class WireError(RuntimeError):
    """The peer sent an invalid or incomplete frame."""


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise WireError(f"connection closed with {remaining} bytes remaining")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_header(sock: socket.socket, metadata: dict[str, Any]) -> None:
    raw = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_METADATA_BYTES:
        raise WireError("metadata is too large")
    sock.sendall(HEADER.pack(MAGIC, len(raw)))
    sock.sendall(raw)


def receive_header(sock: socket.socket) -> dict[str, Any]:
    magic, length = HEADER.unpack(recv_exact(sock, HEADER.size))
    if magic != MAGIC:
        raise WireError("invalid protocol magic")
    if length <= 0 or length > MAX_METADATA_BYTES:
        raise WireError("invalid metadata length")
    try:
        value = json.loads(recv_exact(sock, length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WireError(f"invalid metadata JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise WireError("metadata must be an object")
    return value


def send_file(sock: socket.socket, stream: BinaryIO, chunk_bytes: int) -> None:
    while chunk := stream.read(chunk_bytes):
        sock.sendall(chunk)


def send_receipt(sock: socket.socket, receipt: dict[str, Any]) -> None:
    raw = json.dumps(receipt, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_METADATA_BYTES:
        raise WireError("receipt is too large")
    sock.sendall(LENGTH.pack(len(raw)))
    sock.sendall(raw)


def receive_receipt(sock: socket.socket) -> dict[str, Any]:
    (length,) = LENGTH.unpack(recv_exact(sock, LENGTH.size))
    if length <= 0 or length > MAX_METADATA_BYTES:
        raise WireError("invalid receipt length")
    try:
        value = json.loads(recv_exact(sock, length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WireError(f"invalid receipt JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise WireError("receipt must be an object")
    return value
