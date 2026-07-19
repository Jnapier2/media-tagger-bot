from __future__ import annotations

from pathlib import Path

from mediataggerbot.cache import JsonCache
from mediataggerbot.databases import ApiClientBase


class FakeResponse:
    def __init__(self, status_code: int, payload: dict, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"status={self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = 0

    def request(self, **_kwargs):
        self.calls += 1
        return self.responses.pop(0)


def test_retry_after_rate_limit_recovers_and_records_metrics(tmp_path: Path, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("mediataggerbot.databases.time.sleep", lambda delay: sleeps.append(delay))

    with JsonCache(tmp_path / "cache.sqlite3") as cache:
        client = ApiClientBase(
            cache=cache,
            namespace="test",
            user_agent="MediaTaggerBot-Test/1.0",
            timeout_seconds=30,
            connect_timeout_seconds=5,
            min_interval_seconds=0,
            max_retries=3,
            retry_backoff_seconds=0,
            retry_jitter_seconds=0,
        )
        client.session = FakeSession(
            [
                FakeResponse(429, {"error": "limited"}, {"Retry-After": "0"}),
                FakeResponse(200, {"ok": True}),
            ]
        )
        payload = client.request_json("GET", "https://example.invalid/test", use_cache=False)
        metrics = client.metrics_snapshot()

    assert payload == {"ok": True}
    assert client.session.calls == 2
    assert metrics["requests_sent"] == 2
    assert metrics["retries"] == 1
    assert metrics["rate_limit_responses"] == 1
    assert metrics["successes"] == 1
    assert metrics["failures"] == 0
    assert sleeps == [0.0]
    assert metrics["max_retries"] == 3
    assert metrics["max_attempts"] == 4
    assert metrics["connect_timeout_seconds"] == 5.0
    assert metrics["read_timeout_seconds"] == 30.0
