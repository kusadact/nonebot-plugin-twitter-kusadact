import asyncio
import base64
from contextlib import asynccontextmanager
import importlib
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
from unittest.mock import AsyncMock

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/nitter/quoted-replies.html"
URL = "https://x.com/sharpnelsound/status/2108953577679122591"


@pytest.fixture
def feature(monkeypatch):
    pkg = ModuleType("preview_feature_test")
    pkg.__path__ = [str(ROOT / "nonebot_plugin_twitter")]
    monkeypatch.setitem(sys.modules, pkg.__name__, pkg)
    return SimpleNamespace(**{name: importlib.import_module(f"{pkg.__name__}.{name}") for name in
                             ("tweet_model", "tweet_fetcher", "translation", "llm_client", "preview_server")})


@pytest.mark.parametrize("host", ["x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"])
def test_link_hosts_and_share_queries_are_canonicalized(feature, host):
    location = feature.tweet_fetcher.parse_tweet_url(f"https://{host}/xsser_w/status/2109492334429962632?s=20#media")
    assert location.username == "xsser_w"
    assert location.tweet_id == "2109492334429962632"
    assert location.url == "https://x.com/xsser_w/status/2109492334429962632"


@pytest.mark.parametrize("value", [
    "http://127.0.0.1:18645/alice/status/123", "https://x.com.evil.test/alice/status/123",
    "https://evil.test@x.com/alice/status/123", "https://x.com:444/alice/status/123",
    "https://x.com/alice", "https://x.com/alice/status/123/../../secret", "javascript:alert(1)",
])
def test_input_cannot_choose_arbitrary_fetch_targets(feature, value):
    with pytest.raises(ValueError, match="invalid_tweet_url"):
        feature.tweet_fetcher.parse_tweet_url(value)


@asynccontextmanager
async def client_for(app):
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            yield client


async def completed_job(client, status_url):
    async with asyncio.timeout(3):
        while True:
            response = await client.get(status_url)
            assert response.status_code == 200
            job = response.json()
            if job["status"] in {"completed", "failed"}:
                return job
            await asyncio.sleep(0.005)


def source_view(feature, text="Hello"):
    model = feature.tweet_model
    return model.TweetView(model.Post("101", model.Author("Alice", "alice"), [model.TextPart(text)]))


def renderer(tmp_path):
    path = tmp_path / "rendered.png"
    path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jIuEAAAAASUVORK5CYII="))
    return SimpleNamespace(render=AsyncMock(return_value=[str(path)]))


@pytest.mark.asyncio
async def test_url_to_context_translation_and_downloadable_image(feature, tmp_path):
    requests = []
    def nitter(request):
        assert request.url.path == "/sharpnelsound/status/2108953577679122591"
        assert "hlsPlayback=on" in request.headers["cookie"]
        return httpx.Response(200, text=FIXTURE.read_text())
    async def fetch(base, username, tweet_id, **kwargs):
        return await feature.tweet_fetcher.fetch_nitter_view(base, username, tweet_id,
                     transport=httpx.MockTransport(nitter), **kwargs)
    def llm(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        requests.append(payload)
        results = dict(items=[dict(id=item["id"], needs_translation=True, source_language="英语", translation="中文译文")
                              for item in payload["items"]])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(results, ensure_ascii=False)}}]})
    route = feature.llm_client.Route("https://llm.test/v1", "fake-secret", "gemini-test")
    settings = feature.preview_server.PreviewSettings("http://nitter.test", tmp_path / "previews", route)
    translator = feature.translation.Translator(tmp_path / "translations", transport=httpx.MockTransport(llm))
    render = renderer(tmp_path)
    app = feature.preview_server.create_app(settings, fetch=fetch, translator=translator, renderer=render)
    async with client_for(app) as client:
        assert (await client.get("/")).status_code == 200
        config = (await client.get("/api/config")).json()
        assert config["model"] == "gemini-test" and "fake-secret" not in json.dumps(config)
        response = await client.post("/api/previews", json={"url": URL})
        assert response.status_code == 202
        job = await completed_job(client, response.json()["status_url"])
        assert job["status"] == "completed" and job["translated_posts"] == 3
        assert job["prompt_version"] == feature.translation.PROMPT_VERSION
        assert len(requests) == 1
        assert {r["type"] for r in requests[0]["context"]["relations"]} == {"quotes", "reply_in_thread"}
        image = await client.get(job["images"][0])
        assert image.headers["content-type"] == "image/png"
        assert image.content == (tmp_path / "rendered.png").read_bytes()
        rendered = render.render.await_args.args[0]
        assert rendered.main.quote.media[0].kind == "gif"
        assert rendered.replies[1].media[0].kind == "gif" and rendered.replies[1].translation is None
        original = (await client.get(job["files"]["source.json"])).json()
        translated = (await client.get(job["files"]["translated.json"])).json()
        assert original["main"]["translation"] is None
        assert translated["main"]["translation"]["model"] == "gemini-test"
        assert (await client.get(job["images"][0].replace("tweet-01.png", "job.json"))).status_code == 404
        assert (await client.get("/api/previews/unknown")).status_code == 404
    restored = feature.preview_server.PreviewService(settings, fetch=fetch, translator=translator, renderer=render)
    assert restored.jobs[job["id"]]["status"] == "completed"
    assert "fake-secret" not in "".join(p.read_text() for p in settings.cache_dir.rglob("*.json"))


@pytest.mark.asyncio
async def test_invalid_numeric_result_retries_before_rendering(feature, tmp_path):
    calls = []
    def llm(request):
        calls.append(request)
        text = "需要两分钟" if len(calls) == 1 else "需要2分钟"
        raw = dict(items=[dict(id="101", needs_translation=True, source_language="英语", translation=text)])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(raw)}}]})
    route = feature.llm_client.Route("https://llm.test", "fake-secret", "test-model")
    settings = feature.preview_server.PreviewSettings("http://nitter.test", tmp_path / "previews", route, retry_seconds=0)
    translator = feature.translation.Translator(tmp_path / "translations", transport=httpx.MockTransport(llm))
    render = renderer(tmp_path)
    app = feature.preview_server.create_app(settings, fetch=AsyncMock(return_value=source_view(feature, "It takes 2 minutes")),
                                            translator=translator, renderer=render)
    async with client_for(app) as client:
        response = await client.post("/api/previews", json={"url": URL})
        job = await completed_job(client, response.json()["status_url"])
        assert job["status"] == "completed" and job["translation_attempts"] == 2
        assert job["last_translation_error"] == "protected_content_changed"
        assert len(calls) == 2
        render.render.assert_awaited_once()
        assert render.render.await_args.args[0].main.translation.text == "需要2分钟"


@pytest.mark.asyncio
async def test_translation_failure_history_identifies_each_rejected_post_without_raw_responses(feature, tmp_path):
    calls = []
    source = source_view(feature, "2 times")
    source.replies = [feature.tweet_model.Post("102", parts=[feature.tweet_model.TextPart("Anthropic??")])]
    def llm(request):
        calls.append(request)
        attempt = len(calls)
        data = dict(items=[dict(id="101", needs_translation=True, source_language="英语",
                               translation="两次" if attempt == 3 else "2 次"),
                          dict(id="102", needs_translation=attempt < 4, source_language="英语",
                               translation="fake-secret" if attempt == 2 else "")])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(data)}}]})
    settings = two_routes(feature, tmp_path)
    translator = feature.translation.Translator(tmp_path / "translations", transport=httpx.MockTransport(llm))
    app = feature.preview_server.create_app(settings, fetch=AsyncMock(return_value=source),
        translator=translator, renderer=renderer(tmp_path))
    async with client_for(app) as client:
        response = await client.post("/api/previews", json={"url": URL})
        job = await completed_job(client, response.json()["status_url"])
        assert job["status"] == "completed" and job["translation_attempts"] == len(calls) == 4
        errors = (await client.get(job["files"]["translation-errors.json"])).json()
        assert errors == job["translation_failures"]
        assert [item["details"] for item in errors] == [dict(post_id="102", reason="empty_translation"),
            dict(post_id="102", reason="no_chinese_text"),
            dict(post_id="101", reason="protected_content_changed", changed=["numbers"],
                 number_changes=dict(missing={"2": 1}, added={}))]
        assert job["last_translation_details"] == errors[-1]["details"]
        assert "fake-secret" not in json.dumps(job) + json.dumps(errors)


@pytest.mark.asyncio
async def test_source_timeout_exhaustion_reports_all_five_attempts_without_models(feature, tmp_path):
    calls = []
    def nitter(request):
        calls.append(request)
        raise httpx.ReadTimeout("fake-secret", request=request)
    async def fetch(base, username, tweet_id, **kwargs):
        return await feature.tweet_fetcher.fetch_nitter_view(base, username, tweet_id,
            transport=httpx.MockTransport(nitter), retry_seconds=0, **kwargs)
    translator = SimpleNamespace(translate=AsyncMock())
    render = renderer(tmp_path)
    app = feature.preview_server.create_app(two_routes(feature, tmp_path), fetch=fetch,
        translator=translator, renderer=render)
    async with client_for(app) as client:
        response = await client.post("/api/previews", json={"url": URL})
        job = await completed_job(client, response.json()["status_url"])
        assert job["status"] == "failed" and job["error"]["code"] == "source_timeout"
        assert job["source_attempts"] == job["source_attempt_limit"] == len(calls) == 5
        assert job["source_timeout_seconds"] == 10 and job["translation_attempts"] == 0
        assert "fake-secret" not in json.dumps(job)
        translator.translate.assert_not_awaited()
        render.render.assert_not_awaited()


@pytest.mark.asyncio
async def test_untranslated_mode_and_duplicate_pending_submission(feature, tmp_path):
    release = asyncio.Event()
    async def fetch(*args, **kwargs):
        await release.wait()
        return source_view(feature)
    fetch = AsyncMock(side_effect=fetch)
    translator = SimpleNamespace(translate=AsyncMock())
    settings = feature.preview_server.PreviewSettings("http://nitter.test", tmp_path / "previews")
    app = feature.preview_server.create_app(settings, fetch=fetch, translator=translator, renderer=renderer(tmp_path))
    async with client_for(app) as client:
        assert (await client.post("/api/previews", json={"url": URL})).status_code == 409
        first = await client.post("/api/previews", json={"url": URL, "translate": False})
        duplicate = await client.post("/api/previews", json={"url": URL + "?s=20", "translate": False})
        assert first.json()["id"] == duplicate.json()["id"]
        release.set()
        job = await completed_job(client, first.json()["status_url"])
        assert job["status"] == "completed" and job["translation_status"] == "disabled"
        fetch.assert_awaited_once()
        translator.translate.assert_not_awaited()


@pytest.mark.asyncio
async def test_error_page_fails_without_translating_and_external_origin_is_rejected(feature, tmp_path):
    async def fetch(base, username, tweet_id, **kwargs):
        return await feature.tweet_fetcher.fetch_nitter_view(base, username, tweet_id,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text='<div class="error-panel">Error</div>')), **kwargs)
    route = feature.llm_client.Route("https://llm.test", "fake-secret", "test")
    settings = feature.preview_server.PreviewSettings("http://nitter.test", tmp_path / "previews", route)
    translator = SimpleNamespace(translate=AsyncMock())
    render = renderer(tmp_path)
    app = feature.preview_server.create_app(settings, fetch=fetch, translator=translator, renderer=render)
    async with client_for(app) as client:
        denied = await client.post("/api/previews", headers={"Origin": "https://external.test"}, json={"url": URL})
        assert denied.status_code == 403
        assert (await client.post("/api/previews", json={"url": "https://other.test/status/123"})).status_code == 422
        response = await client.post("/api/previews", json={"url": URL})
        job = await completed_job(client, response.json()["status_url"])
        assert job["status"] == "failed" and job["error"]["code"] == "invalid_nitter_tweet"
        translator.translate.assert_not_awaited()
        render.render.assert_not_awaited()


@pytest.mark.asyncio
async def test_render_exception_contents_are_not_exposed(feature, tmp_path):
    render = SimpleNamespace(render=AsyncMock(side_effect=RuntimeError("fake-secret")))
    settings = feature.preview_server.PreviewSettings("http://nitter.test", tmp_path / "previews")
    app = feature.preview_server.create_app(settings, fetch=AsyncMock(return_value=source_view(feature)), renderer=render)
    async with client_for(app) as client:
        response = await client.post("/api/previews", json={"url": URL, "translate": False})
        job = await completed_job(client, response.json()["status_url"])
        assert job["status"] == "failed" and job["error"]["code"] == "RuntimeError"
        assert "fake-secret" not in json.dumps(job)


def two_routes(feature, tmp_path):
    return feature.preview_server.PreviewSettings(
        "http://nitter.test", tmp_path / "previews",
        feature.llm_client.Route("https://gemini.test/v1", "fake-gemini-secret", "gemini-3.8-flash"),
        fallback_route=feature.llm_client.Route("https://api.deepseek.com/v1", "fake-deepseek-secret", "deepseek-flash"),
        retry_seconds=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("success_at,expected_counts", [(1, (1, 0)), (10, (10, 0)), (11, (10, 1)), (13, (10, 3)), (None, (10, 3))])
async def test_pipeline_budgets_switch_and_stop(feature, tmp_path, success_at, expected_counts):
    settings = two_routes(feature, tmp_path)
    attempts = []
    async def translate(view, route):
        attempts.append(route.model)
        # Simulate a partial result to check that failures never enter the original PNG.
        view.main.translation = feature.tweet_model.Translation(True, "英语", "你好", route.provider, route.model)
        if len(attempts) != success_at:
            raise feature.llm_client.LLMError("http_401")
    render = renderer(tmp_path)
    app = feature.preview_server.create_app(settings, fetch=AsyncMock(return_value=source_view(feature)),
        translator=SimpleNamespace(translate=AsyncMock(side_effect=translate)), renderer=render)
    async with client_for(app) as client:
        response = await client.post("/api/previews", json={"url": URL, "mode": "pipeline"})
        job = await completed_job(client, response.json()["status_url"])
        assert job["status"] == "completed"
        assert (job["primary_attempts"], job["fallback_attempts"]) == expected_counts
        assert attempts == ["gemini-3.8-flash"] * expected_counts[0] + ["deepseek-flash"] * expected_counts[1]
        assert job["translation_attempts"] == sum(expected_counts)
        rendered = render.render.await_args.args[0]
        if success_at is None:
            assert job["translation_status"] == "original_only" and job["original_reason"] == "routes_exhausted"
            assert rendered.main.translation is None and job["translated_posts"] == 0
        else:
            assert job["translation_status"] == "translated"
            assert rendered.main.translation.model == attempts[-1] == job["model"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,model", [("gemini", "gemini-3.8-flash"), ("deepseek", "deepseek-flash")])
@pytest.mark.parametrize("failure", [False, True])
async def test_single_modes_allow_only_one_correction_on_the_selected_model(feature, tmp_path, mode, model, failure):
    async def translate(view, route, *, feedback=None):
        assert route.model == model
        if failure:
            raise feature.llm_client.LLMError("invalid_json")
        view.main.translation = feature.tweet_model.Translation(False, "中文", model=route.model)
    translator = SimpleNamespace(translate=AsyncMock(side_effect=translate))
    render = renderer(tmp_path)
    app = feature.preview_server.create_app(two_routes(feature, tmp_path),
        fetch=AsyncMock(return_value=source_view(feature)), translator=translator, renderer=render)
    async with client_for(app) as client:
        response = await client.post("/api/previews", json={"url": URL, "mode": mode})
        job = await completed_job(client, response.json()["status_url"])
        assert translator.translate.await_count == (2 if failure else 1)
        assert job["translation_attempts"] == (2 if failure else 1) and job["model"] == model
        assert job["status"] == ("failed" if failure else "completed")
        if failure:
            assert job["error"]["code"] == "invalid_json"
            render.render.assert_not_awaited()
        else:
            assert job["translation_status"] == "not_needed"
            render.render.assert_awaited_once()


@pytest.mark.asyncio
async def test_config_is_private_and_pending_jobs_are_distinct_by_mode(feature, tmp_path):
    settings = two_routes(feature, tmp_path)
    app = feature.preview_server.create_app(settings, renderer=renderer(tmp_path))
    # Keep the worker stopped so all requests remain pending for deduplication.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        config = (await client.get("/api/config")).json()
        assert [m["id"] for m in config["modes"]] == ["pipeline", "gemini", "deepseek"]
        assert [[r["attempts"] for r in m["routes"]] for m in config["modes"]] == [[10, 3], [2], [2]]
        ids = []
        for mode in ("pipeline", "gemini", "deepseek"):
            first = await client.post("/api/previews", json={"url": URL, "mode": mode})
            again = await client.post("/api/previews", json={"url": URL + "?s=20", "mode": mode})
            assert first.json()["id"] == again.json()["id"]
            ids.append(first.json()["id"])
            assert "secret" not in first.text
        assert len(set(ids)) == 3
        assert "secret" not in json.dumps(config) and "https://gemini.test" not in json.dumps(config)
        assert (await client.post("/api/previews", json={"url": URL, "mode": "unknown"})).status_code == 422
        assert (await client.post("/api/previews", json={"url": URL, "model": "unauthorized-model"})).status_code == 422
        for path in settings.cache_dir.rglob("*.json"):
            assert "secret" not in path.read_text()


@pytest.mark.asyncio
async def test_fallback_only_configuration_and_no_text_skip_requests(feature, tmp_path):
    settings = two_routes(feature, tmp_path)
    settings.route = None
    translator = SimpleNamespace(translate=AsyncMock(side_effect=feature.llm_client.LLMError("timeout")))
    app = feature.preview_server.create_app(settings, fetch=AsyncMock(return_value=source_view(feature)),
        translator=translator, renderer=renderer(tmp_path))
    async with client_for(app) as client:
        assert (await client.post("/api/previews", json={"url": URL, "mode": "gemini"})).status_code == 409
        response = await client.post("/api/previews", json={"url": URL})
        job = await completed_job(client, response.json()["status_url"])
        assert job["status"] == "completed" and job["primary_attempts"] == 0 and job["fallback_attempts"] == 3
    translator.translate.reset_mock()
    app = feature.preview_server.create_app(settings, fetch=AsyncMock(return_value=source_view(feature, "")),
        translator=translator, renderer=renderer(tmp_path))
    async with client_for(app) as client:
        response = await client.post("/api/previews", json={"url": URL, "mode": "deepseek"})
        job = await completed_job(client, response.json()["status_url"])
        assert job["status"] == "completed" and job["translation_attempts"] == 0
        translator.translate.assert_not_awaited()


def test_private_routes_file_loader_and_redacted_errors(tmp_path):
    spec = importlib.util.spec_from_file_location("preview_cli_test", ROOT / "tools/serve_tweet_preview.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    path = tmp_path / "routes.json"
    path.write_text(json.dumps({"primary": {"url": "https://llm.test/v1", "key": "fake-secret", "model": "gemini-test"}}))
    path.chmod(0o600)
    primary, fallback = cli.load_routes(path)
    assert primary.model == "gemini-test" and fallback is None
    assert "fake-secret" not in repr(primary)
    for content, permissions in [(path.read_text(), 0o644), ('{"key":"fake-secret"', 0o600),
                                 (json.dumps({"primary": {"url": "bad", "key": "fake-secret", "model": "test"}}), 0o600)]:
        path.write_text(content)
        path.chmod(permissions)
        with pytest.raises(ValueError) as error:
            cli.load_routes(path)
        assert "fake-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_assets_start_with_translation_and_layout_waits_for_both(feature, tmp_path):
    asset_started, translation_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    assets = {"http://image.test/test.png": "data:image/png;base64,test"}
    async def prepare(view, *, progress):
        asset_started.set()
        await progress(dict(total=2, completed=1, loaded=1, failed=0))
        await translation_started.wait()
        await release.wait()
        await progress(dict(total=2, completed=2, loaded=2, failed=0))
        return assets
    async def translate(view, route):
        translation_started.set()
        await asset_started.wait()
    render = renderer(tmp_path)
    render.prepare_assets = AsyncMock(side_effect=prepare)
    translator = SimpleNamespace(translate=AsyncMock(side_effect=translate))
    app = feature.preview_server.create_app(two_routes(feature, tmp_path),
        fetch=AsyncMock(return_value=source_view(feature)), translator=translator, renderer=render)
    async with client_for(app) as client:
        response = await client.post("/api/previews", json={"url": URL, "mode": "gemini"})
        async with asyncio.timeout(2):
            await asset_started.wait()
            await translation_started.wait()
            while app.state.preview_service.jobs[response.json()["id"]]["status"] != "downloading_assets":
                await asyncio.sleep(.005)
        job = (await client.get(response.json()["status_url"])).json()
        assert job["asset_completed"] == 1 and job["asset_total"] == 2
        render.render.assert_not_awaited()
        release.set()
        job = await completed_job(client, response.json()["status_url"])
        assert job["status"] == "completed" and job["asset_completed"] == 2 and job["asset_status"] == "ready"
        assert job["asset_seconds"] >= 0 and job["render_seconds"] >= 0
        render.prepare_assets.assert_awaited_once()
        assert render.render.await_args.kwargs["assets"] is assets


@pytest.mark.asyncio
async def test_failed_single_translation_cancels_asset_downloads(feature, tmp_path):
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def prepare(view, *, progress):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    async def translate(view, route, *, feedback=None):
        await started.wait()
        raise feature.llm_client.LLMError("invalid_json")
    render = renderer(tmp_path)
    render.prepare_assets = AsyncMock(side_effect=prepare)
    app = feature.preview_server.create_app(two_routes(feature, tmp_path),
        fetch=AsyncMock(return_value=source_view(feature)),
        translator=SimpleNamespace(translate=AsyncMock(side_effect=translate)), renderer=render)
    async with client_for(app) as client:
        response = await client.post("/api/previews", json={"url": URL, "mode": "gemini"})
        job = await completed_job(client, response.json()["status_url"])
        assert job["error"]["code"] == "invalid_json" and job["asset_status"] == "cancelled"
        assert cancelled.is_set()
        render.render.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_shutdown_cancels_downloads_while_waiting_for_assets(feature, tmp_path):
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def prepare(view, *, progress):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    render = renderer(tmp_path)
    render.prepare_assets = AsyncMock(side_effect=prepare)
    app = feature.preview_server.create_app(two_routes(feature, tmp_path),
        fetch=AsyncMock(return_value=source_view(feature)), renderer=render)
    async with client_for(app) as client:
        response = await client.post("/api/previews", json={"url": URL, "translate": False})
        await started.wait()
    job = app.state.preview_service.jobs[response.json()["id"]]
    assert job["status"] == "failed" and job["error"]["code"] == "interrupted"
    assert job["asset_status"] == "cancelled" and cancelled.is_set()
    render.render.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["source_rate_limited", "source_auth_unavailable", "source_network_error"])
async def test_source_failures_show_safe_specific_reasons_without_calling_models(feature, tmp_path, code):
    error = (httpx.RemoteProtocolError("fake-secret") if code == "source_network_error"
             else feature.tweet_fetcher.SourceError(code))
    translator = SimpleNamespace(translate=AsyncMock())
    render = renderer(tmp_path)
    app = feature.preview_server.create_app(two_routes(feature, tmp_path),
        fetch=AsyncMock(side_effect=error), translator=translator, renderer=render)
    async with client_for(app) as client:
        response = await client.post("/api/previews", json={"url": URL})
        job = await completed_job(client, response.json()["status_url"])
        assert job["status"] == "failed" and job["error"]["code"] == code
        assert job["source_seconds"] >= 0 and job["source_started_at"] > 0
        assert "fake-secret" not in json.dumps(job)
        translator.translate.assert_not_awaited()
        render.render.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_retry_progress_is_exposed_in_job_and_render_runs_once(feature, tmp_path):
    calls = []
    def nitter(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("fake-secret", request=request)
        return httpx.Response(200, text=FIXTURE.read_text())
    async def fetch(base, username, tweet_id, **kwargs):
        return await feature.tweet_fetcher.fetch_nitter_view(base, username, tweet_id,
            transport=httpx.MockTransport(nitter), retry_seconds=0, **kwargs)
    render = renderer(tmp_path)
    app = feature.preview_server.create_app(two_routes(feature, tmp_path), fetch=fetch, renderer=render)
    async with client_for(app) as client:
        response = await client.post("/api/previews", json={"url": URL, "translate": False})
        job = await completed_job(client, response.json()["status_url"])
        assert job["status"] == "completed" and job["source_status"] == "parsed"
        assert job["source_attempts"] == 2 and job["source_http_status"] == 200
        assert job["source_last_error"] == "source_network_error" and job["source_seconds"] >= 0
        assert len(calls) == 2
        render.render.assert_awaited_once()
