"""Filter engine: decides whether a job is one you actually want.

Every check returns a reason string on rejection. Those reasons go straight
into the audit log, which is what makes filter tuning tractable - you can see
exactly why a job you wanted got skipped.

Design rule: when data is missing or unparseable, REJECT rather than accept.
A false negative costs one missed job. A false positive commits you to work
he can't do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Any

from .config import WEEKDAYS, Config
from .models import Job


@dataclass(frozen=True)
class FilterResult:
    matched: bool
    reasons: list[str]

    @property
    def reason_text(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "all checks passed"


def _hhmm(value: Any) -> dtime | None:
    if not value:
        return None
    try:
        hh, mm = (int(p) for p in str(value).split(":"))
        return dtime(hh, mm)
    except (ValueError, TypeError):
        return None


def evaluate(job: Job, cfg: Config, now: datetime | None = None) -> FilterResult:
    """Run every filter. Collects ALL failure reasons, not just the first.

    Collecting all of them matters for tuning: if a job fails on both role and
    time, seeing only 'role' would send you chasing the wrong config knob.
    """
    now = now or datetime.now()
    reasons: list[str] = []

    reasons += _check_role(job, cfg)
    reasons += _check_time(job, cfg, now)
    reasons += _check_location(job, cfg)

    return FilterResult(matched=not reasons, reasons=reasons)


def _check_role(job: Job, cfg: Config) -> list[str]:
    # Title only - see Job.role_text for why notes/employee name are excluded.
    text = job.role_text
    if not text.strip():
        return [
            "role: listing has no position title, so the role cannot be "
            "verified as teaching (failing closed)"
        ]

    for pattern in cfg.role_exclude:
        m = pattern.search(text)
        if m:
            return [f"role: matched exclude pattern /{pattern.pattern}/ on {m.group(0)!r}"]

    for pattern in cfg.role_include:
        if pattern.search(text):
            return []

    return [
        f"role: {job.title or text[:60]!r} matched no include pattern "
        f"({len(cfg.role_include)} configured)"
    ]


def _check_time(job: Job, cfg: Config, now: datetime) -> list[str]:
    out: list[str] = []

    if job.start_dt is None:
        return ["time: could not parse a start time from the listing"]

    earliest = _hhmm(cfg.get("filters.time.earliest_start"))
    latest = _hhmm(cfg.get("filters.time.latest_end"))

    if earliest and job.start_dt.time() < earliest:
        out.append(
            f"time: starts {job.start_dt.strftime('%-I:%M%p')}, "
            f"before earliest_start {earliest.strftime('%-I:%M%p')}"
        )

    if latest:
        if job.end_dt is None:
            out.append("time: latest_end is set but the listing has no end time")
        elif job.end_dt.time() > latest:
            out.append(
                f"time: ends {job.end_dt.strftime('%-I:%M%p')}, "
                f"after latest_end {latest.strftime('%-I:%M%p')}"
            )

    allowed_days = cfg.get("filters.time.allowed_weekdays")
    if allowed_days:
        allowed = {WEEKDAYS[str(d).strip().lower()[:3]] for d in allowed_days}
        if job.start_dt.weekday() not in allowed:
            out.append(
                f"time: falls on {job.start_dt.strftime('%A')}, not in allowed_weekdays"
            )

    duration = job.duration_minutes
    min_dur = cfg.get("filters.time.min_duration_minutes")
    max_dur = cfg.get("filters.time.max_duration_minutes")
    if isinstance(min_dur, (int, float)):
        if duration is None:
            out.append("time: min_duration_minutes is set but duration is unknown")
        elif duration < min_dur:
            out.append(f"time: duration {duration:.0f}min under minimum {min_dur}min")
    if isinstance(max_dur, (int, float)) and duration is not None and duration > max_dur:
        out.append(f"time: duration {duration:.0f}min over maximum {max_dur}min")

    lead = cfg.get("filters.time.min_lead_time_minutes")
    if isinstance(lead, (int, float)):
        minutes_out = (job.start_dt - now).total_seconds() / 60.0
        if minutes_out < lead:
            out.append(
                f"time: starts in {minutes_out:.0f}min, under min_lead_time {lead}min"
            )

    return out


def _check_location(job: Job, cfg: Config) -> list[str]:
    school = (job.school or "").lower()

    denylist = cfg.get("filters.location.denylist") or []
    for entry in denylist:
        if str(entry).lower().strip() and str(entry).lower().strip() in school:
            return [f"location: {job.school!r} matched denylist entry {entry!r}"]

    allowlist = [str(e).lower().strip() for e in (cfg.get("filters.location.allowlist") or [])]
    allowlist = [e for e in allowlist if e]
    if allowlist:
        if not school:
            return ["location: allowlist is set but the listing has no school name"]
        if not any(entry in school for entry in allowlist):
            return [f"location: {job.school!r} is not in the allowlist"]

    return []
