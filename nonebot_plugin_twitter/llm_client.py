"""One HTTP attempt; retry budgets are managed by the persisted pipeline."""

import asyncio
from dataclasses import dataclass, field
from urllib.parse import quote, urlparse, urlunparse

import httpx


class LLMError(ValueError):
    """Safe error category, without URLs, headers, keys or response bodies."""

    def __init__(self, code, *, details=None):
        super().__init__(code)
        self.details = details or {}


@dataclass
class Route:
    url: str
    key: str = field(repr=False)
    model: str = ""

    @classmethod
    def from_config(cls, config):
        if config is None:
            return None
        def value(name):
            val = config.get(name, "") if isinstance(config, dict) else getattr(config, name, "")
            return val.get_secret_value() if hasattr(val, "get_secret_value") else str(val)
        route = cls(value("url").strip().rstrip("/"), value("key"), value("model").strip())
        return route if route.url and route.key and route.model else None

    @property
    def native_gemini(self):
        url = urlparse(self.url)
        return (url.hostname == "generativelanguage.googleapis.com" and "/openai" not in url.path) or url.path.endswith(":generateContent")

    @property
    def provider(self):
        name = self.model.lower()
        host = (urlparse(self.url).hostname or "").lower()
        if self.native_gemini or "gemini" in name:
            return "gemini"
        if "deepseek" in name or host == "api.deepseek.com":
            return "deepseek"
        return "other"

    def endpoint(self):
        url = urlparse(self.url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise LLMError("invalid_endpoint")
        path = url.path.rstrip("/")
        if self.native_gemini:
            prefix = path.split("/models/")[0] if "/models/" in path else path or "/v1beta"
            path = f"{prefix}/models/{quote(self.model.removeprefix('models/'), safe='')}:generateContent"
        elif not path.endswith("/chat/completions"):
            path = (path or "/v1") + "/chat/completions"
        return urlunparse(url._replace(path=path))


async def request_translation(route: Route, system: str, prompt: str, *, timeout=60, proxy=None, transport=None) -> str:
    endpoint = route.endpoint()
    if route.native_gemini:
        headers = {"x-goog-api-key": route.key}
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}
    else:
        headers = {"Authorization": f"Bearer {route.key}"}
        body = {"model": route.model, "messages": [{"role": "system", "content": system},
                {"role": "user", "content": prompt}], "response_format": {"type": "json_object"}, "stream": False}
    try:
        async with httpx.AsyncClient(timeout=timeout, proxy=proxy, transport=transport) as client:
            response = await asyncio.wait_for(client.post(endpoint, headers=headers, json=body), timeout=timeout)
        if not response.is_success:
            raise LLMError(f"http_{response.status_code}")
        if len(response.content) > 2 * 1024 * 1024:
            raise LLMError("response_too_large")
        data = response.json()
        if route.native_gemini:
            candidate = data["candidates"][0]
            if candidate.get("finishReason") not in {None, "STOP"}:
                raise LLMError("incomplete_response")
            text = "".join(p.get("text", "") for p in candidate["content"]["parts"] if not p.get("thought", False))
        else:
            choice = data["choices"][0]
            if choice.get("finish_reason") not in {None, "stop"}:
                raise LLMError("incomplete_response")
            text = choice["message"]["content"]
        if not isinstance(text, str) or not text.strip():
            raise LLMError("empty_response")
        return text
    except (httpx.TimeoutException, asyncio.TimeoutError):
        raise LLMError("timeout") from None
    except httpx.HTTPError:
        raise LLMError("network_error") from None
    except (KeyError, IndexError, TypeError, ValueError) as error:
        if isinstance(error, LLMError):
            raise
        raise LLMError("invalid_response") from None
