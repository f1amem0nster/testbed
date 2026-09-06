from __future__ import annotations

import hashlib
import io
import json
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from channel_twin.app import TwinNode
from channel_twin.config import load_twin_config
from channel_twin.scenario import load_static_scenario, receiver_profile
from channel_twin.spool import SpoolStore


class ChannelTwinIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.token = "channel-integration-token"
        common = {
            "api_listen_host": "127.0.0.1",
            "api_listen_port": 0,
            "data_listen_host": "127.0.0.1",
            "data_listen_port": 0,
            "peer_data_host": "127.0.0.1",
            "peer_data_port": 1,
            "auth_token": self.token,
            "scenario_id": "spaceverse-compatible-static",
            "max_artifact_bytes": 4 * 1024 * 1024,
            "attempt_timeout_seconds": 0.5,
            "retry_interval_seconds": 0.1,
            "io_chunk_bytes": 4096,
        }
        self.ground = TwinNode(
            {
                **common,
                "node_id": "ground-test",
                "peer_node_id": "satellite-test",
                "role": "ground",
                "spool_dir": str(root / "ground"),
            }
        )
        self.satellite = TwinNode(
            {
                **common,
                "node_id": "satellite-test",
                "peer_node_id": "ground-test",
                "role": "satellite",
                "spool_dir": str(root / "satellite"),
            }
        )
        self.ground.config["peer_data_port"] = self.satellite.data_address[1]
        self.satellite.config["peer_data_port"] = self.ground.data_address[1]
        self.ground.start()
        self.satellite.start()

    def tearDown(self) -> None:
        self.ground.stop()
        self.satellite.stop()
        self.tempdir.cleanup()

    def _api(self, node: TwinNode, method: str, path: str, data: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        all_headers = {"X-Channel-Token": self.token, **(headers or {})}
        request = urllib.request.Request(
            f"http://127.0.0.1:{node.api_address[1]}{path}",
            data=data,
            headers=all_headers,
            method=method,
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, response.read()

    def _wait_for_state(self, node: TwinNode, transfer_id: str, state: str, timeout: float = 5) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = node.store.outgoing(transfer_id)
            if record and record.get("state") == state:
                return record
            time.sleep(0.02)
        self.fail(f"{transfer_id} did not reach {state}: {node.store.outgoing(transfer_id)}")

    def test_bidirectional_real_bytes_are_verified_and_downloadable(self) -> None:
        cases = [
            (self.ground, self.satellite, "uplink-1", b"ground-to-satellite\x00" * 1000),
            (self.satellite, self.ground, "downlink-1", b"satellite-to-ground\xff" * 1000),
        ]
        for sender, receiver, transfer_id, payload in cases:
            status, raw = self._api(
                sender,
                "POST",
                "/v1/transfers",
                payload,
                {
                    "Content-Type": "application/octet-stream",
                    "X-Transfer-Id": transfer_id,
                    "X-Run-Id": "integration-static",
                    "X-Artifact-Type": "test-bytes",
                    "X-Content-Sha256": hashlib.sha256(payload).hexdigest(),
                },
            )
            self.assertEqual(202, status)
            self.assertEqual("QUEUED", json.loads(raw)["data"]["state"])
            delivered = self._wait_for_state(sender, transfer_id, "DELIVERED")
            self.assertEqual(hashlib.sha256(payload).hexdigest(), delivered["sha256"])
            download_status, downloaded = self._api(receiver, "GET", f"/v1/deliveries/{transfer_id}/content")
            self.assertEqual(200, download_status)
            self.assertEqual(payload, downloaded)

    def test_unreachable_peer_queues_and_retries_without_terminal_failure(self) -> None:
        unavailable_port = self.satellite.config["peer_data_port"]
        self.satellite.config["peer_data_port"] = 1
        payload = b"wait-for-contact" * 300
        status, _ = self._api(
            self.satellite,
            "POST",
            "/v1/transfers",
            payload,
            {"X-Transfer-Id": "retry-1", "X-Content-Sha256": hashlib.sha256(payload).hexdigest()},
        )
        self.assertEqual(202, status)
        retrying = self._wait_for_state(self.satellite, "retry-1", "RETRY_WAIT")
        self.assertGreaterEqual(int(retrying["attempts"]), 1)
        self.satellite.config["peer_data_port"] = unavailable_port
        self.satellite.wake_sender()
        delivered = self._wait_for_state(self.satellite, "retry-1", "DELIVERED")
        self.assertGreaterEqual(int(delivered["attempts"]), 2)
        self.assertIsNotNone(self.ground.store.incoming("retry-1"))

    def test_duplicate_transfer_id_is_idempotent_for_same_hash(self) -> None:
        payload = b"same-transfer"
        headers = {"X-Transfer-Id": "idempotent-1", "X-Content-Sha256": hashlib.sha256(payload).hexdigest()}
        first, _ = self._api(self.ground, "POST", "/v1/transfers", payload, headers)
        second, raw = self._api(self.ground, "POST", "/v1/transfers", payload, headers)
        self.assertEqual(202, first)
        self.assertEqual(202, second)
        self.assertEqual("idempotent-1", json.loads(raw)["data"]["transfer_id"])


class StaticScenarioTests(unittest.TestCase):
    def test_checked_in_configs_reference_the_valid_static_scenario(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for role in ("ground", "satellite"):
            config = load_twin_config(root / "configs" / "channel" / f"{role}.local.json")
            scenario = load_static_scenario(config["scenario_file"])
            profile = receiver_profile(scenario, role)
            self.assertEqual(110_670_000, profile["rate_bps"])
            self.assertEqual("downlink" if role == "ground" else "uplink", profile["id"])

    def test_interrupted_outbox_is_recovered_as_queued_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = SpoolStore(directory, max_artifact_bytes=1024, chunk_bytes=64)
            first.accept_outgoing(
                "recover-1",
                io.BytesIO(b"durable"),
                7,
                {"source_node": "a", "destination_node": "b"},
            )
            first.update_outgoing("recover-1", state="SENDING")
            recovered = SpoolStore(directory, max_artifact_bytes=1024, chunk_bytes=64)
            self.assertEqual("QUEUED", recovered.outgoing("recover-1")["state"])
            self.assertEqual("recovered_after_process_restart", recovered.outgoing("recover-1")["last_error"])


if __name__ == "__main__":
    unittest.main()
