"""Submit, inspect, and download channel-twin artifacts with no third-party packages."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def request(args: argparse.Namespace, method: str, path: str, *, data: bytes | None = None, headers: dict[str, str] | None = None) -> bytes:
    target = args.api.rstrip("/") + path
    request_headers = {"X-Channel-Token": args.token, **(headers or {})}
    req = urllib.request.Request(target, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {target}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach {target}: {exc.reason}") from exc


def submit_file(args: argparse.Namespace) -> bytes:
    target = urlsplit(args.api)
    if target.scheme not in {"http", "https"} or not target.hostname:
        raise RuntimeError("--api must be an HTTP(S) URL")
    connection_type = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(target.hostname, target.port, timeout=args.timeout)
    size = args.file.stat().st_size
    digest = hashlib.sha256()
    with args.file.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    api_prefix = target.path.rstrip("/")
    try:
        connection.putrequest("POST", api_prefix + "/v1/transfers")
        for name, value in {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
            "X-Channel-Token": args.token,
            "X-Transfer-Id": args.transfer_id,
            "X-Run-Id": args.run_id,
            "X-Artifact-Type": args.artifact_type,
            "X-Content-SHA256": digest.hexdigest(),
        }.items():
            connection.putheader(name, value)
        connection.endheaders()
        with args.file.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                connection.send(chunk)
        response = connection.getresponse()
        body = response.read()
        if response.status >= 400:
            raise RuntimeError(f"HTTP {response.status}: {body.decode('utf-8', errors='replace')}")
        return body
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True, help="Twin API base URL, for example http://127.0.0.1:8090")
    parser.add_argument("--token", required=True)
    parser.add_argument("--timeout", type=float, default=60)
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit = subparsers.add_parser("submit")
    submit.add_argument("--id", required=True, dest="transfer_id")
    submit.add_argument("--file", required=True, type=Path)
    submit.add_argument("--run-id", default="manual-static")
    submit.add_argument("--artifact-type", default="application/octet-stream")

    status = subparsers.add_parser("status")
    status.add_argument("--id", required=True, dest="transfer_id")

    deliveries = subparsers.add_parser("deliveries")
    deliveries.add_argument("--id", dest="transfer_id")

    download = subparsers.add_parser("download")
    download.add_argument("--id", required=True, dest="transfer_id")
    download.add_argument("--output", required=True, type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "submit":
            raw = submit_file(args)
        elif args.command == "status":
            raw = request(args, "GET", f"/v1/transfers/{args.transfer_id}")
        elif args.command == "deliveries":
            suffix = f"/{args.transfer_id}" if args.transfer_id else ""
            raw = request(args, "GET", "/v1/deliveries" + suffix)
        else:
            raw = request(args, "GET", f"/v1/deliveries/{args.transfer_id}/content")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(raw)
            print(f"wrote {len(raw)} bytes to {args.output}")
            return 0
        print(json.dumps(json.loads(raw), ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
