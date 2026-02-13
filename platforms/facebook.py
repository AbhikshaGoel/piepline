"""
platforms/facebook.py - Post to Facebook Page via Graph API.
Posts: text + link OR photo + caption + link.
"""
import requests
from platforms.base import BasePlatform, PostResult
import config


class FacebookPlatform(BasePlatform):
    API = "https://graph.facebook.com/v19.0"

    def __init__(self):
        super().__init__("facebook")
        self.page_id = config.FACEBOOK_PAGE_ID
        self.token   = config.FACEBOOK_ACCESS_TOKEN

    def validate_credentials(self) -> bool:
        try:
            r = requests.get(f"{self.API}/me",
                             params={"access_token": self.token}, timeout=10)
            data = r.json()
            if "error" in data:
                self.log.error(f"FB auth error: {data['error']['message']}")
                return False
            self.log.info(f"✅ Facebook: authenticated as {data.get('name')}")
            return True
        except Exception as e:
            self.log.error(f"Facebook credential check failed: {e}")
            return False

    def post_text(self, text: str, link: str = "") -> PostResult:
        payload = {"message": text, "access_token": self.token}
        if link:
            payload["link"] = link
        try:
            r = requests.post(f"{self.API}/{self.page_id}/feed",
                              data=payload, timeout=30)
            data = r.json()
            if "error" in data:
                return PostResult(False, "facebook",
                                  error_message=data["error"]["message"])
            post_id = data.get("id", "")
            self.log.info(f"✅ Facebook text posted: {post_id}")
            return PostResult(True, "facebook", post_id,
                              f"https://facebook.com/{post_id}")
        except Exception as e:
            return PostResult(False, "facebook", error_message=str(e))

    def post_image(self, text: str, image_path: str, link: str = "") -> PostResult:
        payload = {"message": f"{text}\n\n{link}" if link else text,
                   "access_token": self.token}
        try:
            with open(image_path, "rb") as img:
                r = requests.post(f"{self.API}/{self.page_id}/photos",
                                  data=payload,
                                  files={"source": img},
                                  timeout=60)
            data = r.json()
            if "error" in data:
                return PostResult(False, "facebook",
                                  error_message=data["error"]["message"])
            post_id = data.get("post_id", data.get("id", ""))
            self.log.info(f"✅ Facebook image posted: {post_id}")
            return PostResult(True, "facebook", post_id,
                              f"https://facebook.com/{post_id}")
        except Exception as e:
            return PostResult(False, "facebook", error_message=str(e))
