"""
FastCaptcha API Service - Automated CAPTCHA solving integration.

This module provides integration with FastCaptcha API for automatically
solving CAPTCHAs encountered during visa booking automation. zuie

API Documentation: https://fastcaptcha.io/api/docs
"""

import asyncio
import base64
import io
import json
import logging
import os
import time
from typing import Optional

import requests
from PIL import Image
from playwright.async_api import Page


class FastCaptchaService:
    """Service for solving CAPTCHAs using FastCaptcha API."""

    BASE_URL = "https://api.fastcaptcha.io/v1"
    
    # Supported CAPTCHA types
    IMAGE_CAPTCHA = "ImageCaptcha"
    RECAPTCHA_V2 = "NoCaptchaTaskProxyless"
    RECAPTCHA_V3 = "RecaptchaV3TaskProxyless"
    HCAPTCHA = "HCaptchaTaskProxyless"

    def __init__(self, api_key: Optional[str] = None):
        """Initialize FastCaptcha service.
        
        Args:
            api_key: FastCaptcha API key. If None, reads from FASTCAPTCHA_API_KEY env var.
        """
        self.api_key = api_key or os.getenv("FASTCAPTCHA_API_KEY")
        if not self.api_key:
            raise ValueError(
                "FastCaptcha API key not provided. "
                "Set FASTCAPTCHA_API_KEY environment variable or pass api_key parameter."
            )
        self.logger = logging.getLogger(__name__)

    def _make_request(
        self,
        endpoint: str,
        method: str = "POST",
        data: Optional[dict] = None,
        timeout: int = 30,
    ) -> dict:
        """Make HTTP request to FastCaptcha API.
        
        Args:
            endpoint: API endpoint (e.g., 'create', 'result')
            method: HTTP method
            data: Request payload
            timeout: Request timeout in seconds
            
        Returns:
            API response as dictionary
            
        Raises:
            requests.RequestException: If API request fails
        """
        url = f"{self.BASE_URL}/{endpoint}"
        headers = {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
        }

        try:
            response = requests.request(
                method=method,
                url=url,
                json=data,
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            self.logger.error(f"FastCaptcha API request failed: {e}")
            raise

    def solve_image_captcha(
        self,
        image_data: bytes,
        captcha_type: str = IMAGE_CAPTCHA,
        timeout: int = 180,
        poll_interval: float = 2.0,
    ) -> Optional[str]:
        """Solve image CAPTCHA.
        
        Args:
            image_data: CAPTCHA image as bytes
            captcha_type: Type of CAPTCHA (default: ImageCaptcha)
            timeout: Maximum time to wait for solution in seconds
            poll_interval: Time between status checks in seconds
            
        Returns:
            CAPTCHA solution string, or None if failed
        """
        try:
            # Encode image to base64
            image_base64 = base64.b64encode(image_data).decode("utf-8")
            
            # Create task
            create_data = {
                "clientKey": self.api_key,
                "task": {
                    "type": captcha_type,
                    "body": image_base64,
                },
            }
            
            self.logger.debug("Creating FastCaptcha task for image CAPTCHA")
            create_response = self._make_request("create", data=create_data)
            
            if not create_response.get("taskId"):
                self.logger.error(
                    f"Failed to create FastCaptcha task: {create_response}"
                )
                return None
            
            task_id = create_response["taskId"]
            self.logger.info(f"FastCaptcha task created: {task_id}")
            
            # Poll for result
            start_time = time.time()
            while time.time() - start_time < timeout:
                result_data = {
                    "clientKey": self.api_key,
                    "taskId": task_id,
                }
                
                result_response = self._make_request("getResult", data=result_data)
                
                if result_response.get("status") == "ready":
                    solution = result_response.get("solution", {}).get("text")
                    if solution:
                        self.logger.info(f"CAPTCHA solved successfully: {task_id}")
                        return solution
                    else:
                        self.logger.error("No solution in ready response")
                        return None
                
                if result_response.get("errorId"):
                    self.logger.error(
                        f"FastCaptcha error: {result_response.get('errorDescription')}"
                    )
                    return None
                
                await_time = min(poll_interval, timeout - (time.time() - start_time))
                time.sleep(await_time)
            
            self.logger.error(f"FastCaptcha timeout after {timeout}s")
            return None
            
        except Exception as e:
            self.logger.error(f"Error solving image CAPTCHA: {e}", exc_info=True)
            return None

    async def solve_image_captcha_async(
        self,
        image_data: bytes,
        captcha_type: str = IMAGE_CAPTCHA,
        timeout: int = 180,
        poll_interval: float = 2.0,
    ) -> Optional[str]:
        """Async wrapper for solve_image_captcha.
        
        Args:
            image_data: CAPTCHA image as bytes
            captcha_type: Type of CAPTCHA
            timeout: Maximum time to wait for solution
            poll_interval: Time between status checks
            
        Returns:
            CAPTCHA solution string, or None if failed
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self.solve_image_captcha,
            image_data,
            captcha_type,
            timeout,
            poll_interval,
        )

    async def extract_captcha_image(
        self,
        page: Page,
        image_selectors: list[str],
        timeout: int = 10,
    ) -> Optional[bytes]:
        """Extract CAPTCHA image from page.
        
        Args:
            page: Playwright page object
            image_selectors: List of CSS selectors to try
            timeout: Timeout for finding image in milliseconds
            
        Returns:
            Image data as bytes, or None if not found
        """
        for selector in image_selectors:
            try:
                locator = page.locator(selector)
                if await locator.count() > 0:
                    element = locator.first
                    if await element.is_visible(timeout=timeout):
                        # Get screenshot of just the CAPTCHA image
                        image_bytes = await element.screenshot()
                        self.logger.debug(f"Extracted CAPTCHA image using selector: {selector}")
                        return image_bytes
            except Exception as e:
                self.logger.debug(f"Failed to extract image with selector '{selector}': {e}")
                continue
        
        self.logger.warning("Could not find CAPTCHA image on page")
        return None

    async def find_captcha_input(
        self,
        page: Page,
        input_selectors: list[str],
        timeout: int = 5,
    ) -> Optional[str]:
        """Find CAPTCHA input field selector on page.
        
        Args:
            page: Playwright page object
            input_selectors: List of CSS selectors to try
            timeout: Timeout for finding input in milliseconds
            
        Returns:
            CSS selector of input field, or None if not found
        """
        for selector in input_selectors:
            try:
                locator = page.locator(selector)
                if await locator.count() > 0:
                    element = locator.first
                    if await element.is_visible(timeout=timeout):
                        self.logger.debug(f"Found CAPTCHA input using selector: {selector}")
                        return selector
            except Exception as e:
                self.logger.debug(f"Failed to find input with selector '{selector}': {e}")
                continue
        
        self.logger.warning("Could not find CAPTCHA input field on page")
        return None

    async def fill_captcha_solution(
        self,
        page: Page,
        input_selector: str,
        solution: str,
        submit_selector: Optional[str] = None,
    ) -> bool:
        """Fill CAPTCHA solution into input field.
        
        Args:
            page: Playwright page object
            input_selector: CSS selector of input field
            solution: CAPTCHA solution text
            submit_selector: Optional selector of submit button to click after filling
            
        Returns:
            True if successful, False otherwise
        """
        try:
            await page.locator(input_selector).first.fill(solution, timeout=5000)
            self.logger.info(f"Filled CAPTCHA solution into {input_selector}")
            
            if submit_selector:
                await page.locator(submit_selector).first.click(timeout=5000)
                self.logger.info(f"Clicked submit button: {submit_selector}")
                # Wait for page navigation or CAPTCHA verification
                await asyncio.sleep(2)
            
            return True
        except Exception as e:
            self.logger.error(f"Failed to fill CAPTCHA solution: {e}")
            return False

    async def solve_page_captcha(
        self,
        page: Page,
        image_selectors: list[str],
        input_selectors: list[str],
        submit_selector: Optional[str] = None,
        timeout: int = 180,
    ) -> bool:
        """Complete CAPTCHA solving workflow on page.
        
        Args:
            page: Playwright page object
            image_selectors: List of selectors to find CAPTCHA image
            input_selectors: List of selectors to find CAPTCHA input
            submit_selector: Optional selector of submit button
            timeout: Total timeout for entire process
            
        Returns:
            True if CAPTCHA was solved, False otherwise
        """
        start_time = time.time()
        
        try:
            # Find CAPTCHA image
            self.logger.info("Attempting to extract CAPTCHA image...")
            image_data = await asyncio.wait_for(
                self.extract_captcha_image(page, image_selectors),
                timeout=10,
            )
            
            if not image_data:
                self.logger.warning("No CAPTCHA image found")
                return False
            
            # Find input field
            self.logger.info("Looking for CAPTCHA input field...")
            input_selector = await asyncio.wait_for(
                self.find_captcha_input(page, input_selectors),
                timeout=5,
            )
            
            if not input_selector:
                self.logger.warning("No CAPTCHA input field found")
                return False
            
            # Solve CAPTCHA
            remaining_timeout = timeout - (time.time() - start_time)
            self.logger.info(f"Sending CAPTCHA to FastCaptcha (timeout: {remaining_timeout}s)...")
            
            solution = await self.solve_image_captcha_async(
                image_data,
                timeout=int(remaining_timeout),
            )
            
            if not solution:
                self.logger.error("Failed to get CAPTCHA solution from FastCaptcha")
                return False
            
            # Fill solution
            self.logger.info(f"Filling CAPTCHA solution: {solution[:20]}...")
            success = await self.fill_captcha_solution(
                page,
                input_selector,
                solution,
                submit_selector,
            )
            
            return success
            
        except asyncio.TimeoutError:
            self.logger.error("CAPTCHA solving operation timed out")
            return False
        except Exception as e:
            self.logger.error(f"Unexpected error during CAPTCHA solving: {e}", exc_info=True)
            return False


# Global service instance
_service_instance: Optional[FastCaptchaService] = None


def get_fastcaptcha_service(api_key: Optional[str] = None) -> FastCaptchaService:
    """Get or create global FastCaptcha service instance.
    
    Args:
        api_key: Optional API key to override environment variable
        
    Returns:
        FastCaptchaService instance
    """
    global _service_instance
    if _service_instance is None:
        _service_instance = FastCaptchaService(api_key)
    return _service_instance


def reset_fastcaptcha_service() -> None:
    """Reset global service instance (useful for testing)."""
    global _service_instance
    _service_instance = None
