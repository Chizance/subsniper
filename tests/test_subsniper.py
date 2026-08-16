"""Tests for the parts where a bug costs real money or a real commitment.

Priorities, in order:
  1. Auto-accept never fires when it shouldn't (dry run, caps, kill switch).
  2. Role filtering never mistakes an admin posting for a teaching one, and
     never rejects a teaching one because of incidental text.
  3. Time filtering fails closed on unparseable data.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from subsniper import filters  # noqa: E402
from subsniper.config import load_config  # noqa: E402
from subsniper.models import Job  # noqa: E402
from subsniper.parser import looks_logged_out, parse_jobs  # noqa: E402
from subsniper.state import Store  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "available_jobs.html"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cfg():
    # Fall back to the example so the suite runs on a fresh clone, before
    # anyone has copied it to config.yaml.
    path = ROOT / "config.yaml"
    if not path.exists():
        path = ROOT / "config.example.yaml"
    return load_config(path, ROOT / ".env", require_credentials=False)


@pytest.fixture(scope="module")
def jobs():
    return parse_jobs(FIXTURE.read_text(encoding="utf-8"))


def _by_school(jobs, fragment):
    for job in jobs:
        if fragment.lower() in job.school.lower():
            return job
    raise AssertionError(f"no fixture job at {fragment!r}")


# -- parsing -------------------------------------------------------------------

def test_parses_every_job_row(jobs):
    assert len(jobs) == 11


def test_parses_fields_off_the_real_schema(jobs):
    job = _by_school(jobs, "Riverside")
    assert job.title == "Teacher, Grade 9 English"
    assert job.employee == "Rebecca Alvarez"
    assert job.school == "Riverside High School"
    assert job.start_dt == datetime(2026, 8, 17, 8, 0)
    assert job.end_dt == datetime(2026, 8, 17, 15, 0)
    assert job.duration_minutes == 420
    assert job.job_id == "4410021"


def test_multiday_job_uses_end_date(jobs):
    job = _by_school(jobs, "Cedar")
    assert job.start_dt.date() == datetime(2026, 8, 25).date()
    assert job.end_dt.date() == datetime(2026, 8, 27).date()


def test_no_data_state_yields_no_jobs():
    html = '<div id="availableJobs"><table class="jobList">' \
           '<tr class="noData"><td>no available assignments</td></tr></table></div>'
    assert parse_jobs(html) == []


def test_job_ids_are_stable_across_polls():
    """Unstable IDs would re-notify for the same job on every single poll."""
    first = parse_jobs(FIXTURE.read_text(encoding="utf-8"))
    second = parse_jobs(FIXTURE.read_text(encoding="utf-8"))
    assert [j.job_id for j in first] == [j.job_id for j in second]


def test_detects_logged_out_page():
    assert looks_logged_out('<form id="loginForm"><input name="password"></form>')
    assert not looks_logged_out('<div id="availableJobs">/Substitute/Home</div>' + "x" * 6000)


# -- role filtering ------------------------------------------------------------

@pytest.mark.parametrize(
    "school,should_match",
    [
        ("Riverside", True),    # Teacher, Grade 9 English
        ("Hillcrest", False),        # Assistant Principal
        ("Oakdale", False),      # Instructional Aide
        ("Willow", False),     # School Nurse
        ("Maple", False),          # empty title -> fail closed
        ("Stonebridge", True),         # teacher w/ "Principal's Office" in notes
    ],
)
def test_role_filter(jobs, cfg, school, should_match):
    job = _by_school(jobs, school)
    assert filters.evaluate(job, cfg).matched is should_match


def test_role_matching_ignores_notes_and_employee_name(jobs, cfg):
    """The false-negative this guards against is subtle and expensive.

    A teacher job whose notes say "report to the Principal's Office", for an
    employee surnamed Coach, must still match. Both words are on the exclude
    list; neither belongs to the role.
    """
    job = _by_school(jobs, "Stonebridge")
    assert "Principal" in job.notes or "Principal" in job.searchable_text
    assert "Coach" in job.employee
    assert job.role_text == "Teacher, Grade 6 Science"
    assert filters.evaluate(job, cfg).matched


def test_untitled_job_fails_closed(jobs, cfg):
    job = _by_school(jobs, "Maple")
    result = filters.evaluate(job, cfg)
    assert not result.matched
    assert "failing closed" in result.reason_text


# -- time filtering ------------------------------------------------------------

@pytest.mark.parametrize(
    "school,fragment",
    [
        ("Fairview High", "before earliest_start"),
        ("Lakeview", "after latest_end"),
        ("Brookside Elementary", "under minimum"),
        ("Summit Continuation", "not in allowed_weekdays"),
    ],
)
def test_time_filter_reasons(jobs, cfg, school, fragment):
    result = filters.evaluate(_by_school(jobs, school), cfg)
    assert not result.matched
    assert fragment in result.reason_text


def test_unparseable_start_time_is_rejected(cfg):
    job = Job.from_payload({"id": "x", "positionName": "Teacher, Grade 4"})
    result = filters.evaluate(job, cfg)
    assert not result.matched
    assert "could not parse a start time" in result.reason_text


def test_min_lead_time_rejects_imminent_jobs(cfg):
    now = datetime(2026, 8, 17, 7, 55)
    job = Job.from_payload({
        "id": "soon", "positionName": "Teacher, Grade 5",
        "startDate": "8/17/2026", "startTime": "8:00 AM", "endTime": "3:00 PM",
    })
    result = filters.evaluate(job, cfg, now=now)
    assert not result.matched
    assert "min_lead_time" in result.reason_text


def test_collects_all_failure_reasons_not_just_the_first(cfg):
    """Seeing only one reason sends you tuning the wrong config knob."""
    job = Job.from_payload({
        "id": "multi", "positionName": "Teacher, Band",
        "startDate": "8/22/2026", "startTime": "6:00 AM", "endTime": "6:30 AM",
    })
    reasons = filters.evaluate(job, cfg, now=datetime(2026, 8, 1)).reasons
    assert len(reasons) >= 3


# -- overlap detection ---------------------------------------------------------

def test_overlapping_jobs_detected(jobs):
    a = _by_school(jobs, "Riverside")           # Mon 8/17 8:00-15:00
    b = Job.from_payload({
        "id": "clash", "positionName": "Teacher, Art",
        "startDate": "8/17/2026", "startTime": "1:00 PM", "endTime": "4:00 PM",
    })
    assert a.overlaps(b) and b.overlaps(a)


def test_non_overlapping_jobs_not_flagged(jobs):
    a = _by_school(jobs, "Riverside")           # Mon 8/17
    b = _by_school(jobs, "Hillcrest")                # Tue 8/18
    assert not a.overlaps(b)


# -- safety rails --------------------------------------------------------------

def test_dry_run_is_the_shipped_default(cfg):
    """If this ever flips, a fresh install starts accepting jobs unattended."""
    assert cfg.dry_run is True


def test_accept_caps_are_bounded(cfg):
    per_day = cfg.get("autoaccept.max_accepts_per_day")
    per_run = cfg.get("autoaccept.max_accepts_per_run")
    assert 0 < per_run <= per_day <= 5


def test_config_rejects_a_subsecond_poll_interval(tmp_path):
    """A 1s interval is ~86k requests/day and would get the account flagged."""
    import yaml
    from subsniper.config import ConfigError

    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    raw["polling"]["windows"][0]["interval_seconds"] = 1
    bad = tmp_path / "config.yaml"
    bad.write_text(yaml.safe_dump(raw))

    with pytest.raises(ConfigError, match="denial-of-service"):
        load_config(bad, ROOT / ".env", require_credentials=False)


def test_config_rejects_contradictory_time_window(tmp_path):
    import yaml
    from subsniper.config import ConfigError

    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    raw["filters"]["time"]["earliest_start"] = "15:00"
    raw["filters"]["time"]["latest_end"] = "09:00"
    bad = tmp_path / "config.yaml"
    bad.write_text(yaml.safe_dump(raw))

    with pytest.raises(ConfigError, match="no job could ever match"):
        load_config(bad, ROOT / ".env", require_credentials=False)


def test_config_rejects_empty_include_list(tmp_path):
    """An empty include list would match every posting, including admin roles."""
    import yaml
    from subsniper.config import ConfigError

    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    raw["filters"]["role"]["include"] = []
    bad = tmp_path / "config.yaml"
    bad.write_text(yaml.safe_dump(raw))

    with pytest.raises(ConfigError, match="match every job"):
        load_config(bad, ROOT / ".env", require_credentials=False)


def test_daily_cap_counts_only_real_accepts(tmp_path, jobs):
    """Dry-run records must never consume the real daily budget."""
    store = Store(tmp_path)
    job = jobs[0]
    store.record_accept(job, ok=True, detail="dry", dry_run=True)
    assert store.accepts_today() == 0
    store.record_accept(job, ok=True, detail="real", dry_run=False)
    assert store.accepts_today() == 1


def test_seen_state_survives_restart(tmp_path, jobs):
    """A crash loop must not re-notify for jobs already handled."""
    Store(tmp_path).mark_seen(jobs)
    reopened = Store(tmp_path)
    assert all(not reopened.is_new(job) for job in jobs)


def test_already_accepted_blocks_a_repeat(tmp_path, jobs):
    store = Store(tmp_path)
    job = jobs[0]
    assert not store.already_accepted(job)
    store.record_accept(job, ok=True, detail="ok", dry_run=False)
    assert Store(tmp_path).already_accepted(job)
