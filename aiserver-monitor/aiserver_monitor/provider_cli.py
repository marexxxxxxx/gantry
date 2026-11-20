"""Gantry provider registry CLI.

`server.sh provider list|show|add|rm|rename|edit` writes and reads
``providers/*.toml`` and never touches the gateway or a running model.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .registry import (
    PROVIDER_KINDS,
    Provider,
    RegistryError,
    default_registry_dir,
    delete_provider,
    dumps_provider,
    load_provider,
    load_registry,
    rename_provider,
    save_provider,
)


def _dir(args: argparse.Namespace) -> Path:
    return Path(args.providers_dir).expanduser() if args.providers_dir else default_registry_dir()


def _settings_from_args(args: argparse.Namespace, base: dict | None = None) -> dict:
    settings = dict(base or {})
    mapping = {
        "context": args.context,
        "max_tokens": args.max_tokens,
        "kv": args.kv,
        "keep_alive": args.keep_alive,
        "parallel": args.parallel,
        "idle_unload_min": args.idle_unload_min,
        "vram_reserve_mib": args.vram_reserve_mib,
    }
    for key, value in mapping.items():
        if value is not None:
            settings[key] = value
    if args.auto_load is not None:
        settings["auto_load"] = args.auto_load
    if args.exclusive_gpu is not None:
        settings["exclusive_gpu"] = args.exclusive_gpu
    return settings


def _cmd_list(args: argparse.Namespace) -> int:
    providers = load_registry(_dir(args))
    if not providers:
        print(f"Keine Provider in {_dir(args)}")
        return 0
    print(f"{'ID':<18} {'NAME':<34} {'KIND':<8} {'PORT':>5} {'CTX':>7} {'AUTO':>5}  MODEL")
    for p in sorted(providers.values(), key=lambda item: item.id):
        auto = "yes" if p.settings.get("auto_load") else "no"
        print(
            f"{p.id:<18} {p.name[:34]:<34} {p.kind:<8} {p.port:>5} "
            f"{p.settings.get('context', 0):>7} {auto:>5}  {p.model}"
        )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    provider = load_provider(_dir(args) / f"{args.id}.toml")
    if args.json:
        print(json.dumps(provider.__dict__ | {"source": str(provider.source or "")}, indent=2, ensure_ascii=False))
    else:
        print(dumps_provider(provider))
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    provider = Provider(
        id=args.id,
        name=args.name,
        kind=args.kind,
        enabled=not args.disabled,
        host=args.host,
        port=args.port,
        model=args.model,
        health_url=args.health_url,
        pidfile=args.pidfile,
        logfile=args.logfile,
        workdir=args.workdir,
        start_cmd=args.start_cmd or [],
        stop_cmd=args.stop_cmd or [],
        settings=settings,
    )
    path = save_provider(provider, _dir(args))
    print(f"Provider {provider.id} gespeichert: {path}")
    return 0


def _cmd_rm(args: argparse.Namespace) -> int:
    path = _dir(args) / f"{args.id}.toml"
    if not path.exists():
        print(f"Provider {args.id} nicht gefunden", file=sys.stderr)
        return 1
    if not args.yes:
        answer = input(f"Provider {args.id} wirklich loeschen? [y/N] ").strip().lower()
        if answer not in ("y", "yes", "j", "ja"):
            print("abgebrochen")
            return 1
    delete_provider(args.id, _dir(args))
    print(f"Provider {args.id} geloescht")
    return 0


def _cmd_rename(args: argparse.Namespace) -> int:
    provider = rename_provider(args.old, args.new, _dir(args))
    print(f"Provider {args.old} -> {provider.id} umbenannt")
    return 0


def _cmd_edit(args: argparse.Namespace) -> int:
    provider = load_provider(_dir(args) / f"{args.id}.toml")
    for field in ("name", "kind", "host", "port", "model", "health_url", "pidfile", "logfile", "workdir"):
        value = getattr(args, field)
        if value is not None:
            setattr(provider, field, value)
    if args.start_cmd is not None:
        provider.start_cmd = args.start_cmd
    if args.stop_cmd is not None:
        provider.stop_cmd = args.stop_cmd
    if args.enabled is not None:
        provider.enabled = args.enabled
    provider.settings = _settings_from_args(args, provider.settings)
    path = save_provider(provider, _dir(args))
    print(f"Provider {provider.id} aktualisiert: {path}")
    return 0


def _cmd_path(args: argparse.Namespace) -> int:
    print(_dir(args))
    return 0


def _add_common(parser: argparse.ArgumentParser, *, editing: bool = False) -> None:
    parser.add_argument("--providers-dir", metavar="DIR", help="Registry-Verzeichnis (Default: <repo>/providers)")
    if editing:
        defaults = {"name": None, "kind": None, "host": None, "port": None,
                    "model": None, "health_url": None, "pidfile": None,
                    "logfile": None, "workdir": None}
    else:
        defaults = {"name": None, "kind": "custom", "host": "127.0.0.1",
                    "port": 0, "model": "", "health_url": "", "pidfile": "",
                    "logfile": "", "workdir": "."}
    parser.add_argument("--name", default=defaults["name"], help="Anzeigename")
    parser.add_argument("--kind", default=defaults["kind"], choices=PROVIDER_KINDS, help="Provider-Typ")
    parser.add_argument("--host", default=defaults["host"], help="Host (Default 127.0.0.1)")
    parser.add_argument("--port", default=defaults["port"], type=int, help="Port")
    parser.add_argument("--model", default=defaults["model"], help="Modell-ID beim Provider")
    parser.add_argument("--health-url", default=defaults["health_url"], help="Health-Endpoint")
    parser.add_argument("--pidfile", default=defaults["pidfile"], help="PID-Datei")
    parser.add_argument("--logfile", default=defaults["logfile"], help="Log-Datei")
    parser.add_argument("--workdir", default=defaults["workdir"], help="Arbeitsverzeichnis")
    parser.add_argument("--start-cmd", default=None, nargs="+", help="Startbefehl (Liste)")
    parser.add_argument("--stop-cmd", default=None, nargs="+", help="Stopbefehl (Liste)")
    parser.add_argument(
        "--auto-load", default=None, action=argparse.BooleanOptionalAction, help="Beim ersten Request starten"
    )
    parser.add_argument(
        "--exclusive-gpu", default=None, action=argparse.BooleanOptionalAction, help="Nur dieser Provider darf die GPU nutzen"
    )
    parser.add_argument(
        "--enabled", default=None, action=argparse.BooleanOptionalAction, help="Provider aktiv"
    )
    parser.add_argument("--context", default=None, type=int, help="Context-Groesse")
    parser.add_argument("--max-tokens", default=None, type=int, help="max_tokens-Deckel")
    parser.add_argument("--kv", default=None, help="KV-Format (int8, k8v4, q4_0, fp16)")
    parser.add_argument("--keep-alive", default=None, type=int, help="keep_alive (Sekunden, -1 = forever)")
    parser.add_argument("--parallel", default=None, type=int, help="Parallele Slots")
    parser.add_argument("--idle-unload-min", default=None, type=int, help="Idle-Unload nach Minuten (0 = nie)")
    parser.add_argument("--vram-reserve-mib", default=None, type=int, help="VRAM-Reserve in MiB")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="server.sh provider", description="Gantry-Provider-Registry verwalten")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="Provider auflisten")
    p.add_argument("--providers-dir")
    p.set_defaults(func=_cmd_list)

    p = sub.add_parser("show", help="Provider anzeigen")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.add_argument("--providers-dir")
    p.set_defaults(func=_cmd_show)

    p = sub.add_parser("add", help="Provider anlegen")
    p.add_argument("id")
    _add_common(p)
    p.add_argument("--disabled", action="store_true", help="Provider deaktiviert anlegen")
    p.set_defaults(func=_cmd_add)

    p = sub.add_parser("rm", help="Provider loeschen")
    p.add_argument("id")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--providers-dir")
    p.set_defaults(func=_cmd_rm)

    p = sub.add_parser("rename", help="Provider umbenennen")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--providers-dir")
    p.set_defaults(func=_cmd_rename)

    p = sub.add_parser("edit", help="Provider-Einstellungen aendern")
    p.add_argument("id")
    _add_common(p, editing=True)
    p.set_defaults(func=_cmd_edit)

    p = sub.add_parser("path", help="Registry-Verzeichnis anzeigen")
    p.add_argument("--providers-dir")
    p.set_defaults(func=_cmd_path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "add" and not args.name:
        parser.error("--name ist fuer 'add' erforderlich")
    try:
        return args.func(args)
    except RegistryError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
