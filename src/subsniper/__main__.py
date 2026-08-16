"""CLI entrypoint.

    python -m subsniper run            # start the service
    python -m subsniper run --once     # single poll cycle, then exit
    python -m subsniper check          # validate config + login, change nothing
    python -m subsniper test-notify    # prove Pushover works end to end
    python -m subsniper replay FILE    # run saved HTML/JSON through the filters
    python -m subsniper status         # what has it accepted lately
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .filters import evaluate
from .models import Job
from .parser import parse_jobs
from .poller import Poller
from .state import Store


def _setup_logging(cfg=None, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if cfg is not None:
        app_file = cfg.get("logging.app_file")
        if app_file:
            path = cfg.root / str(app_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(path, encoding="utf-8"))
        level = getattr(logging, str(cfg.get("logging.level", "INFO")).upper(), level)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        handlers=handlers,
        force=True,
    )


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config, args.env)
    _setup_logging(cfg, args.verbose)

    poller = Poller(cfg, once=args.once)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _graceful(*_: object) -> None:
        logging.getLogger(__name__).info("shutdown signal received, stopping after this cycle")
        poller.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _graceful)
        except NotImplementedError:  # Windows
            signal.signal(sig, _graceful)

    try:
        loop.run_until_complete(poller.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Validate config and confirm we can reach an authenticated jobs list."""
    cfg = load_config(args.config, args.env)
    _setup_logging(cfg, args.verbose)
    log = logging.getLogger("check")

    log.info("config OK: %d include / %d exclude role patterns, %d poll windows",
             len(cfg.role_include), len(cfg.role_exclude), len(cfg.poll_windows))
    log.info("dry_run=%s  max/day=%s  kill_switch=%s",
             cfg.dry_run, cfg.get("autoaccept.max_accepts_per_day"),
             cfg.kill_switch_path.name if cfg.kill_switch_path else None)

    from .frontline import FrontlineClient

    async def _probe() -> int:
        client = FrontlineClient(cfg)
        try:
            await client.start()
            result = await client.poll()
            log.info("authenticated OK - %d job(s) currently listed, %dms",
                     len(result.jobs), result.latency_ms)
            for job in result.jobs:
                verdict = evaluate(job, cfg)
                mark = "MATCH " if verdict.matched else "skip  "
                log.info("  %s %s -> %s", mark, job.summary(), verdict.reason_text)
            return 0
        finally:
            await client.close()

    try:
        return asyncio.run(_probe())
    except Exception as exc:  # noqa: BLE001
        log.error("check failed: %s", exc)
        return 1


def cmd_test_notify(args: argparse.Namespace) -> int:
    cfg = load_config(args.config, args.env)
    _setup_logging(cfg, args.verbose)
    from .notify import Notifier

    notifier = Notifier(cfg)
    try:
        ok = notifier.heartbeat(
            "If you're reading this on your phone, Pushover is wired up correctly."
        )
        print("sent" if ok else "FAILED - check PUSHOVER_USER_KEY / PUSHOVER_API_TOKEN")
        return 0 if ok else 1
    finally:
        notifier.close()


def cmd_replay(args: argparse.Namespace) -> int:
    """Run a saved page or job list through the filters. No network, no accepts."""
    cfg = load_config(args.config, args.env, require_credentials=False)
    _setup_logging(cfg, args.verbose)

    raw = Path(args.file).read_text(encoding="utf-8")
    if args.file.endswith(".json"):
        payloads = json.loads(raw)
        jobs = [Job.from_payload(p) for p in payloads]
    else:
        jobs = parse_jobs(raw)

    if not jobs:
        print("no jobs parsed from that file")
        return 1

    matched = 0
    for job in jobs:
        verdict = evaluate(job, cfg)
        matched += verdict.matched
        mark = "MATCH" if verdict.matched else "skip "
        print(f"{mark}  {job.summary()}")
        print(f"        role text: {job.role_text[:90]!r}")
        print(f"        {verdict.reason_text}")
        print()
    print(f"{matched}/{len(jobs)} would match")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config, args.env, require_credentials=False)
    store = Store(cfg.root)
    print(f"dry_run:          {cfg.dry_run}")
    print(f"accepted today:   {store.accepts_today()} / {cfg.get('autoaccept.max_accepts_per_day')}")
    kill = cfg.kill_switch_path
    print(f"kill switch:      {'ACTIVE' if kill and kill.exists() else 'off'}")
    recent = store.accepted_jobs()[-10:]
    print(f"\nlast {len(recent)} accepted:")
    for job in recent:
        print(f"  {job.summary()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="subsniper", description="Frontline sub-job sniper")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--env", default=".env")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="start the polling service")
    p_run.add_argument("--once", action="store_true", help="one cycle then exit")
    p_run.set_defaults(func=cmd_run)

    sub.add_parser("check", help="validate config and auth").set_defaults(func=cmd_check)
    sub.add_parser("test-notify", help="send a test push").set_defaults(func=cmd_test_notify)
    sub.add_parser("status", help="show accept ledger").set_defaults(func=cmd_status)

    p_replay = sub.add_parser("replay", help="run a saved page/json through the filters")
    p_replay.add_argument("file")
    p_replay.set_defaults(func=cmd_replay)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
