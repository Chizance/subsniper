"""Normalized job representation.

Frontline's payload shape varies by district and changes without notice, so
everything funnels through `Job.from_payload`, which is deliberately forgiving
about field names. When a district returns something unexpected, the raw
payload is preserved so the audit log still shows exactly what came back.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Any

# Candidate key names, most specific first. Frontline districts differ here.
_ID_KEYS = ("id", "jobId", "job_id", "absenceId", "absence_id", "confirmationNumber")
_TITLE_KEYS = (
    "positionName", "position", "positionTitle", "jobTitle", "title",
    "assignmentType", "classification", "positionType", "subjectName",
)
_SCHOOL_KEYS = (
    "locationName", "location", "schoolName", "school", "siteName", "building",
    "organizationName",
)
_EMPLOYEE_KEYS = ("employeeName", "employee", "absentEmployee", "teacherName", "forName")
_START_KEYS = ("startDate", "start", "startDateTime", "beginDate", "date", "absenceDate")
_END_KEYS = ("endDate", "end", "endDateTime", "finishDate")
_START_TIME_KEYS = ("startTime", "beginTime", "timeStart", "startTimeOfDay")
_END_TIME_KEYS = ("endTime", "finishTime", "timeEnd", "endTimeOfDay")
_NOTES_KEYS = ("notes", "note", "comments", "description", "specialInstructions")

_TIME_RE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\s*(?::(\d{2}))?\s*([AaPp])?\.?[Mm]?\.?\s*$"
)


def _first(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Case-insensitive lookup across a list of candidate keys."""
    lowered = {str(k).lower(): v for k, v in payload.items()}
    for key in keys:
        val = lowered.get(key.lower())
        if val not in (None, ""):
            return val
    return None


def parse_time(value: Any) -> dtime | None:
    """Parse the many shapes Frontline uses for a time-of-day."""
    if value is None or value == "":
        return None
    if isinstance(value, dtime):
        return value
    if isinstance(value, datetime):
        return value.time()

    text = str(value).strip()

    # Embedded in an ISO datetime
    if "T" in text or (" " in text and "-" in text):
        parsed = parse_datetime(text)
        if parsed:
            return parsed.time()

    m = _TIME_RE.match(text)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    meridiem = m.group(4)
    if meridiem:
        upper = meridiem.upper()
        if upper == "P" and hour != 12:
            hour += 12
        elif upper == "A" and hour == 12:
            hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return dtime(hour, minute)


def parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, dtime.min)

    text = str(value).strip()
    # Normalize trailing Z and strip timezone offsets - Frontline times are
    # local to the district, and mixing tz-aware/naive comparisons is a
    # reliable source of off-by-hours bugs.
    text = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", text).strip()

    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",
        "%m/%d/%y", "%b %d, %Y", "%B %d, %Y",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


@dataclass
class Job:
    """A single available assignment, normalized."""

    job_id: str
    title: str
    school: str
    employee: str
    start_dt: datetime | None
    end_dt: datetime | None
    notes: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Job":
        title = str(_first(payload, _TITLE_KEYS) or "").strip()
        school = str(_first(payload, _SCHOOL_KEYS) or "").strip()
        employee = str(_first(payload, _EMPLOYEE_KEYS) or "").strip()
        notes = str(_first(payload, _NOTES_KEYS) or "").strip()

        start_dt = _combine(
            _first(payload, _START_KEYS), _first(payload, _START_TIME_KEYS)
        )
        end_raw = _first(payload, _END_KEYS)
        end_time_raw = _first(payload, _END_TIME_KEYS)
        # Single-day jobs often omit endDate but still carry endTime.
        if end_raw is None and end_time_raw is not None and start_dt is not None:
            end_dt = _combine(start_dt.date().isoformat(), end_time_raw)
        else:
            end_dt = _combine(end_raw, end_time_raw)

        raw_id = _first(payload, _ID_KEYS)
        job_id = str(raw_id) if raw_id is not None else _synthetic_id(
            title, school, employee, start_dt
        )

        return cls(
            job_id=job_id,
            title=title,
            school=school,
            employee=employee,
            start_dt=start_dt,
            end_dt=end_dt,
            notes=notes,
            raw=payload,
        )

    # -- derived properties ----------------------------------------------------
    @property
    def duration_minutes(self) -> float | None:
        if not self.start_dt or not self.end_dt:
            return None
        delta = (self.end_dt - self.start_dt).total_seconds() / 60.0
        return delta if delta >= 0 else None

    @property
    def role_text(self) -> str:
        """Text the role filter matches against - the position title ONLY.

        Deliberately excludes notes and the absent employee's name. Both are
        false-match minefields: a note reading "report to the Principal's
        office" would otherwise reject a legitimate teacher job, and an
        employee surnamed Coach or Marshall would do the same. The title is
        the only field that actually carries role semantics.

        When a district leaves this empty the filter fails closed (no include
        pattern can match ""), which is the safe direction.
        """
        return self.title

    @property
    def searchable_text(self) -> str:
        """Everything about the job, for logging and human review only.

        Not used for filtering - see `role_text`.
        """
        return " | ".join(p for p in (self.title, self.notes, self.employee) if p)

    def overlaps(self, other: "Job") -> bool:
        if not all([self.start_dt, self.end_dt, other.start_dt, other.end_dt]):
            # Without both ranges, fall back to same-day comparison - the safe
            # assumption is that two jobs on one day conflict.
            if self.start_dt and other.start_dt:
                return self.start_dt.date() == other.start_dt.date()
            return False
        return self.start_dt < other.end_dt and other.start_dt < self.end_dt

    def summary(self) -> str:
        when = "time TBD"
        if self.start_dt:
            when = self.start_dt.strftime("%a %b %-d")
            if self.end_dt:
                when += f" {self.start_dt.strftime('%-I:%M%p')}-{self.end_dt.strftime('%-I:%M%p')}"
        bits = [b for b in (self.title or "Untitled", self.school, when) if b]
        return " | ".join(bits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "title": self.title,
            "school": self.school,
            "employee": self.employee,
            "start": self.start_dt.isoformat() if self.start_dt else None,
            "end": self.end_dt.isoformat() if self.end_dt else None,
            "duration_minutes": self.duration_minutes,
            "notes": self.notes[:500],
        }


def _combine(date_value: Any, time_value: Any) -> datetime | None:
    base = parse_datetime(date_value)
    tod = parse_time(time_value)
    if base is None:
        return None
    if tod is None:
        return base
    return datetime.combine(base.date(), tod)


def _synthetic_id(title: str, school: str, employee: str, start: datetime | None) -> str:
    """Stable fallback ID when the payload carries no identifier.

    Must be deterministic across polls, otherwise every poll would look like a
    brand new job and fire duplicate notifications.
    """
    seed = "|".join([title, school, employee, start.isoformat() if start else ""])
    return "syn-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
