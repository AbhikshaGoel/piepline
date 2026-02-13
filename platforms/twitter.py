"""
platforms/twitter.py - Post to Twitter / X via API v2.
Requires: requests-oauthlib (pip install requests-oauthlib)
Falls back gracefully if library not installed.
Tweet = title (trimmed to 240 chars) + link.
"""
import requests
from platforms.base import BasePlatform, PostResult
import config


class TwitterPlatform(BasePlatform):
    TWEET_URL = "https://api.twitter.com/2/tweets"

    def __init__(self):
        super().__init__("twitter")
        self._session = None
        self._ok      = False
        self._setup()

    def _setup(self):
        try:
            from requests_oauthlib import OAuth1Session
            self._session = OAuth1Session(
                client_key=config.TWITTER_API_KEY,
                client_secret=config.TWITTER_API_SECRET,
                resource_owner_key=config.TWITTER_ACCESS_TOKEN,
                resource_owner_secret=config.TWITTER_ACCESS_SECRET,
            )
            self._ok = True
        except ImportError:
            self.log.warning("⚠️  requests-oauthlib not installed — Twitter disabled")
        except Exception as e:
            self.log.error(f"Twitter setup error: {e}")

    def validate_credentials(self) -> bool:
        if not self._ok:
            return False
        try:
            r = self._session.get(
                "https://api.twitter.com/2/users/me", timeout=10
            )
            data = r.json()
            if "data" in data:
                self.log.info(f"✅ Twitter: @{data['data'].get('username')}")
                return True
            self.log.error(f"Twitter auth failed: {data}")
            return False
        except Exception as e:
            self.log.error(f"Twitter credential check failed: {e}")
            return False

    def post_text(self, text: str, link: str = "") -> PostResult:
        if not self._ok:
            return PostResult(False, "twitter", error_message="Twitter not configured")

        # Build tweet (280 char limit)
        tweet = text
        if link:
            # Keep 24 chars for URL + 1 space
            max_text = 280 - 25
            if len(tweet) > max_text:
                tweet = tweet[:max_text - 1] + "…"
            tweet = f"{tweet} {link}"
        else:
            if len(tweet) > 280:
                tweet = tweet[:279] + "…"

        try:
            r = self._session.post(
                self.TWEET_URL,
                json={"text": tweet},
                timeout=30,
            )
            data = r.json()
            if r.status_code == 429:
                return PostResult(False, "twitter",
                                  error_message="Rate limited (429)")
            if "data" not in data:
                return PostResult(False, "twitter",
                                  error_message=str(data))
            tweet_id = data["data"]["id"]
            self.log.info(f"✅ Twitter posted: {tweet_id}")
            return PostResult(True, "twitter", tweet_id,
                              f"https://twitter.com/i/web/status/{tweet_id}")
        except Exception as e:
            return PostResult(False, "twitter", error_message=str(e))

    def post_image(self, text: str, image_path: str, link: str = "") -> PostResult:
        """Twitter media upload requires v1.1 endpoint — simplified to text+link."""
        self.log.info("ℹ️  Twitter: image upload skipped, posting text+link only")
        return self.post_text(text, link)
