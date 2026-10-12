import asyncio
import importlib
from pathlib import Path
import sys
from types import ModuleType

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "tests/fixtures/nitter/quoted-replies.html").read_text()


@pytest.fixture
def fetcher(monkeypatch):
    package = ModuleType("fetcher_feature_test")
    package.__path__ = [str(ROOT / "nonebot_plugin_twitter")]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    return importlib.import_module(f"{package.__name__}.tweet_fetcher")


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError])
async def test_connection_failure_retries_once_and_saves_only_valid_source(fetcher, tmp_path, error_type):
    attempts, updates = [], []
    async def progress(details):
        updates.append(details)
    def respond(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise error_type("fake-secret", request=request)
        return httpx.Response(200, text=HTML)
    path = tmp_path / "source.html"
    view = await fetcher.fetch_nitter_view("http://nitter.test", "sharpnelsound", "2108953577679122591",
        transport=httpx.MockTransport(respond), progress=progress, source_path=path, retry_seconds=0)
    assert len(attempts) == 2 and view.main.id == "2108953577679122591"
    assert path.read_text() == HTML
    assert [u["source_status"] for u in updates] == ["requesting", "retry_wait", "requesting", "received", "parsed"]
    assert [u["source_attempts"] for u in updates if "source_attempts" in u] == [1, 2]
    assert "fake-secret" not in str(updates)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 503, 504])
async def test_temporary_gateway_failure_recovers_once(fetcher, status):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status if len(calls) == 1 else 200, text=HTML)
    await fetcher.fetch_nitter_view("http://nitter.test", "sharpnelsound", "2108953577679122591",
        transport=httpx.MockTransport(respond), retry_seconds=0)
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "body", "code"), [
    (429, "", "source_rate_limited"),
    (404, '<div class="error-panel">Instance is fully rate limited</div>', "source_rate_limited"),
    (404, '<div class="error-panel">Instance has no auth tokens</div>', "source_auth_unavailable"),
    (200, '<div class="error-panel">Could not authenticate you</div>', "source_auth_unavailable"),
])
async def test_rate_limits_and_auth_failures_are_identified_without_retry(fetcher, tmp_path, status, body, code):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, text=body)
    path = tmp_path / "source.html"
    with pytest.raises(fetcher.SourceError, match=code):
        await fetcher.fetch_nitter_view("http://nitter.test", "alice", "123",
            transport=httpx.MockTransport(respond), source_path=path, retry_seconds=0)
    assert len(calls) == 1 and not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "headers"), [(404, {}), (403, {}), (503, {"Retry-After": "60"})])
async def test_missing_access_restricted_and_server_wait_responses_are_not_retried(fetcher, status, headers):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers=headers, text="Error")
    with pytest.raises(httpx.HTTPStatusError):
        await fetcher.fetch_nitter_view("http://nitter.test", "alice", "123",
            transport=httpx.MockTransport(respond), retry_seconds=0)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_read_timeout_retries_five_times_with_ten_second_timeout(fetcher):
    calls = []
    def respond(request):
        calls.append(request)
        raise httpx.ReadTimeout("fake-secret", request=request)
    with pytest.raises(httpx.ReadTimeout):
        await fetcher.fetch_nitter_view("http://nitter.test", "alice", "123",
            transport=httpx.MockTransport(respond), retry_seconds=0)
    assert len(calls) == 5
    assert all(set(request.extensions["timeout"].values()) == {10} for request in calls)


@pytest.mark.asyncio
async def test_connection_retry_is_bounded(fetcher):
    calls = []
    def respond(request):
        calls.append(request)
        raise httpx.RemoteProtocolError("fake-secret", request=request)
    with pytest.raises(httpx.RemoteProtocolError):
        await fetcher.fetch_nitter_view("http://nitter.test", "alice", "123",
            transport=httpx.MockTransport(respond), retry_seconds=0)
    assert len(calls) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "network", "gateway"])
async def test_success_on_fifth_attempt_stops_without_a_sixth_request(fetcher, failure):
    calls, updates = [], []
    async def progress(details):
        updates.append(details)
    def respond(request):
        calls.append(request)
        if len(calls) == 5:
            return httpx.Response(200, text=HTML)
        if failure == "timeout":
            raise httpx.ReadTimeout("fake-secret", request=request)
        if failure == "network":
            raise httpx.ConnectError("fake-secret", request=request)
        return httpx.Response(503)
    await fetcher.fetch_nitter_view("http://nitter.test", "sharpnelsound", "2108953577679122591",
        transport=httpx.MockTransport(respond), progress=progress, retry_seconds=0)
    assert len(calls) == 5
    assert [u["source_attempts"] for u in updates if "source_attempts" in u] == [1, 2, 3, 4, 5]
    assert all(u["source_attempt_limit"] == 5 and u["source_timeout_seconds"] == 10
               for u in updates if "source_attempts" in u)
    assert updates[-1]["source_status"] == "parsed"


@pytest.mark.asyncio
async def test_whole_request_deadline_cancels_each_stalled_attempt(fetcher):
    calls, cancelled = [], []
    async def respond(request):
        calls.append(request)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(request)
    with pytest.raises(httpx.ReadTimeout):
        await asyncio.wait_for(fetcher.fetch_nitter_view("http://nitter.test", "alice", "123",
            transport=httpx.MockTransport(respond), timeout=0.01, retry_seconds=0), 1)
    assert len(calls) == len(cancelled) == 5


@pytest.mark.asyncio
async def test_gateway_failure_stops_after_five_attempts(fetcher):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(503)
    with pytest.raises(httpx.HTTPStatusError):
        await fetcher.fetch_nitter_view("http://nitter.test", "alice", "123",
            transport=httpx.MockTransport(respond), retry_seconds=0)
    assert len(calls) == 5


@pytest.mark.asyncio
async def test_retry_wait_can_be_cancelled_without_sending_again(fetcher):
    waiting = asyncio.Event()
    calls = []
    async def progress(details):
        if details["source_status"] == "retry_wait":
            waiting.set()
    def respond(request):
        calls.append(request)
        raise httpx.ConnectError("fake-secret", request=request)
    task = asyncio.create_task(fetcher.fetch_nitter_view("http://nitter.test", "alice", "123",
        transport=httpx.MockTransport(respond), progress=progress))
    await asyncio.wait_for(waiting.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 1
