"""Fetch structured public tweets without importing the bot runtime."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
import re
import time
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .nitter_parser import parse_nitter
from .tweet_model import TweetView

TWEET_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
TWEET_PATH = re.compile(r"/([A-Za-z0-9_]{1,15})/status/([1-9][0-9]{0,24})/?$")
SOURCE_TIMEOUT_SECONDS = 10
SOURCE_ATTEMPT_LIMIT = 5


class SourceError(ValueError):
    """A known upstream condition, without response bodies or credentials."""


def check_source_error(response):
    if response.status_code == 429:
        raise SourceError("source_rate_limited")
    if "error-panel" not in response.text:
        return
    panel = BeautifulSoup(response.text, "html.parser").select_one(".error-panel")
    text = panel.get_text(" ", strip=True).lower() if panel else ""
    if "fully rate limited" in text:
        raise SourceError("source_rate_limited")
    if any(marker in text for marker in ("instance has no auth tokens", "could not authenticate you")):
        raise SourceError("source_auth_unavailable")


@dataclass(frozen=True)
class TweetLocation:
    username: str
    tweet_id: str

    @property
    def url(self) -> str:
        return f"https://x.com/{self.username}/status/{self.tweet_id}"


def parse_tweet_url(value: str) -> TweetLocation:
    try:
        parsed = urlparse(value.strip())
        match = TWEET_PATH.fullmatch(parsed.path)
        valid = (parsed.scheme == "https" and parsed.hostname in TWEET_HOSTS
                 and parsed.port in {None, 443} and not parsed.username and not parsed.password and match)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("invalid_tweet_url")
    return TweetLocation(*match.groups())


async def fetch_nitter_view(base: str, username: str, tweet_id: str, *, proxy=None,
                            timeout=SOURCE_TIMEOUT_SECONDS, headers=None, transport=None, source_path: Path | None = None,
                            progress=None, retry_seconds=2) -> TweetView:
    base = base.rstrip("/")
    async def report(**details):
        if progress:
            await progress(details)

    async with httpx.AsyncClient(proxy=proxy, timeout=timeout, http2=True, transport=transport,
                                 cookies={"hlsPlayback": "on"}) as client:
        for attempt in range(1, SOURCE_ATTEMPT_LIMIT + 1):
            await report(source_status="requesting", source_attempts=attempt, source_retry_at=None,
                         source_attempt_limit=SOURCE_ATTEMPT_LIMIT, source_timeout_seconds=timeout)
            retry_error = None
            try:
                # Bound the whole request, including connection and reading the body.
                # httpx's per-phase timeout alone can exceed the requested deadline.
                response = await asyncio.wait_for(client.get(f"{base}/{username}/status/{tweet_id}",
                    headers=headers or {"User-Agent": "Mozilla/5.0"}), timeout=timeout)
            except (httpx.TimeoutException, asyncio.TimeoutError):
                if attempt == SOURCE_ATTEMPT_LIMIT:
                    raise httpx.ReadTimeout("source_timeout") from None
                retry_error = "source_timeout"
            except (httpx.NetworkError, httpx.RemoteProtocolError):
                if attempt == SOURCE_ATTEMPT_LIMIT:
                    raise
                retry_error = "source_network_error"
            else:
                await report(source_status="received", source_http_status=response.status_code,
                             source_bytes=len(response.content))
                check_source_error(response)
                if (attempt < SOURCE_ATTEMPT_LIMIT and response.status_code in {502, 503, 504}
                        and "retry-after" not in response.headers):
                    retry_error = f"source_http_{response.status_code}"
                else:
                    response.raise_for_status()
                    break
            # Do not retry missing tweets/auth failures or disregard rate-limit waits.
            await report(source_status="retry_wait", source_last_error=retry_error,
                         source_retry_at=time.time() + retry_seconds)
            await asyncio.sleep(retry_seconds)
    view = parse_nitter(response.text, base, username, tweet_id)
    await report(source_status="parsed", source_retry_at=None)
    if source_path is not None:
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(response.text, "utf8")
    return view
