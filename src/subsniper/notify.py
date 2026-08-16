"""Pushover delivery.

Priority tiers are deliberate:
  2 (Emergency) - job ACCEPTED. Bypasses Do Not Disturb and repeats until
      acknowledged, because Nick is now committed to work and needs to know.
  1 (High)      - job MATCHED but not accepted (dry run, cap reached, kill
      switch on). Bypasses quiet hours, doesn't nag.
 -1 (Low)       - errors and heartbeats. Silent; won't wake anyone at 4am.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Config
from .models import Job

log = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class Notifier:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._creds = cfg.credentials
        self._client = httpx.Client(timeout=_TIMEOUT)

    def close(self) -> None:
        self._client.close()

    # -- public API ------------------------------------------------------------
    def job_accepted(self, job: Job, detail: str, dry_run: bool) -> bool:
        prefix = "[DRY RUN] Would accept" if dry_run else "ACCEPTED"
        title = f"{prefix}: {job.title or 'Sub job'}"
        return self._send(
            title=title[:250],
            message=self._body(job, detail),
            priority=int(self.cfg.get("notifications.pushover.accepted_priority", 2)),
            retry=int(self.cfg.get("notifications.pushover.accepted_retry_seconds", 60)),
            expire=int(self.cfg.get("notifications.pushover.accepted_expire_seconds", 600)),
            sound=str(self.cfg.get("notifications.pushover.accepted_sound", "persistent")),
        )

    def job_matched_not_accepted(self, job: Job, why: str) -> bool:
        return self._send(
            title=f"Job matched (not accepted): {job.title or 'Sub job'}"[:250],
            message=f"{self._body(job, '')}\n\nNot accepted: {why}",
            priority=int(self.cfg.get("notifications.pushover.matched_priority", 1)),
            sound=str(self.cfg.get("notifications.pushover.matched_sound", "pushover")),
        )

    def job_seen_nonmatching(self, job: Job, reasons: str) -> bool:
        if not self.cfg.get("notifications.notify_on_nonmatching", False):
            return False
        return self._send(
            title=f"Skipped: {job.title or 'Sub job'}"[:250],
            message=f"{self._body(job, '')}\n\nFiltered out: {reasons}",
            priority=-1,
        )

    def error(self, summary: str) -> bool:
        return self._send(
            title="SubSniper error",
            message=summary[:900],
            priority=int(self.cfg.get("notifications.pushover.error_priority", -1)),
        )

    def heartbeat(self, summary: str) -> bool:
        return self._send(title="SubSniper is running", message=summary[:900], priority=-1)

    # -- internals -------------------------------------------------------------
    @staticmethod
    def _body(job: Job, detail: str) -> str:
        lines = []
        if job.school:
            lines.append(f"School: {job.school}")
        if job.employee:
            lines.append(f"For: {job.employee}")
        if job.start_dt:
            when = job.start_dt.strftime("%a %b %-d")
            if job.end_dt:
                when += f", {job.start_dt.strftime('%-I:%M%p')} - {job.end_dt.strftime('%-I:%M%p')}"
            lines.append(f"When: {when}")
        dur = job.raw.get("_duration_name") or (
            f"{job.duration_minutes:.0f} min" if job.duration_minutes else ""
        )
        if dur:
            lines.append(f"Duration: {dur}")
        if detail:
            lines.append(f"\n{detail}")
        return "\n".join(lines) or job.summary()

    def _send(
        self,
        title: str,
        message: str,
        priority: int,
        retry: int | None = None,
        expire: int | None = None,
        sound: str | None = None,
    ) -> bool:
        payload: dict[str, Any] = {
            "token": self._creds.pushover_api_token,
            "user": self._creds.pushover_user_key,
            "title": title,
            "message": message,
            "priority": priority,
        }
        if self._creds.pushover_device:
            payload["device"] = self._creds.pushover_device
        if sound:
            payload["sound"] = sound
        # Pushover REQUIRES retry/expire for emergency priority
        if priority == 2:
            payload["retry"] = max(30, int(retry or 60))
            payload["expire"] = min(10800, int(expire or 600))

        try:
            resp = self._client.post(PUSHOVER_URL, data=payload)
            if resp.status_code == 200:
                return True
            log.error("Pushover rejected the message (HTTP %s): %s", resp.status_code, resp.text[:300])
            return False
        except httpx.HTTPError as exc:
            log.error("Pushover request failed: %s", exc)
            return False
