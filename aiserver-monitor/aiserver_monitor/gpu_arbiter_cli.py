"""gantry-gpu — CLI fuer den GPU-Arbiter.

Duenner Wrapper um die ``/api/arbiter/*``-Endpoints des Gantry-Gateways.
Ein Agent, der die GPU exklusiv braucht (3D-Render, A/V-Gen), ruft z. B.::

    python -m aiserver_monitor.gpu_arbiter_cli lease run -- blender -b scene.blend

Der Aufruf blockiert, bis der Job fertig ist; der Exit-Code des CLI ist der
Exit-Code des Jobs. Gateway-URL aus Env ``AISERVER_MONITOR_ARBITER_URL``
(Default ``http://127.0.0.1:30000``) oder per ``--url``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

import httpx

GATEWAY_URL_ENV = "AISERVER_MONITOR_ARBITER_URL"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:30000"
TOKEN_ENV = "AISERVER_MONITOR_ARBITER_TOKEN"


def _gateway_url() -> str:
    return os.environ.get(GATEWAY_URL_ENV, DEFAULT_GATEWAY_URL)


def _token() -> str | None:
    return os.environ.get(TOKEN_ENV) or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gantry-gpu",
        description="Exklusiver GPU-Zugriff ueber den Gantry-GPU-Arbiter.",
    )
    parser.add_argument(
        "--url",
        default=_gateway_url(),
        help="Gateway-URL (Default: Env AISERVER_MONITOR_ARBITER_URL oder %s)"
        % DEFAULT_GATEWAY_URL,
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    sub.add_parser("state", help="Zeigt den Arbiter-Zustand.")

    lease = sub.add_parser("lease", help="Lease-Verwaltung.")
    lease_sub = lease.add_subparsers(dest="lease_action", required=True)

    run = lease_sub.add_parser(
        "run", help="Job exklusiv auf der GPU ausfuehren."
    )
    run.add_argument("--timeout", type=float, dest="timeout_s", default=None,
                     help="Lease-Timeout in Sekunden.")
    run.add_argument("--vram", type=int, dest="vram_budget_mib", default=None,
                     help="VRAM-Budget in MiB.")
    run.add_argument("--cwd", default=None, help="Arbeitsverzeichnis.")
    run.add_argument("command", nargs=argparse.REMAINDER)

    acq = lease_sub.add_parser("acquire", help="Lease anfordern und halten.")
    acq.add_argument("--timeout", type=float, dest="timeout_s", default=None)
    acq.add_argument("--vram", type=int, dest="vram_budget_mib", default=None)

    rel = lease_sub.add_parser("release", help="Lease freigeben.")
    rel.add_argument("lease_id")

    st = lease_sub.add_parser("status", help="Status eines Leases anzeigen.")
    st.add_argument("lease_id")

    return parser


def parse(argv: list[str]) -> argparse.Namespace:
    """Reines argparse-Parsing (kein Netz) — separat testbar."""
    args = build_parser().parse_args(argv)
    command = getattr(args, "command", None)
    if command and command[0] == "--":
        args.command = command[1:]
    return args


def _lease_status(snapshot: dict, lease_id: str) -> str:
    if snapshot.get("active") == lease_id:
        return "active"
    if lease_id in snapshot.get("queue", []):
        return "queued"
    return "unknown"


def _print_json(obj: object) -> None:
    print(json.dumps(obj, indent=2))


def main(argv: Optional[list[str]] = None) -> int:
    args = parse(sys.argv[1:] if argv is None else argv)
    headers = {}
    if (token := _token()):
        headers["X-Arbiter-Token"] = token
    with httpx.Client(base_url=args.url, timeout=None, headers=headers) as client:
        if args.subcommand == "state":
            resp = client.get("/api/arbiter/state")
            resp.raise_for_status()
            _print_json(resp.json())
            return 0

        if args.lease_action == "run":
            if not args.command:
                print(
                    "gantry-gpu: kein Kommando angegeben (nach --).",
                    file=sys.stderr,
                )
                return 2
            body: dict = {"command": args.command}
            if args.cwd is not None:
                body["cwd"] = args.cwd
            if args.timeout_s is not None:
                body["timeout_s"] = args.timeout_s
            if args.vram_budget_mib is not None:
                body["vram_budget_mib"] = args.vram_budget_mib
            resp = client.post("/api/arbiter/run", json=body)
            resp.raise_for_status()
            result = resp.json()
            if result.get("stdout"):
                sys.stdout.write(result["stdout"])
                if not result["stdout"].endswith("\n"):
                    sys.stdout.write("\n")
            if result.get("stderr"):
                sys.stderr.write(result["stderr"])
                if not result["stderr"].endswith("\n"):
                    sys.stderr.write("\n")
            return result.get("exit_code") or 0

        if args.lease_action == "acquire":
            body = {}
            if args.timeout_s is not None:
                body["timeout_s"] = args.timeout_s
            if args.vram_budget_mib is not None:
                body["vram_budget_mib"] = args.vram_budget_mib
            resp = client.post("/api/arbiter/lease", json=body)
            resp.raise_for_status()
            _print_json(resp.json())
            return 0

        if args.lease_action == "release":
            resp = client.post(f"/api/arbiter/lease/{args.lease_id}/release")
            resp.raise_for_status()
            _print_json(resp.json())
            return 0

        if args.lease_action == "status":
            resp = client.get("/api/arbiter/state")
            resp.raise_for_status()
            print(_lease_status(resp.json(), args.lease_id))
            return 0

    return 2  # pragma: no cover - argparse verhindert das


if __name__ == "__main__":
    sys.exit(main())
