# SubSniper

A faster replacement for SubAlert. Polls Frontline Absence Management for newly
posted substitute jobs, pushes them to your phone through Pushover, and can
auto-accept the ones matching your rules.

---

## Why this is faster

Every existing tool in this space polls Frontline **once every 60 seconds**:

| Tool | Poll interval | Auto-accept | Cost |
|---|---|---|---|
| SubAlert | 60s | No | $4.95–6.95/mo |
| SubSidekick | 60s | No | $9.99/mo |
| Jobulator (Frontline's own) | 60s | No | Paid |
| **SubSniper** | **5s in the morning rush** | **Yes** | Free |

So SubAlert isn't malfunctioning — a 60-second loop means you learn about a job
**30 seconds after posting on average, up to 60 seconds worst case**. Then you
still have to wake up, unlock your phone, and tap. Meanwhile every other
SubAlert subscriber got the identical alert at the identical moment, which turns
it into a pure reaction-time race you'll usually lose.

SubSniper attacks both halves of that:

1. **Detection.** A 5-second poll during the morning window cuts average
   detection lag from ~30s to ~2.5s. Measured against the live site, one poll
   costs **118ms**.
2. **Reaction.** Auto-accept removes human reaction time from the loop
   entirely — the accept click fires before a person could have read the alert.

---

## Read this before you run it

**This almost certainly violates Frontline's Terms of Service.** Automated
access and automated accepting are not sanctioned. The realistic risk is your
account getting rate-limited, flagged, or locked, and a locked account means no
sub jobs at all. That's a real cost, so a few things are deliberately built in:

- Polling is **windowed** — hard only during posting windows, slow otherwise. A
  flat 5-second poll would be ~17,000 requests/day; the shipped config is closer
  to ~4,000, which is a far less conspicuous footprint.
- Every interval carries **±25% random jitter**, because a perfectly regular
  request cadence is the single easiest bot signature to spot.
- The config **refuses to start** with an interval under 3 seconds.

**Auto-accept commits you to real work.** A filter mistake books a job you have
to actually show up for, or call in to cancel. This ships with `dry_run: true`
and it should stay that way for at least a week.

---

## Setup

### 1. Install

```bash
git clone <wherever you put this> subsniper
cd subsniper

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium
```

### 2. Credentials

```bash
cp .env.example .env
chmod 600 .env                       # Windows: skip
```

Fill in `.env`. Frontline credentials and Pushover keys live here and **nowhere
else** — not in `config.yaml`, not in chat, not in the repo. `.env` is
gitignored.

For Pushover: sign up at [pushover.net](https://pushover.net), install the app
(**$5 one-time per platform**), copy your **user key** from the dashboard, then
create an Application/API Token named "SubSniper" and copy that too.

### 3. Config

```bash
cp config.example.yaml config.yaml
```

Then verify Pushover and Frontline both work:

```bash
python -m subsniper test-notify   # should buzz the phone
python -m subsniper check         # logs in, lists jobs, changes nothing
```

`check` prints every currently-posted job and whether it would match — the
fastest way to sanity-check filters against reality.

---

## Tuning the filters

The two that matter are in `config.yaml`.

**Time window:**

```yaml
filters:
  time:
    earliest_start: "07:00"
    latest_end: "16:00"
    min_duration_minutes: 180
    allowed_weekdays: [mon, tue, wed, thu, fri]
    min_lead_time_minutes: 30    # don't accept something starting in 10 minutes
```

**Teacher-only:** a job must match one `include` pattern and zero `exclude`
patterns (case-insensitive regex). The shipped `exclude` list covers principal,
AP, counselor, nurse, aide, paraprofessional, custodian, clerk, librarian,
psychologist, speech, therapist, coach, and more.

Role matching reads **only the position title** — never the notes or the absent
employee's name. That's deliberate: a job whose notes say *"report to the
Principal's Office"*, or one covering a teacher surnamed *Coach*, would
otherwise be thrown out. There's a regression test pinning this exact case.

After a few days, tune against the audit log rather than guessing:

```bash
# what got skipped and why
grep job_skipped logs/audit.jsonl | tail -20

# what matched
grep job_matched logs/audit.jsonl | tail -20
```

Every skip records its reason. If you see a job you wanted get skipped, the
reason string names the exact setting to change.

You can also replay a saved page offline — no network, no accepts:

```bash
python -m subsniper replay tests/fixtures/available_jobs.html
```

---

## Going live

Only after a week of watching `logs/audit.jsonl` and agreeing with every match:

```yaml
autoaccept:
  dry_run: false
  max_accepts_per_day: 2
```

The accept path runs a fixed gauntlet, and every branch is logged:

1. kill switch present → notify only
2. arm file required but missing → notify only
3. `dry_run` on → notify only
4. already accepted this job → skip
5. per-run cap reached → notify only
6. per-day cap reached → notify only
7. overlaps an already-accepted job → notify only
8. → accept

**Kill switch.** Stops all accepting immediately, without stopping the service.
Notifications keep coming.

```bash
touch STOP     # accepting off
rm STOP        # accepting on
```

**Arm file** (opt-in inverse) — set `arm_file: ARMED` and accepting only happens
on days you run `touch ARMED`. Good for "only auto-accept when I actually want
work."

Check state any time:

```bash
python -m subsniper status
```

---

## Running it 24/7

The machine must be **awake** at 5am. A sleeping laptop polls nothing.

**Windows** (run once, elevated):

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install_windows.ps1
powercfg /change standby-timeout-ac 0      # stop it sleeping while plugged in
```

**Linux** — edit the paths and user in `deploy/subsniper.service`, then:

```bash
sudo cp deploy/subsniper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now subsniper
journalctl -u subsniper -f
```

Both configs restart automatically on crash. A daily heartbeat push at 04:45
(silent, priority -1) confirms it's alive — if that stops arriving, it's dead.

---

## How it works

**Detection (hot path).** Frontline's substitute portal is a legacy ASP.NET app;
jobs are server-rendered into `#availableJobs table.jobList` on
`/Substitute/Home`. There is no JSON API. SubSniper issues an authenticated
same-origin `fetch` from inside the logged-in browser context and parses the
HTML. No rendering, no navigation — 118ms per poll.

**Accept (correctness path).** Navigates the real page and clicks the real
`.acceptButton`, rather than synthesizing a POST. Slower (~2s) but it inherits
CSRF tokens and Frontline's own click handlers, so it stays correct when they
change things.

**Session.** Playwright persistent context, so the login survives restarts —
re-login is the most fragile step in the pipeline and this avoids it.

Row schema was captured from the live site by reading the page's own
`#jobTemplate`: `.title` (role), `.name`, `.itemDate`, `.multiEndDate`,
`.startTime`, `.endTime`, `.durationName`, `.tenantName`, `.locationName`,
`.confNum`, `.acceptButton`.

---

## Design rule

**When data is missing or unparseable, reject.** A false negative costs one
missed job. A false positive commits you to work you can't do. So a listing with
no position title, or an unparseable start time, is always skipped — never
accepted on the assumption it's fine.

---

## Tests

```bash
python -m pytest tests/ -q
```

31 tests, covering parsing against the real DOM schema, every filter rejection
path, overlap detection, and the safety rails — including that `dry_run`
defaults to on, that dry-run records never consume the real daily budget, and
that the config refuses a sub-3-second poll interval.

---

## Troubleshooting

**No notifications.** `python -m subsniper test-notify`. If that fails it's the
Pushover keys.

**"login did not produce an authenticated session."** Usually a district SSO
portal. Set `frontline.login_url` to the portal you actually use. If the
district requires a PIN, fill `FRONTLINE_PIN` in `.env`.

**Matching nothing.** Run `check` during a window when jobs are posted. If real
jobs show as `skip`, the printed reason names the setting to change. Setting
`notifications.notify_on_nonmatching: true` for a day or two shows everything
being filtered out.

**Accepts failing with "job row no longer present."** Someone beat us to it.
If it's constant, drop the morning `interval_seconds` to 3.
