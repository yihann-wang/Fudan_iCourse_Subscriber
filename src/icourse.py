"""
iCourse API client for Fudan University's smart teaching platform.

Provides access to course details, lecture lists, video URLs,
and video downloads through WebVPN.
"""

import hashlib
import time
import uuid
from urllib.parse import urlparse

from . import config
from .webvpn import WebVPNSession, get_vpn_url


class ReplayNotAvailableError(RuntimeError):
    """The school has scheduled a replay but has not opened it yet."""


def _check_replay_response(data, sub_id):
    if data.get("code") == 0:
        return
    message = str(data.get("msg") or "")
    if "视频未到开放时间" in message:
        raise ReplayNotAvailableError("回放未到开放时间，开放后再试")
    raise RuntimeError(f"API error for sub-info {sub_id}: {message}")


class ICourseClient:
    """Client for the iCourse API, operating through WebVPN."""

    def __init__(self, vpn_session: WebVPNSession):
        self.vpn = vpn_session
        self.base_url = config.ICOURSE_BASE
        self._userinfo = None

    def get_userinfo(self) -> dict:
        """Get current user info (id, tenant_id, phone, account).

        Caches the result for the session.
        """
        if self._userinfo is not None:
            return self._userinfo

        url = f"{self.base_url}/userapi/v1/infosimple"
        resp = self.vpn.get(url)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") not in (0, 200):
            raise RuntimeError(f"Failed to get userinfo: {data.get('msg')}")

        self._userinfo = data.get("params") or data.get("data", {})
        return self._userinfo

    def check_alive(self) -> bool:
        """Quick session health check (non-cached)."""
        try:
            resp = self.vpn.get(
                f"{self.base_url}/userapi/v1/infosimple", timeout=10
            )
            return resp.status_code == 200 and resp.json().get("code") in (0, 200)
        except Exception:
            return False

    def sign_video_url(
        self, video_url: str, now: int | None = None
    ) -> str:
        """Sign a video URL with CDN authentication parameters.

        Adds clientUUID and t parameters required for video download.
        The t parameter format: {user_id}-{timestamp}-{md5_hash}
        where md5_hash = md5(pathname + user_id + tenant_id + reversed_phone + timestamp)
        """
        userinfo = self.get_userinfo()
        user_id = userinfo.get("id", "")
        tenant_id = userinfo.get("tenant_id", "")
        phone = str(userinfo.get("phone", ""))

        if now is None:
            now = int(time.time())

        reversed_phone = phone[::-1]
        pathname = urlparse(video_url).path

        hash_input = f"{pathname}{user_id}{tenant_id}{reversed_phone}{now}"
        md5_hash = hashlib.md5(hash_input.encode()).hexdigest()
        t_param = f"{user_id}-{now}-{md5_hash}"

        client_uuid = str(uuid.uuid4())
        sep = "&" if "?" in video_url else "?"
        return f"{video_url}{sep}clientUUID={client_uuid}&t={t_param}"

    def get_course_detail(self, course_id: str) -> dict:
        """Get course details including title, teacher, and lecture list.

        Returns dict with keys: title, teacher, lectures
        Each lecture has: sub_id, sub_title, lecturer_name, date, has_playback
        """
        url = f"{self.base_url}/courseapi/v3/multi-search/get-course-detail"
        resp = self.vpn.get(url, params={"course_id": course_id})
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(
                f"API error for course {course_id}: {data.get('msg')}"
            )

        course_data = data.get("data", {})
        title = course_data.get("title", "Unknown")
        teacher = course_data.get("realname", "Unknown")

        # Parse the nested sub_list: {year: {month: {day: [items]}}}
        lectures = []
        sub_list = course_data.get("sub_list", {})
        for year, months in sub_list.items():
            for month, days in months.items():
                for day, items in days.items():
                    for item in items:
                        if "id" in item:
                            lectures.append(
                                {
                                    "sub_id": item["id"],
                                    "sub_title": item.get("sub_title", ""),
                                    "lecturer_name": item.get(
                                        "lecturer_name", ""
                                    ),
                                    "date": f"{year}-{month}-{day}",
                                    "has_playback": str(item.get("playback_status")) == "1",
                                }
                            )

        return {"title": title, "teacher": teacher, "lectures": lectures}

    def get_course_list(
        self, term: str = "24", page: int = 1, per_page: int = 20
    ) -> dict:
        """Get a paginated list of courses for a given term.

        Returns dict with keys: total, courses (list of course dicts)
        """
        url = f"{self.base_url}/portal/courseapi/v3/multi-search/get-course-list"
        params = {
            "tenant": config.TENANT_CODE,
            "title": "",
            "term": term,
            "kkxy_code": "",
            "course_type": "",
            "course_student_type": "",
            "page": page,
            "per_page": per_page,
        }
        resp = self.vpn.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            raise RuntimeError(f"API error: {data.get('msg')}")

        result = data.get("data", {})
        return {
            "total": int(result.get("total", 0)),
            "courses": result.get("list", []),
        }

    def get_lecture_detail(self, course_id: str, sub_id: str) -> dict:
        """Get details for a specific lecture, including video URL info.

        The video URL is typically embedded in the course detail's sub_list
        items. This method retrieves the full course detail and finds the
        matching lecture by sub_id.
        """
        detail = self.get_course_detail(course_id)
        for lecture in detail["lectures"]:
            if str(lecture["sub_id"]) == str(sub_id):
                return lecture
        raise ValueError(
            f"Lecture {sub_id} not found in course {course_id}"
        )

    def get_transcript(self, sub_id: str) -> str | None:
        """Get the transcript text for a lecture.

        Returns the full transcript text, empty string if no transcript,
        or None on error.
        """
        url = f"{self.base_url}/courseapi/v3/web-socket/search-trans-result"
        resp = self.vpn.get(
            url, params={"sub_id": sub_id, "format": "json"}
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            return None

        result_list = data.get("list", [])
        if not result_list:
            return ""

        all_content = result_list[0].get("all_content", [])
        if not all_content:
            return ""

        all_content.sort(key=lambda x: x.get("BeginSec", 0))
        return " ".join(
            seg.get("Text", "") for seg in all_content if seg.get("Text")
        )

    def get_sub_detail(self, course_id: str, sub_id: str) -> dict:
        """Get detailed info for a specific lecture (unsigned URL).

        Returns the full sub-detail data from the API.
        Note: The video URL returned here is NOT signed for CDN auth.
        Use get_sub_info() instead for a signed/downloadable URL.
        """
        url = f"{self.base_url}/courseapi/v3/multi-search/get-sub-detail"
        resp = self.vpn.get(url, params={
            "course_id": course_id, "sub_id": sub_id
        })
        resp.raise_for_status()
        data = resp.json()

        _check_replay_response(data, sub_id)

        return data.get("data", {})

    def get_sub_info(self, course_id: str, sub_id: str) -> dict:
        """Get lecture info including video URLs and timestamp.

        Returns the full sub-info data from the API.
        The playurl dict maps stream indices to video URLs.
        The 'now' field provides the server timestamp for CDN signing.
        """
        url = (
            f"{self.base_url}"
            f"/courseapi/v3/portal-home-setting/get-sub-info"
        )
        resp = self.vpn.get(url, params={
            "course_id": course_id, "sub_id": sub_id
        })
        resp.raise_for_status()
        data = resp.json()

        _check_replay_response(data, sub_id)

        return data.get("data", {})

    def get_video_url(self, course_id: str, sub_id: str) -> str | None:
        """Get a signed MP4 video URL for a specific lecture.

        Uses the get-sub-info API to get the base video URL, then
        signs it with CDN authentication parameters (clientUUID, t).

        Returns the signed video URL string if found, None otherwise.
        """
        info = self.get_sub_info(course_id, sub_id)

        # Get server timestamp for signing
        now = info.get("now")
        if isinstance(now, str):
            now = int(now)

        # Extract base video URL from playurl dict or video_list
        base_url = None

        # Try video_list first (has preview_url without /0/ prefix)
        video_list = info.get("video_list", {})
        if isinstance(video_list, dict):
            for _, v in video_list.items():
                if isinstance(v, dict):
                    preview = v.get("preview_url")
                    if preview and preview.endswith(".mp4"):
                        base_url = preview
                        break

        # Fallback: try playurl dict (has /0/ prefix, may need stripping)
        if not base_url:
            playurl = info.get("playurl", {})
            if isinstance(playurl, dict):
                for k, v in playurl.items():
                    if k == "now":
                        continue
                    if isinstance(v, str) and v.endswith(".mp4"):
                        base_url = v
                        break

        # Last resort: try unsigned get-sub-detail
        if not base_url:
            try:
                detail = self.get_sub_detail(course_id, sub_id)
                content = detail.get("content", {})
                playback = content.get("playback", {})
                if playback and playback.get("url"):
                    base_url = playback["url"]
            except Exception:
                pass

        if not base_url:
            print(f"    No video URL found for {sub_id} (tried video_list, playurl, sub_detail)")
            return None

        return self.sign_video_url(base_url, now=now)

    def get_video_response(self, video_url: str, **kwargs):
        """Open a replay with the same referrer as the WebVPN player.

        The replay server rejects otherwise valid signed URLs without this
        header. The caller owns the streamed response and must close it.
        """
        headers = {"Referer": get_vpn_url(self.base_url + "/")}
        headers.update(kwargs.pop("headers", None) or {})
        kwargs.setdefault("stream", True)
        kwargs.setdefault("timeout", 300)
        getter = (self.vpn.get_raw if video_url.startswith(config.WEBVPN_BASE + "/")
                  else self.vpn.get)
        return getter(video_url, headers=headers, **kwargs)

    def get_stream_params(self, video_url: str) -> tuple[str, str]:
        """Get WebVPN URL and HTTP headers for direct streaming (e.g., ffmpeg).

        Returns:
            (vpn_url, http_headers) where http_headers is ffmpeg-compatible.
        """
        vpn_url = (video_url if video_url.startswith(config.WEBVPN_BASE + "/")
                   else get_vpn_url(video_url))
        cookies = "; ".join(
            f"{c.name}={c.value}" for c in self.vpn.session.cookies
        )
        headers = (f"Cookie: {cookies}\r\nUser-Agent: {config.USER_AGENT}\r\n"
                   f"Referer: {get_vpn_url(self.base_url + '/')}\r\n")
        return vpn_url, headers

    def download_video(
        self,
        video_url: str,
        output_path: str,
        chunk_size: int = 8192,
    ) -> str:
        """Use the same verified, resumable download path as the desktop pipeline."""
        from .video_download import download_video

        download_video(self, video_url, output_path, chunk_size=chunk_size,
                       message=lambda text: print(f"    {text}"))
        return output_path
