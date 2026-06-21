"""Submeta.io API client.

Handles authentication (REST), course discovery and video token retrieval
(GraphQL) via https://b.submeta.io.

Auth: POST /auth/login → access_token + session cookies
GraphQL: POST /api with Bearer token + cookies + X-CSRF-Protection header
Videos: Cloudflare Stream via signed JWT tokens from getVideoForWatchAuth
"""

import json
import logging
import time
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor

log = logging.getLogger(__name__)

API_URL = "https://b.submeta.io/api"
AUTH_URL = "https://b.submeta.io/auth/login"
CLOUDFLARE_DOMAIN = "customer-3j2pofw9vdbl9sfy.cloudflarestream.com"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"

HEADERS_BASE = {
    "User-Agent": USER_AGENT,
    "Origin": "https://submeta.io",
    "Referer": "https://submeta.io/",
    "Accept": "application/json",
}


class SubmetaAuthError(Exception):
    pass


class SubmetaAPIError(Exception):
    pass


class SubmetaClient:
    def __init__(self, username: str, password: str, api_url: str = API_URL,
                 cloudflare_domain: str = CLOUDFLARE_DOMAIN):
        self.username = username
        self.password = password
        self.api_url = api_url
        self.auth_url = AUTH_URL
        self.cloudflare_domain = cloudflare_domain
        self.token = None
        self._cookie_jar = CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(self._cookie_jar))

    def _request(self, url: str, data: bytes | None = None,
                 headers: dict | None = None, method: str = "GET",
                 timeout: int = 30) -> bytes:
        """Raw HTTP request with retries, backoff, and cookie-based session."""
        hdrs = {**HEADERS_BASE, **(headers or {})}
        if self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"

        for attempt in range(3):
            req = Request(url, data=data, headers=hdrs, method=method)
            try:
                resp = self._opener.open(req, timeout=timeout)
                return resp.read()
            except HTTPError as e:
                if e.code in (401, 403) and attempt == 0 and self.token:
                    log.info(f"Got {e.code}, re-authenticating...")
                    self.token = None
                    self.login()
                    hdrs["Authorization"] = f"Bearer {self.token}"
                    continue
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    wait = (attempt + 1) * 2
                    log.warning(f"HTTP {e.code}, retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                body = ""
                try:
                    body = e.read().decode()
                except Exception:
                    pass
                raise SubmetaAPIError(f"HTTP {e.code}: {body}") from e
            except Exception as e:
                if attempt < 2:
                    time.sleep((attempt + 1) * 2)
                    continue
                raise

    def _graphql(self, operation_name: str, query: str,
                 variables: dict | None = None) -> dict:
        """Execute a GraphQL query/mutation."""
        payload = {
            "operationName": operation_name,
            "query": query,
            "variables": variables or {},
        }
        data = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "X-CSRF-Protection": "1",
        }
        raw = self._request(self.api_url, data=data, headers=headers, method="POST")
        result = json.loads(raw)

        if "errors" in result and result["errors"]:
            raise SubmetaAPIError(f"GraphQL errors: {result['errors']}")
        return result.get("data", result)

    def login(self) -> str:
        """Authenticate via REST endpoint, store access token and session cookies."""
        payload = json.dumps({
            "username": self.username,
            "password": self.password,
        }).encode()
        headers = {**HEADERS_BASE, "Content-Type": "application/json"}
        req = Request(self.auth_url, data=payload, headers=headers, method="POST")

        try:
            resp = self._opener.open(req, timeout=30)
            result = json.loads(resp.read())
        except HTTPError as e:
            body = ""
            try:
                body = e.read().decode()
            except Exception:
                pass
            raise SubmetaAuthError(f"Login failed: HTTP {e.code}: {body}") from e

        token = result.get("access_token")
        if not token:
            raise SubmetaAuthError(f"No access_token in response: {result}")

        self.token = token
        user = result.get("user", {})
        log.info(f"Logged in as {user.get('username', 'unknown')} ({user.get('email', '')})")
        return token

    def discover_catalog(self) -> list[dict]:
        """Get all courses via searchCourses GraphQL query with pagination."""
        all_courses = []
        offset = 0
        limit = 50

        while True:
            data = self._graphql("SearchCourses", """
                query SearchCourses($limit: Int, $offset: Int) {
                    searchCourses(limit: $limit, offset: $offset) {
                        courses {
                            id title slug duration level category
                            authors { name handle }
                        }
                        pageInfo { hasNextPage }
                    }
                }
            """, {"limit": limit, "offset": offset})

            search = data.get("searchCourses", {})
            courses = search.get("courses", [])
            has_next = search.get("pageInfo", {}).get("hasNextPage", False)

            for c in courses:
                authors = c.get("authors", [])
                handle = authors[0]["handle"] if authors else "unknown"
                instructor = authors[0]["name"] if authors else "Unknown"
                all_courses.append({
                    "id": c["id"],
                    "title": c["title"],
                    "slug": c["slug"],
                    "instructor": instructor,
                    "handle": handle,
                    "duration": c.get("duration", 0),
                    "level": c.get("level"),
                    "category": c.get("category"),
                })

            log.info(f"Catalog page offset={offset}: {len(courses)} courses (total: {len(all_courses)})")

            if not courses or not has_next:
                break
            offset += len(courses)
            time.sleep(0.5)

        log.info(f"Catalog complete: {len(all_courses)} courses")
        return all_courses

    def get_course(self, slug: str, handle: str) -> dict:
        """Get full course structure with chapters and videos via GraphQL.

        Returns: {
            "id": str, "title": str, "instructor": str, "slug": str, "handle": str,
            "description": str, "duration": float,
            "chapters": [{"id": str, "title": str, "order": int,
                          "videos": [{"id": str, "title": str, "order": int, "duration": float}]}]
        }
        """
        data = self._graphql("GetCourse", """
            query GetCourse($slug: String, $handle: String) {
                getCourse(courseSlug: $slug, creatorHandle: $handle) {
                    course {
                        id title slug description duration level category
                        authors { name handle }
                        chapters {
                            id title order
                            contents {
                                __typename
                                ... on Video { id title duration order slug }
                            }
                        }
                    }
                    errors { key message }
                }
            }
        """, {"slug": slug, "handle": handle})

        course_result = data.get("getCourse", {})
        errors = course_result.get("errors")
        if errors:
            raise SubmetaAPIError(f"getCourse errors: {errors}")

        course = course_result.get("course")
        if not course:
            raise SubmetaAPIError(f"No course data for {handle}/{slug}")

        authors = course.get("authors", [])
        result = {
            "id": course["id"],
            "title": course.get("title", "Unknown"),
            "instructor": authors[0]["name"] if authors else "Unknown",
            "handle": authors[0]["handle"] if authors else "unknown",
            "slug": course.get("slug", slug),
            "description": course.get("description", ""),
            "duration": course.get("duration", 0),
            "level": course.get("level"),
            "category": course.get("category"),
            "chapters": [],
        }

        for chapter in course.get("chapters", []):
            ch = {
                "id": chapter.get("id", ""),
                "title": chapter.get("title", "Untitled"),
                "order": chapter.get("order", 0),
                "videos": [],
            }
            for item in chapter.get("contents", []):
                if item.get("__typename") == "Video":
                    ch["videos"].append({
                        "id": item["id"],
                        "title": item.get("title", "Untitled"),
                        "order": item.get("order", 0),
                        "duration": item.get("duration"),
                    })
            result["chapters"].append(ch)

        return result

    def discover_videos(self) -> list[dict]:
        """Get all standalone videos via searchVideos GraphQL query with pagination."""
        all_videos = []
        offset = 0
        limit = 50

        while True:
            data = self._graphql("SearchVideos", """
                query SearchVideos($limit: Int, $offset: Int) {
                    searchVideos(limit: $limit, offset: $offset) {
                        videos {
                            id title description duration
                            publishedAt insertedAt
                            tags { id term }
                            authors { name handle }
                            entities { name handle }
                        }
                        pageInfo { hasNextPage }
                    }
                }
            """, {"limit": limit, "offset": offset})

            search = data.get("searchVideos", {})
            videos = search.get("videos", [])
            has_next = search.get("pageInfo", {}).get("hasNextPage", False)

            for v in videos:
                authors = v.get("authors", [])
                handle = authors[0]["handle"] if authors else "unknown"
                instructor = authors[0]["name"] if authors else "Unknown"
                tags = [t.get("term", "") for t in v.get("tags", [])]
                entities = [e.get("name", "") for e in v.get("entities", []) if e.get("name")]
                all_videos.append({
                    "id": v["id"],
                    "title": v.get("title", "Untitled"),
                    "description": v.get("description", ""),
                    "duration": v.get("duration", 0),
                    "instructor": instructor,
                    "handle": handle,
                    "tags": tags,
                    "entities": entities,
                    "published_at": v.get("publishedAt"),
                    "inserted_at": v.get("insertedAt"),
                })

            log.info(f"Videos page offset={offset}: {len(videos)} videos (total: {len(all_videos)})")

            if not videos or not has_next:
                break
            offset += len(videos)
            time.sleep(0.5)

        log.info(f"Video catalog complete: {len(all_videos)} standalone videos")
        return all_videos

    def get_standalone_video_url(self, video_id: str) -> str:
        """Get download URL for a standalone video (isStandalone=True)."""
        data = self._graphql("GetVideoForWatchAuth", """
            query GetVideoForWatchAuth($id: ID!, $isStandalone: Boolean) {
                result: getVideoForWatchAuth(id: $id, isStandalone: $isStandalone) {
                    video { id token }
                    isAuthorized
                    errors { key message }
                }
            }
        """, {"id": video_id, "isStandalone": True})

        result = data.get("result", {})
        errors = result.get("errors")
        if errors:
            raise SubmetaAPIError(f"Video token error: {errors}")
        if not result.get("isAuthorized"):
            raise SubmetaAPIError(f"Not authorized for standalone video {video_id}")

        token = result.get("video", {}).get("token")
        if not token:
            raise SubmetaAPIError(f"No token for standalone video {video_id}: {data}")
        return f"https://{self.cloudflare_domain}/{token}/manifest/video.mpd"

    def get_video_token(self, video_id: str) -> str:
        """Get a signed Cloudflare Stream JWT token for a video."""
        data = self._graphql("GetVideoForWatchAuth", """
            query GetVideoForWatchAuth($id: ID!, $isStandalone: Boolean) {
                result: getVideoForWatchAuth(id: $id, isStandalone: $isStandalone) {
                    video { id token }
                    isAuthorized
                    errors { key message }
                }
            }
        """, {"id": video_id, "isStandalone": False})

        result = data.get("result", {})
        errors = result.get("errors")
        if errors:
            raise SubmetaAPIError(f"Video token error: {errors}")
        if not result.get("isAuthorized"):
            raise SubmetaAPIError(f"Not authorized for video {video_id}")

        token = result.get("video", {}).get("token")
        if not token:
            raise SubmetaAPIError(f"No token for video {video_id}: {data}")
        return token

    def get_video_url(self, video_id: str) -> str:
        """Get the full Cloudflare Stream MPD download URL for a video."""
        token = self.get_video_token(video_id)
        return f"https://{self.cloudflare_domain}/{token}/manifest/video.mpd"
