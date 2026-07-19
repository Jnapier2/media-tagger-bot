from __future__ import annotations

import hashlib
import logging
import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode

import requests

from .cache import JsonCache
from .rate_limit import RateLimiter
from .utils import redact_sensitive_text

LOG = logging.getLogger(__name__)
TRANSIENT_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}
_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$")


def normalize_isrc(value: Any) -> str | None:
    normalized = "".join(ch for ch in str(value or "").upper() if ch.isalnum())
    return normalized if _ISRC_RE.fullmatch(normalized) else None


class ApiClientBase:
    def __init__(
        self,
        cache: JsonCache,
        namespace: str,
        user_agent: str,
        timeout_seconds: int,
        min_interval_seconds: float,
        max_retries: int = 3,
        retry_backoff_seconds: float = 2.0,
        connect_timeout_seconds: int | None = None,
        retry_jitter_seconds: float = 0.5,
    ) -> None:
        self.cache = cache
        self.namespace = namespace
        self.user_agent = user_agent
        self.timeout: tuple[float, float] = (
            max(1.0, float(connect_timeout_seconds or min(timeout_seconds, 10))),
            max(1.0, float(timeout_seconds)),
        )
        self.rate = RateLimiter(min_interval_seconds)
        self.max_retries = max(0, int(max_retries))
        self.max_attempts = self.max_retries + 1
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.retry_jitter_seconds = max(0.0, float(retry_jitter_seconds))
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        self.failure_streak = 0
        self.circuit_open_until = 0.0
        self.circuit_breaker_seconds = 300.0
        self.failure_streak_to_open = 3
        self._circuit_skip_log_count = 0
        self.metrics: dict[str, Any] = {
            "namespace": namespace,
            "cache_hits": 0,
            "cache_misses": 0,
            "requests_sent": 0,
            "successes": 0,
            "retries": 0,
            "failures": 0,
            "rate_limit_responses": 0,
            "circuit_skips": 0,
            "total_retry_wait_seconds": 0.0,
            "last_status_code": None,
            "last_error": "",
        }

    def _cache_key(self, method: str, url: str, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> str:
        normalized = f"{method.upper()}\n{url}\n{urlencode(sorted((params or {}).items()), doseq=True)}\n{urlencode(sorted((body or {}).items()), doseq=True)}"
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def request_json(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any] | list[Any] | None:
        key = self._cache_key(method, url, params=params, body=data)
        if use_cache:
            cached = self.cache.get(self.namespace, key)
            if cached is not None:
                self.metrics["cache_hits"] += 1
                return cached
            self.metrics["cache_misses"] += 1
        if time.monotonic() < self.circuit_open_until:
            self.metrics["circuit_skips"] += 1
            self._circuit_skip_log_count += 1
            if self._circuit_skip_log_count == 1 or self._circuit_skip_log_count % 500 == 0:
                remaining = max(0.0, self.circuit_open_until - time.monotonic())
                LOG.warning(
                    "%s API circuit is open; skipped %s request(s) so far (about %.0fs remaining)",
                    self.namespace,
                    self._circuit_skip_log_count,
                    remaining,
                )
            return None

        final_error = ""
        for attempt in range(1, self.max_attempts + 1):
            self.rate.wait()
            response: requests.Response | None = None
            transient = False
            try:
                self.metrics["requests_sent"] += 1
                response = self.session.request(
                    method=method.upper(),
                    url=url,
                    params=params,
                    data=data,
                    headers=headers,
                    timeout=self.timeout,
                )
                self.metrics["last_status_code"] = response.status_code
                transient = response.status_code in TRANSIENT_HTTP_STATUS
                if response.status_code == 429:
                    self.metrics["rate_limit_responses"] += 1
                if transient and attempt < self.max_attempts:
                    delay = self._retry_delay(attempt, response.headers.get("Retry-After"))
                    self.metrics["retries"] += 1
                    self.metrics["total_retry_wait_seconds"] += delay
                    LOG.warning(
                        "%s %s returned %s on attempt %s/%s; retrying in %.2fs",
                        method,
                        url,
                        response.status_code,
                        attempt,
                        self.max_attempts,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                payload = response.json()
                if use_cache:
                    self.cache.set(self.namespace, key, payload)
                self.failure_streak = 0
                self.circuit_open_until = 0.0
                self._circuit_skip_log_count = 0
                self.metrics["successes"] += 1
                self.metrics["last_error"] = ""
                return payload
            except (requests.Timeout, requests.ConnectionError) as exc:
                transient = True
                final_error = redact_sensitive_text(str(exc))
            except requests.HTTPError as exc:
                status = response.status_code if response is not None else None
                transient = bool(status in TRANSIENT_HTTP_STATUS)
                final_error = redact_sensitive_text(str(exc))
            except (ValueError, requests.RequestException) as exc:
                transient = False
                final_error = redact_sensitive_text(str(exc))
            except Exception as exc:  # fail isolated; unexpected client/runtime issue
                transient = False
                final_error = redact_sensitive_text(str(exc))

            LOG.warning(
                "API request failed (%s %s) attempt %s/%s: %s",
                method,
                url,
                attempt,
                self.max_attempts,
                final_error,
            )
            if transient and attempt < self.max_attempts:
                delay = self._retry_delay(attempt, response.headers.get("Retry-After") if response is not None else None)
                self.metrics["retries"] += 1
                self.metrics["total_retry_wait_seconds"] += delay
                time.sleep(delay)
                continue
            break

        self.metrics["failures"] += 1
        self.metrics["last_error"] = final_error
        if transient:
            self.failure_streak += 1
            if self.failure_streak >= self.failure_streak_to_open:
                self.circuit_open_until = time.monotonic() + self.circuit_breaker_seconds
                self._circuit_skip_log_count = 0
                LOG.warning(
                    "%s API circuit opened for %.0fs after repeated transient failures",
                    self.namespace,
                    self.circuit_breaker_seconds,
                )
        else:
            # A permanent 4xx or item-specific validation failure proves the provider is
            # reachable; it must not disable unrelated lookups for the rest of the library.
            self.failure_streak = 0
            self.circuit_open_until = 0.0
            self._circuit_skip_log_count = 0
        return None

    def _retry_delay(self, attempt: int, retry_after: str | None) -> float:
        parsed = _parse_retry_after(retry_after)
        if parsed is not None:
            return max(0.0, min(parsed, 900.0))
        exponential = self.retry_backoff_seconds * (2 ** max(0, attempt - 1))
        jitter = random.uniform(0.0, self.retry_jitter_seconds) if self.retry_jitter_seconds else 0.0
        return min(120.0, exponential + jitter)

    def metrics_snapshot(self) -> dict[str, Any]:
        return {
            **self.metrics,
            "connect_timeout_seconds": self.timeout[0],
            "read_timeout_seconds": self.timeout[1],
            "max_retries": self.max_retries,
            "max_attempts": self.max_attempts,
            "circuit_open": time.monotonic() < self.circuit_open_until,
            "failure_streak": self.failure_streak,
        }


class AcoustIDClient(ApiClientBase):
    BASE_URL = "https://api.acoustid.org/v2/lookup"

    def __init__(self, client_key: str, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.client_key = client_key.strip()

    @property
    def enabled(self) -> bool:
        return bool(self.client_key)

    def lookup_fingerprint(self, duration: int, fingerprint: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        data = {
            "client": self.client_key,
            "duration": int(duration),
            "fingerprint": fingerprint,
            "meta": "recordings releasegroups releases tracks compress",
            "format": "json",
        }
        payload = self.request_json("POST", self.BASE_URL, data=data)
        return payload if isinstance(payload, dict) else None


class MusicBrainzClient(ApiClientBase):
    BASE = "https://musicbrainz.org/ws/2"

    def lookup_recording(self, mbid: str) -> dict[str, Any] | None:
        mbid = (mbid or "").strip()
        if not mbid:
            return None
        url = f"{self.BASE}/recording/{mbid}"
        params = {"fmt": "json", "inc": "artist-credits+releases+release-groups+genres+tags+isrcs"}
        payload = self.request_json("GET", url, params=params)
        return payload if isinstance(payload, dict) else None

    def lookup_isrc(self, isrc: str) -> list[dict[str, Any]]:
        normalized = normalize_isrc(isrc)
        if not normalized:
            return []
        url = f"{self.BASE}/isrc/{normalized}"
        params = {"fmt": "json", "inc": "artist-credits+releases+release-groups+genres+tags+isrcs"}
        payload = self.request_json("GET", url, params=params)
        if isinstance(payload, dict):
            return [item for item in payload.get("recordings", []) if isinstance(item, dict)]
        return []

    def lookup_release_group(self, mbid: str) -> dict[str, Any] | None:
        mbid = (mbid or "").strip()
        if not mbid:
            return None
        payload = self.request_json("GET", f"{self.BASE}/release-group/{mbid}", params={"fmt": "json", "inc": "genres+tags+artist-credits"})
        return payload if isinstance(payload, dict) else None

    def lookup_artist(self, mbid: str) -> dict[str, Any] | None:
        mbid = (mbid or "").strip()
        if not mbid:
            return None
        payload = self.request_json("GET", f"{self.BASE}/artist/{mbid}", params={"fmt": "json", "inc": "genres+tags+aliases"})
        return payload if isinstance(payload, dict) else None

    def search_recording(self, artist: str | None, title: str, limit: int = 5) -> list[dict[str, Any]]:
        title = (title or "").strip()
        artist = (artist or "").strip()
        if not title:
            return []
        query_parts = [f'recording:"{_escape_lucene(title)}"']
        if artist:
            query_parts.append(f'artist:"{_escape_lucene(artist)}"')
        params = {"fmt": "json", "query": " AND ".join(query_parts), "limit": max(1, min(int(limit), 25))}
        payload = self.request_json("GET", f"{self.BASE}/recording", params=params)
        if isinstance(payload, dict):
            return [r for r in payload.get("recordings", []) if isinstance(r, dict)]
        return []


class LastFmClient(ApiClientBase):
    BASE = "https://ws.audioscrobbler.com/2.0/"

    def __init__(self, api_key: str, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.api_key = api_key.strip()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def track_get_top_tags(self, artist: str, title: str) -> list[str]:
        if not self.enabled or not artist or not title:
            return []
        params = {"method": "track.getTopTags", "artist": artist, "track": title, "api_key": self.api_key, "format": "json"}
        payload = self.request_json("GET", self.BASE, params=params)
        tags: list[str] = []
        if isinstance(payload, dict):
            tag_node = payload.get("toptags", {}).get("tag", []) if isinstance(payload.get("toptags"), dict) else []
            if isinstance(tag_node, dict):
                tag_node = [tag_node]
            for tag in tag_node:
                if isinstance(tag, dict) and tag.get("name"):
                    tags.append(str(tag["name"]))
        return tags

    def track_get_info(self, artist: str | None = None, title: str | None = None, mbid: str | None = None, autocorrect: bool = True) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        params: dict[str, Any] = {"method": "track.getInfo", "api_key": self.api_key, "format": "json", "autocorrect": 1 if autocorrect else 0}
        if mbid:
            params["mbid"] = mbid
        elif artist and title:
            params["artist"] = artist
            params["track"] = title
        else:
            return None
        payload = self.request_json("GET", self.BASE, params=params)
        return payload if isinstance(payload, dict) else None


class DiscogsClient(ApiClientBase):
    BASE = "https://api.discogs.com"

    def __init__(self, user_token: str, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.user_token = user_token.strip()
        if self.user_token:
            self.session.headers.update({"Authorization": f"Discogs token={self.user_token}"})

    @property
    def enabled(self) -> bool:
        return bool(self.user_token)

    def search_release(self, artist: str | None, title: str | None, limit: int = 3) -> list[dict[str, Any]]:
        if not self.enabled or not title:
            return []
        params: dict[str, Any] = {"type": "release", "per_page": limit, "page": 1, "release_title": title}
        if artist:
            params["artist"] = artist
        payload = self.request_json("GET", f"{self.BASE}/database/search", params=params)
        return [r for r in payload.get("results", []) if isinstance(r, dict)] if isinstance(payload, dict) else []

    def search_track(self, artist: str | None, title: str | None, limit: int = 3) -> list[dict[str, Any]]:
        if not self.enabled or not title:
            return []
        params: dict[str, Any] = {"type": "release", "per_page": max(1, min(int(limit), 10)), "page": 1, "track": title}
        if artist:
            params["artist"] = artist
        payload = self.request_json("GET", f"{self.BASE}/database/search", params=params)
        return [r for r in payload.get("results", []) if isinstance(r, dict)] if isinstance(payload, dict) else []

    def lookup_release(self, release_id: str) -> dict[str, Any] | None:
        if not self.enabled or not str(release_id or "").strip():
            return None
        payload = self.request_json("GET", f"{self.BASE}/releases/{str(release_id).strip()}")
        return payload if isinstance(payload, dict) else None


def _escape_lucene(text: str) -> str:
    # Escape the most important Lucene reserved characters used by MusicBrainz search.
    return "".join(("\\" + ch) if ch in r'+-!(){}[]^"~*?:\\/' else ch for ch in text)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    raw = value.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(raw)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None
