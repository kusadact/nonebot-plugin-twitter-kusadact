import asyncio
import importlib
import json
from pathlib import Path
import struct
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

from bs4 import BeautifulSoup
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/nitter/poll.html"
POST_ID = "2109152716954222849"


@pytest.fixture
def feature(monkeypatch):
    package = ModuleType("poll_feature_test")
    package.__path__ = [str(ROOT / "nonebot_plugin_twitter")]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    return SimpleNamespace(**{name: importlib.import_module(f"{package.__name__}.{name}") for name in
                             ("tweet_model", "nitter_parser", "translation", "llm_client", "renderer", "pipeline", "preview_server")})


def source(feature):
    return feature.nitter_parser.parse_nitter(FIXTURE.read_text(), "http://nitter.test", "thsottiaux", POST_ID)


def translated_response(payload):
    labels = ["5小时上限，每周额度2倍", "没有5小时上限，额度不变", "查看结果"]
    return json.dumps(dict(items=[dict(id=item["id"], needs_translation=True, source_language="英语",
        translation=labels[item["poll_option"] - 1] if "poll_option" in item else "Pro用户，你更希望采用哪种方案？")
        for item in payload["items"]]))


def translator(feature, tmp_path, requests):
    def handler(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        requests.append(payload)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": translated_response(payload)}}]})
    return feature.translation.Translator(tmp_path, transport=httpx.MockTransport(handler))


def test_real_poll_roundtrip_digest_and_text_fallback(feature):
    view = source(feature)
    poll = view.main.poll
    assert view.main.text == "Pro users, would you rather have"
    assert [option.text for option in poll.options] == ["5h limits, 2X week usage", "no 5h limits, same usage", "show result"]
    assert [option.percentage for option in poll.options] == [33, 38, 29]
    assert [option.leader for option in poll.options] == [False, True, False]
    assert poll.votes == "240,643" and poll.status == "Final results"
    digest = view.content_digest()
    poll.options[0].translation = feature.tweet_model.Translation(True, "英语", "5小时上限，每周额度2倍")
    assert view.content_digest() == digest
    restored = feature.tweet_model.TweetView.from_dict(view.to_dict())
    assert restored.to_dict() == view.to_dict()
    restored.main.poll.options[0].percentage = 34
    assert restored.content_digest() != digest
    fallback = view.legacy_fields()["text"][0]
    assert "5h limits, 2X week usage · 33%" in fallback and "240,643 票\n已结束" in fallback
    legacy = view.to_dict()
    legacy["main"].pop("poll")
    assert feature.tweet_model.TweetView.from_dict(legacy).main.poll is None


def test_quote_and_reply_polls_do_not_leak_into_main(feature):
    poll = '<div class="poll"><div class="poll-meter"><span class="poll-choice-value">50%</span><span class="poll-choice-option">Yes</span></div><span class="poll-info">2 votes • 1 hour</span></div>'
    html = f'''<div class="main-thread"><div class="main-tweet timeline-item"><div class="tweet-body">
        <div class="tweet-content">Main</div><div class="quote"><a class="quote-link" href="/bob/status/102"></a>
        <div class="quote-text">Quote</div>{poll}</div></div></div></div>
        <div class="timeline-item"><a class="tweet-link" href="/bob/status/103"></a>{poll}</div>'''
    view = feature.nitter_parser.parse_nitter(html, "http://nitter.test", "alice", "101")
    assert view.main.poll is None
    assert view.main.quote.poll.options[0].text == "Yes"
    assert view.replies[0].poll.status == "1 hour"
    soup = BeautifulSoup(feature.renderer.build_html(view), "html.parser")
    assert len(soup.select(".poll")) == 2
    assert soup.select_one(".quote-card .poll-info").text == "2 票 · 剩余 1 小时"
    assert soup.select_one(".reply .poll-original").text == "Yes"


@pytest.mark.parametrize(("value", "expected"), [("0%", 0), ("100%", 100), ("101%", None), ("-1%", None), ("NaN", None)])
def test_unknown_or_invalid_percentages_are_not_fabricated(feature, value, expected):
    html = f'''<div class="main-thread"><div class="main-tweet"><div class="poll"><div class="poll-meter">
        <span class="poll-choice-value">{value}</span><span class="poll-choice-option">Yes</span></div>
        <div class="poll-meter"><span class="poll-choice-value">50%</span></div></div></div></div>'''
    view = feature.nitter_parser.parse_nitter(html, "http://nitter.test", "alice", "101")
    assert len(view.main.poll.options) == 1 and view.main.poll.options[0].percentage == expected
    assert view.main.poll.votes == "" and view.main.poll.status == ""
    soup = BeautifulSoup(feature.renderer.build_html(view), "html.parser")
    assert bool(soup.select(".poll-percentage")) == (expected is not None)
    assert not soup.select(".poll-info")


def test_poll_items_share_context_without_sending_vote_counts_to_model(feature):
    view = source(feature)
    payload = feature.translation.build_payload(view)
    assert [item["id"] for item in payload["items"]] == [POST_ID, *[f"{POST_ID}:poll:{i}" for i in range(1, 4)]]
    assert payload["context"]["relations"] == [dict(type="poll_option_of", from_id=f"{POST_ID}:poll:{i}", to_id=POST_ID) for i in range(1, 4)]
    assert "240,643" not in json.dumps(payload) and "33%" not in json.dumps(payload)
    view.main.parts = []
    assert len(feature.translation.build_input(view)) == 3  # A poll-only post still needs translation.
    view.main.poll.options[0].text = "🤩"
    assert len(feature.translation.build_input(view)) == 2
    payload = feature.translation.build_payload(view)
    assert next(node for node in payload["context"]["posts"] if node["id"] == f"{POST_ID}:poll:1")["context_only"]
    with pytest.raises(feature.llm_client.LLMError, match="input_budget_exceeded"):
        feature.translation.build_input(view, max_chars=1)


def test_poll_validation_feedback_identifies_post_and_option(feature):
    payload = feature.translation.build_payload(source(feature))
    raw = json.loads(translated_response(payload))
    raw["items"][1]["translation"] = "五小时上限，每周额度翻倍"
    with pytest.raises(feature.llm_client.LLMError, match="protected_content_changed") as caught:
        feature.translation.validate_response(json.dumps(raw), payload["items"])
    feedback = feature.translation.validation_feedback(caught.value)
    assert feedback["post_id"] == POST_ID and feedback["poll_option"] == 1
    assert feedback["number_changes"]["missing"] == {"5": 1, "2": 1}
    raw["items"].pop()
    raw["items"][1]["translation"] = "5小时上限，每周额度2倍"
    with pytest.raises(feature.llm_client.LLMError, match="missing_ids"):
        feature.translation.validate_response(json.dumps(raw), payload["items"])


@pytest.mark.asyncio
async def test_poll_translations_are_batched_cached_and_escaped(feature, tmp_path):
    view = source(feature)
    requests = []
    client = translator(feature, tmp_path, requests)
    route = feature.llm_client.Route("https://llm.test", "test-only", "test-model")
    await client.translate(view, route)
    assert len(requests) == 1 and len(requests[0]["items"]) == 4
    restored = source(feature)
    await client.translate(restored, route)
    assert len(requests) == 1 and restored.to_dict() == view.to_dict()
    poll = restored.main.poll
    poll.options[0].text += '<img src="https://evil.test">'
    poll.options[0].translation.text += "<script>alert(1)</script>"
    soup = BeautifulSoup(feature.renderer.build_html(restored), "html.parser")
    assert len(soup.select(".poll-translated")) == 3 and len(soup.select(".poll-percentage")) == 3
    assert not soup.select(".poll img, script")
    assert '<img src="https://evil.test">' in soup.select_one(".poll-original").text
    assert "<script>" in soup.select_one(".poll-translated").text
    assert soup.select_one(".poll-info").text == "240,643 票 · 已结束"
    assert len(soup.select(".translation-label")) == 1
    restored.main.translation = None
    assert BeautifulSoup(feature.renderer.build_html(restored), "html.parser").select_one(".poll .translation-label")


def pipeline_for(feature, tmp_path, client, *, enabled=True, view=None):
    view = view or source(feature)
    state = {f"user{i}": dict(timeline_seen=["status:100"], since_id="100", group={}, private={}, pending_tweets=[
        dict(signature=f"status:{POST_ID}", tweet_id=POST_ID, source_user_name="thsottiaux", is_retweet=False)]) for i in range(2)}
    config = SimpleNamespace(twitter_template_enabled=True, twitter_translation_enabled=enabled,
        twitter_translation_primary=dict(url="https://llm.test", key="test-only", model="test-model"), twitter_translation_fallback=None)
    def update(function):
        function(state)
    worker = feature.pipeline.TweetPipeline(read_state=lambda: json.loads(json.dumps(state)), update_state=update,
        fetch=AsyncMock(side_effect=lambda *args: feature.tweet_model.TweetView.from_dict(view.to_dict())),
        translator=client, render=AsyncMock(return_value=[]), prepare=AsyncMock(return_value=dict(status=True, delivery_parts=[])),
        deliver=AsyncMock(return_value=True), config=config, cache_dir=tmp_path / "pipeline")
    return worker, state


@pytest.mark.asyncio
async def test_shared_production_translation_copies_poll_options(feature, tmp_path):
    requests = []
    worker, state = pipeline_for(feature, tmp_path, translator(feature, tmp_path / "translations", requests))
    fresh = source(feature)
    fresh.main.poll.votes = "240,650"
    fresh.main.poll.options[0].percentage = 34
    worker.fetch.side_effect = [source(feature), fresh]
    await worker.tick()
    assert len(requests) == 1 and worker.render.await_count == 2
    for call in worker.render.await_args_list:
        poll = call.args[0].main.poll
        assert poll.options[0].translation.text == "5小时上限，每周额度2倍"
    assert worker.render.await_args_list[0].args[0].main.poll.votes == "240,643"
    assert worker.render.await_args_list[1].args[0].main.poll.votes == "240,650"
    assert worker.render.await_args_list[1].args[0].main.poll.options[0].percentage == 34
    assert all(not user["pending_tweets"] for user in state.values())


@pytest.mark.asyncio
async def test_original_fallback_clears_poll_translations_but_keeps_source(feature, tmp_path):
    view = source(feature)
    route = feature.llm_client.Route("https://llm.test", "test-only", "test-model")
    payload = feature.translation.build_payload(view)
    feature.translation.apply_results(view, feature.translation.validate_response(translated_response(payload), payload["items"]), route)
    client = SimpleNamespace(timeout=60, translate=AsyncMock())
    worker, _ = pipeline_for(feature, tmp_path, client, enabled=False, view=view)
    await worker.tick()
    client.translate.assert_not_awaited()
    for call in worker.render.await_args_list:
        restored = call.args[0]
        assert all(target.translation is None for _, target in restored.translation_targets())
        assert restored.main.poll.votes == "240,643"


@pytest.mark.asyncio
async def test_preview_marks_poll_only_translation_as_translated(feature, tmp_path):
    view = source(feature)
    view.main.parts = []
    requests = []
    image = tmp_path / "image.png"
    image.write_bytes(b"test-image")
    render = SimpleNamespace(render=AsyncMock(return_value=[str(image)]))
    route = feature.llm_client.Route("https://llm.test", "test-only", "test-model")
    settings = feature.preview_server.PreviewSettings("http://nitter.test", tmp_path / "previews", route, retry_seconds=0)
    app = feature.preview_server.create_app(settings, fetch=AsyncMock(return_value=view),
        translator=translator(feature, tmp_path / "translations", requests), renderer=render)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            response = await client.post("/api/previews", json={"url": f"https://x.com/thsottiaux/status/{POST_ID}"})
            async with asyncio.timeout(3):
                while True:
                    job = (await client.get(response.json()["status_url"])).json()
                    if job["status"] in {"completed", "failed"}:
                        break
                    await asyncio.sleep(0.005)
    assert job["status"] == "completed" and job["translation_status"] == "translated"
    assert job["translated_posts"] == 1 and job["translation_attempts"] == 1
    assert len(requests[0]["items"]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [360, 640])
async def test_browser_poll_with_long_options_has_no_overflow(feature, tmp_path, width):
    browser = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not browser.is_file():
        pytest.skip("Requires local Chrome for visual integration")
    view = source(feature)
    view.main.poll.options[0].translation = feature.tweet_model.Translation(True, "英语", "5小时上限，每周额度2倍。" * 10, "other", "mock-layout-only")
    renderer = feature.renderer.TweetRenderer(tmp_path, {"executable_path": str(browser)}, width=width)
    paths = await renderer.render(view, assets={})
    assert len(paths) == 1
    actual_width, height = struct.unpack(">II", Path(paths[0]).read_bytes()[16:24])
    assert actual_width == width and height > 200
    assert "240,643 票" in next(tmp_path.glob("*.html")).read_text()
