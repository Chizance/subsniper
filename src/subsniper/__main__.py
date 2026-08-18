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
from datetime import datetime, timedelta
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


def cmd_version(args: argparse.Namespace) -> int:
    """Show what's installed and whether GitHub has something newer."""
    from . import __version__

    # Deliberately does NOT load config. "What version am I on?" must work even
    # when the install is broken - that's often exactly when you're asking.
    root = Path(args.config).expanduser().resolve().parent
    if not root.exists():
        root = Path.cwd()
    print(f"SubSniper {__version__}")

    stamp = root / "VERSION.txt"
    installed = None
    if stamp.exists():
        line = stamp.read_text(encoding="utf-8").strip().splitlines()[0]
        print(f"installed: {line}")
        installed = line.split()[0]
    else:
        print("installed: unknown (no VERSION.txt - installed before the updater existed)")

    if args.no_check:
        return 0

    try:
        import httpx

        r = httpx.get(
            "https://api.github.com/repos/Chizance/subsniper/commits/main",
            headers={"User-Agent": "SubSniper"},
            timeout=15.0,
        )
        if r.status_code != 200:
            print(f"could not check for updates (HTTP {r.status_code})")
            return 0
        data = r.json()
        latest = data.get("sha", "")
        msg = str(data.get("commit", {}).get("message", "")).splitlines()[0]
        print(f"latest:    {latest[:7]}  {msg}")
        if installed and latest.startswith(installed):
            print("\nUp to date.")
        else:
            print("\nAn update is available. Run update.ps1 to install it.")
    except Exception as exc:  # noqa: BLE001 - never let a version check break anything
        print(f"could not check for updates: {exc}")
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    """Read the audit log and report what actually happened.

    Answers the question a phone can't: was SubSniper even running when the jobs
    were posted, and if it was, what did it decide about each one?
    """
    from collections import Counter

    cfg = load_config(args.config, args.env, require_credentials=False)
    audit = cfg.root / str(cfg.get("logging.audit_file", "logs/audit.jsonl"))
    if not audit.exists():
        print(f"No audit log at {audit}")
        print("SubSniper has never completed a startup in this folder.")
        return 1

    cutoff = datetime.now() - timedelta(days=int(args.days))
    events: list[dict] = []
    with audit.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
                ts = datetime.fromisoformat(rec["ts"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if ts >= cutoff:
                rec["_ts"] = ts
                events.append(rec)

    if not events:
        print(f"No audit entries in the last {args.days} day(s).")
        return 1

    counts = Counter(e.get("event") for e in events)
    starts = [e for e in events if e.get("event") == "service_start"]
    alives = sorted(
        (e["_ts"] for e in events if e.get("event") == "alive")
    )

    print(f"=== SubSniper diagnosis: last {args.days} day(s) ===")
    print(f"log: {audit}")
    print(f"window: {events[0]['_ts']:%Y-%m-%d %H:%M} .. {events[-1]['_ts']:%Y-%m-%d %H:%M}\n")

    print("-- activity ---------------------------------------------------")
    for name in ("service_start", "warm_restart", "primed", "alive", "job_matched",
                 "job_skipped", "accept_attempt", "accept_blocked", "poll_error",
                 "auth_error", "reauth_failed", "refused_second_instance"):
        if counts.get(name):
            print(f"  {name:<24} {counts[name]}")
    print()

    print("-- restarts ---------------------------------------------------")
    if len(starts) <= 1:
        print("  1 start. Good - it stayed up.")
    else:
        print(f"  {len(starts)} starts. Repeated restarts cause missed jobs.")
        for s in starts[-8:]:
            print(f"    {s['_ts']:%m-%d %H:%M:%S}  (start #{s.get('starts_today','?')} that day)")
    print()

    print("-- coverage gaps (when it was NOT watching) -------------------")
    if not alives:
        print("  No proof-of-life entries. Either this is an older version, or")
        print("  it never completed a successful poll.")
    else:
        gaps = []
        for a, b in zip(alives, alives[1:]):
            mins = (b - a).total_seconds() / 60.0
            if mins > 10:
                gaps.append((a, b, mins))
        span_h = (alives[-1] - alives[0]).total_seconds() / 3600.0
        window_h = (events[-1]["_ts"] - events[0]["_ts"]).total_seconds() / 3600.0
        if not gaps:
            print(f"  None found across the {span_h:.1f}h that has proof-of-life data.")
            if window_h - span_h > 1.0:
                print(f"  CAUTION: the log covers {window_h:.1f}h but only {span_h:.1f}h of it")
                print("  has proof-of-life marks (older versions didn't record them), so")
                print("  this says nothing about the rest. Not a clean bill of health.")
        else:
            print(f"  {len(gaps)} gap(s) over 10 minutes:")
            for a, b, mins in gaps[-12:]:
                flag = "  <-- MORNING RUSH" if _overlaps_rush(a, b) else ""
                print(f"    {a:%m-%d %H:%M} -> {b:%m-%d %H:%M}  ({mins:.0f} min){flag}")
    print()

    errors = [e for e in events if e.get("event") in ("poll_error", "auth_error", "reauth_failed")]
    print("-- errors -----------------------------------------------------")
    if not errors:
        print("  None.")
    else:
        ok = len([e for e in events if e.get("event") == "alive"])
        print(f"  {len(errors)} error(s). Polls that succeeded: ~{ok} proof-of-life marks.")
        by_msg: Counter = Counter(str(e.get("error", "?"))[:160] for e in errors)
        print("  most common:")
        for msg, n in by_msg.most_common(5):
            print(f"    {n:>4}x  {msg}")
        print(f"  most recent: {errors[-1]['_ts']:%m-%d %H:%M:%S}  {str(errors[-1].get('error',''))[:200]}")
    print()

    print("-- why jobs were skipped --------------------------------------")
    reasons: Counter = Counter()
    for e in events:
        if e.get("event") == "job_skipped":
            for r in e.get("reasons", []):
                reasons[str(r).split(":")[0]] += 1
    if not reasons:
        print("  No jobs were filtered out.")
    else:
        for reason, n in reasons.most_common():
            print(f"  {n:>4}  {reason}")
    print()

    matched = [e for e in events if e.get("event") == "job_matched"]
    print(f"-- matches ({len(matched)}) ---------------------------------------------")
    for e in matched[-10:]:
        job = e.get("job", {})
        print(f"  {e['_ts']:%m-%d %H:%M}  {job.get('title','?')} @ {job.get('school','?')}")
    if not matched:
        print("  None matched your filters in this window.")

    return 0


def _overlaps_rush(a: datetime, b: datetime, start_hour: int = 5, end_hour: int = 9) -> bool:
    """Did this gap cover any of the morning posting window?"""
    cur = a
    while cur < b:
        if start_hour <= cur.hour < end_hour:
            return True
        cur += timedelta(minutes=15)
    return start_hour <= b.hour < end_hour


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

    p_ver = sub.add_parser("version", help="show version and check for updates")
    p_ver.add_argument("--no-check", action="store_true", help="don't contact GitHub")
    p_ver.set_defaults(func=cmd_version)

    p_diag = sub.add_parser("diagnose", help="read the audit log and report what happened")
    p_diag.add_argument("--days", type=int, default=2, help="how far back to look (default 2)")
    p_diag.set_defaults(func=cmd_diagnose)

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
