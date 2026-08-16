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

    async def run(self) -> None:
        await self.client.start()
        log.info(
            "SubSniper started | dry_run=%s | max/day=%s",
            self.cfg.dry_run,
            self.cfg.get("autoaccept.max_accepts_per_day"),
        )
        self.store.audit("service_start", dry_run=self.cfg.dry_run)

        # On the very first poll, treat everything already posted as "seen" so a
        # cold start doesn't fire notifications (or accepts) for a backlog of
        # stale jobs that have been sitting there for hours.
        try:
            first = await self.client.poll()
            self.store.mark_seen(first.jobs)
            log.info("primed with %d already-posted job(s)", len(first.jobs))
            self.store.audit("primed", count=len(first.jobs))
        except (AuthError, TransientError) as exc:
            log.warning("priming poll failed: %s", exc)

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
        self.notifier.close()
        await self.client.close()

    # -- one cycle -------------------------------------------------------------
    async def _tick(self) -> None:
        self._maybe_heartbeat()
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
        target = str(self.cfg.get("notifications.daily_heartbeat_time", "04:45"))
        try:
            hh, mm = (int(p) for p in target.split(":"))
        except ValueError:
            return
        now = datetime.now()
        if (now.hour, now.minute) >= (hh, mm):
            self._last_heartbeat = today
            mode = "DRY RUN" if self.cfg.dry_run else "ARMED"
            self.notifier.heartbeat(
                f"Mode: {mode}\nAccepted today: {self.store.accepts_today()}"
                f"/{self.cfg.get('autoaccept.max_accepts_per_day')}"
            )

    def stop(self) -> None:
        self._stopping = True
