"""
platforms/instagram.py - Instagram Business via Graph API.
Flow: Create container → wait → Publish container.
Requires: INSTAGRAM_ACCOUNT_ID + INSTAGRAM_ACCESS_TOKEN (same Facebook token works).
Image must be a public URL. We upload dummy image to FB CDN or use image_url param.
"""
import time
import requests
from platforms.base import BasePlatform, PostResult
import config


class InstagramPlatform(BasePlatform):
    API = "https://graph.facebook.com/v19.0"

    def __init__(self):
        super().__init__("instagram")
        self.account_id = config.INSTAGRAM_ACCOUNT_ID
        self.token      = config.INSTAGRAM_ACCESS_TOKEN

    def validate_credentials(self) -> bool:
        try:
            r = requests.get(
                f"{self.API}/{self.account_id}",
                params={"fields": "name,username", "access_token": self.token},
                timeout=10,
            )
            data = r.json()
            if "error" in data:
                self.log.error(f"Instagram auth error: {data['error']['message']}")
                return False
            self.log.info(f"✅ Instagram: @{data.get('username')}")
            return True
        except Exception as e:
            self.log.error(f"Instagram credential check failed: {e}")
            return False

    def post_text(self, text: str, link: str = "") -> PostResult:
        """Instagram doesn't support text-only posts — skip gracefully."""
        self.log.info("ℹ️  Instagram: text-only post not supported, need image")
        return PostResult(False, "instagram",
                          error_message="Instagram requires image")

    def post_image(self, text: str, image_path: str, link: str = "") -> PostResult:
        """
        Instagram requires a PUBLIC image URL.
        If image_path is a local file, we need to serve it publicly.
        For simplicity, accept image_path as a URL too (str starting with http).
        """
        caption = f"{text}\n\n🔗 {link}" if link else text

        # Determine image source
        if image_path.startswith("http"):
            image_url = image_path
        else:
            # Local file — use a generic placeholder URL as fallback
            # In production, upload to your CDN / S3 first
            self.log.warning("⚠️  Instagram needs public URL, using placeholder")
            image_url = "https://placehold.co/1080x1080.jpg"

        try:
            # Step 1: Create media container
            r = requests.post(
                f"{self.API}/{self.account_id}/media",
                data={
                    "image_url":  image_url,
                    "caption":    caption[:2200],  # IG caption limit
                    "access_token": self.token,
                },
                timeout=30,
            )
            data = r.json()
            if "error" in data:
                return PostResult(False, "instagram",
                                  error_message=data["error"]["message"])
            container_id = data.get("id")

            # Step 2: Wait for container to process (usually < 3s)
            time.sleep(3)

            # Step 3: Publish
            r2 = requests.post(
                f"{self.API}/{self.account_id}/media_publish",
                data={"creation_id": container_id, "access_token": self.token},
                timeout=30,
            )
            data2 = r2.json()
            if "error" in data2:
                return PostResult(False, "instagram",
                                  error_message=data2["error"]["message"])

            post_id = data2.get("id", "")
            self.log.info(f"✅ Instagram published: {post_id}")
            return PostResult(True, "instagram", post_id)

        except Exception as e:
            return PostResult(False, "instagram", error_message=str(e))
