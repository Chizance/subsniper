"""Parse Frontline's server-rendered job table into Job objects.

Schema captured from a live Absence Management instance (legacy Aesop,
absencesub.frontlineeducation.com) by reading the page's own `#jobTemplate`
client-side template. Districts vary; verify against your own with
`python -m subsniper check`.

Row structure inside `#availableJobs table.jobList`:

    tbody.job
      tr.summary
        td.date      -> .itemDate, .multiEndDate (multi-day jobs)
        td.times     -> .startTime, .endTime
        td.duration  -> .durationName ("Full Day", "Half Day AM", ...)
        td.location  -> .tenantName (district), .locationName (school)
        td.more      -> a.acceptButton, a.rejectButton, a.showDetailsButton
        .name        -> absent employee
        .title       -> POSITION / ROLE  <- teacher-vs-admin filtering hinges on this
        .confNum     -> confirmation number (present once accepted)
      tr.detail      -> .reportTo, .reportToLocation, .locationPhone

Because `.title` is the only role signal, a job whose `.title` is empty is
treated as UNKNOWN and will fail the role filter rather than pass it.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from .models import Job

JOBS_CONTAINER = "#availableJobs"
JOB_ROW = "tbody.job, tr.job"


def _text(node: Tag | None, selector: str) -> str:
    if node is None:
        return ""
    found = node.select_one(selector)
    if found is None:
        return ""
    return re.sub(r"\s+", " ", found.get_text(" ", strip=True)).strip()


def _job_key(row: Tag) -> str | None:
    """Frontline stamps the row with an id like `job_1234567` or similar."""
    for attr in ("id", "data-id", "data-jobid", "jobid"):
        val = row.get(attr)
        if val:
            m = re.search(r"(\d{4,})", str(val))
            if m:
                return m.group(1)
            return str(val)
    # Fall back to any descendant carrying a numeric id
    for desc in row.find_all(attrs={"id": True}, limit=10):
        m = re.search(r"(\d{5,})", str(desc.get("id")))
        if m:
            return m.group(1)
    return None


def parse_jobs(html: str) -> list[Job]:
    """Extract available jobs from a /Substitute/Home response.

    Returns [] for the explicit "no available assignments" state. Raises
    NotLoggedIn-style detection is handled by the caller via `looks_logged_out`.
    """
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one(JOBS_CONTAINER)
    if container is None:
        # Some districts render the table without the wrapper id
        container = soup.select_one("table.jobList")
    if container is None:
        return []

    if container.select_one("tr.noData"):
        return []

    jobs: list[Job] = []
    for row in container.select(JOB_ROW):
        job = _parse_row(row)
        if job is not None:
            jobs.append(job)
    return jobs


def _parse_row(row: Tag) -> Job | None:
    start_date = _text(row, ".itemDate")
    end_date = _text(row, ".multiEndDate") or start_date
    start_time = _text(row, ".startTime")
    end_time = _text(row, ".endTime")

    title = _text(row, ".title")
    employee = _text(row, ".name")
    school = _text(row, ".locationName")
    district = _text(row, ".tenantName")
    duration_name = _text(row, ".durationName")
    report_to = _text(row, ".reportTo")
    conf = _text(row, ".confNum")

    # A row with no date and no title is chrome (header/spacer), not a job.
    if not start_date and not title:
        return None

    payload: dict[str, Any] = {
        "id": _job_key(row) or "",
        "positionName": title,
        "employeeName": employee,
        "locationName": school,
        "tenantName": district,
        "startDate": start_date,
        "endDate": end_date,
        "startTime": start_time,
        "endTime": end_time,
        "durationName": duration_name,
        "reportTo": report_to,
        "confirmationNumber": conf,
        "notes": " ".join(p for p in (duration_name, report_to) if p),
    }
    if not payload["id"]:
        payload.pop("id")

    job = Job.from_payload(payload)
    job.raw["_accept_selector"] = _accept_selector(row)
    job.raw["_duration_name"] = duration_name
    job.raw["_district"] = district
    return job


def _accept_selector(row: Tag) -> str | None:
    """A CSS selector that re-finds this row's accept control in a live page.

    Used by the accept path, which clicks the real control rather than
    reconstructing a POST - that keeps us correct through CSRF tokens and any
    handler logic Frontline runs on click.
    """
    btn = row.select_one("a.acceptButton, .acceptButton")
    if btn is None:
        return None
    row_id = row.get("id")
    if row_id:
        return f"#{row_id} .acceptButton"
    return None


def looks_logged_out(html: str) -> bool:
    """Detect a session that has expired and bounced to the sign-in page."""
    lowered = html[:20000].lower()
    signals = (
        'name="password"',
        "type=\"password\"",
        "sign in to your account",
        "id=\"loginform\"",
        "/sso/login",
        "session has expired",
        "your session has timed out",
    )
    if any(s in lowered for s in signals):
        return True
    # The authenticated shell always carries the sub nav; its absence alongside
    # a small body is a strong logged-out signal.
    if "substitute/home" not in lowered and len(html) < 5000:
        return True
    return False
