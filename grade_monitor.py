import asyncio
import sqlite3
import smtplib
import os
import time
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ─────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────

load_dotenv()

PROFILE_URL = "https://mis.bau.edu.lb/web/v12/iconnectv12/base/profileV2.aspx"
INTER_URL   = (
    "https://mis.bau.edu.lb/web/v12/iconnectv12/cas/intermediate.aspx"
    "?TargetURL=https://mis.bau.edu.lb/web/v12/iconnectv12/base/profileV2.aspx"
)

USERNAME   = os.getenv("BAU_USERNAME")
PASSWORD   = os.getenv("BAU_PASSWORD")
GMAIL_FROM = os.getenv("GMAIL_SENDER")
GMAIL_PASS = os.getenv("GMAIL_APP_PASS")
GMAIL_TO   = os.getenv("EMAIL_RECIPIENT")

DB_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grades.db")
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grade_monitor.log")

MAX_RETRIES       = 3
RETRY_DELAY       = 45    # seconds between retries
MIN_RUN_INTERVAL  = 1800  # 30 min — won't hit the server more often than this
NOTIFY_EMPTY      = True  # set False to skip "no new grades" emails

# Timeouts (ms) — BAU portal is notoriously slow
T_PAGE_LOAD  = 120_000   # 2 min  — initial page / goto
T_SELECTOR   = 60_000    # 1 min  — waiting for a DOM element
T_REDIRECT   = 60_000    # 1 min  — waiting for URL to change
T_IDLE       = 45_000    # 45 s   — networkidle after a redirect

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ─────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS grades (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                term     TEXT NOT NULL,
                course   TEXT NOT NULL,
                title    TEXT,
                credits  TEXT,
                level    TEXT,
                grade    TEXT,
                sgpa     TEXT,
                added_at TEXT,
                UNIQUE(term, course)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS run_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ran_at     TEXT NOT NULL,
                success    INTEGER NOT NULL DEFAULT 0
            )
        """)


def load_existing() -> set:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT term, course FROM grades").fetchall()
    return set(rows)


def save_grade(g: dict):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR IGNORE INTO grades
                (term, course, title, credits, level, grade, sgpa, added_at)
            VALUES
                (:term, :course, :title, :credits, :level, :grade, :sgpa, :added_at)
        """, g)


def seconds_since_last_run() -> float:
    """Returns seconds since the last SUCCESSFUL run, or infinity if never run."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT ran_at FROM run_log WHERE success=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return float("inf")
    last = datetime.fromisoformat(row[0])
    return (datetime.now() - last).total_seconds()


def record_run(success: bool):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO run_log (ran_at, success) VALUES (?, ?)",
            (datetime.now().isoformat(), 1 if success else 0),
        )

# ─────────────────────────────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────────────────────────────

def _send(msg):
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.ehlo()
        s.starttls()
        s.login(GMAIL_FROM, GMAIL_PASS)
        s.sendmail(GMAIL_FROM, GMAIL_TO, msg.as_string())


def _try_send(msg, label: str):
    if not all([GMAIL_FROM, GMAIL_PASS, GMAIL_TO]):
        log.error("Email credentials missing — check .env (GMAIL_SENDER, GMAIL_APP_PASS, EMAIL_RECIPIENT).")
        return
    for attempt in range(1, 4):
        try:
            _send(msg)
            log.info(f"Email sent → {GMAIL_TO} ({label})")
            return
        except smtplib.SMTPAuthenticationError:
            log.error("Gmail auth failed — check GMAIL_SENDER / GMAIL_APP_PASS. Not retrying.")
            return
        except Exception as e:
            log.warning(f"Email attempt {attempt}/3 failed: {e}")
            if attempt < 3:
                time.sleep(5)
    log.error(f"All 3 email attempts failed for: {label}")


def _grade_color(grade: str) -> str:
    if grade.startswith("A"): return "#28a745"
    if grade.startswith("B"): return "#fd7e14"
    return "#dc3545"


def send_new_grades_email(new_grades: list):
    rows = "".join(f"""
        <tr>
          <td style="padding:8px;border:1px solid #dee2e6">{g['term']}</td>
          <td style="padding:8px;border:1px solid #dee2e6"><strong>{g['course']}</strong></td>
          <td style="padding:8px;border:1px solid #dee2e6">{g['title']}</td>
          <td style="padding:8px;border:1px solid #dee2e6;text-align:center">{g['credits']}</td>
          <td style="padding:8px;border:1px solid #dee2e6;text-align:center;
                     color:{_grade_color(g['grade'])};font-weight:bold">{g['grade']}</td>
          <td style="padding:8px;border:1px solid #dee2e6;text-align:center">{g['sgpa']}</td>
        </tr>""" for g in new_grades)

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:700px;margin:auto">
      <div style="background:#004080;color:white;padding:16px;border-radius:8px 8px 0 0">
        <h2 style="margin:0">BAU Grade Alert</h2>
        <p style="margin:4px 0 0">{datetime.now().strftime('%d %b %Y, %H:%M')}</p>
      </div>
      <div style="border:1px solid #dee2e6;border-top:none;padding:16px;border-radius:0 0 8px 8px">
        <p>Hi Nizar! <strong>{len(new_grades)} new grade(s)</strong> just posted:</p>
        <table style="width:100%;border-collapse:collapse;font-size:0.95em">
          <thead>
            <tr style="background:#f8f9fa">
              <th style="padding:8px;border:1px solid #dee2e6;text-align:left">Term</th>
              <th style="padding:8px;border:1px solid #dee2e6;text-align:left">Course</th>
              <th style="padding:8px;border:1px solid #dee2e6;text-align:left">Title</th>
              <th style="padding:8px;border:1px solid #dee2e6;text-align:center">Credits</th>
              <th style="padding:8px;border:1px solid #dee2e6;text-align:center">Grade</th>
              <th style="padding:8px;border:1px solid #dee2e6;text-align:center">SGPA</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        <p style="margin-top:16px">
          <a href="https://iconnect.bau.edu.lb"
             style="background:#004080;color:white;padding:10px 20px;border-radius:4px;text-decoration:none">
            View on iConnect
          </a>
        </p>
        <p style="color:#6c757d;font-size:0.85em;margin-top:24px">Sent by grade_monitor.py</p>
      </div>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"BAU: {len(new_grades)} New Grade{'s' if len(new_grades) > 1 else ''} Posted"
    msg["From"]    = GMAIL_FROM
    msg["To"]      = GMAIL_TO
    msg.attach(MIMEText(html, "html"))
    _try_send(msg, f"{len(new_grades)} new grade(s)")


def send_no_new_grades_email():
    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:700px;margin:auto">
      <div style="background:#004080;color:white;padding:16px;border-radius:8px 8px 0 0">
        <h2 style="margin:0">BAU Grade Monitor — All Clear</h2>
        <p style="margin:4px 0 0">{datetime.now().strftime('%d %b %Y, %H:%M')}</p>
      </div>
      <div style="border:1px solid #dee2e6;border-top:none;padding:16px;border-radius:0 0 8px 8px">
        <p>Hi Nizar! Checked your iConnect grades — <strong>no new grades</strong> since last run.</p>
        <p style="color:#6c757d;font-size:0.85em">Sent by grade_monitor.py</p>
      </div>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "BAU Grade Monitor — No New Grades"
    msg["From"]    = GMAIL_FROM
    msg["To"]      = GMAIL_TO
    msg.attach(MIMEText(html, "html"))
    _try_send(msg, "no new grades")

# ─────────────────────────────────────────────────────────────────────
# LOGIN HELPERS
# ─────────────────────────────────────────────────────────────────────

async def _fill_wso2_login(page, username: str, password: str):
    """
    Fill and submit the EIS WSO2 login form.
    Confirmed selectors from debug_eis.py:
      - #usernameUserInput  (visible text input)
      - #password           (visible password input)
      - [name='submit_form'] (submit button — has no id)
    """
    await page.wait_for_selector("#usernameUserInput", state="visible", timeout=T_SELECTOR)
    await page.fill("#usernameUserInput", username)
    await page.fill("#password", password)
    await page.click("[name='submit_form']")


async def _do_login(page) -> None:
    """
    Go directly to intermediate.aspx, which redirects to the EIS SSO gate
    at eis.bau.edu.lb.  We authenticate there and get redirected back to
    profileV2.aspx — no need to touch the slow iconnect.bau.edu.lb at all.
    """
    log.info("Navigating directly to intermediate.aspx (bypasses slow iconnect.bau.edu.lb)...")
    try:
        await page.goto(INTER_URL, wait_until="domcontentloaded", timeout=T_PAGE_LOAD)
    except Exception as e:
        err = str(e)
        if "ERR_ABORTED" in err or "net::" in err:
            log.info(f"Redirect from intermediate.aspx (expected). Now at: {page.url}")
        else:
            raise

    await page.wait_for_load_state("networkidle", timeout=T_IDLE)
    log.info(f"After intermediate redirect. Now at: {page.url}")


async def _handle_eis_sso(page) -> None:
    """
    Handle the second CAS/SSO gate at eis.bau.edu.lb.
    The portal sometimes requires a second authentication step via
    WSO2 IS at eis.bau.edu.lb.  We detect it by URL, fill the form,
    and wait for the redirect back to mis.bau.edu.lb.
    """
    if "eis.bau.edu.lb" not in page.url:
        return

    log.info("Second SSO gate detected — re-authenticating at eis.bau.edu.lb...")

    try:
        await _fill_wso2_login(page, USERNAME, PASSWORD)
    except PlaywrightTimeout:
        # The EIS page is present but the login form is not visible.
        # This can happen when the CAS ticket was already consumed and the
        # page is mid-redirect.  Wait for it to settle.
        log.info("EIS login form not visible — waiting for redirect to complete...")
        await page.wait_for_load_state("networkidle", timeout=T_IDLE)
        if "eis.bau.edu.lb" in page.url:
            raise RuntimeError(f"Stuck on EIS SSO page after waiting. URL: {page.url}")
        log.info(f"Redirect completed without needing EIS form. Now at: {page.url}")
        return

    # Wait for the redirect away from eis.bau.edu.lb.
    try:
        await page.wait_for_url(
            lambda url: "eis.bau.edu.lb" not in url,
            timeout=T_REDIRECT,
        )
        log.info(f"EIS SSO passed. Now at: {page.url}")
    except PlaywrightTimeout:
        # URL may not have updated yet but the content might be right.
        log.warning("EIS SSO URL didn't change within timeout — checking page content...")
        await page.wait_for_load_state("networkidle", timeout=T_IDLE)
        log.info(f"Continuing despite URL still being: {page.url}")


async def _navigate_to_profile(page) -> None:
    """
    At this point we are either on the EIS SSO gate or already on profileV2.
    Handle EIS auth if needed, then confirm we're on the profile page.
    """
    # Handle EIS SSO gate if it appeared after the intermediate.aspx redirect.
    await _handle_eis_sso(page)

    # If we still aren't on the profile page, navigate there directly.
    if PROFILE_URL not in page.url:
        log.info(f"Not on profile page yet ({page.url}) — navigating directly...")
        try:
            await page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=T_PAGE_LOAD)
        except Exception as e:
            if "ERR_ABORTED" in str(e) or "net::" in str(e):
                await page.wait_for_load_state("networkidle", timeout=T_IDLE)
            else:
                raise
        await _handle_eis_sso(page)

    log.info(f"Profile page: {page.url}")

# ─────────────────────────────────────────────────────────────────────
# GRADE EXTRACTION
# ─────────────────────────────────────────────────────────────────────

async def _cell_text(cells: list, index: int) -> str:
    if index >= len(cells):
        return ""
    return (await cells[index].inner_text()).strip()


async def _scrape_table(page) -> list:
    """Read whatever rows are currently visible in #AcademicHistory."""
    try:
        await page.wait_for_selector("#AcademicHistory tbody tr", timeout=T_SELECTOR)
    except PlaywrightTimeout:
        log.info("Table not visible yet — waiting for networkidle then retrying...")
        await page.wait_for_load_state("networkidle", timeout=T_IDLE)
        try:
            await page.wait_for_selector("#AcademicHistory tbody tr", timeout=T_SELECTOR)
        except PlaywrightTimeout:
            return []

    rows = await page.query_selector_all("#AcademicHistory tbody tr")
    grades = []
    for row in rows:
        cells = await row.query_selector_all("td")
        if len(cells) < 6:
            continue
        grade_val = await _cell_text(cells, 5)
        if grade_val in ("TR", ""):
            continue
        grades.append({
            "term":     await _cell_text(cells, 0),
            "course":   await _cell_text(cells, 1),
            "title":    await _cell_text(cells, 2),
            "credits":  await _cell_text(cells, 3),
            "level":    await _cell_text(cells, 4),
            "grade":    grade_val,
            "sgpa":     await _cell_text(cells, 6) if len(cells) > 6 else "",
            "added_at": datetime.now().isoformat(),
        })
    return grades


async def _extract_grades(page) -> list:
    """
    The portal's #ddlSchedule dropdown only exposes 'current' and 'previous'
    terms — there is no 'all' option.  Scrape both and merge by (term, course)
    so we never miss a grade from either window.
    """
    log.info("Waiting for Academic History table (#AcademicHistory)...")
    all_grades: dict[tuple, dict] = {}

    for term_val, label in [("current", "Current Term"), ("previous", "Previous Term")]:
        # #ddlSchedule is hidden behind a custom CSS widget so select_option()
        # fails.  __doPostBack() is strict-mode and can't be called from
        # page.evaluate() directly.  Instead: set the value and dispatch a
        # native 'change' event — the onchange attribute then calls __doPostBack
        # in its own context where strict-mode is fine.
        await page.evaluate("""(val) => {
            var sel = document.querySelector('#ddlSchedule');
            sel.value = val;
            sel.dispatchEvent(new Event('change', {bubbles: true}));
        }""", term_val)
        await page.wait_for_load_state("networkidle", timeout=T_IDLE)
        try:
            batch = await _scrape_table(page)
            log.info(f"  {label}: {len(batch)} grade(s).")
        except PlaywrightTimeout:
            log.warning(f"  {label}: table did not appear (timeout) — skipping this term.")
            batch = []
        for g in batch:
            all_grades[(g["term"], g["course"])] = g

    grades = list(all_grades.values())
    log.info(f"Scraped {len(grades)} grade(s) total (current + previous terms).")
    return grades

# ─────────────────────────────────────────────────────────────────────
# SCRAPING — ORCHESTRATION
# ─────────────────────────────────────────────────────────────────────

async def _scrape_once() -> list:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(user_agent=USER_AGENT)
            page    = await context.new_page()

            await _do_login(page)
            await _navigate_to_profile(page)
            return await _extract_grades(page)
        finally:
            await browser.close()


async def scrape_grades() -> list:
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt > 1:
                log.info(f"Retry {attempt}/{MAX_RETRIES} — waiting {RETRY_DELAY}s...")
                await asyncio.sleep(RETRY_DELAY)
            return await _scrape_once()
        except PlaywrightTimeout as e:
            last_err = e
            log.warning(f"Attempt {attempt}/{MAX_RETRIES} timed out: {e}")
        except RuntimeError as e:
            log.error(f"Fatal scrape error (not retrying): {e}")
            return []
        except Exception as e:
            last_err = e
            log.warning(f"Attempt {attempt}/{MAX_RETRIES} failed: {e}")

    log.error(f"All {MAX_RETRIES} scrape attempts failed. Last error: {last_err}")
    return []

# ─────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────

async def main():
    log.info("═" * 50)
    log.info("BAU Grade Monitor — started")

    if not USERNAME or not PASSWORD:
        log.error("BAU_USERNAME / BAU_PASSWORD missing in .env. Exiting.")
        return

    init_db()

    # Rate-limit: refuse to hammer the portal more than once per MIN_RUN_INTERVAL.
    secs = seconds_since_last_run()
    if secs < MIN_RUN_INTERVAL:
        mins_left = int((MIN_RUN_INTERVAL - secs) / 60)
        log.info(
            f"Last successful run was {int(secs/60)} min ago "
            f"(min interval {MIN_RUN_INTERVAL//60} min). "
            f"Skipping — try again in ~{mins_left} min."
        )
        return

    existing = load_existing()
    log.info(f"DB has {len(existing)} known grade(s).")

    scraped = await scrape_grades()

    if not scraped:
        record_run(success=False)
        log.error("Scraping returned 0 results — nothing saved, no email sent.")
        return

    record_run(success=True)

    # The portal may show only the current term — never all historical grades.
    # So never compare set sizes. Just find anything scraped that isn't in the DB.
    new_grades = [g for g in scraped if (g["term"], g["course"]) not in existing]

    if not new_grades:
        log.info("No new grades. Everything up to date.")
        if NOTIFY_EMPTY:
            send_no_new_grades_email()
        return

    log.info(f"{len(new_grades)} new grade(s) found:")
    for g in new_grades:
        log.info(f"  {g['term']} | {g['course']} | {g['title']} | {g['grade']}")
        save_grade(g)

    send_new_grades_email(new_grades)
    log.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
