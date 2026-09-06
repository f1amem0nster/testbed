"""Dependency-free static channel-twin gateway.

The application API and the peer TCP data path are deliberately separate.  OVS/tc
is attached to the configured data addresses by the Linux deployment scripts.
"""

from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import signal
import socket
import socketserver
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .telemetry import JsonlEventLogger

from .config import TwinConfigError, load_twin_config
from .scenario import load_gateway_scenario
from .spool import SpoolError, SpoolStore, utc_now
from .trace import sha256_file
from .wire import WireError, receive_header, receive_receipt, send_file, send_header, send_receipt


class _ReusableThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], runtime: "TwinNode") -> None:
        self.runtime = runtime
        super().__init__(address, _PeerHandler)


class _PeerHandler(socketserver.BaseRequestHandler):
    server: _ReusableThreadingTCPServer

    def handle(self) -> None:
        runtime = self.server.runtime
        transfer_id: str | None = None
        try:
            self.request.settimeout(float(runtime.config["attempt_timeout_seconds"]))
            metadata = receive_header(self.request)
            transfer_id_value = metadata.get("transfer_id")
            transfer_id = transfer_id_value if isinstance(transfer_id_value, str) else None
            supplied = metadata.get("auth_token")
            expected = runtime.config["auth_token"]
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
                raise WireError("peer authentication failed")
            if metadata.get("destination_node") != runtime.config["node_id"]:
                raise WireError("destination_node does not match this gateway")
            if metadata.get("scenario_id") != runtime.config["scenario_id"]:
                raise WireError("peer scenario_id does not match this gateway")
            if metadata.get("scenario_sha256") != runtime.scenario_sha256:
                raise WireError("peer scenario_sha256 does not match this gateway")
            operation = metadata.get("operation", "PUT")
            if operation == "QUERY":
                existing = runtime.store.incoming(transfer_id or "")
                if existing is not None and existing.get("sha256") != metadata.get("sha256"):
                    raise WireError("transfer_id conflicts with an existing payload")
                receipt = {
                    "ok": True,
                    "transfer_id": transfer_id,
                    "status": "already_present" if existing is not None else "not_found",
                    "sha256": metadata.get("sha256"),
                }
                send_receipt(self.request, receipt)
                return
            if operation != "PUT":
                raise WireError("unsupported peer operation")
            receipt = runtime.store.receive_from_socket(self.request, metadata)
            receipt["receiver_node"] = runtime.config["node_id"]
            receipt["received_at"] = utc_now()
            runtime.events.emit("transfer_received_verified", **receipt)
            send_receipt(self.request, receipt)
        except Exception as exc:
            runtime.events.emit("peer_receive_failed", transfer_id=transfer_id, detail=str(exc))
            try:
                send_receipt(
                    self.request,
                    {"ok": False, "transfer_id": transfer_id, "error": str(exc), "receiver_node": runtime.config["node_id"]},
                )
            except OSError:
                pass


class _ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], runtime: "TwinNode") -> None:
        self.runtime = runtime
        super().__init__(address, _ApiHandler)


class _ApiHandler(BaseHTTPRequestHandler):
    server: _ApiServer
    server_version = "SatelliteGroundTwin/0.1"
    protocol_version = "HTTP/1.1"

    @property
    def runtime(self) -> "TwinNode":
        return self.server.runtime

    def log_message(self, format: str, *args: Any) -> None:
        self.runtime.events.emit(
            "api_access",
            client_ip=self.client_address[0],
            method=self.command,
            path=self.path,
            message=format % args,
        )

    def _authorized(self) -> bool:
        expected = self.runtime.config["auth_token"]
        supplied = self.headers.get("X-Channel-Token", "")
        return not expected or hmac.compare_digest(supplied, expected)

    def _json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"ok": False, "error": {"code": code, "message": message}})

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._error(HTTPStatus.UNAUTHORIZED, "unauthorized", "missing or invalid X-Channel-Token")
        return False

    @staticmethod
    def _parts(path: str) -> list[str]:
        return [unquote(part) for part in path.split("/") if part]

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/health":
            pending = sum(record.get("state") != "DELIVERED" for record in self.runtime.store.list_outgoing())
            self._json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "status": "ready",
                    "node_id": self.runtime.config["node_id"],
                    "role": self.runtime.config["role"],
                    "scenario_id": self.runtime.config["scenario_id"],
                    "scenario_sha256": self.runtime.scenario_sha256,
                    "pending_transfers": pending,
                },
            )
            return
        if not self._require_auth():
            return
        parts = self._parts(path)
        try:
            if parts == ["v1", "system", "info"]:
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "data": {
                            "node_id": self.runtime.config["node_id"],
                            "role": self.runtime.config["role"],
                            "scenario_id": self.runtime.config["scenario_id"],
                            "scenario_sha256": self.runtime.scenario_sha256,
                            "api_address": list(self.runtime.api_address),
                            "data_address": list(self.runtime.data_address),
                            "peer_data_address": [
                                self.runtime.config["peer_data_host"],
                                self.runtime.config["peer_data_port"],
                            ],
                            "spool_dir": self.runtime.config["spool_dir"],
                        },
                    },
                )
            elif parts == ["v1", "transfers"]:
                self._json(HTTPStatus.OK, {"ok": True, "data": self.runtime.store.list_outgoing()})
            elif len(parts) == 3 and parts[:2] == ["v1", "transfers"]:
                record = self.runtime.store.outgoing(parts[2])
                if record is None:
                    self._error(HTTPStatus.NOT_FOUND, "not_found", "transfer does not exist")
                else:
                    self._json(HTTPStatus.OK, {"ok": True, "data": record})
            elif parts == ["v1", "deliveries"]:
                self._json(HTTPStatus.OK, {"ok": True, "data": self.runtime.store.list_incoming()})
            elif len(parts) == 3 and parts[:2] == ["v1", "deliveries"]:
                record = self.runtime.store.incoming(parts[2])
                if record is None:
                    self._error(HTTPStatus.NOT_FOUND, "not_found", "delivery does not exist")
                else:
                    self._json(HTTPStatus.OK, {"ok": True, "data": record})
            elif len(parts) == 4 and parts[:2] == ["v1", "deliveries"] and parts[3] == "content":
                self._send_file(self.runtime.store.incoming_payload(parts[2]))
            else:
                self._error(HTTPStatus.NOT_FOUND, "not_found", f"no route for GET {path}")
        except SpoolError as exc:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_transfer", str(exc))

    def _send_file(self, path: Path) -> None:
        length = path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        with path.open("rb") as stream:
            while chunk := stream.read(int(self.runtime.config["io_chunk_bytes"])):
                self.wfile.write(chunk)
        self.close_connection = True

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path != "/v1/transfers":
            self._error(HTTPStatus.NOT_FOUND, "not_found", f"no route for POST {path}")
            return
        if not self._require_auth():
            return
        transfer_id = self.headers.get("X-Transfer-Id", "")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._error(HTTPStatus.LENGTH_REQUIRED, "length_required", "Content-Length is required")
            return
        try:
            length = int(raw_length)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_length", "Content-Length must be an integer")
            return
        expected_sha256 = self.headers.get("X-Content-SHA256", "").lower()
        if len(expected_sha256) != 64 or any(character not in "0123456789abcdef" for character in expected_sha256):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_sha256", "X-Content-SHA256 must be 64 hexadecimal characters")
            return
        metadata = {
            "run_id": self.headers.get("X-Run-Id", "static-local"),
            "artifact_type": self.headers.get("X-Artifact-Type", "application/octet-stream"),
            "source_node": self.runtime.config["node_id"],
            "destination_node": self.runtime.config["peer_node_id"],
            "direction": "UPLINK" if self.runtime.config["role"] == "ground" else "DOWNLINK",
            "scenario_id": self.runtime.config["scenario_id"],
            "scenario_sha256": self.runtime.scenario_sha256,
        }
        try:
            record = self.runtime.store.accept_outgoing(
                transfer_id,
                self.rfile,
                length,
                metadata,
                expected_sha256,
            )
            self.runtime.events.emit(
                "transfer_accepted",
                transfer_id=transfer_id,
                payload_bytes=record["payload_bytes"],
                sha256=record["sha256"],
            )
            self.runtime.wake_sender()
            self._json(HTTPStatus.ACCEPTED, {"ok": True, "data": record})
        except SpoolError as exc:
            self._error(HTTPStatus.CONFLICT if "already exists" in str(exc) else HTTPStatus.BAD_REQUEST, "spool_error", str(exc))
        except OSError as exc:
            self._error(HTTPStatus.INSUFFICIENT_STORAGE, "storage_error", str(exc))


class TwinNode:
    """One endpoint of the two-node channel twin."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.scenario = load_gateway_scenario(config["scenario_file"]) if config.get("scenario_file") else None
        self.scenario_sha256 = sha256_file(config["scenario_file"]) if config.get("scenario_file") else None
        if self.scenario is not None and self.scenario["scenario_id"] != config["scenario_id"]:
            raise TwinConfigError("scenario_id does not match scenario_file")
        self.store = SpoolStore(
            config["spool_dir"],
            int(config["max_artifact_bytes"]),
            int(config["io_chunk_bytes"]),
        )
        log_path = Path(config["spool_dir"]) / "events.jsonl"
        self.events = JsonlEventLogger(log_path, config["node_id"], config["role"])
        self._stop = threading.Event()
        self._sender_wakeup = threading.Event()
        self._threads: list[threading.Thread] = []
        self._close_lock = threading.Lock()
        self._closed = False
        self.data_server = _ReusableThreadingTCPServer(
            (config["data_listen_host"], int(config["data_listen_port"])), self
        )
        self.api_server = _ApiServer(
            (config["api_listen_host"], int(config["api_listen_port"])), self
        )

    @property
    def api_address(self) -> tuple[str, int]:
        host, port = self.api_server.server_address[:2]
        return str(host), int(port)

    @property
    def data_address(self) -> tuple[str, int]:
        host, port = self.data_server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        self.events.emit(
            "twin_started",
            scenario_id=self.config["scenario_id"],
            api_address=list(self.api_address),
            data_address=list(self.data_address),
        )
        for target, name in (
            (self.data_server.serve_forever, "peer-receiver"),
            (self.api_server.serve_forever, "application-api"),
            (self._sender_loop, "serial-sender"),
        ):
            thread = threading.Thread(target=target, name=f"{self.config['role']}-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
        self._sender_wakeup.set()
        self.api_server.shutdown()
        self.data_server.shutdown()
        self.api_server.server_close()
        self.data_server.server_close()
        for thread in self._threads:
            thread.join(timeout=3)
        self.events.emit("twin_stopped")

    def wake_sender(self) -> None:
        self._sender_wakeup.set()

    def _sender_loop(self) -> None:
        while not self._stop.is_set():
            record = self.store.next_pending()
            if record is None:
                self._sender_wakeup.clear()
                self._sender_wakeup.wait(timeout=1.0)
                continue
            self._send_once(record)
            if self.store.outgoing(record["transfer_id"])["state"] != "DELIVERED":  # type: ignore[index]
                self._sender_wakeup.clear()
                self._sender_wakeup.wait(timeout=float(self.config["retry_interval_seconds"]))

    def _send_once(self, record: dict[str, Any]) -> None:
        transfer_id = record["transfer_id"]
        attempts = int(record.get("attempts", 0)) + 1
        self.store.update_outgoing(
            transfer_id,
            state="SENDING",
            attempts=attempts,
            attempt_started_at=utc_now(),
            last_error=None,
        )
        wire_metadata = {
            key: record[key]
            for key in (
                "run_id",
                "transfer_id",
                "artifact_type",
                "source_node",
                "destination_node",
                "direction",
                "scenario_id",
                "scenario_sha256",
                "payload_bytes",
                "sha256",
            )
        }
        wire_metadata["auth_token"] = self.config["auth_token"]
        try:
            query_metadata = {**wire_metadata, "operation": "QUERY"}
            with socket.create_connection(
                (self.config["peer_data_host"], int(self.config["peer_data_port"])),
                timeout=float(self.config["attempt_timeout_seconds"]),
            ) as connection:
                connection.settimeout(float(self.config["attempt_timeout_seconds"]))
                send_header(connection, query_metadata)
                query_receipt = receive_receipt(connection)
            if not query_receipt.get("ok") or query_receipt.get("sha256") != record["sha256"]:
                raise WireError(f"peer rejected idempotency query: {query_receipt}")
            if query_receipt.get("status") == "already_present":
                updated = self.store.update_outgoing(
                    transfer_id,
                    state="DELIVERED",
                    delivered_at=utc_now(),
                    receiver_status="already_present",
                    last_error=None,
                )
                self.events.emit(
                    "transfer_delivered",
                    transfer_id=transfer_id,
                    attempts=updated["attempts"],
                    payload_bytes=updated["payload_bytes"],
                    sha256=updated["sha256"],
                    recovered_by_query=True,
                )
                return
            if query_receipt.get("status") != "not_found":
                raise WireError(f"unexpected idempotency query status: {query_receipt}")
            wire_metadata["operation"] = "PUT"
            with socket.create_connection(
                (self.config["peer_data_host"], int(self.config["peer_data_port"])),
                timeout=float(self.config["attempt_timeout_seconds"]),
            ) as connection:
                connection.settimeout(float(self.config["attempt_timeout_seconds"]))
                send_header(connection, wire_metadata)
                with self.store.outgoing_payload(transfer_id).open("rb") as payload:
                    send_file(connection, payload, int(self.config["io_chunk_bytes"]))
                receipt = receive_receipt(connection)
            if not receipt.get("ok") or receipt.get("sha256") != record["sha256"]:
                raise WireError(f"peer rejected transfer: {receipt}")
            updated = self.store.update_outgoing(
                transfer_id,
                state="DELIVERED",
                delivered_at=utc_now(),
                receiver_status=receipt.get("status"),
                last_error=None,
            )
            self.events.emit(
                "transfer_delivered",
                transfer_id=transfer_id,
                attempts=updated["attempts"],
                payload_bytes=updated["payload_bytes"],
                sha256=updated["sha256"],
            )
        except Exception as exc:
            self.store.update_outgoing(
                transfer_id,
                state="RETRY_WAIT",
                last_error=str(exc),
                attempt_failed_at=utc_now(),
            )
            self.events.emit("transfer_retry_wait", transfer_id=transfer_id, attempts=attempts, detail=str(exc))


def run(config: dict[str, Any]) -> None:
    node = TwinNode(config)
    node.start()
    print(
        f"{config['role'].capitalize()} channel twin API http://{node.api_address[0]}:{node.api_address[1]} "
        f"data tcp://{node.data_address[0]}:{node.data_address[1]}",
        flush=True,
    )

    def request_stop(signum: int, _frame: Any) -> None:
        print(f"Channel twin received signal {signum}; shutting down", flush=True)
        node._stop.set()

    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, request_stop)
    try:
        while not node._stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        node.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one satellite-ground channel-twin endpoint")
    parser.add_argument("--config", required=True, help="Path to the twin node JSON config")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run(load_twin_config(args.config))
    except TwinConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    return 0
