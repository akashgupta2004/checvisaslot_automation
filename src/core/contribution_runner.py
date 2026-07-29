import argparse
import asyncio
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import Page, async_playwright, Error as PlaywrightError

from src.auth.browser import connect_to_chrome
from src.auth.login import wait_for_waiting_room, CloudflareBlockException
from src.auth.cdp_client import ensure_on_portal


OFC_URL = "https://www.usvisascheduling.com/en-US/ofc-schedule/"

OFC_CITY_VALUES = {
    "CHENNAI": "3f6bf614-b0db-ec11-a7b4-001dd80234f6",
    "CHENNAI VAC": "3f6bf614-b0db-ec11-a7b4-001dd80234f6",
    "HYDERABAD": "436bf614-b0db-ec11-a7b4-001dd80234f6",
    "HYDERABAD VAC": "436bf614-b0db-ec11-a7b4-001dd80234f6",
    "KOLKATA": "466bf614-b0db-ec11-a7b4-001dd80234f6",
    "KOLKATA VAC": "466bf614-b0db-ec11-a7b4-001dd80234f6",
    "MUMBAI": "486bf614-b0db-ec11-a7b4-001dd80234f6",
    "MUMBAI VAC": "486bf614-b0db-ec11-a7b4-001dd80234f6",
    "DELHI": "4a6bf614-b0db-ec11-a7b4-001dd80234f6",
    "NEW DELHI": "4a6bf614-b0db-ec11-a7b4-001dd80234f6",
    "NEW DELHI VAC": "4a6bf614-b0db-ec11-a7b4-001dd80234f6",
}

DEFAULT_CITY_ORDER = ["NEW DELHI", "MUMBAI", "HYDERABAD", "CHENNAI", "KOLKATA"]


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [OFC_CONTRIB] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ofc_contribution_runner")


def _state_file(customer: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in customer)
    state_dir = Path(os.getenv("CHECKVISA_STATE_DIR", Path(__file__).parent.parent.parent / "state"))
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / f"contribution_state_{safe}.json"


def _write_state(customer: str, payload: dict) -> None:
    payload = {
        "customer_name": customer,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    _state_file(customer).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def normalize_city(city: str) -> str:
    city = city.strip().upper()
    if city not in OFC_CITY_VALUES:
        raise ValueError(f"Unsupported OFC city: {city}")
    return city


def parse_cities(raw: str) -> list[str]:
    if not raw:
        return DEFAULT_CITY_ORDER[:]
    cities = []
    for part in raw.split(","):
        if not part.strip():
            continue
        if part.strip().upper() == "ANY":
            return DEFAULT_CITY_ORDER[:]
        city = normalize_city(part)
        if city not in cities:
            cities.append(city)
    return cities or DEFAULT_CITY_ORDER[:]


async def _wait_for_page_idle(page: Page, timeout_ms: int = 15_000) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass


async def try_saved_session_ofc_page(page: Page, customer: str, wait_seconds: int = 600) -> bool:
    log.info("Checking saved session directly on OFC page.")
    try:
        await page.goto(OFC_URL, wait_until="domcontentloaded", timeout=120_000)
    except Exception as exc:
        log.warning(f"Direct OFC navigation did not complete cleanly: {exc}")

    try:
        await wait_for_waiting_room(
            page,
            log,
            timeout_minutes=max(1, wait_seconds // 60),
            ready_selector="#post_select",
        )
    except TimeoutError:
        log.info(f"Saved session stayed in waiting room too long. Current URL: {page.url}")
        _write_state(customer, {"status": "saved_session_waiting_room_timeout", "url": page.url})
        return False
    except CloudflareBlockException as e:
        log.error(f"Cloudflare Block detected during saved session check: {e}")
        sys.exit(43)

    await _wait_for_page_idle(page)

    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            if await page.locator("#post_select").count() > 0 and await page.locator("#post_select").first.is_visible():
                _write_state(customer, {"status": "ofc_page_ready", "url": page.url, "session_reused": True})
                log.info("Saved session active; OFC page ready.")
                return True

            url = page.url.lower()
            login_visible = await page.locator("#signInName, input[type='password']").count() > 0
            if any(k in url for k in ["login", "logon", "signin", "b2clogin"]) or login_visible:
                log.info(f"Saved session redirected to login. Current URL: {page.url}")
                _write_state(customer, {"status": "saved_session_inactive", "url": page.url})
                return False
        except Exception:
            pass

        await asyncio.sleep(1)

    log.info(f"Saved session did not expose OFC controls after waiting. Current URL: {page.url}")
    _write_state(customer, {"status": "saved_session_inactive", "url": page.url})
    return False


async def go_to_ofc_page(page: Page, customer: str) -> bool:
    if "/ofc-schedule" in page.url.lower():
        log.info("Already on OFC schedule page.")
    else:
        if not await ensure_on_portal(page, log, timeout_seconds=90):
            return False

        clicked = False
        for selector in ["#continue_application", "a[href*='/ofc-schedule']", "a:has-text('Schedule Appointment')"]:
            try:
                if await page.locator(selector).count() > 0:
                    await page.locator(selector).first.click()
                    clicked = True
                    log.info(f"Clicked schedule link: {selector}")
                    break
            except Exception:
                continue

        if not clicked:
            log.info("Schedule link not found on landing page; navigating directly to OFC page.")
            try:
                await page.goto(OFC_URL, wait_until="domcontentloaded", timeout=120_000)
            except Exception as nav_err:
                log.warning(f"Direct OFC navigation interrupted ({nav_err.__class__.__name__}); waiting for page to settle...")
                await asyncio.sleep(3)
                # If we landed on the profile page, try to complete it or navigate past it
                if "/profile" in page.url.lower():
                    log.info("Landed on profile page; attempting to proceed...")
                    for btn_sel in ["button:has-text('Continue')", "button:has-text('Save')", "input[type='submit']", "a:has-text('Continue')"]:
                        try:
                            if await page.locator(btn_sel).count() > 0:
                                await page.locator(btn_sel).first.click()
                                log.info(f"Clicked profile button: {btn_sel}")
                                await asyncio.sleep(3)
                                break
                        except Exception:
                            continue
                    # Try OFC navigation again
                    try:
                        await page.goto(OFC_URL, wait_until="domcontentloaded", timeout=60_000)
                    except Exception:
                        log.warning("Second OFC navigation also interrupted; continuing to check page state...")

    await _wait_for_page_idle(page)

    try:
        await page.wait_for_selector("#post_select", state="visible", timeout=60_000)
    except Exception:
        log.error(f"OFC city dropdown did not appear. Current URL: {page.url}")
        _write_state(customer, {"status": "ofc_page_failed", "url": page.url})
        return False

    _write_state(customer, {"status": "ofc_page_ready", "url": page.url})
    log.info("OFC page ready.")
    return True


async def rotate_city(page: Page, customer: str, city: str, dwell_seconds: int) -> dict:
    city_value = OFC_CITY_VALUES[city]
    log.info(f"Selecting OFC city: {city}")

    await page.select_option("#post_select", value=city_value)

    loading_deadline = time.time() + 45
    last_message = ""
    while time.time() < loading_deadline:
        try:
            msg = (await page.locator("#datepicker-message").inner_text(timeout=1000)).strip()
            last_message = " ".join(msg.split())
            if "loading" not in last_message.lower():
                break
        except Exception:
            pass
        await asyncio.sleep(1)

    await asyncio.sleep(max(2, dwell_seconds))

    try:
        message = (await page.locator("#datepicker-message").inner_text(timeout=1000)).strip()
        message = " ".join(message.split())
    except Exception:
        message = last_message

    try:
        available_dates = await page.locator(".greenday").count()
    except Exception:
        available_dates = 0

    try:
        submit_enabled = await page.locator("#submitbtn").is_enabled()
    except Exception:
        submit_enabled = False

    status = {
        "status": "city_checked",
        "mode": "contribution_only",
        "city": city,
        "city_value": city_value,
        "message": message,
        "available_date_count": available_dates,
        "submit_enabled": submit_enabled,
        "url": page.url,
    }
    _write_state(customer, status)
    log.info(f"{city}: {message or 'no message'} | green dates: {available_dates}")
    return status


async def run(
    cdp_port: int,
    customer: str,
    cities: list[str],
    dwell_seconds: int,
    cycles: int,
    max_rotations: int,
    min_gap_seconds: int,
    max_gap_seconds: int,
) -> None:
    async with async_playwright() as pw:
        browser, context, page = await connect_to_chrome(pw, cdp_port, log, handle_dialogs=True)

        if not await go_to_ofc_page(page, customer):
            sys.exit(1)

        await contribute_from_page(page, customer, cities, dwell_seconds, cycles, max_rotations, min_gap_seconds, max_gap_seconds)


async def contribute_from_page(
    page: Page,
    customer: str,
    cities: list[str],
    dwell_seconds: int,
    cycles: int,
    max_rotations: int,
    min_gap_seconds: int,
    max_gap_seconds: int,
) -> None:
    cycle_no = 0
    rotation_count = 0
    consecutive_failures = 0
    while cycles <= 0 or cycle_no < cycles:
        cycle_no += 1
        log.info(f"Starting contribution cycle {cycle_no}.")
        for city in cities:
            if max_rotations > 0 and rotation_count >= max_rotations:
                _write_state(customer, {
                    "status": "rotation_limit_reached",
                    "mode": "contribution_only",
                    "rotation_count": rotation_count,
                    "max_rotations": max_rotations,
                })
                log.info(f"Rotation limit reached: {rotation_count}/{max_rotations}")
                return
            try:
                await rotate_city(page, customer, city, dwell_seconds)
                rotation_count += 1
                consecutive_failures = 0  # reset failures on success
                _write_state(customer, {
                    "status": "rotation_count_updated",
                    "mode": "contribution_only",
                    "city": city,
                    "rotation_count": rotation_count,
                    "max_rotations": max_rotations,
                })
                log.info(f"Rotation count for {customer}: {rotation_count}/{max_rotations or 'unlimited'}")
                gap = random.uniform(min_gap_seconds, max_gap_seconds)
                log.info(f"Waiting {gap:.1f}s before next rotation.")
                await asyncio.sleep(gap)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                consecutive_failures += 1
                exc_str = str(exc).lower()

                # Get page url safely to prevent nested crash if browser is closed/crashed
                url = "unknown (page/browser closed)"
                try:
                    if not page.is_closed():
                        url = page.url
                except Exception:
                    pass

                log.error(f"City rotation failed for {city} (consecutive failures: {consecutive_failures}): {exc}", exc_info=True)
                _write_state(customer, {
                    "status": "city_failed",
                    "city": city,
                    "error": str(exc),
                    "url": url,
                })

                # Check if we should exit with 429 cooldown
                is_rate_limited = "429" in exc_str or "too many requests" in exc_str or "access denied" in exc_str
                is_closed = page.is_closed() or "closed" in exc_str or "target closed" in exc_str
                
                if is_rate_limited:
                    log.error(f"Rate limiting or block detected (429/Access Denied) for {customer}. Exiting with code 42 to signal restart cooldown.")
                    sys.exit(42)
                elif is_closed:
                    log.error(f"Page or browser context was closed for {customer}. Exiting with code 42 to signal restart cooldown.")
                    sys.exit(42)
                elif consecutive_failures >= 3:
                    log.error(f"Too many consecutive failures ({consecutive_failures}) for {customer}. Exiting with code 42 to signal restart cooldown.")
                    sys.exit(42)

                await asyncio.sleep(10)

    _write_state(customer, {
        "status": "complete",
        "mode": "contribution_only",
        "cycles": cycles,
        "rotation_count": rotation_count,
        "max_rotations": max_rotations,
    })
    log.info("Contribution cycles complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Rotate OFC cities for contribution-only sessions.")
    parser.add_argument("--cdp-port", type=int, default=9222)
    parser.add_argument("--customer", default="default")
    parser.add_argument("--cities", default=",".join(DEFAULT_CITY_ORDER))
    parser.add_argument("--dwell-seconds", type=int, default=12)
    parser.add_argument("--cycles", type=int, default=0, help="0 means run forever")
    parser.add_argument("--max-rotations", type=int, default=0, help="0 means no rotation limit")
    parser.add_argument("--min-gap-seconds", type=int, default=15)
    parser.add_argument("--max-gap-seconds", type=int, default=20)
    parser.add_argument("--saved-session-only", action="store_true", help="Only use existing session; exit if OFC is not already accessible")
    parser.add_argument("--saved-session-wait-seconds", type=int, default=600, help="Seconds to wait for queue/waiting room during saved-session check")
    args = parser.parse_args()

    cities = parse_cities(args.cities)
    log.info(f"Contribution-only mode for {args.customer}: {', '.join(cities)}")
    if args.saved_session_only:
        async def saved_session_main() -> None:
            async with async_playwright() as pw:
                browser, context, page = await connect_to_chrome(pw, args.cdp_port, log, handle_dialogs=True)
                if not await try_saved_session_ofc_page(page, args.customer, args.saved_session_wait_seconds):
                    sys.exit(3)
                await contribute_from_page(
                    page,
                    args.customer,
                    cities,
                    args.dwell_seconds,
                    args.cycles,
                    args.max_rotations,
                    args.min_gap_seconds,
                    max(args.min_gap_seconds, args.max_gap_seconds),
                )

        asyncio.run(saved_session_main())
    else:
        asyncio.run(run(
            args.cdp_port,
            args.customer,
            cities,
            args.dwell_seconds,
            args.cycles,
            args.max_rotations,
            args.min_gap_seconds,
            max(args.min_gap_seconds, args.max_gap_seconds),
        ))


if __name__ == "__main__":
    main()
