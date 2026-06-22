import asyncio
import logging
import os
import time

from playwright.async_api import Page

from src.auth.fastcaptcha_service import get_fastcaptcha_service


CAPTCHA_IMAGE_SELECTORS = [
    "img[src*='captcha' i]",
    "img[src*='Captcha' i]",
    "img[src*='VerifyImage' i]",
    "img[src*='CaptchaImage' i]",
    "img[alt*='captcha' i]",
    "img[class*='captcha' i]",
    "img[id*='captcha' i]",
    ".captcha img",
    "#captcha img",
]

CAPTCHA_INPUT_SELECTORS = [
    "#extension_atlasCaptchaResponse",
    "#CaptchaInputText",
    "#captchaText",
    "input[name*='captcha' i]",
    "input[id*='captcha' i]",
    "input[class*='captcha' i]",
    "input[placeholder*='captcha' i]",
    "input[placeholder*='code' i]",
]

# Check if FastCaptcha should be used (disabled by default, enable via env var)
USE_FASTCAPTCHA = os.getenv("USE_FASTCAPTCHA", "false").lower() in ("true", "1", "yes")


async def _first_visible_selector(page: Page, selectors: list[str]) -> str | None:
    for selector in selectors:
        loc = page.locator(selector)
        try:
            if await loc.count() > 0 and await loc.first.is_visible():
                return selector
        except Exception:
            continue
    return None


async def solve_captcha_with_fastcaptcha(
    page: Page,
    log: logging.Logger,
    timeout_seconds: int = 180,
) -> bool:
    """Solve CAPTCHA using FastCaptcha API.
    
    Args:
        page: Playwright page object
        log: Logger instance
        timeout_seconds: Timeout for solving in seconds
        
    Returns:
        True if CAPTCHA was solved, False otherwise
    """
    try:
        service = get_fastcaptcha_service()
        log.info("Attempting to solve CAPTCHA using FastCaptcha API...")
        
        success = await service.solve_page_captcha(
            page=page,
            image_selectors=CAPTCHA_IMAGE_SELECTORS,
            input_selectors=CAPTCHA_INPUT_SELECTORS,
            timeout=timeout_seconds,
        )
        
        if success:
            log.info("CAPTCHA solved successfully with FastCaptcha")
            await asyncio.sleep(2)  # Wait for verification
            return True
        else:
            log.warning("FastCaptcha failed to solve CAPTCHA, falling back to manual")
            return False
            
    except Exception as e:
        log.warning(f"FastCaptcha error: {e}, falling back to manual solving")
        return False


async def solve_captcha_on_page(
    page: Page,
    api_key: str,
    log: logging.Logger,
    timeout_seconds: int = 180,
    use_fastcaptcha: bool = False,
) -> bool:
    """Solve CAPTCHA on page - automatically or manually.

    Args:
        page: Playwright page object
        api_key: API key (kept for compatibility)
        log: Logger instance
        timeout_seconds: Timeout for solving in seconds
        use_fastcaptcha: If True, try FastCaptcha first; if False, use manual solving
        
    Returns:
        True if CAPTCHA was solved, False otherwise
    """
    image_selector = await _first_visible_selector(page, CAPTCHA_IMAGE_SELECTORS)
    input_selector = await _first_visible_selector(page, CAPTCHA_INPUT_SELECTORS)

    if not image_selector and not input_selector:
        log.info("No CAPTCHA field detected.")
        return True

    # Try FastCaptcha if enabled
    if use_fastcaptcha or USE_FASTCAPTCHA:
        log.info("FastCaptcha enabled, attempting automatic CAPTCHA solving...")
        success = await solve_captcha_with_fastcaptcha(page, log, timeout_seconds)
        if success:
            return True
        # Fall through to manual solving if FastCaptcha failed

    # Manual solving fallback
    log.info("Using manual CAPTCHA solving. Please solve it in the browser.")
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        try:
            if input_selector:
                value = await page.locator(input_selector).first.input_value(timeout=1000)
                if value.strip():
                    log.info("CAPTCHA field has been filled.")
                    return True

            url = page.url.lower()
            if not any(k in url for k in ["login", "logon", "signin", "b2clogin"]):
                log.info("Page moved forward after CAPTCHA.")
                return True
        except Exception:
            pass

        await asyncio.sleep(1)

    log.error("Timed out waiting for manual CAPTCHA completion.")
    return False
