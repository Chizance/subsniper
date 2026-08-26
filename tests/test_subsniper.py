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

# Pin the clock. The fixture uses fixed dates, so without this the suite starts
# failing the moment those dates fall into the past and the lead-time filter
# (correctly) rejects jobs that have already started.
NOW = datetime(2026, 8, 16, 12, 0)
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
    assert filters.evaluate(job, cfg, now=NOW).matched is should_match


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
    assert filters.evaluate(job, cfg, now=NOW).matched


def test_untitled_job_fails_closed(jobs, cfg):
    job = _by_school(jobs, "Maple")
    result = filters.evaluate(job, cfg, now=NOW)
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
    result = filters.evaluate(_by_school(jobs, school), cfg, now=NOW)
    assert not result.matched
    assert fragment in result.reason_text


def test_unparseable_start_time_is_rejected(cfg):
    job = Job.from_payload({"id": "x", "positionName": "Teacher, Grade 4"})
    result = filters.evaluate(job, cfg, now=NOW)
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


# -- restart handling (the bug that silently ate Nick's jobs) -------------------

def test_fresh_state_reads_as_cold_start(tmp_path):
    """No state file at all -> prime, so a first run doesn't spam a backlog."""
    store = Store(tmp_path)
    assert store.seen_age_seconds() is None


def test_recent_state_reads_as_warm_restart(tmp_path, jobs):
    """State written seconds ago -> do NOT prime.

    This is the regression that mattered: priming on every start meant every
    restart silently absorbed all jobs posted since the last one, so they never
    produced a notification. Anything under the cold-start threshold must be
    treated as warm.
    """
    store = Store(tmp_path)
    store.mark_seen(jobs)
    age = store.seen_age_seconds()
    assert age is not None and age < 60
    assert age < 21600, "recent state must not be classified as a cold start"


def test_heartbeat_survives_a_restart(tmp_path):
    """Heartbeat state must persist, or a crash loop still looks healthy.

    Previously this lived only in memory, so each restart fired a fresh
    heartbeat - the duplicate notifications were the only outward sign that
    anything was wrong.
    """
    store = Store(tmp_path)
    assert not store.heartbeat_sent_today()
    store.mark_heartbeat_sent()
    assert Store(tmp_path).heartbeat_sent_today()


def test_restarts_are_counted(tmp_path):
    Store(tmp_path).record_service_start()
    Store(tmp_path).record_service_start()
    assert Store(tmp_path).record_service_start() == 3
    assert Store(tmp_path).starts_today() == 3


def test_second_instance_is_detected(tmp_path):
    """Two copies double the request rate and race on the same state files."""
    store = Store(tmp_path)
    assert not store.another_instance_running()
    store.touch_lock()
    assert Store(tmp_path).another_instance_running()
    store.release_lock()
    assert not Store(tmp_path).another_instance_running()


def test_stale_lock_does_not_block_startup(tmp_path):
    """A lock left behind by a crash must not wedge it shut forever."""
    store = Store(tmp_path)
    store.touch_lock()
    assert not store.another_instance_running(stale_seconds=0.0)


# -- updater safety ------------------------------------------------------------

UPDATER = ROOT / "update.ps1"


def test_updater_exists():
    assert UPDATER.exists(), "update.ps1 is referenced by the README and must ship"


@pytest.mark.parametrize(
    "must_preserve",
    [".env", "config.yaml", "state", "logs", ".venv", "STOP"],
)
def test_updater_never_overwrites_user_data(must_preserve):
    """The updater replaces program files in place. Anything the user owns must
    be on its preserve list.

    Dropping `.env` from that list would overwrite real Frontline credentials
    with the blank template on the next update - the tool would silently stop
    working and the cause would be invisible. `config.yaml` would silently reset
    every filter to the defaults. `state` would make it re-notify or re-accept
    jobs it had already handled.
    """
    body = UPDATER.read_text(encoding="utf-8")
    start = body.index("$Preserve")
    block = body[start:body.index(")", start)]
    assert f'"{must_preserve}"' in block, (
        f"{must_preserve!r} is missing from the updater's $Preserve list - "
        "an update would destroy it"
    )


def test_updater_backs_up_before_replacing():
    body = UPDATER.read_text(encoding="utf-8")
    assert "backupDir" in body and "Copy-Item" in body, "updater must back up first"


def test_updater_refuses_while_running():
    """Replacing files under a live process corrupts state mid-write."""
    body = UPDATER.read_text(encoding="utf-8")
    assert "running.lock" in body, "updater must check the liveness lock"


# -- polling must survive the site's own instrumentation -----------------------

def test_poll_does_not_use_in_page_fetch_as_primary():
    """The hot path must not depend on the page's window.fetch.

    Frontline ships Dynatrace RUM (ruxitagentjs), which patches window.fetch.
    In the field that wrapper threw "TypeError: Failed to fetch" on roughly 9 of
    every 10 polls while login and the heartbeat both looked fine. The context
    request API shares the same cookie jar but runs outside page JavaScript, so
    nothing the site loads can break it.
    """
    body = (ROOT / "src" / "subsniper" / "frontline.py").read_text(encoding="utf-8")
    fetch_impl = body[body.index("async def _fetch"):]
    ctx_pos = fetch_impl.index("self._ctx.request.get")
    page_pos = fetch_impl.index("page.evaluate")
    assert ctx_pos < page_pos, (
        "the context request must be tried BEFORE the in-page fetch fallback"
    )


def test_backoff_cannot_sleep_through_the_morning_rush():
    """A 900s backoff during a 5s window is the same as being switched off.

    This happened: persistent errors drove the backoff to its ceiling and it
    polled four times an hour straight through the rush.
    """
    body = (ROOT / "src" / "subsniper" / "poller.py").read_text(encoding="utf-8")
    bump = body[body.index("def _bump_backoff"):body.index("def _maybe_heartbeat")]
    assert "interval_for" in bump, "backoff must consider the current poll window"
    assert "60.0" in bump, "backoff must be capped during fast windows"


def test_sustained_failures_trigger_a_notification():
    """Silence must mean "no jobs", not "quietly broken".

    The heartbeat reported "SubSniper is running" for a full day while nearly
    every poll failed. Running and working are different claims.
    """
    body = (ROOT / "src" / "subsniper" / "poller.py").read_text(encoding="utf-8")
    assert "_maybe_warn_failing" in body
    warn = body[body.index("def _maybe_warn_failing"):body.index("def _next_interval")]
    assert "self.notifier.error" in warn, "must actually push a notification"
    assert "3600" in warn, "must rate-limit so it can't spam"


# -- Windows launchers ---------------------------------------------------------

@pytest.mark.parametrize("launcher", ["Setup SubSniper.bat", "Update SubSniper.bat"])
def test_launcher_exists(launcher):
    assert (ROOT / launcher).exists(), f"{launcher} is what the README tells users to click"


@pytest.mark.parametrize("launcher", ["Setup SubSniper.bat", "Update SubSniper.bat"])
def test_launcher_clears_mark_of_the_web(launcher):
    """Scripts extracted from a downloaded zip are blocked by Windows.

    Observed in the field: the window opened and closed instantly with no
    message at all, on a machine where the same script ran fine for the
    developer. The difference was that his copy came from a git clone (no zone
    marking) and had a loosened execution policy; the user's came from a zip on
    a default-Restricted machine.
    """
    body = (ROOT / launcher).read_text(encoding="utf-8")
    assert "Unblock-File" in body, "must clear the Mark of the Web"
    assert "-ExecutionPolicy Bypass" in body, "must not depend on the machine's policy"


@pytest.mark.parametrize("launcher", ["Setup SubSniper.bat", "Update SubSniper.bat"])
def test_launcher_always_pauses(launcher):
    """A window that vanishes tells the user nothing.

    The pause has to be in the .bat, not the .ps1 - a script that dies early
    never reaches its own prompt, which is precisely the failure being fixed.
    """
    body = (ROOT / launcher).read_text(encoding="utf-8")
    assert "pause" in body.lower()


def test_scripts_do_not_rely_on_their_own_closing_prompt():
    for name in ("update.ps1", "setup.ps1"):
        body = (ROOT / name).read_text(encoding="utf-8")
        assert 'Read-Host "Press Enter to close"' not in body, (
            f"{name} should let the .bat handle pausing - its own prompt is "
            "unreachable when the script dies early"
        )


# -- the doctor report ---------------------------------------------------------

def test_doctor_never_prints_credential_values():
    """The report is meant to be pasted into a chat or emailed.

    It must report whether each credential is set, never what it is.
    """
    body = (ROOT / "src" / "subsniper" / "__main__.py").read_text(encoding="utf-8")
    doc = body[body.index("def cmd_doctor"):body.index("def cmd_diagnose")]
    assert "len(val)" in doc, "should report length, not value"
    for leak in ("os.getenv(key)}", "{val}", "creds.password", "creds.username"):
        assert leak not in doc, f"doctor must not emit {leak}"


def test_doctor_warns_when_filtered_jobs_are_silent():
    """notify_on_nonmatching=False makes a rejected job look like no job.

    That ambiguity is the single biggest obstacle to diagnosing "nothing is
    arriving", so the report has to name it.
    """
    body = (ROOT / "src" / "subsniper" / "__main__.py").read_text(encoding="utf-8")
    doc = body[body.index("def cmd_doctor"):body.index("def cmd_diagnose")]
    assert "notify_on_nonmatching" in doc
    assert "NO notification" in doc


def test_doctor_reports_every_filter_that_can_reject_a_job(cfg):
    """If a filter can silently drop a job, the report must show its value."""
    body = (ROOT / "src" / "subsniper" / "__main__.py").read_text(encoding="utf-8")
    doc = body[body.index("def cmd_doctor"):body.index("def cmd_diagnose")]
    for setting in ("earliest_start", "latest_end", "min_duration_minutes",
                    "allowed_weekdays", "min_lead_time_minutes",
                    "role_include", "denylist"):
        assert setting in doc, f"doctor omits {setting}, which can reject jobs"
