# SubSniper

**Get told about substitute jobs seconds after they're posted — and grab them automatically.**

SubSniper watches your Frontline Absence Management account and sends an alert to
your phone the moment a new job appears. If the job matches the rules you set
(the right hours, an actual teaching job), it can accept it for you before
anyone else has finished reading their notification.

Download it here: **https://github.com/Chizance/subsniper**

---

## Why you keep losing jobs

It probably isn't you being slow.

Every alert app checks Frontline **once a minute** — SubAlert, SubSidekick, and
even Frontline's own Jobulator app. That means:

- A job gets posted.
- Up to **60 seconds** go by before the app notices it.
- Everyone using that app gets buzzed at the *exact same moment*.
- Now it's a race between however many people are awake and holding their phone.

So by the time your phone lights up, you're already a minute late and competing
with everyone else who got the same alert simultaneously.

SubSniper checks **every 5 seconds** during the morning rush, so it spots the
job almost immediately. And because it can accept the job itself, there's no
waking up, unlocking, and tapping — it's done before a person could react.

---

## How often it checks

It doesn't check at one fixed rate. It checks **hard when jobs actually get
posted, and slowly the rest of the time.**

| When | How often | Why |
|---|---|---|
| **Weekday mornings, 5:00–8:30am** | every **5 seconds** | The rush. Most jobs get posted here, and this is the race you're trying to win. |
| **Weekday afternoons, 2:00–6:00pm** | every 10 seconds | Teachers calling out for tomorrow. |
| **Evenings, 6:00–10:00pm** (Sun–Thu) | every 20 seconds | Late postings for the next day. |
| **Everything else** | every 2 minutes | Overnight, weekends, midday. Jobs are rare, so it idles. |

**It never stops.** Leave it running and it watches around the clock — just
faster or slower depending on the hour. So if you check on it at 3pm on a
Sunday and it seems quiet, that's correct: it's looking every 2 minutes because
nothing gets posted then.

Each check is also given a small random nudge (up to 25% earlier or later) so
it isn't hitting Frontline on a perfect metronome.

**Why not 5 seconds all day?** Two reasons. It would be about 17,000 requests a
day instead of roughly 3,800 — far more conspicuous, and Frontline's rules are
already the main risk here. And it would buy nothing: checking every 5 seconds
at 2am doesn't find jobs that aren't there.

For comparison, SubAlert and every similar app check **once every 60 seconds,
all day** — about 1,400 times. During the morning rush SubSniper checks 12×
more often than they do, and stays quieter than they are the rest of the time.

You can change any of this in `config.yaml` under `polling:` — each window has
its own days, hours, and `interval_seconds`. It refuses to start with anything
under 3 seconds.

---

## Before you start

You'll need:

- **A Windows computer that stays turned on overnight.** This is the big one.
  Jobs get posted around 5am, and a sleeping laptop can't watch for anything. A
  desktop that's always on is ideal.
- **Your Frontline username and password.**
- **A phone** (iPhone or Android).
- **About $5** for the Pushover app, one time, not a subscription.
- **About 20 minutes** for setup.

You do **not** need to know anything about programming. You'll type a few
commands exactly as written, and fill in two files that look like forms.

---

## Please read this part

**This breaks Frontline's rules.** They don't allow apps to log in automatically
or accept jobs for you. Nobody is likely to come after you personally, but the
realistic risk is that **your Frontline account gets locked** — and then you get
no jobs at all, which is worse than where you started. SubSniper is built to be
careful and quiet about it, but the risk isn't zero. That's your call to make.

**Auto-accepting means you're actually booked.** If your settings are wrong, it
can accept a job you can't work, and you'll have to call the school and cancel.
Because of that, SubSniper starts in **practice mode** — it sends alerts but
never accepts anything. Leave it that way for about a week and check that it's
picking the jobs you'd have picked yourself. Only then turn accepting on.

---

## Setup

### Step 1 — Install Python

SubSniper is written in a language called Python, so your computer needs it.

Go to **https://www.python.org/downloads/** and click the big yellow download
button. Run the installer.

⚠️ **On the very first screen, tick the box that says "Add python.exe to PATH"**
before clicking Install. It's easy to miss and nothing will work without it. If
you miss it, just run the installer again and tick it.

### Step 2 — Download SubSniper

Go to **https://github.com/Chizance/subsniper**, click the green **Code**
button, then **Download ZIP**.

Right-click the downloaded file → **Extract All**. Put the folder somewhere you
can find it again, like `C:\SubSniper`.

### Step 3 — Run the setup

Open the folder and **double-click `Setup SubSniper.bat`**.

A window opens and does everything for you — it downloads what's needed and
creates your settings files. It takes a few minutes; the browser download near
the end is large, so let it finish.

> Use the `.bat`, not `setup.ps1` directly. Windows silently blocks PowerShell
> scripts extracted from a downloaded zip — the window just closes with no
> message. The `.bat` handles that.

When it's done it opens a file called `.env` in Notepad. Leave that open —
that's Step 5.

### Step 4 — Set up notifications on your phone

SubSniper sends alerts through an app called **Pushover**. It's reliable and it
can override silent mode, which matters a lot at 5am.

1. Go to **https://pushover.net** and create an account.
2. After logging in, you'll see **Your User Key** — a long string of letters and
   numbers. Copy it.
3. Scroll down to **Your Applications** and click **Create an Application/API
   Token**. Name it `SubSniper`, agree to the terms, click Create. It gives you
   an **API Token**. Copy that too.
4. Install the **Pushover** app on your phone and sign in. It costs $5 once.

Keep both of those codes handy for the next step.

### Step 5 — Fill in your details

In the Notepad window that opened, fill in the blanks after each `=` sign. Don't
add spaces or quotation marks — just type right after the equals sign:

```
FRONTLINE_USERNAME=your.frontline.username
FRONTLINE_PASSWORD=your-frontline-password
FRONTLINE_PIN=
PUSHOVER_USER_KEY=the-user-key-you-copied
PUSHOVER_API_TOKEN=the-api-token-you-copied
```

Leave `FRONTLINE_PIN` blank unless your district makes you type a PIN when you
log in. **Save the file** (Ctrl+S) and close Notepad.

This file stays on your computer only. It is never uploaded anywhere, and it's
specifically excluded from the public code.

### Step 6 — Check that it works

Open the SubSniper folder, click the address bar at the top of the window, type
`powershell`, and press Enter. A blue window opens. Paste this and press Enter:

```
.venv\Scripts\python.exe -m subsniper test-notify
```

**Your phone should buzz.** If it does, notifications are working.

Now check Frontline:

```
.venv\Scripts\python.exe -m subsniper check
```

This logs into your account, lists any jobs currently posted, and shows whether
each one matches your rules. It doesn't accept anything. If it says it logged in
successfully, you're set.

### Step 7 — Start it

Double-click **`Start SubSniper.bat`** in the folder.

A window opens and stays open. **That window has to stay open** for SubSniper to
keep watching — minimize it, don't close it. Leave the computer on overnight.

That's it. You'll get a quiet "still running" notification each morning at 4:45,
and a loud one whenever a matching job appears.

---

## Setting your preferences

Open **`config.yaml`** in the SubSniper folder with Notepad. Most of it you can
ignore. These are the parts worth changing:

**What hours you'll work** — find the section that looks like this:

```yaml
    earliest_start: "07:00"
    latest_end: "16:00"
    min_duration_minutes: 180
```

- `earliest_start` — won't take anything starting before this time
- `latest_end` — won't take anything ending after this time
- `min_duration_minutes` — skips short jobs (180 = three hours)

Times use a 24-hour clock: `07:00` is 7am, `16:00` is 4pm. Keep the quotation
marks.

**Which days** — remove any day you don't want:

```yaml
    allowed_weekdays: [mon, tue, wed, thu, fri]
```

**Teaching jobs only** — this is already set up. SubSniper only takes jobs whose
title mentions *teacher*, *teach*, or *instructor*, and it throws out principal,
assistant principal, counselor, nurse, aide, paraprofessional, custodian, clerk,
librarian, psychologist, speech, therapist, coach, and others.

**Particular schools** — if there are schools you won't drive to, add them to the
`denylist`. If you *only* want certain schools, put those in the `allowlist`
instead:

```yaml
  location:
    allowlist: []
    denylist: ["Some School Name"]
```

Save the file. **Restart SubSniper** (close the window, double-click
`Start SubSniper.bat` again) for changes to take effect.

---

## Turning on auto-accept

**Don't do this on day one.** Run it in practice mode for about a week first.

Each time SubSniper finds a matching job it sends you an alert saying *"DRY RUN —
Would accept."* Read those. If for a week straight it's flagging jobs you'd
genuinely have taken, and not flagging ones you wouldn't, your settings are good.

Then open `config.yaml`, find:

```yaml
autoaccept:
  dry_run: true
```

Change `true` to `false`. Save, and restart SubSniper.

Right below it is a safety limit:

```yaml
  max_accepts_per_day: 2
```

That's the most jobs it will ever book in one day. Even if something goes badly
wrong, it can't sign you up for a whole week.

### The stop switch

To stop it accepting jobs *right now* without shutting anything down — say
you're sick, or you already have plans — create an empty file named **`STOP`**
(no file extension) in the SubSniper folder.

Easiest way: in the SubSniper folder, right-click → New → Text Document, then
rename it to exactly `STOP` and confirm when Windows warns you about changing the
extension.

While `STOP` exists, you still get alerts but nothing is ever accepted. Delete
the file to turn accepting back on.

---

## What the alerts look like

| Alert | Means |
|---|---|
| **ACCEPTED: Teacher, Grade 5** | You're booked. Repeats until you tap it. |
| **DRY RUN — Would accept** | Practice mode. It matched, nothing was booked. |
| **Job matched (not accepted)** | Matched, but the daily limit or STOP file blocked it. |
| **SubSniper is running** | Silent daily check-in at 4:45am. Everything's fine. |
| **SubSniper error** | Something's wrong — see below. |

If the 4:45am check-in stops arriving, SubSniper isn't running anymore. Usually
that means the computer restarted or the window got closed.

---

## Updating

When there's a new version, **double-click `Update SubSniper.bat`.** That's the
whole process.

It checks GitHub, backs up your current copy, downloads the new version, and
installs it. **Your settings are never touched** - your logins (`.env`), your
job preferences (`config.yaml`), and its memory of jobs it has already seen all
stay exactly as they are. Only the program itself is replaced.

Two things it will stop and tell you about:

- **SubSniper is still running.** Close the SubSniper window first, then run the
  updater again. Replacing files under a live program corrupts things.
- **You're already up to date.** Nothing happens.

> **If a window flashes open and closes instantly**, you ran `update.ps1`
> directly instead of the `.bat`. Windows blocks PowerShell scripts that came
> out of a downloaded zip, and it does it silently. Use
> `Update SubSniper.bat` — it clears that block for you.

To look without installing anything, open PowerShell in the folder and run:

```
.\"Update SubSniper.bat" -Check
```

Or ask the program itself:

```
.venv\Scripts\python.exe -m subsniper version
```

After it finishes, **start SubSniper again** — double-click
`Start SubSniper.bat`. The updater doesn't restart it for you.

### If an update makes things worse

Every update saves a complete copy of what you had first, so going back is
straightforward:

1. **Close SubSniper** if it's running.
2. Open the **`backups`** folder inside your SubSniper folder. Inside are folders
   named by date and time, like `2026-08-18_071500`. Open the newest one.
3. Select everything inside it (**Ctrl+A**), copy (**Ctrl+C**).
4. Go back to your SubSniper folder and paste (**Ctrl+V**). Say **yes** to
   replacing files.
5. Double-click `Start SubSniper.bat`.

You're now exactly where you were before the update, settings and all. Nothing
is lost — tell whoever set this up for you what went wrong, and they can look at
it without any time pressure.

Old backup folders are safe to delete once things have been working for a while.

---

## First thing to run when it "didn't work"

```
.venv\Scripts\python.exe -m subsniper doctor
```

This checks everything at once — whether it's running, what your settings are,
whether your logins are filled in, what it's been doing, and whether it can
still reach Frontline and your phone right now. It saves the result as
**`subsniper-report.txt`** in the SubSniper folder.

**Send that file to whoever set this up for you.** It contains no passwords —
only whether each one is filled in. It answers in one go what otherwise takes a
dozen back-and-forth messages.

If you'd rather just see the history without the live tests:

```
.venv\Scripts\python.exe -m subsniper diagnose
```

This reads SubSniper's own log and tells you what actually happened — most
importantly **whether it was even running** when the jobs were posted. It
reports how many times it restarted, any stretch of time it wasn't watching
(flagging gaps that cover the morning rush), and for every job it saw, why it
was skipped.

Almost every "it didn't work" turns out to be a coverage gap — the computer
slept, or the window got closed — rather than anything wrong with the filters.
Run this before changing any settings.

---

## When something goes wrong

**Phone never buzzes.** Run the `test-notify` command from Step 6. If it fails,
your Pushover keys are wrong — recopy them from pushover.net, watching for extra
spaces.

**"Login did not produce an authenticated session."** SubSniper couldn't get
into Frontline. Check your username and password by logging in manually in a
browser. If your district makes you enter a PIN, add it to `FRONTLINE_PIN` in
`.env`. If your district uses its own login page rather than Frontline's, that
address needs to go in `config.yaml` under `login_url` — ask whoever set this up
for you.

**It never finds anything.** Run the `check` command during a time jobs are
normally posted. For each job it prints the reason it was skipped — usually the
hours in `config.yaml` are narrower than the jobs actually being offered.

**It's finding jobs but never accepting.** Almost always `dry_run` is still set
to `true`, or a `STOP` file is sitting in the folder.

**"Job row no longer present."** Someone accepted it first. Normal occasionally.
If it's every single time, something's slow — worth looking at.

**Nothing works after a Windows restart.** SubSniper doesn't restart itself
unless it's installed as a scheduled task. Just double-click
`Start SubSniper.bat` again. To make it automatic, right-click
`deploy\install_windows.ps1` → Run with PowerShell (needs admin).

---

## Keeping the computer awake

Windows going to sleep stops SubSniper. To prevent that while it's plugged in,
open PowerShell and run:

```
powercfg /change standby-timeout-ac 0
```

Also worth checking Settings → System → Power that the screen can sleep but the
computer doesn't.

---

## For developers

Architecture notes, measurements, and design rationale are in
[docs/TECHNICAL.md](docs/TECHNICAL.md). Tests: `python -m pytest tests/ -q`.

MIT licensed. Not affiliated with, endorsed by, or supported by Frontline
Education.
