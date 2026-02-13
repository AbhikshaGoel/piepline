"""
Facebook Page posting via Graph API.
Posts text and images to your Facebook Page.
"""

import requests
from typing import Optional

import config
from platforms.base import BasePlatform, PostResult


class FacebookPlatform(BasePlatform):
    """Facebook Page poster using Graph API."""

    API_BASE = "https://graph.facebook.com/v19.0"

    def __init__(self):
        super().__init__("facebook")
        self.page_id = config.FACEBOOK_PAGE_ID
        self.access_token = config.FACEBOOK_ACCESS_TOKEN

    def _is_dry_run(self) -> bool:
        """Safely check if DRY_RUN is enabled in config."""
        return getattr(config, "DRY_RUN", False)

    def validate_credentials(self) -> bool:
        """Verify the access token is still valid."""
        try:
            resp = requests.get(
                f"{self.API_BASE}/me",
                params={"access_token": self.access_token},
                timeout=10,
            )
            data = resp.json()
            if "error" in data:
                self.log.error(
                    f"Facebook auth error: {data['error']['message']}"
                )
                return False
            self.log.info(f"Facebook authenticated as: {data.get('name')}")
            return True
        except requests.RequestException as e:
            self.log.error(f"Facebook connection error: {e}")
            return False

    def post_text(self, text: str, link: str = "") -> PostResult:
        """Post a text update to the Facebook Page."""
        try:
            payload = {
                "message": text,
                "access_token": self.access_token,
            }
            if link:
                payload["link"] = link

            # Check Dry Run safely
            if self._is_dry_run():
                self.log.info(f"[DRY RUN] Would post to Facebook: {text[:80]}")
                return PostResult(
                    success=True,
                    platform="facebook",
                    platform_post_id="dry_run",
                )

            resp = requests.post(
                f"{self.API_BASE}/{self.page_id}/feed",
                data=payload,
                timeout=30,
            )
            data = resp.json()

            if "error" in data:
                return PostResult(
                    success=False,
                    platform="facebook",
                    error_message=data["error"]["message"],
                )

            post_id = data.get("id", "")
            self.log.info(f"Posted to Facebook: {post_id}")

            return PostResult(
                success=True,
                platform="facebook",
                platform_post_id=post_id,
                platform_url=f"https://facebook.com/{post_id}",
            )

        except requests.RequestException as e:
            return PostResult(
                success=False,
                platform="facebook",
                error_message=str(e),
            )

    def post_image(
        self, text: str, image_path: str, link: str = ""
    ) -> PostResult:
        """Post a photo with caption to the Facebook Page."""
        try:
            payload = {
                "message": text,
                "access_token": self.access_token,
            }
            if link:
                payload["message"] = f"{text}\n\n{link}"

            # Check Dry Run safely
            if self._is_dry_run():
                self.log.info(
                    f"[DRY RUN] Would post image to Facebook: {text[:80]}"
                )
                return PostResult(
                    success=True,
                    platform="facebook",
                    platform_post_id="dry_run",
                )

            with open(image_path, "rb") as img_file:
                resp = requests.post(
                    f"{self.API_BASE}/{self.page_id}/photos",
                    data=payload,
                    files={"source": img_file},
                    timeout=60,
                )

            data = resp.json()

            if "error" in data:
                return PostResult(
                    success=False,
                    platform="facebook",
                    error_message=data["error"]["message"],
                )

            post_id = data.get("post_id", data.get("id", ""))
            self.log.info(f"Posted image to Facebook: {post_id}")

            return PostResult(
                success=True,
                platform="facebook",
                platform_post_id=post_id,
                platform_url=f"https://facebook.com/{post_id}",
            )

        except (requests.RequestException, IOError) as e:
            return PostResult(
                success=False,
                platform="facebook",
                error_message=str(e),
            )