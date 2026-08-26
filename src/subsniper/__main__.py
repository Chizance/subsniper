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


def cmd_doctor(args: argparse.Namespace) -> int:
    """Collect everything needed to diagnose a problem into one file.

    Exists because "it doesn't work" is not actionable, and gathering the
    pieces one screenshot at a time has repeatedly cost days. This asks every
    question at once and writes a single report that can be pasted or sent.

    Credential VALUES are never included - only whether each one is set.
    """
    import platform
    from collections import Counter

    out: list[str] = []

    def w(line: str = "") -> None:
        out.append(line)

    def section(name: str) -> None:
        w()
        w("=" * 62)
        w(name)
        w("=" * 62)

    w(f"SubSniper report - generated {datetime.now():%Y-%m-%d %H:%M:%S}")

    # -- 1. what is installed --------------------------------------------------
    section("1. SYSTEM")
    try:
        from . import __version__
        w(f"subsniper version : {__version__}")
    except Exception as exc:  # noqa: BLE001
        w(f"subsniper version : ERROR {exc}")
    w(f"python            : {sys.version.split()[0]}")
    w(f"platform          : {platform.system()} {platform.release()}")
    w(f"working directory : {Path.cwd()}")

    root = Path(args.config).expanduser().resolve().parent
    if not root.exists():
        root = Path.cwd()
    w(f"project directory : {root}")

    stamp = root / "VERSION.txt"
    w(f"installed build   : {stamp.read_text(encoding='utf-8').strip().splitlines()[0] if stamp.exists() else 'unknown (no VERSION.txt)'}")

    for dep in ("playwright", "bs4", "httpx", "yaml", "dotenv"):
        try:
            __import__(dep)
            w(f"  dependency {dep:<12} OK")
        except Exception as exc:  # noqa: BLE001
            w(f"  dependency {dep:<12} MISSING ({exc})")

    # -- 2. is it running ------------------------------------------------------
    section("1b. BROWSER PROFILE LOCATION")
    try:
        from .frontline import browser_profile_dir
        cfg_probe = load_config(args.config, args.env, require_credentials=False)
        prof = browser_profile_dir(cfg_probe)
        w(f"profile dir       : {prof}")
        risky = ("onedrive", "dropbox", "google drive", "icloud", "\\documents\\")
        hit = [r for r in risky if r in str(prof).lower()]
        if hit:
            w()
            w("*** WARNING: this profile is inside a cloud-synced folder ***")
            w(f"  matched: {hit}")
            w("  A browser profile is thousands of files with database locks,")
            w("  written constantly. Sync engines corrupt it and the browser")
            w("  dies mid-run. Set frontline.browser_profile_dir in config.yaml")
            w("  to somewhere outside OneDrive, e.g. C:\\SubSniperProfile")
        else:
            w("not inside a known cloud-synced folder - good")
    except Exception as exc:  # noqa: BLE001
        w(f"could not determine profile dir: {exc}")

    section("2. IS IT RUNNING RIGHT NOW")
    try:
        store = Store(root)
        lock = store.lock_path
        if not lock.exists():
            w("NO - no lock file. SubSniper is not running.")
        else:
            age = datetime.now().timestamp() - lock.stat().st_mtime
            if age < 90:
                w(f"YES - last heartbeat {age:.0f}s ago.")
            else:
                w(f"NO - lock file is stale ({age/60:.0f} minutes old). It stopped or crashed.")
        w(f"starts today      : {store.starts_today()}")
        w(f"accepted today    : {store.accepts_today()}")
        w(f"heartbeat sent    : {store.heartbeat_sent_today()}")
        seen_age = store.seen_age_seconds()
        w(f"job state age     : {'never written' if seen_age is None else f'{seen_age/60:.0f} min'}")
    except Exception as exc:  # noqa: BLE001
        w(f"ERROR reading state: {exc}")

    # -- 3. configuration ------------------------------------------------------
    section("3. CONFIGURATION")
    cfg = None
    try:
        cfg = load_config(args.config, args.env, require_credentials=False)
        w(f"dry_run           : {cfg.dry_run}   (True = alerts only, never accepts)")
        w(f"max accepts/day   : {cfg.get('autoaccept.max_accepts_per_day')}")
        kill = cfg.kill_switch_path
        w(f"kill switch       : {'ACTIVE - accepting is blocked' if kill and kill.exists() else 'off'}")
        w()
        w("FILTERS - a job must pass every one of these:")
        w(f"  starts no earlier than : {cfg.get('filters.time.earliest_start')}")
        w(f"  ends no later than     : {cfg.get('filters.time.latest_end')}")
        w(f"  at least this long     : {cfg.get('filters.time.min_duration_minutes')} minutes")
        w(f"  on these days          : {cfg.get('filters.time.allowed_weekdays')}")
        w(f"  starting at least      : {cfg.get('filters.time.min_lead_time_minutes')} min from now")
        w(f"  title must match       : {[p.pattern for p in cfg.role_include]}")
        w(f"  title must NOT match   : {len(cfg.role_exclude)} exclusion patterns")
        allow = cfg.get('filters.location.allowlist') or []
        deny = cfg.get('filters.location.denylist') or []
        w(f"  only these schools     : {allow if allow else '(any)'}")
        w(f"  never these schools    : {deny if deny else '(none)'}")
        w()
        w("POLL SCHEDULE:")
        for win in cfg.poll_windows:
            w(f"  {win.name:<22} {win.start:%H:%M}-{win.end:%H:%M} every {win.interval_seconds:.0f}s")
        w(f"  {'everything else':<22} every {cfg.get('polling.default_interval_seconds')}s")

        # The silence trap: while tuning, a filtered-out job produces no
        # notification at all, which is indistinguishable from no job being
        # posted. That ambiguity is exactly what makes "it doesn't work"
        # impossible to act on.
        if not cfg.get("notifications.notify_on_nonmatching", False):
            w()
            w("NOTE: notify_on_nonmatching is OFF.")
            w("  Jobs that fail the filters above produce NO notification, so a")
            w("  job that was posted and rejected looks identical to no job at")
            w("  all. If you are trying to work out why nothing is arriving, set")
            w("  notifications.notify_on_nonmatching: true in config.yaml for a")
            w("  few days - you will get a silent alert for every job it saw and")
            w("  skipped, with the reason.")
    except Exception as exc:  # noqa: BLE001
        w(f"ERROR loading config: {exc}")

    # -- 4. credentials present (never values) ---------------------------------
    section("4. CREDENTIALS (values are never shown)")
    import os
    from dotenv import load_dotenv

    load_dotenv(Path(args.env).expanduser(), override=False)
    for key, required in (
        ("FRONTLINE_USERNAME", True), ("FRONTLINE_PASSWORD", True),
        ("FRONTLINE_PIN", False),
        ("PUSHOVER_USER_KEY", True), ("PUSHOVER_API_TOKEN", True),
    ):
        val = (os.getenv(key) or "").strip()
        if val:
            w(f"  {key:<22} set ({len(val)} characters)")
        else:
            w(f"  {key:<22} {'*** MISSING ***' if required else 'blank (optional)'}")

    # -- 5. recent activity ----------------------------------------------------
    section("5. WHAT IT HAS BEEN DOING (last 3 days)")
    audit = root / "logs" / "audit.jsonl"
    if not audit.exists():
        w("No audit log. It has never completed a startup in this folder.")
    else:
        cutoff = datetime.now() - timedelta(days=3)
        events = []
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
            w("No activity in the last 3 days.")
        else:
            counts = Counter(e.get("event") for e in events)
            for name, n in counts.most_common():
                w(f"  {name:<26} {n}")
            w()
            alives = sorted(e["_ts"] for e in events if e.get("event") == "alive")
            gaps = [(a, b, (b - a).total_seconds() / 60)
                    for a, b in zip(alives, alives[1:]) if (b - a).total_seconds() > 600]
            w(f"coverage gaps over 10 min: {len(gaps)}")
            for a, b, mins in gaps[-6:]:
                flag = "  <-- COVERS MORNING RUSH" if _overlaps_rush(a, b) else ""
                w(f"  {a:%m-%d %H:%M} -> {b:%m-%d %H:%M}  ({mins:.0f} min){flag}")
            w()
            reasons: Counter = Counter()
            for e in events:
                if e.get("event") == "job_skipped":
                    for r in e.get("reasons", []):
                        reasons[str(r)[:70]] += 1
            if reasons:
                w("jobs seen but filtered out:")
                for reason, n in reasons.most_common(10):
                    w(f"  {n:>3}x  {reason}")
            else:
                w("jobs seen but filtered out: none")
            w()
            errs = [e for e in events if e.get("event") in ("poll_error", "auth_error")]
            w(f"errors: {len(errs)}")
            seen_err = set()
            for e in reversed(errs):
                key = str(e.get("error", ""))[:120]
                if key in seen_err:
                    continue
                seen_err.add(key)
                w(f"  {e['_ts']:%m-%d %H:%M}  {key}")
                if len(seen_err) >= 5:
                    break
            w()
            w("last 15 events:")
            for e in events[-15:]:
                extra = ""
                if e.get("event") in ("job_matched", "job_skipped", "accept_attempt"):
                    job = e.get("job", {})
                    extra = f"  {job.get('title','?')} @ {job.get('school','?')}"
                w(f"  {e['_ts']:%m-%d %H:%M:%S}  {e.get('event','?')}{extra}")

    # -- 6. live connectivity --------------------------------------------------
    section("6. LIVE TESTS")
    if args.no_network:
        w("skipped (--no-network)")
    elif cfg is None:
        w("skipped - config failed to load")
    else:
        try:
            cfg_full = load_config(args.config, args.env, require_credentials=True)
        except ConfigError as exc:
            cfg_full = None
            w(f"cannot run live tests: {exc}")

        if cfg_full is not None:
            from .notify import Notifier

            notifier = Notifier(cfg_full)
            try:
                ok = notifier.heartbeat("SubSniper doctor: notification test.")
                w(f"Pushover        : {'OK - your phone should have buzzed' if ok else 'FAILED - check the keys in .env'}")
            except Exception as exc:  # noqa: BLE001
                w(f"Pushover        : ERROR {exc}")
            finally:
                notifier.close()

            from .frontline import FrontlineClient

            async def _probe() -> None:
                client = FrontlineClient(cfg_full)
                try:
                    await client.start()
                    res = await client.poll()
                    w(f"Frontline       : OK - logged in, {len(res.jobs)} job(s) listed, {res.latency_ms}ms")
                    for job in res.jobs:
                        verdict = evaluate(job, cfg_full)
                        mark = "WOULD MATCH" if verdict.matched else "would skip "
                        w(f"    {mark}  {job.summary()}")
                        if not verdict.matched:
                            w(f"                 reason: {verdict.reason_text}")
                finally:
                    await client.close()

            try:
                asyncio.run(_probe())
            except Exception as exc:  # noqa: BLE001
                w(f"Frontline       : FAILED - {type(exc).__name__}: {exc}")

    # -- 7. log tail -----------------------------------------------------------
    section("7. LAST 30 LOG LINES")
    applog = root / "logs" / "subsniper.log"
    if applog.exists():
        try:
            lines = applog.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-30:]:
                w("  " + line)
        except OSError as exc:
            w(f"could not read log: {exc}")
    else:
        w("no log file yet")

    report = "\n".join(out)
    print(report)

    dest = root / "subsniper-report.txt"
    try:
        dest.write_text(report, encoding="utf-8")
        print()
        print("=" * 62)
        print(f"Saved to: {dest}")
        print("Send that file to whoever is helping you. It contains no passwords.")
        print("=" * 62)
    except OSError as exc:
        print(f"\nCould not save report: {exc}")
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

    p_doc = sub.add_parser("doctor", help="collect everything into one report file")
    p_doc.add_argument("--no-network", action="store_true",
                       help="skip the live Pushover and Frontline tests")
    p_doc.set_defaults(func=cmd_doctor)

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
