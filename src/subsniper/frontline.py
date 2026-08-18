"""Frontline session management, polling, and job acceptance.

Two-speed design, chosen after measuring the real site:

  DETECTION (hot path)  - an authenticated GET of /Substitute/Home issued
    through the browser context's request API, which shares the logged-in
    cookie jar but runs outside page JavaScript. ~40KB, no rendering, no
    navigation. This is what runs every few seconds.

    It deliberately does NOT call fetch() inside the page. Frontline loads
    Dynatrace RUM, which patches window.fetch; in the field that wrapper
    threw "TypeError: Failed to fetch" on nearly every poll. Any script the
    site ships can patch fetch, and we don't control what they ship.

  ACCEPT (correctness path) - navigates the real page and clicks the real
    `.acceptButton`. Slower, but it inherits CSRF tokens and whatever click
    handlers Frontline runs, so it stays correct as they change things.

Login is performed by Playwright against Frontline's own form using
credentials from .env. They are passed straight into the page and are never
logged, echoed, or persisted anywhere by this code.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    async_playwright,
)

from .config import Config
from .models import Job
from .parser import looks_logged_out, parse_jobs

log = logging.getLogger(__name__)

JOBS_PATH = "/Substitute/Home"


class AuthError(RuntimeError):
    """Session is invalid and could not be re-established."""


class TransientError(RuntimeError):
    """A poll failed in a way that's probably worth retrying."""


@dataclass
class PollResult:
    jobs: list[Job]
    latency_ms: int
    status: int


class FrontlineClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None
        self._origin: str = ""
        self._auth_failures = 0

    # -- lifecycle -------------------------------------------------------------
    async def start(self) -> None:
        self._pw = await async_playwright().start()
        state_path = self.cfg.root / "state" / "browser"
        state_path.mkdir(parents=True, exist_ok=True)

        # A persistent context keeps the Frontline session across restarts, so a
        # service restart at 4am doesn't force a fresh login (and a fresh login
        # is the single most fragile step in the whole pipeline).
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(state_path),
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        await self.ensure_authenticated()

    async def close(self) -> None:
        for closer in (self._ctx, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:  # pragma: no cover - best effort teardown
                    pass
        if self._pw is not None:
            await self._pw.stop()

    # -- auth ------------------------------------------------------------------
    async def ensure_authenticated(self) -> None:
        """Make sure we're logged in, logging in only if we actually need to."""
        assert self._page is not None
        page = self._page

        login_url = str(self.cfg.get("frontline.login_url", "https://app.frontlineeducation.com/"))
        try:
            await page.goto(login_url, wait_until="domcontentloaded", timeout=45_000)
        except PWTimeout as exc:
            raise TransientError(f"could not reach Frontline: {exc}") from exc

        settle = float(self.cfg.get("frontline.login_settle_seconds", 8))
        await asyncio.sleep(settle)

        if await self._is_authenticated():
            self._origin = self._origin or _origin_of(page.url)
            log.info("existing Frontline session is valid (%s)", self._origin)
            self._auth_failures = 0
            return

        log.info("no valid session, performing login")
        await self._perform_login()

        if not await self._is_authenticated():
            self._auth_failures += 1
            raise AuthError(
                "login did not produce an authenticated session. Check "
                "FRONTLINE_USERNAME / FRONTLINE_PASSWORD in .env, and whether "
                "the district requires a PIN or SSO."
            )
        self._origin = _origin_of(self._page.url)
        self._auth_failures = 0
        log.info("login succeeded (%s)", self._origin)

    async def _is_authenticated(self) -> bool:
        assert self._page is not None
        url = self._page.url or ""
        if "absencesub" in url and "/Substitute/" in url:
            return True
        # Follow the app picker through to Absence Management if we landed there
        if "/select" in url:
            try:
                link = self._page.locator("a", has_text="Absence Management").first
                if await link.count() > 0:
                    await link.click(timeout=10_000)
                    await self._page.wait_for_load_state("domcontentloaded", timeout=30_000)
                    await asyncio.sleep(3)
                    return "/Substitute/" in (self._page.url or "")
            except PWTimeout:
                return False
        try:
            await self._page.goto(
                _join(self._origin or "https://absencesub.frontlineeducation.com", JOBS_PATH),
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            await asyncio.sleep(2)
            return "/Substitute/" in (self._page.url or "")
        except PWTimeout:
            return False

    async def _perform_login(self) -> None:
        """Fill Frontline's sign-in form.

        Credentials go directly from the environment into the page. They are
        never written to logs - note there is no logging of `creds` anywhere in
        this method, and Credentials.__repr__ is redacted.
        """
        assert self._page is not None
        page = self._page
        creds = self.cfg.credentials

        user_sel = (
            "input#username, input[name='username'], input[name='Username'], "
            "input[type='email'], input[name='UserName']"
        )
        pass_sel = "input#password, input[name='password'], input[name='Password'], input[type='password']"

        try:
            await page.wait_for_selector(user_sel, timeout=30_000)
            await page.fill(user_sel, creds.username)
            await page.fill(pass_sel, creds.password)

            submit = page.locator(
                "button[type='submit'], input[type='submit'], #SignIn, button:has-text('Sign In')"
            ).first
            await submit.click(timeout=15_000)
            await page.wait_for_load_state("domcontentloaded", timeout=45_000)
            await asyncio.sleep(float(self.cfg.get("frontline.login_settle_seconds", 8)))

            if creds.pin:
                pin_sel = "input[name='pin'], input#pin, input[name='Pin']"
                if await page.locator(pin_sel).count() > 0:
                    await page.fill(pin_sel, creds.pin)
                    await page.locator(
                        "button[type='submit'], input[type='submit']"
                    ).first.click(timeout=15_000)
                    await page.wait_for_load_state("domcontentloaded", timeout=30_000)
                    await asyncio.sleep(3)
        except PWTimeout as exc:
            raise AuthError(
                "timed out on the Frontline login form. If the district uses a "
                "custom SSO portal, set frontline.login_url to that portal."
            ) from exc

    # -- polling ---------------------------------------------------------------
    async def poll(self) -> PollResult:
        """Fetch and parse the available-jobs list. The hot path."""
        assert self._page is not None
        origin = self._origin or "https://absencesub.frontlineeducation.com"
        url = _join(origin, JOBS_PATH)

        status, html, latency = await self._fetch(url)

        if status in (401, 403) or looks_logged_out(html):
            self._auth_failures += 1
            raise AuthError(f"session appears expired (HTTP {status})")
        if status >= 500:
            raise TransientError(f"Frontline returned HTTP {status}")
        if status != 200:
            raise TransientError(f"unexpected HTTP {status}")

        self._auth_failures = 0
        return PollResult(jobs=parse_jobs(html), latency_ms=latency, status=status)

    async def _fetch(self, url: str) -> tuple[int, str, int]:
        """Retrieve the jobs page. Returns (status, html, latency_ms).

        Uses the browser context's own request API rather than calling fetch()
        inside the page. Both share the logged-in cookie jar, but this one runs
        outside page JavaScript.

        That distinction is load-bearing. Frontline ships Dynatrace RUM
        (ruxitagentjs), which monkey-patches window.fetch to instrument every
        request. Our in-page fetch went through that wrapper, and it threw
        "TypeError: Failed to fetch" on essentially every poll - the site's own
        telemetry breaking our request. Going around page JS sidesteps the whole
        class of problem: any script the site loads can patch fetch, and we
        can't control what they ship.

        The in-page path is kept as a fallback in case a district's setup makes
        the context request fail instead.
        """
        assert self._ctx is not None and self._page is not None

        started = time.perf_counter()
        try:
            resp = await self._ctx.request.get(url, timeout=25_000)
            html = await resp.text()
            return resp.status, html, int((time.perf_counter() - started) * 1000)
        except Exception as api_exc:  # noqa: BLE001 - fall through to the backup
            log.debug("context request failed (%s), trying in-page fetch", api_exc)

        try:
            started = time.perf_counter()
            result: dict[str, Any] = await self._page.evaluate(
                """async (url) => {
                    const r = await fetch(url, {
                        credentials: 'same-origin',
                        redirect: 'follow',
                        cache: 'no-store',
                    });
                    return { status: r.status, html: await r.text() };
                }""",
                url,
            )
            return (
                int(result.get("status", 0)),
                str(result.get("html", "")),
                int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:
            raise TransientError(f"both fetch paths failed: {exc}") from exc

    # -- accept ----------------------------------------------------------------
    async def accept(self, job: Job) -> tuple[bool, str]:
        """Accept a job by clicking the real control. Returns (ok, detail).

        Deliberately does NOT synthesize a POST. Frontline's accept path runs
        through jQuery handlers and may carry an anti-forgery token; clicking
        the actual element is both more correct and more durable.
        """
        assert self._page is not None
        page = self._page
        origin = self._origin or "https://absencesub.frontlineeducation.com"

        try:
            await page.goto(_join(origin, JOBS_PATH), wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(600)

            locator = None
            selector = job.raw.get("_accept_selector")
            if selector:
                candidate = page.locator(selector)
                if await candidate.count() > 0:
                    locator = candidate.first

            if locator is None:
                # Re-find the row by its distinguishing text, then take its
                # accept control. Guards against the row order shifting between
                # detection and accept.
                row = page.locator("tbody.job, tr.job").filter(
                    has_text=job.school or job.title
                )
                if await row.count() == 0:
                    return False, "job row no longer present - almost certainly taken"
                locator = row.first.locator(".acceptButton").first

            if await locator.count() == 0:
                return False, "accept control not found on the row"

            await locator.click(timeout=10_000)
            await page.wait_for_timeout(1500)

            # Frontline may raise an in-page confirmation dialog
            for confirm in ("button:has-text('Yes')", "button:has-text('Confirm')", ".confirmButton"):
                node = page.locator(confirm)
                try:
                    if await node.count() > 0 and await node.first.is_visible():
                        await node.first.click(timeout=5_000)
                        await page.wait_for_timeout(1500)
                        break
                except PWTimeout:
                    pass

            body = (await page.content()).lower()
            if "already" in body and "filled" in body:
                return False, "job was already filled"
            if "confirmation" in body or "confirmation number" in body:
                return True, "accepted (confirmation shown)"
            return True, "accept click submitted"

        except PWTimeout as exc:
            return False, f"timed out during accept: {exc}"
        except Exception as exc:  # noqa: BLE001 - surface anything to the audit log
            return False, f"accept failed: {exc}"

    @property
    def auth_failures(self) -> int:
        return self._auth_failures


def _origin_of(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _join(origin: str, path: str) -> str:
    return origin.rstrip("/") + "/" + path.lstrip("/")
