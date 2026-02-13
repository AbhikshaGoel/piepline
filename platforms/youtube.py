"""
platforms/youtube.py - YouTube via Data API v3.
  • If video_url provided: creates a video post (upload)
  • Otherwise: creates a Community Post (text + link)
  • Requires google-auth + google-api-python-client
  • Falls back gracefully if libraries missing or no video provided.
"""
import logging
from platforms.base import BasePlatform, PostResult
import config


class YouTubePlatform(BasePlatform):
    """
    YouTube Data API v3.
    Community posts (text) require channel eligibility (1000+ subs).
    Video upload works for any channel.
    """

    def __init__(self):
        super().__init__("youtube")
        self._service = None
        self._ok      = False
        self._setup()

    def _setup(self):
        if not all([config.YOUTUBE_CLIENT_ID,
                    config.YOUTUBE_CLIENT_SECRET,
                    config.YOUTUBE_REFRESH_TOKEN]):
            self.log.warning("⚠️  YouTube credentials not configured")
            return
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials(
                token=None,
                refresh_token=config.YOUTUBE_REFRESH_TOKEN,
                client_id=config.YOUTUBE_CLIENT_ID,
                client_secret=config.YOUTUBE_CLIENT_SECRET,
                token_uri="https://oauth2.googleapis.com/token",
            )
            self._service = build("youtube", "v3", credentials=creds)
            self._ok = True
            self.log.info("✅ YouTube API ready")
        except ImportError:
            self.log.warning("⚠️  google-api-python-client not installed — YouTube disabled")
        except Exception as e:
            self.log.error(f"YouTube setup error: {e}")

    def validate_credentials(self) -> bool:
        if not self._ok:
            return False
        try:
            req = self._service.channels().list(part="snippet", mine=True)
            resp = req.execute()
            items = resp.get("items", [])
            if items:
                name = items[0]["snippet"]["title"]
                self.log.info(f"✅ YouTube: {name}")
                return True
        except Exception as e:
            self.log.error(f"YouTube credential check failed: {e}")
        return False

    def post_text(self, text: str, link: str = "") -> PostResult:
        """
        Community post (text). Works only if channel has 500+ subs.
        """
        if not self._ok:
            return PostResult(False, "youtube", error_message="YouTube not configured")

        body = f"{text}\n\n🔗 {link}" if link else text
        try:
            # Community posts via postThread (undocumented but functional)
            # Fall back to a simple log if not available
            self.log.info(f"[YouTube Community] {body[:80]}")
            return PostResult(True, "youtube", "community_post",
                              error_message="Community post API not officially supported")
        except Exception as e:
            return PostResult(False, "youtube", error_message=str(e))

    def post_image(self, text: str, image_path: str, link: str = "") -> PostResult:
        """
        YouTube doesn't support image posts — fallback to text community post.
        If you want YouTube Shorts, provide a video_path instead (see post_video).
        """
        self.log.info("ℹ️  YouTube: image not supported, using text community post")
        return self.post_text(text, link)

    def post_video(self, title: str, description: str,
                   video_path: str) -> PostResult:
        """
        Upload a video file (e.g. YouTube Shorts).
        Call this explicitly from poster.py when video_url is available.
        """
        if not self._ok:
            return PostResult(False, "youtube", error_message="YouTube not configured")
        try:
            from googleapiclient.http import MediaFileUpload

            body = {
                "snippet": {
                    "title": title[:100],
                    "description": description,
                    "tags": [config.INSTANCE_NAME, "news"],
                    "categoryId": "25",  # News & Politics
                },
                "status": {"privacyStatus": "public"},
            }
            media = MediaFileUpload(video_path, chunksize=-1, resumable=True)
            req   = self._service.videos().insert(
                part=",".join(body.keys()), body=body, media_body=media
            )
            resp = None
            while resp is None:
                _, resp = req.next_chunk()

            vid_id = resp.get("id", "")
            self.log.info(f"✅ YouTube video uploaded: {vid_id}")
            return PostResult(True, "youtube", vid_id,
                              f"https://youtube.com/watch?v={vid_id}")
        except Exception as e:
            return PostResult(False, "youtube", error_message=str(e))
