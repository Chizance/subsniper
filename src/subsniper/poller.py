"""The main loop: poll, filter, decide, notify, accept.

The accept decision passes through a fixed gauntlet, in this order:

  1. kill switch present?          -> notify only
  2. arm file required & missing?  -> notify only
  3. dry_run enabled?              -> notify only
  4. already accepted this job?    -> skip silently
  5. per-run cap reached?          -> notify only
  6. per-day cap reached?          -> notify only
  7. overlaps an accepted job?     -> notify only
  8. -> accept

Every branch is written to the audit log with its reason. Nothing accepts
silently, and nothing is skipped silently except an exact duplicate.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import date, datetime
from pathlib import Path

from . import filters
from .config import Config
from .frontline import AuthError, FrontlineClient, TransientError
from .models import Job
from .notify import Notifier
from .state import Store

log = logging.getLogger(__name__)


class Poller:
    def __init__(self, cfg: Config, once: bool = False) -> None:
        self.cfg = cfg
        self.once = once
        self.client = FrontlineClient(cfg)
        self.store = Store(cfg.root)
        self.notifier = Notifier(cfg)
        self._backoff = 0.0
        self._last_heartbeat: date | None = None
        self._stopping = False
        self._polls = 0
        self._last_alive: datetime | None = None

    async def run(self) -> None:
        # Two copies polling at once double the request rate against Frontline and
        # race each other on the state files.
        if self.store.another_instance_running():
            log.error(
                "another SubSniper appears to be running already (see %s). "
                "Refusing to start a second copy. If you're sure nothing else is "
                "running, delete that file and try again.",
                self.store.lock_path,
            )
            self.store.audit("refused_second_instance")
            return

        await self.client.start()
        log.info(
            "SubSniper started | dry_run=%s | max/day=%s",
            self.cfg.dry_run,
            self.cfg.get("autoaccept.max_accepts_per_day"),
        )
        starts_today = self.store.record_service_start()
        self.store.audit("service_start", dry_run=self.cfg.dry_run, starts_today=starts_today)

        # Priming exists so a genuine cold start doesn't fire notifications for a
        # backlog of stale jobs posted hours ago. But it must NOT run on a quick
        # restart: state is current, so anything unseen is genuinely new, and
        # swallowing it silently is exactly how jobs go missing.
        stale_after = float(self.cfg.get("polling.cold_start_after_seconds", 21600))
        age = self.store.seen_age_seconds()
        cold_start = age is None or age > stale_after

        try:
            first = await self.client.poll()
            if cold_start:
                self.store.mark_seen(first.jobs)
                log.info("cold start: priming with %d already-posted job(s)", len(first.jobs))
                self.store.audit("primed", count=len(first.jobs), state_age_seconds=age)
            else:
                log.info(
                    "warm restart (state %.0fs old): NOT priming, %d listed job(s) "
                    "will be evaluated normally",
                    age, len(first.jobs),
                )
                self.store.audit("warm_restart", listed=len(first.jobs), state_age_seconds=age)
        except (AuthError, TransientError) as exc:
            log.warning("priming poll failed: %s", exc)

        # Restarting repeatedly is invisible from the phone but breaks everything.
        # Surface it rather than letting it look healthy.
        if starts_today >= 5:
            log.warning("service has started %d times today - something is restarting it", starts_today)
            self.notifier.error(
                f"SubSniper has restarted {starts_today} times today. It may be "
                "crash-looping or being started twice, which can cause missed jobs. "
                "Check logs/subsniper.log."
            )

        try:
            while not self._stopping:
                await self._tick()
                if self.once:
                    break
                await asyncio.sleep(self._next_interval())
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self.store.audit("service_stop")
        self.store.release_lock()
        self.notifier.close()
        await self.client.close()

    # -- one cycle -------------------------------------------------------------
    async def _tick(self) -> None:
        self._maybe_heartbeat()
        self.store.touch_lock()
        try:
            result = await self.client.poll()
        except AuthError as exc:
            await self._handle_auth_error(exc)
            return
        except TransientError as exc:
            self._bump_backoff()
            log.warning("poll failed (%s), backing off %.0fs", exc, self._backoff)
            self.store.audit("poll_error", error=str(exc), backoff=self._backoff)
            return

        self._backoff = 0.0
        self._polls += 1
        # Periodic proof-of-life in the audit log. Without this there's no way to
        # tell "no jobs were posted" apart from "we weren't running" after the
        # fact - which is the exact ambiguity that made this morning hard to read.
        now_ts = datetime.now()
        if self._last_alive is None or (now_ts - self._last_alive).total_seconds() >= 300:
            self._last_alive = now_ts
            self.store.audit(
                "alive", polls=self._polls, listed=len(result.jobs), latency_ms=result.latency_ms
            )

        new_jobs = [j for j in result.jobs if self.store.is_new(j)]
        if result.jobs:
            log.debug("%d job(s) listed, %d new (%dms)", len(result.jobs), len(new_jobs), result.latency_ms)

        accepted_this_run = 0
        for job in new_jobs:
            accepted_this_run += await self._consider(job, accepted_this_run)

        self.store.mark_seen(result.jobs)

    async def _consider(self, job: Job, accepted_this_run: int) -> int:
        """Evaluate one new job. Returns 1 if it was accepted, else 0."""
        verdict = filters.evaluate(job, self.cfg)

        if not verdict.matched:
            self.store.audit(
                "job_skipped", job=job.to_dict(), reasons=verdict.reasons
            )
            log.info("skip %s -> %s", job.summary(), verdict.reason_text)
            self.notifier.job_seen_nonmatching(job, verdict.reason_text)
            return 0

        log.info("MATCH %s", job.summary())
        self.store.audit("job_matched", job=job.to_dict())

        blocked = self._accept_blocked(job, accepted_this_run)
        if blocked is not None:
            self.store.audit("accept_blocked", job=job.to_dict(), reason=blocked)
            log.info("not accepting %s -> %s", job.job_id, blocked)
            if self.cfg.dry_run:
                self.notifier.job_accepted(job, "Filters matched. Dry run is on, so nothing was accepted.", dry_run=True)
                self.store.record_accept(job, ok=False, detail=blocked, dry_run=True)
            else:
                self.notifier.job_matched_not_accepted(job, blocked)
            return 0

        ok, detail = await self.client.accept(job)
        self.store.record_accept(job, ok=ok, detail=detail, dry_run=False)
        self.store.audit("accept_attempt", job=job.to_dict(), ok=ok, detail=detail)

        if ok:
            log.info("ACCEPTED %s (%s)", job.summary(), detail)
            self.notifier.job_accepted(job, detail, dry_run=False)
            return 1

        log.warning("accept failed for %s: %s", job.job_id, detail)
        self.notifier.job_matched_not_accepted(job, f"Accept attempt failed: {detail}")
        return 0

    def _accept_blocked(self, job: Job, accepted_this_run: int) -> str | None:
        """Return a human-readable reason to NOT accept, or None to proceed."""
        kill = self.cfg.kill_switch_path
        if kill is not None and kill.exists():
            return f"kill switch active ({kill.name} exists)"

        arm = self.cfg.arm_file_path
        if arm is not None and not arm.exists():
            return f"not armed ({arm.name} does not exist)"

        if self.cfg.dry_run:
            return "dry_run is enabled in config.yaml"

        if self.store.already_accepted(job):
            return "this job was already accepted"

        per_run = int(self.cfg.get("autoaccept.max_accepts_per_run", 1))
        if accepted_this_run >= per_run:
            return f"per-run cap reached ({per_run})"

        per_day = int(self.cfg.get("autoaccept.max_accepts_per_day", 1))
        today = self.store.accepts_today()
        if today >= per_day:
            return f"daily cap reached ({today}/{per_day})"

        if self.cfg.get("autoaccept.prevent_overlap", True):
            for existing in self.store.accepted_jobs():
                if job.overlaps(existing):
                    return f"overlaps an already-accepted job ({existing.summary()})"

        return None

    # -- auth / backoff --------------------------------------------------------
    async def _handle_auth_error(self, exc: AuthError) -> None:
        threshold = int(self.cfg.get("polling.reauth_after_auth_failures", 2))
        log.warning("auth error (%s), failures=%d", exc, self.client.auth_failures)
        self.store.audit("auth_error", error=str(exc), failures=self.client.auth_failures)

        if self.client.auth_failures >= threshold:
            try:
                await self.client.ensure_authenticated()
                log.info("re-authenticated successfully")
                self.store.audit("reauth_ok")
                self._backoff = 0.0
                return
            except (AuthError, TransientError) as reauth_exc:
                log.error("re-authentication failed: %s", reauth_exc)
                self.store.audit("reauth_failed", error=str(reauth_exc))
                self.notifier.error(
                    f"Could not log back into Frontline: {reauth_exc}\n\n"
                    "SubSniper is still running but is not seeing jobs."
                )
        self._bump_backoff()

    def _bump_backoff(self) -> None:
        base = float(self.cfg.get("polling.error_backoff_seconds", 30))
        cap = float(self.cfg.get("polling.error_backoff_max_seconds", 900))
        self._backoff = min(cap, base if self._backoff == 0 else self._backoff * 2)

    def _next_interval(self) -> float:
        if self._backoff > 0:
            return self._backoff
        now = datetime.now()
        interval, _name = self.cfg.interval_for(now.weekday(), now.time())
        jitter = float(self.cfg.get("polling.jitter_fraction", 0.25))
        if jitter > 0:
            interval *= 1.0 + random.uniform(-jitter, jitter)
        return max(1.0, interval)

    def _maybe_heartbeat(self) -> None:
        if not self.cfg.get("notifications.daily_heartbeat", True):
            return
        today = date.today()
        if self._last_heartbeat == today:
            return
        # Persisted, not just in-memory. Previously a restart reset this and fired
        # a fresh heartbeat, so a crash-looping service still looked healthy from
        # the phone - duplicate heartbeats were the only symptom.
        if self.store.heartbeat_sent_today():
            self._last_heartbeat = today
            return
        target = str(self.cfg.get("notifications.daily_heartbeat_time", "04:45"))
        try:
            hh, mm = (int(p) for p in target.split(":"))
        except ValueError:
            return
        now = datetime.now()
        if (now.hour, now.minute) >= (hh, mm):
            self._last_heartbeat = today
            self.store.mark_heartbeat_sent()
            mode = "DRY RUN" if self.cfg.dry_run else "ARMED"
            from . import __version__

            self.notifier.heartbeat(
                f"Mode: {mode}\nAccepted today: {self.store.accepts_today()}"
                f"/{self.cfg.get('autoaccept.max_accepts_per_day')}\n"
                f"Restarts today: {self.store.starts_today()}\n"
                f"Version: {__version__}"
            )

    def stop(self) -> None:
        self._stopping = True
