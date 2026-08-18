"""Persistent state: which jobs we've seen, what we've accepted, and the ledger.

Everything here survives restarts. That matters most for the accept ledger:
if the service crashes and restarts mid-morning, the daily accept cap must
still hold, otherwise a crash loop could accept a job on every boot.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .models import Job

log = logging.getLogger(__name__)


def _atomic_write(path: Path, payload: str) -> None:
    """Write via temp file + replace so a crash can't leave truncated JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.state_dir = root / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.seen_path = self.state_dir / "seen_jobs.json"
        self.accepts_path = self.state_dir / "accepts.json"
        self.audit_path = root / "logs" / "audit.jsonl"
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)

        self.meta_path = self.state_dir / "meta.json"
        self.lock_path = self.state_dir / "running.lock"

        self._seen: dict[str, str] = self._load(self.seen_path, {})
        self._accepts: list[dict[str, Any]] = self._load(self.accepts_path, [])
        self._meta: dict[str, Any] = self._load(self.meta_path, {})

    # -- meta (heartbeat, restart tracking) ------------------------------------
    def _save_meta(self) -> None:
        _atomic_write(self.meta_path, json.dumps(self._meta, indent=2))

    def seen_age_seconds(self) -> float | None:
        """How long since the seen-set was last written. None if never.

        This is what distinguishes a genuine cold start (nothing known, prime to
        avoid spamming a backlog) from a restart moments after the last poll
        (state is current, so anything unseen is genuinely new and must NOT be
        silently swallowed).
        """
        if not self.seen_path.exists():
            return None
        try:
            return max(0.0, datetime.now().timestamp() - self.seen_path.stat().st_mtime)
        except OSError:
            return None

    def heartbeat_sent_today(self) -> bool:
        return self._meta.get("last_heartbeat_date") == date.today().isoformat()

    def mark_heartbeat_sent(self) -> None:
        self._meta["last_heartbeat_date"] = date.today().isoformat()
        self._save_meta()

    def record_service_start(self) -> int:
        """Log this start and return how many times we've started today.

        A high count means the process is crash-looping, which is invisible from
        the phone but devastating: each restart used to swallow every job posted
        since the last one.
        """
        today = date.today().isoformat()
        starts = self._meta.get("starts", {})
        if not isinstance(starts, dict):
            starts = {}
        starts[today] = int(starts.get(today, 0)) + 1
        # Keep only the last 14 days
        for key in sorted(starts)[:-14]:
            starts.pop(key, None)
        self._meta["starts"] = starts
        self._meta["last_start"] = datetime.now().isoformat(timespec="seconds")
        self._save_meta()
        return starts[today]

    def starts_today(self) -> int:
        starts = self._meta.get("starts", {})
        return int(starts.get(date.today().isoformat(), 0)) if isinstance(starts, dict) else 0

    # -- single-instance lock --------------------------------------------------
    def another_instance_running(self, stale_seconds: float = 90.0) -> bool:
        """True if a second copy is already polling.

        Two instances race on the same state files and double-poll Frontline,
        which doubles the request rate and the odds of being flagged.
        """
        if not self.lock_path.exists():
            return False
        try:
            age = datetime.now().timestamp() - self.lock_path.stat().st_mtime
        except OSError:
            return False
        return age < stale_seconds

    def touch_lock(self) -> None:
        """Refresh the liveness lock. Called every poll cycle."""
        try:
            self.lock_path.write_text(
                datetime.now().isoformat(timespec="seconds"), encoding="utf-8"
            )
        except OSError:
            pass

    def release_lock(self) -> None:
        self.lock_path.unlink(missing_ok=True)

    @staticmethod
    def _load(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("could not read %s (%s), starting fresh", path.name, exc)
            return default

    # -- seen-job tracking -----------------------------------------------------
    def is_new(self, job: Job) -> bool:
        return job.job_id not in self._seen

    def mark_seen(self, jobs: Iterable[Job]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        changed = False
        for job in jobs:
            if job.job_id not in self._seen:
                self._seen[job.job_id] = now
                changed = True
        if changed:
            self._prune_seen()
            _atomic_write(self.seen_path, json.dumps(self._seen, indent=2))

    def _prune_seen(self, keep: int = 5000) -> None:
        """Cap the seen-set so it can't grow without bound over a school year."""
        if len(self._seen) <= keep:
            return
        ordered = sorted(self._seen.items(), key=lambda kv: kv[1], reverse=True)
        self._seen = dict(ordered[:keep])

    # -- accept ledger ---------------------------------------------------------
    def accepts_today(self) -> int:
        today = date.today().isoformat()
        return sum(
            1
            for a in self._accepts
            if a.get("ok") and str(a.get("at", "")).startswith(today)
        )

    def accepted_jobs(self) -> list[Job]:
        out = []
        for entry in self._accepts:
            if not entry.get("ok"):
                continue
            payload = entry.get("job_payload")
            if isinstance(payload, dict):
                try:
                    out.append(Job.from_payload(payload))
                except Exception:  # noqa: BLE001 - a bad old record must not break a poll
                    continue
        return out

    def record_accept(self, job: Job, ok: bool, detail: str, dry_run: bool) -> None:
        self._accepts.append(
            {
                "at": datetime.now().isoformat(timespec="seconds"),
                "job_id": job.job_id,
                "ok": bool(ok) and not dry_run,
                "dry_run": dry_run,
                "detail": detail,
                "summary": job.summary(),
                "job_payload": {k: v for k, v in job.raw.items() if not k.startswith("_")},
            }
        )
        # Keep the ledger bounded but long enough to cover a full school year
        self._accepts = self._accepts[-2000:]
        _atomic_write(self.accepts_path, json.dumps(self._accepts, indent=2))

    def already_accepted(self, job: Job) -> bool:
        return any(a.get("job_id") == job.job_id and a.get("ok") for a in self._accepts)

    # -- audit -----------------------------------------------------------------
    def audit(self, event: str, **fields: Any) -> None:
        """Append one JSON line. This is the source of truth for tuning filters."""
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            **fields,
        }
        try:
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError as exc:  # pragma: no cover
            log.warning("could not write audit record: %s", exc)
