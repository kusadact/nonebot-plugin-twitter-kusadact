import asyncio
import base64
import importlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
from types import ModuleType, SimpleNamespace
import sys
from unittest.mock import AsyncMock
import zlib

from bs4 import BeautifulSoup
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/nitter"


@pytest.fixture
def modules(monkeypatch):
    pkg = ModuleType("tweet_feature_test")
    pkg.__path__ = [str(ROOT / "nonebot_plugin_twitter")]
    monkeypatch.setitem(sys.modules, pkg.__name__, pkg)
    return SimpleNamespace(**{name: importlib.import_module(f"{pkg.__name__}.{name}") for name in
                            ("tweet_model", "nitter_parser", "translation", "llm_client", "renderer", "pipeline")})


def view(modules, text="Hello @alice 42 https://example.org/long/path"):
    model = modules.tweet_model
    return model.TweetView(model.Post("101", model.Author("Alice", "alice"), [model.TextPart(text)]))


def test_reply_groups_survive_serialization_without_joining_independent_posts(modules):
    parsed = modules.nitter_parser.parse_nitter((FIXTURES / "reply-chains.html").read_text(), "http://nitter.test", "hinayua_r18", "1951560929818132695")
    assert len(parsed.replies) == 10  # One main-thread continuation, four pairs and one independent reply.
    paired = [post for post in parsed.replies if post.reply_group]
    assert len(paired) == 8
    for index in range(0, 8, 2):
        assert paired[index].reply_group == paired[index + 1].reply_group == paired[index].id
    assert parsed.replies[-1].reply_group == ""
    restored = modules.tweet_model.TweetView.from_dict(parsed.to_dict())
    soup = BeautifulSoup(modules.renderer.build_html(restored), "html.parser")
    assert len(soup.select(".reply-chain")) == 4 and len(soup.select(".reply-group")) == 6
    assert all(len(group.select(":scope > .reply")) == 2 for group in soup.select(".reply-chain"))
    assert len(soup.select(".reply")) == 10
    # Separate replies by the same author do not imply a conversation edge.
    parsed.replies = [paired[1], paired[3]]
    assert not BeautifulSoup(modules.renderer.build_html(parsed), "html.parser").select(".reply-chain")
    legacy = view(modules).to_dict()
    legacy["main"].pop("reply_group")
    assert modules.tweet_model.TweetView.from_dict(legacy).main.reply_group == ""


def response(items, *, text="你好 @alice 42 https://example.org/long/path", needs=True):
    return json.dumps({"items": [dict(id=item["id"], needs_translation=needs, source_language="英语", translation=text if needs else "")
                                 for item in items]})


def test_actual_quote_and_reply_media_remain_owned(modules):
    parsed = modules.nitter_parser.parse_nitter((FIXTURES / "quoted-replies.html").read_text(), "http://nitter.test", "sharpnelsound", "2108953577679122591")
    assert parsed.main.text == "kawaii move🤩"
    assert parsed.main.media == []
    assert parsed.main.quote.author.username == "Tsune13226012"
    assert len(parsed.main.quote.media) == 1
    assert parsed.main.quote.media[0].kind == "gif"
    assert len(parsed.replies) == 2
    assert parsed.replies[0].media == []
    assert parsed.replies[1].text == ""
    assert len(parsed.replies[1].media) == 1
    assert parsed.legacy_fields()["video_url"] == ""
    assert parsed.legacy_fields()["gif_url_list"] == [parsed.main.quote.media[0].url, parsed.replies[1].media[0].url]
    assert parsed.main.quote.media[0].poster.startswith("http://nitter.test/")
    assert modules.tweet_model.TweetView.from_dict(parsed.to_dict()).to_dict() == parsed.to_dict()
    rendered = BeautifulSoup(modules.renderer.build_html(parsed), "html.parser")
    assert not rendered.select(".reply .replying-to")
    assert [badge.get_text(strip=True) for badge in rendered.select(".video-badge")] == ["GIF", "GIF"]


def test_nested_reply_preserves_other_recipient_context(modules):
    parsed = view(modules)
    parsed.replies = [modules.tweet_model.Post("102", replying_to=["ALICE", "bob"])]
    soup = BeautifulSoup(modules.renderer.build_html(parsed), "html.parser")
    assert soup.select_one(".reply .replying-to").text == "回复 @bob"


def test_links_entities_paragraphs_and_scope(modules):
    html = '''<div class="main-thread"><div class="main-tweet timeline-item"><div class="tweet-body">
      <a class="fullname">Alice</a><a class="username">@alice</a><span class="icon-ok"></span>
      <div class="tweet-content">Hello <a href="/bob">@bob</a><br><a href="/search?q=%23test">#test</a>
      <a href="https://example.org/long/path?q=123">example.org/long…</a><p>Second paragraph</p></div>
      <div class="attachments"><a class="still-image" href="/pic/main.jpg"><img src="/pic/main-small.jpg"></a></div>
      <div class="quote"><a class="quote-link" href="/bob/status/102#m"></a><a class="username">@bob</a>
      <div class="quote-text">Quote</div><a class="still-image" href="/pic/quote.jpg"><img src="/pic/quote-small.jpg"></a></div>
      <div class="tweet-stats"><span class="tweet-stat"><span class="icon-heart"></span>123</span></div>
      </div></div></div><div class="timeline-item"><a class="tweet-link" href="/c/status/103#m"></a>
      <div class="tweet-content">Reply</div><a class="still-image" href="/pic/reply.jpg"><img src="/pic/reply-small.jpg"></a></div>'''
    parsed = modules.nitter_parser.parse_nitter(html, "http://nitter.test", "alice", "101")
    assert "https://example.org/long/path?q=123" in parsed.main.text
    assert "…" not in parsed.main.text
    assert "@bob\n#test" in parsed.main.text
    assert "Second paragraph" in parsed.main.text
    assert {part.kind for part in parsed.main.parts} >= {"mention", "hashtag", "url"}
    assert parsed.main.author.verified
    assert parsed.main.stats == {"likes": "123"}
    assert parsed.main.media[0].url.endswith("/main.jpg")
    assert len(parsed.main.media) == 1
    assert parsed.main.quote.media[0].url.endswith("/quote.jpg")
    assert parsed.replies[0].media[0].url.endswith("/reply.jpg")


def test_legacy_video_prefers_real_video_over_gif(modules):
    model = modules.tweet_model
    parsed = view(modules)
    parsed.main.media = [model.Media("gif", "http://nitter.test/gif.mp4"), model.Media("video", "http://nitter.test/movie.m3u8")]
    assert parsed.legacy_fields()["video_url"].endswith("movie.m3u8")


@pytest.mark.parametrize("text", ["", "🤩😭", "https://example.com/abc123", "@alice 🎉", "123 + 456"])
def test_local_nonsemantic_content_skips_translation(modules, text):
    assert modules.translation.build_input(view(modules, text)) == []


def test_all_post_texts_share_one_input_and_chinese_is_judged(modules):
    parsed = view(modules, "中文内容")
    model = modules.tweet_model
    parsed.main.quote = model.Post("102", parts=[model.TextPart("English quote")])
    parsed.replies = [model.Post("103", parts=[model.TextPart("Reply")]), model.Post("104")]
    items = modules.translation.build_input(parsed)
    assert [item["id"] for item in items] == ["101", "102", "103"]
    assert len(modules.translation.validate_response(response(items, needs=False), items)) == 3


def test_real_fixture_exposes_quote_and_thread_relations_with_context_only_gif(modules):
    parsed = modules.nitter_parser.parse_nitter((FIXTURES / "quoted-replies.html").read_text(), "http://nitter.test", "sharpnelsound", "2108953577679122591")
    payload = modules.translation.build_payload(parsed)
    posts = {post["id"]: post for post in payload["context"]["posts"]}
    assert payload["main_id"] == parsed.main.id
    assert posts[parsed.main.id]["roles"] == ["main"]
    assert posts[parsed.main.quote.id]["roles"] == ["quote"]
    assert posts[parsed.main.quote.id]["media_types"] == ["gif"]
    assert posts[parsed.replies[1].id]["context_only"] is True
    assert posts[parsed.replies[1].id]["text"] == ""
    assert posts[parsed.replies[1].id]["media_types"] == ["gif"]
    assert payload["context"]["relations"] == [
        dict(type="quotes", from_id=parsed.main.id, to_id=parsed.main.quote.id),
        *[dict(type="reply_in_thread", from_id=reply.id, to_id=parsed.main.id) for reply in parsed.replies],
    ]
    assert parsed.replies[1].id not in {item["id"] for item in payload["items"]}
    # Only textual items require results; a model translating context-only IDs is rejected.
    raw = json.loads(response(payload["items"], needs=False))
    modules.translation.validate_response(json.dumps(raw), payload["items"])
    raw["items"].append(dict(id=parsed.replies[1].id, needs_translation=False, source_language="无法确定", translation=""))
    with pytest.raises(modules.llm_client.LLMError, match="invalid_ids"):
        modules.translation.validate_response(json.dumps(raw), payload["items"])


def test_link_only_main_remains_context_for_reply_and_its_quote(modules):
    model = modules.tweet_model
    parsed = view(modules, "https://example.org/music")
    parsed.main.links = [model.LinkPreview(url="https://example.org/music", title="TOMORA live at RED MARQUEE", domain="example.org")]
    parsed.replies = [model.Post("102", parts=[model.TextPart("Loved that set")], replying_to=["alice"],
                                quote=model.Post("103", parts=[model.TextPart("Encore!")]))]
    payload = modules.translation.build_payload(parsed)
    assert [item["id"] for item in payload["items"]] == ["102", "103"]
    main_context = payload["context"]["posts"][0]
    assert main_context["context_only"] is True
    assert main_context["text"] == "https://example.org/music"
    assert main_context["link_previews"][0]["title"] == "TOMORA live at RED MARQUEE"
    assert payload["context"]["relations"] == [
        dict(type="reply_in_thread", from_id="102", to_id="101"),
        dict(type="quotes", from_id="102", to_id="103"),
    ]


def test_repeated_post_merges_roles_and_recipients_without_duplicate_translation(modules):
    model = modules.tweet_model
    parsed = view(modules, "Great set")
    parsed.main.quote = model.Post("102", model.Author("Bob", "bob"), [model.TextPart("Live tonight")])
    reply = model.Post.from_dict(parsed.to_dict()["main"]["quote"])
    reply.replying_to = ["alice"]
    parsed.replies = [reply]
    payload = modules.translation.build_payload(parsed)
    assert [item["id"] for item in payload["items"]] == ["101", "102"]
    assert payload["context"]["posts"][1]["roles"] == ["quote", "reply"]
    assert payload["context"]["posts"][1]["replying_to"] == ["alice"]
    assert parsed.main.quote.replying_to == []
    assert len(payload["context"]["relations"]) == 2
    reply.parts = [model.TextPart("Different post")]
    with pytest.raises(modules.llm_client.LLMError, match="duplicate_source_id"):
        modules.translation.build_payload(parsed)


def test_input_budget_includes_link_context_without_truncating_it(modules):
    parsed = view(modules, "Great set")
    baseline = modules.translation.build_payload(parsed)
    budget = len(modules.translation.SYSTEM_PROMPT) + len(json.dumps(baseline, ensure_ascii=False)) + 100
    modules.translation.build_payload(parsed, budget)
    parsed.main.links = [modules.tweet_model.LinkPreview(title="Music festival " * 100)]
    with pytest.raises(modules.llm_client.LLMError, match="input_budget_exceeded"):
        modules.translation.build_input(parsed, budget)
    assert parsed.main.links[0].title == "Music festival " * 100


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown", "bad_bool", "no_language", "url", "mention", "number", "non_chinese"])
def test_invalid_results_are_rejected_as_whole_tweet(modules, mutation):
    items = modules.translation.build_input(view(modules))
    data = json.loads(response(items))
    if mutation == "missing": data["items"] = []
    elif mutation == "duplicate": data["items"].append(data["items"][0])
    elif mutation == "unknown": data["items"][0]["id"] = "unknown"
    elif mutation == "bad_bool": data["items"][0]["needs_translation"] = "true"
    elif mutation == "no_language": data["items"][0]["source_language"] = ""
    elif mutation == "url": data["items"][0]["translation"] = "你好 @alice 42 https://example.org/long/…"
    elif mutation == "mention": data["items"][0]["translation"] = "你好 @bob 42 https://example.org/long/path"
    elif mutation == "number": data["items"][0]["translation"] = "你好 @alice 43 https://example.org/long/path"
    elif mutation == "non_chinese": data["items"][0]["translation"] = items[0]["text"]
    with pytest.raises(modules.llm_client.LLMError):
        modules.translation.validate_response(json.dumps(data), items)


@pytest.mark.parametrize(("text", "reason"), [("", "empty_translation"), ("  ", "empty_translation"),
    ("Anthropic??", "no_chinese_text"), ("fake-secret", "no_chinese_text")])
def test_invalid_translation_identifies_the_reply_without_storing_model_text(modules, text, reason):
    parsed = view(modules, "Math!")
    parsed.replies = [modules.tweet_model.Post("102", parts=[modules.tweet_model.TextPart("Anthropic??")])]
    items = modules.translation.build_input(parsed)
    raw = dict(items=[dict(id="101", needs_translation=True, source_language="英语", translation="算一下！"),
                      dict(id="102", needs_translation=True, source_language="英语", translation=text)])
    with pytest.raises(modules.llm_client.LLMError, match="invalid_translation") as caught:
        modules.translation.validate_response(json.dumps(raw), items)
    assert caught.value.details == dict(post_id="102", reason=reason)
    assert str(caught.value) == "invalid_translation"
    assert "fake-secret" not in json.dumps(caught.value.details)


def test_name_only_reply_can_remain_original_when_marked_as_not_needing_translation(modules):
    items = modules.translation.build_input(view(modules, "Anthropic??"))
    results = modules.translation.validate_response(response(items, needs=False), items)
    assert results["101"]["needs_translation"] is False and results["101"]["text"] == ""


@pytest.mark.parametrize(("translation", "changed"), [
    ("你好 @alice 42 https://example.org/short", "urls"),
    ("你好 @bob 42 https://example.org/long/path", "mentions"),
    ("你好 @alice 43 https://example.org/long/path", "numbers"),
])
def test_protected_content_diagnostics_report_fields_and_numeric_differences(modules, translation, changed):
    items = modules.translation.build_input(view(modules))
    with pytest.raises(modules.llm_client.LLMError, match="protected_content_changed") as caught:
        modules.translation.validate_response(response(items, text=translation), items)
    expected = dict(post_id="101", reason="protected_content_changed", changed=[changed])
    if changed == "numbers":
        expected["number_changes"] = dict(missing={"42": 1}, added={"43": 1})
    assert caught.value.details == expected


@pytest.mark.parametrize(("text", "changes"), [
    ("$9k在2年里，每月$375，少10英镑都能发现。", dict(missing={"375": 1}, added={"10": 1})),
    ("9000美元在2年里，每月375美元，少十英镑都能发现。", dict(missing={"9": 1, "375": 1}, added={"9000": 1})),
])
def test_number_diagnostics_distinguish_counts_conversions_and_added_digits(modules, text, changes):
    original = "$9k in 2 years is $375 a month; noticing a $375 deduction; a tenner less!"
    items = modules.translation.build_input(view(modules, original))
    with pytest.raises(modules.llm_client.LLMError, match="protected_content_changed") as caught:
        modules.translation.validate_response(response(items, text=text), items)
    assert caught.value.details["number_changes"] == changes


def test_number_diagnostics_exclude_url_mention_digits_and_raw_response(modules):
    items = modules.translation.build_input(view(modules, "42 times @alice123 https://example.org/567"))
    text = "fake-secret 43 次 @alice123 https://example.org/567"
    with pytest.raises(modules.llm_client.LLMError, match="protected_content_changed") as caught:
        modules.translation.validate_response(response(items, text=text), items)
    assert caught.value.details == dict(post_id="101", reason="protected_content_changed", changed=["numbers"],
                                       number_changes=dict(missing={"42": 1}, added={"43": 1}))
    assert "fake-secret" not in json.dumps(caught.value.details)


def test_budget_never_truncates_posts(modules):
    with pytest.raises(modules.llm_client.LLMError, match="input_budget_exceeded"):
        modules.translation.build_input(view(modules, "Hello " * 2000), 1000)


@pytest.mark.parametrize("url,model,expected,native", [
    ("https://generativelanguage.googleapis.com", "gemini-test", "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent", True),
    ("https://generativelanguage.googleapis.com/v1beta", "models/gemini-test", "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent", True),
    ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-test", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", False),
    ("https://api.deepseek.com", "deepseek-flash", "https://api.deepseek.com/v1/chat/completions", False),
    ("https://proxy.test/v1/chat/completions", "gemini-test", "https://proxy.test/v1/chat/completions", False),
    ("https://proxy.test/v1/models/old:generateContent", "gemini-test", "https://proxy.test/v1/models/gemini-test:generateContent", True),
])
def test_protocol_uses_url_not_model(modules, url, model, expected, native):
    route = modules.llm_client.Route(url, "fake-secret", model)
    assert route.endpoint() == expected
    assert route.native_gemini is native
    assert "fake-secret" not in repr(route)


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False])
async def test_real_http_adapter_and_translation_cache(modules, tmp_path, native):
    requests = []
    parsed = view(modules)
    model = modules.tweet_model
    parsed.main.quote = model.Post("102", parts=[model.TextPart("🤩")], media=[model.Media("gif", "https://media.test/cover.mp4")])
    parsed.main.links = [model.LinkPreview(title="Music festival", url="https://example.org/long/path")]
    items = modules.translation.build_input(parsed)
    expected_input = modules.translation.build_payload(parsed)
    def handler(request):
        requests.append(request)
        payload = json.loads(request.content)
        if native:
            assert request.headers["x-goog-api-key"] == "fake-secret"
            assert payload["systemInstruction"]["parts"][0]["text"] == modules.translation.SYSTEM_PROMPT
            assert json.loads(payload["contents"][0]["parts"][0]["text"]) == expected_input
            return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": response(items)}]}}]})
        assert request.headers["authorization"] == "Bearer fake-secret"
        assert payload["model"] == "test-model" and payload["stream"] is False
        assert payload["messages"][0]["content"] == modules.translation.SYSTEM_PROMPT
        assert json.loads(payload["messages"][1]["content"]) == expected_input
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": response(items)}}]})
    route = modules.llm_client.Route("https://generativelanguage.googleapis.com" if native else "https://proxy.test/v1", "fake-secret", "test-model")
    translator = modules.translation.Translator(tmp_path, transport=httpx.MockTransport(handler))
    await translator.translate(parsed, route)
    await translator.translate(model.TweetView.from_dict(parsed.to_dict()), route)
    assert len(requests) == 1
    assert parsed.main.translation.model == "test-model"
    assert parsed.main.translation.provider == ("gemini" if native else "other")
    assert all("fake-secret" not in path.read_text() for path in tmp_path.glob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_setting", ["PROMPT_VERSION", "SYSTEM_PROMPT"])
async def test_prompt_upgrade_preserves_old_cache_and_requests_new_translation(modules, tmp_path, monkeypatch, changed_setting):
    requests = []
    parsed = view(modules, "That set was incredible")
    def handler(request):
        requests.append(json.loads(request.content))
        text = "这场演出太棒了" if len(requests) == 1 else "这场演出真精彩"
        return httpx.Response(200, json={"choices": [{"message": {"content": response(
            modules.translation.build_input(parsed), text=text)}}]})
    translator = modules.translation.Translator(tmp_path, transport=httpx.MockTransport(handler))
    route = modules.llm_client.Route("https://proxy.test/v1", "fake-secret", "test-model")
    current_value = getattr(modules.translation, changed_setting)
    monkeypatch.setattr(modules.translation, changed_setting, "previous-value")
    await translator.translate(parsed, route)
    previous_path = translator.cache_path(modules.translation.build_payload(parsed), route)
    previous_content = previous_path.read_bytes()

    monkeypatch.setattr(modules.translation, changed_setting, current_value)
    await translator.translate(parsed, route)
    await translator.translate(parsed, route)
    assert len(requests) == 2
    assert parsed.main.translation.text == "这场演出真精彩"
    assert previous_path.read_bytes() == previous_content
    assert len(list(tmp_path.glob("*.json"))) == 2


@pytest.mark.asyncio
async def test_changed_scene_context_invalidates_translation_cache(modules, tmp_path):
    requests = []
    parsed = view(modules, "That set was incredible")
    model = modules.tweet_model
    parsed.main.links = [model.LinkPreview(title="TOMORA concert")]
    def handler(request):
        data = json.loads(json.loads(request.content)["messages"][1]["content"])
        requests.append(data)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": response(data["items"], text="这场演出太棒了")}}]})
    translator = modules.translation.Translator(tmp_path, transport=httpx.MockTransport(handler))
    route = modules.llm_client.Route("https://proxy.test/v1", "fake-secret", "test-model")
    await translator.translate(parsed, route)
    parsed.main.links[0].title = "Tennis final"
    await translator.translate(parsed, route)
    # Changing image transport URLs does not alter the context sent to the model.
    parsed.main.links[0].image = "https://media.test/new.jpg"
    await translator.translate(parsed, route)
    assert len(requests) == 2
    assert requests[0]["items"] == requests[1]["items"]
    assert requests[0]["context"] != requests[1]["context"]


@pytest.mark.asyncio
async def test_http_errors_and_bad_envelope_hide_secret(modules):
    route = modules.llm_client.Route("https://proxy.test", "fake-secret", "test-model")
    for status, body, category in [(401, {"error": "fake-secret"}, "http_401"), (200, {"error": "fake-secret"}, "invalid_response")]:
        with pytest.raises(modules.llm_client.LLMError, match=category) as error:
            await modules.llm_client.request_translation(route, "system", "prompt", transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body)))
        assert "fake-secret" not in str(error.value)


def test_production_template_escapes_source_and_model_and_keeps_media_only_reply(modules):
    parsed = view(modules, '<script>alert(1)</script> https://example.org/verylong')
    model = modules.tweet_model
    parsed.main.translation = model.Translation(True, "英语", '<img src=x onerror=alert(1)>中文', "other", '<script>model</script>')
    parsed.replies = [model.Post("102", media=[model.Media("image", "http://nitter.test/image.jpg")])]
    html = modules.renderer.build_html(parsed)
    soup = BeautifulSoup(html, "html.parser")
    assert not soup.select("script, button, select, input, a")
    assert len(soup.select(".translation")) == 1
    assert len(soup.select(".reply .media-frame")) == 1
    assert "加载失败" in soup.select_one(".reply").text
    assert '<script>' not in html
    assert 'model&lt;/script&gt;' in html
    assert "verylong" in soup.get_text()


def make_pipeline(modules, tmp_path, *, attempts=0, translator=None, fetch=None, deliver=None, render=None, config=None, users=1, clock=None, prepare_assets=None):
    state = {f"user{i}": dict(timeline_seen=["status:100"], since_id="100", group={}, private={}, pending_tweets=[
        dict(signature="status:101", tweet_id="101", source_user_name="alice", is_retweet=False)]) for i in range(users)}
    def read(): return json.loads(json.dumps(state))
    def update(fn): return fn(state)
    config = config or SimpleNamespace(twitter_template_enabled=True, twitter_translation_enabled=True,
                                      twitter_translation_primary=dict(url="https://primary.test", key="fake-secret", model="gemini-test"),
                                      twitter_translation_fallback=dict(url="https://fallback.test", key="fake-secret", model="deepseek-test"))
    translator = translator or SimpleNamespace(timeout=60, translate=AsyncMock())
    pipeline = modules.pipeline.TweetPipeline(read_state=read, update_state=update, config=config,
        fetch=fetch or AsyncMock(return_value=view(modules)), render=render or AsyncMock(return_value=[]), translator=translator,
        prepare=AsyncMock(return_value=dict(status=True, delivery_parts=[])), deliver=deliver or AsyncMock(return_value=True),
        cache_dir=tmp_path, clock=clock or (lambda: 0), prepare_assets=prepare_assets)
    if attempts:
        entry = state["user0"]["pending_tweets"][0]
        entry["processing"] = dict(stage="translation_wait", primary_attempts=attempts, fallback_attempts=0,
                                   view_cache=pipeline.write_view("user0", entry, view(modules)))
    return pipeline, state, translator


@pytest.mark.asyncio
async def test_pipeline_prefetch_overlaps_translation_and_survives_retry(modules, tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    now = [0]
    assets = {"avatar": "data:image/png;base64,test"}
    async def prepare(view):
        started.set()
        await release.wait()
        return assets
    calls = 0
    async def translate(view, route):
        nonlocal calls
        calls += 1
        await started.wait()
        if calls == 1:
            raise modules.llm_client.LLMError("timeout")
    prepare = AsyncMock(side_effect=prepare)
    translator = SimpleNamespace(timeout=60, translate=AsyncMock(side_effect=translate))
    render = AsyncMock(return_value=[])
    pipeline, state, _ = make_pipeline(modules, tmp_path, translator=translator, render=render,
                                      prepare_assets=prepare, clock=lambda: now[0])
    async with asyncio.timeout(2):
        await pipeline.tick()
    assert state["user0"]["pending_tweets"][0]["processing"]["stage"] == "translation_wait"
    task = next(iter(pipeline.asset_tasks.values()))
    assert not task.done()
    render.assert_not_awaited()
    release.set()
    await task
    now[0] = 60
    await pipeline.tick()
    prepare.assert_awaited_once()
    assert render.await_args.kwargs["assets"] is assets
    assert not pipeline.asset_tasks and not state["user0"]["pending_tweets"]


@pytest.mark.asyncio
async def test_pipeline_shutdown_cancels_prefetch(modules, tmp_path):
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def prepare(view):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    async def translate(view, route):
        await started.wait()
        await asyncio.Event().wait()
    pipeline, _, _ = make_pipeline(modules, tmp_path, prepare_assets=prepare,
        translator=SimpleNamespace(timeout=60, translate=AsyncMock(side_effect=translate)))
    task = asyncio.create_task(pipeline.run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and not pipeline.asset_tasks


@pytest.mark.asyncio
async def test_removed_pending_tweet_cancels_prefetch(modules, tmp_path):
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def prepare(view):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    async def translate(view, route):
        await started.wait()
        raise modules.llm_client.LLMError("timeout")
    pipeline, state, _ = make_pipeline(modules, tmp_path, prepare_assets=prepare,
        translator=SimpleNamespace(timeout=60, translate=AsyncMock(side_effect=translate)))
    await pipeline.tick()
    state["user0"]["pending_tweets"] = []
    await pipeline.tick()
    assert cancelled.is_set() and not pipeline.asset_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("attempts", [0, 9])
async def test_primary_success_never_uses_fallback(modules, tmp_path, attempts):
    pipeline, state, translator = make_pipeline(modules, tmp_path, attempts=attempts)
    await pipeline.tick()
    assert translator.translate.await_count == 1
    assert translator.translate.await_args.args[1].model == "gemini-test"
    assert state["user0"]["pending_tweets"] == []


@pytest.mark.asyncio
async def test_ten_plus_three_persisted_budget_and_sixty_second_waits(modules, tmp_path):
    now = [0]
    translator = SimpleNamespace(timeout=60, translate=AsyncMock(side_effect=modules.llm_client.LLMError("network_error")))
    pipeline, state, _ = make_pipeline(modules, tmp_path, translator=translator, clock=lambda: now[0])
    for index in range(9):
        await pipeline.tick()
        task = state["user0"]["pending_tweets"][0]["processing"]
        assert task["primary_attempts"] == index + 1
        assert task["next_attempt_at"] == now[0] + 60
        before = translator.translate.await_count
        now[0] += 59
        await pipeline.tick()
        assert translator.translate.await_count == before
        now[0] += 1
    # 10th primary and 1st fallback execute in the same due tick.
    await pipeline.tick()
    task = state["user0"]["pending_tweets"][0]["processing"]
    assert task["primary_attempts"] == 10 and task["fallback_attempts"] == 1
    now[0] += 60
    await pipeline.tick()
    now[0] += 60
    await pipeline.tick()
    assert translator.translate.await_count == 13
    assert state["user0"]["pending_tweets"] == []
    assert pipeline.render.await_count == 1


@pytest.mark.asyncio
async def test_waiting_task_does_not_block_other_due_tasks(modules, tmp_path):
    translator = SimpleNamespace(timeout=60, translate=AsyncMock(side_effect=modules.llm_client.LLMError("timeout")))
    pipeline, state, _ = make_pipeline(modules, tmp_path, translator=translator, users=2)
    pipeline.fetch.side_effect = [view(modules, "First tweet"), view(modules, "Second tweet")]
    await pipeline.tick()
    assert translator.translate.await_count == 2
    await pipeline.tick()
    assert translator.translate.await_count == 2


@pytest.mark.asyncio
async def test_same_content_shares_retry_budget_across_subscriptions(modules, tmp_path):
    now = [0]
    translator = SimpleNamespace(timeout=60, translate=AsyncMock(side_effect=modules.llm_client.LLMError("timeout")))
    pipeline, state, _ = make_pipeline(modules, tmp_path, translator=translator, users=2, clock=lambda: now[0])
    for _ in range(12):
        await pipeline.tick()
        now[0] += 60
    assert translator.translate.await_count == 13
    assert all(user["pending_tweets"] == [] for user in state.values())


@pytest.mark.asyncio
async def test_changed_context_does_not_share_retry_budget(modules, tmp_path):
    translator = SimpleNamespace(timeout=60, translate=AsyncMock(side_effect=modules.llm_client.LLMError("timeout")))
    pipeline, state, _ = make_pipeline(modules, tmp_path, translator=translator, users=2)
    first, second = view(modules, "Great set"), view(modules, "Great set")
    first.main.links = [modules.tweet_model.LinkPreview(title="Music festival")]
    second.main.links = [modules.tweet_model.LinkPreview(title="Tennis final")]
    pipeline.fetch.side_effect = [first, second]
    await pipeline.tick()
    assert translator.translate.await_count == 2
    assert all(user["pending_tweets"][0]["processing"]["primary_attempts"] == 1 for user in state.values())


@pytest.mark.asyncio
async def test_successful_translation_shared_across_subscriptions(modules, tmp_path):
    async def translate(parsed, route):
        parsed.main.translation = modules.tweet_model.Translation(True, "英语", "你好", route.provider, route.model)
    translator = SimpleNamespace(timeout=60, translate=AsyncMock(side_effect=translate))
    pipeline, state, _ = make_pipeline(modules, tmp_path, translator=translator, users=2)
    await pipeline.tick()
    translator.translate.assert_awaited_once()
    assert pipeline.render.await_args_list[1].args[0].main.translation.text == "你好"


@pytest.mark.asyncio
async def test_new_media_only_reply_changes_context_and_keeps_its_media(modules, tmp_path):
    async def translate(parsed, route):
        parsed.main.translation = modules.tweet_model.Translation(True, "英语", "你好", route.provider, route.model)
    translator = SimpleNamespace(timeout=60, translate=AsyncMock(side_effect=translate))
    pipeline, state, _ = make_pipeline(modules, tmp_path, translator=translator, users=2)
    second = view(modules)
    second.replies.append(modules.tweet_model.Post("102", media=[modules.tweet_model.Media("image", "http://nitter.test/new-image.jpg")]))
    pipeline.fetch.side_effect = [view(modules), second]
    await pipeline.tick()
    assert translator.translate.await_count == 2
    rendered = pipeline.render.await_args_list[1].args[0]
    assert rendered.main.translation.text == "你好"
    assert len(rendered.replies) == 1 and rendered.replies[0].media[0].url.endswith("new-image.jpg")


@pytest.mark.asyncio
async def test_disabling_translation_resumes_waiting_job_as_original(modules, tmp_path):
    translator = SimpleNamespace(timeout=60, translate=AsyncMock(side_effect=modules.llm_client.LLMError("timeout")))
    now = [0]
    pipeline, state, _ = make_pipeline(modules, tmp_path, translator=translator, clock=lambda: now[0])
    await pipeline.tick()
    pipeline.config.twitter_translation_enabled = False
    now[0] = 60
    await pipeline.tick()
    assert translator.translate.await_count == 1
    assert pipeline.render.await_args.args[0].main.translation is None
    assert state["user0"]["pending_tweets"] == []


@pytest.mark.asyncio
async def test_direct_nitter_gif_video_needs_no_external_parser(plugin, monkeypatch):
    requested = []
    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, content=b"fake-video")
    client = httpx.AsyncClient
    monkeypatch.setattr(plugin.api.httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(handler)))
    files = await plugin.api.get_video_path('http://nitter.test/pic/video.twimg.com%2Ftweet_video%2Fa.mp4')
    assert requested == ['https://video.twimg.com/tweet_video/a.mp4']
    assert Path(files[0]).read_bytes() == b"fake-video"
    second = await plugin.api.get_video_path('http://nitter.test/pic/video.twimg.com%2Ftweet_video%2Fb.mp4')
    assert second != files and Path(files[0]).read_bytes() == b"fake-video"


@pytest.mark.asyncio
async def test_gif_mp4_sends_looping_image_and_retries_frozen_attachment(plugin, tmp_path, monkeypatch):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("GIF conversion integration requires ffmpeg and ffprobe")
    source = tmp_path / "source.mp4"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc2=size=160x120:rate=15:duration=1", "-c:v", "mpeg4", str(source)], check=True)
    requested = []
    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, content=source.read_bytes())
    client = httpx.AsyncClient
    monkeypatch.setattr(plugin.api.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(handler)))
    settings = dict(status=True, r18=True, media=False)
    state = {"alice": dict(screen_name="Alice", group={"1": dict(settings), "2": dict(settings)}, private={})}
    plugin.api.update_twitter_list(lambda current: current.update(state))
    image = tmp_path / "tweet.png"
    image.write_bytes(make_png())
    info = dict(status=True, rendered_images=[str(image)], r18=False, media=True, text=[], pic_url_list=[], video_url="",
                gif_url_list=["http://nitter.test/pic/video.twimg.com%2Ftweet_video%2Fa.mp4"])
    prepared = await plugin.api.prepare_tweet_delivery(info, "alice", "101", state)
    assert [part["part"] for part in prepared["delivery_parts"]] == ["image:0", "gif:0"]
    gif = prepared["delivery_parts"][-1]["message"][0]
    assert gif["type"] == "image"
    data = base64.b64decode(gif["data"]["file"].removeprefix("base64://"))
    assert data.startswith(b"GIF89a") and b"NETSCAPE2.0\x03\x01\x00\x00" in data
    frames = subprocess.check_output([ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
                                     "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", prepared["pinned_files"][0]], text=True)
    assert int(frames.strip()) == 15
    async def send(*, group_id, msg):
        return group_id == 1 or not msg[0].data["file"].startswith("base64://R0lG")
    plugin.tools.send_group_msg_by_bots.side_effect = send
    assert not await plugin.api.tweet_handle(prepared, "alice", "101", state)
    receipts = plugin.api.read_twitter_list()["alice"]["pending_deliveries"]["101"]
    assert receipts == {"group:1": ["image:0", "gif:0"], "group:2": ["image:0"]}
    plugin.tools.send_group_msg_by_bots.reset_mock(side_effect=True)
    plugin.tools.send_group_msg_by_bots.return_value = True
    restored = json.loads(json.dumps(prepared))
    assert await plugin.api.tweet_handle(restored, "alice", "101", state)
    assert plugin.tools.send_group_msg_by_bots.await_count == 1
    assert plugin.tools.send_group_msg_by_bots.await_args.kwargs["group_id"] == 2
    # The manual/original-content route reuses the same looping GIF attachment.
    context = await plugin.api.get_tweet_context(info, "alice", "101")
    assert context[-1].type == "image" and context[-1].data["file"] == gif["data"]["file"]
    assert requested == ["https://video.twimg.com/tweet_video/a.mp4"]


@pytest.mark.asyncio
async def test_interrupt_counts_attempt_and_resumes_without_reset(modules, tmp_path):
    now = [0]
    translator = SimpleNamespace(timeout=60, translate=AsyncMock(side_effect=asyncio.CancelledError()))
    pipeline, state, _ = make_pipeline(modules, tmp_path, translator=translator, clock=lambda: now[0])
    with pytest.raises(asyncio.CancelledError):
        await pipeline.tick()
    task = state["user0"]["pending_tweets"][0]["processing"]
    assert task["primary_attempts"] == 1 and task["request_inflight"]
    translator.translate = AsyncMock()
    # Simulate a new worker over exactly the persisted queue and artifacts.
    fresh = modules.pipeline.TweetPipeline(read_state=pipeline.read_state, update_state=pipeline.update_state,
        fetch=pipeline.fetch, render=pipeline.render, translator=translator, prepare=pipeline.prepare,
        deliver=pipeline.deliver, config=pipeline.config, cache_dir=tmp_path, clock=lambda: now[0])
    await fresh.tick()
    translator.translate.assert_not_called()
    now[0] = 120
    await fresh.tick()
    translator.translate.assert_awaited_once()
    pipeline.fetch.assert_awaited_once()
    assert state["user0"]["since_id"] == "101"


@pytest.mark.asyncio
async def test_delivery_reuses_prepared_artifact_and_render_on_retry(modules, tmp_path):
    now = [0]
    deliver = AsyncMock(side_effect=[False, True])
    pipeline, state, translator = make_pipeline(modules, tmp_path, deliver=deliver, clock=lambda: now[0])
    await pipeline.tick()
    task = state["user0"]["pending_tweets"][0]["processing"]
    assert task["stage"] == "delivering"
    before = Path(task["delivery_cache"]).read_bytes()
    now[0] = 60
    # Changing config after delivery began cannot change the frozen message artifact.
    pipeline.config.twitter_translation_primary["model"] = "another-model"
    await pipeline.tick()
    assert translator.translate.await_count == 1
    assert pipeline.render.await_count == 1 and pipeline.prepare.await_count == 1
    assert deliver.await_args_list[0].args[0] == deliver.await_args_list[1].args[0]
    assert Path(task["delivery_cache"]).read_bytes() == before
    assert state["user0"]["pending_tweets"] == []


@pytest.mark.asyncio
async def test_two_render_failures_fall_back_without_more_llm_calls(modules, tmp_path):
    now = [0]
    render = AsyncMock(side_effect=ValueError("browser_unavailable"))
    pipeline, state, translator = make_pipeline(modules, tmp_path, render=render, clock=lambda: now[0])
    await pipeline.tick()
    assert state["user0"]["pending_tweets"][0]["processing"]["stage"] == "translated"
    now[0] = 60
    await pipeline.tick()
    assert render.await_count == 2 and translator.translate.await_count == 1
    assert pipeline.prepare.await_args.args[0]["rendered_images"] == []
    assert state["user0"]["pending_tweets"] == []


@pytest.mark.asyncio
async def test_duplicate_concurrent_ticks_only_claim_once(modules, tmp_path):
    gate = asyncio.Event()
    async def blocked_fetch(*args):
        await gate.wait()
        return view(modules)
    fetch = AsyncMock(side_effect=blocked_fetch)
    pipeline, state, _ = make_pipeline(modules, tmp_path, fetch=fetch)
    first = asyncio.create_task(pipeline.tick())
    await asyncio.sleep(0)
    await pipeline.tick()
    gate.set()
    await first
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_original_only_without_routes_or_translatable_text(modules, tmp_path):
    config = SimpleNamespace(twitter_template_enabled=True, twitter_translation_enabled=True)
    pipeline, state, translator = make_pipeline(modules, tmp_path, config=config)
    await pipeline.tick()
    translator.translate.assert_not_called()
    assert state["user0"]["pending_tweets"] == []


@pytest.mark.asyncio
async def test_timeline_discovery_does_no_fetch_or_send_in_background_mode(plugin):
    state = {'alice': dict(screen_name='Alice', since_id='100', timeline_seen=['status:100'], group={}, private={})}
    plugin.api.update_twitter_list(lambda current: current.update(state))
    fetch, deliver = AsyncMock(), AsyncMock()
    entries = [dict(signature='status:101', tweet_id='101', source_user_name='alice', is_retweet=False)]
    assert await plugin.delivery.process_timeline('alice', entries, None, read_state=plugin.api.read_twitter_list,
        update_state=plugin.api.update_twitter_list, fetch_tweet=fetch, deliver_tweet=deliver, enqueue_only=True)
    fetch.assert_not_called()
    deliver.assert_not_called()
    assert plugin.api.read_twitter_list()['alice']['pending_tweets'][0]['tweet_id'] == '101'


def make_png(width=160, height=240):
    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff)
    rows = b''.join(b'\0' + bytes([y % 256, 80, 160]) * width for y in range(height))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b'')


@pytest.mark.asyncio
async def test_browser_render_preserves_every_pixel_and_media_ratio(modules, tmp_path):
    model = modules.tweet_model
    parsed = view(modules, "Hello\n\n" + "https://example.org/" + "a" * 400)
    parsed.main.media = [model.Media("image", "http://nitter.test/image.png")]
    parsed.replies = [model.Post(str(i), model.Author("Reply", "reply"), [model.TextPart("完整回复 " * 120)]) for i in range(102, 110)]
    path = os.environ.get("TWITTER_TEST_BROWSER", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    launch = {"executable_path": path} if Path(path).is_file() else {}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={"content-type": "image/png"}, content=make_png()))
    renderer = modules.renderer.TweetRenderer(tmp_path, launch, max_height=1000, transport=transport)
    from playwright.async_api import Error
    try:
        paths = await renderer.render(parsed)
    except Error as error:
        if "Executable doesn't exist" in str(error):
            pytest.skip("Install Playwright Chromium or set TWITTER_TEST_BROWSER for rendering integration")
        raise
    assert len(paths) > 1
    heights = []
    for path in paths:
        width, height = struct.unpack(">II", Path(path).read_bytes()[16:24])
        assert width == 640 and 0 < height <= 1000
        heights.append(height)
    manifest = next(p for p in tmp_path.glob("*.json"))
    metrics = json.loads(manifest.read_text())
    assert sum(heights) == metrics["height"]
    media = metrics["media"][0]
    assert media["width"] / media["height"] == pytest.approx(160 / 240, abs=0.01)
    html = next(tmp_path.glob("*.html")).read_text()
    assert html.count('class="reply"') == 8
    assert "data:image/png;base64," in html and "加载失败" not in html
    # Cache reuse needs neither HTTP nor another browser rendering pass.
    assert await renderer.render(parsed) == paths


@pytest.mark.asyncio
async def test_cancelled_asset_download_closes_all_children(modules, tmp_path):
    parsed = view(modules)
    parsed.main.media = [modules.tweet_model.Media("image", f"http://image.test/{i}.png") for i in range(3)]
    started = asyncio.Event()
    active = 0
    async def download(request):
        nonlocal active
        active += 1
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1
    renderer = modules.renderer.TweetRenderer(tmp_path, transport=httpx.MockTransport(download))
    task = asyncio.create_task(renderer.prepare_assets(parsed))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0


@pytest.mark.asyncio
async def test_asset_progress_reports_cache_hits_and_exhausted_downloads(modules, tmp_path):
    parsed = view(modules)
    parsed.main.media = [modules.tweet_model.Media("image", "http://image.test/ok.png"),
                         modules.tweet_model.Media("image", "http://image.test/missing.png")]
    calls = []
    def download(request):
        calls.append(request.url.path)
        return httpx.Response(404) if request.url.path.endswith("missing.png") else httpx.Response(200, headers={"content-type":"image/png"}, content=make_png())
    renderer = modules.renderer.TweetRenderer(tmp_path, transport=httpx.MockTransport(download))
    events = []
    async def progress(counts): events.append(counts)
    assets = await renderer.prepare_assets(parsed, progress=progress)
    assert list(assets) == ["http://image.test/ok.png"]
    assert events[0] == dict(total=2, completed=0, loaded=0, failed=0)
    assert events[-1] == dict(total=2, completed=2, loaded=1, failed=1)
    calls.clear()
    await renderer.prepare_assets(parsed)
    assert calls == ["/missing.png"]


@pytest.mark.asyncio
async def test_rendered_images_and_video_only_retry_failed_parts(plugin, tmp_path, monkeypatch):
    settings = dict(status=True, r18=True, media=False)
    state = {'alice': dict(screen_name='Alice', group={'1': dict(settings), '2': dict(settings)}, private={'3': dict(settings)})}
    plugin.api.update_twitter_list(lambda current: current.update(state))
    paths = []
    for i in range(2):
        path = tmp_path / f"image{i}.png"
        path.write_bytes(make_png())
        paths.append(str(path))
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"fake-video-only-for-message-test")
    download = AsyncMock(return_value=[str(video_path)])
    monkeypatch.setattr(plugin.api, 'get_video_path', download)
    monkeypatch.setattr(plugin.api, 'get_video', AsyncMock(return_value=plugin.api.MessageSegment.video('http://bot.test/video.mp4')))
    plugin.config.twitter_node = True
    info = dict(status=True, rendered_images=paths, r18=False, media=True, text=['text already in image'], pic_url_list=[], video_url='http://video.test/a.mp4')
    prepared = await plugin.api.prepare_tweet_delivery(info, 'alice', '101', state)
    assert [part['part'] for part in prepared['delivery_parts']] == ['image:0', 'image:1', 'video:0']
    assert [part['mode'] for part in prepared['delivery_parts']] == ['node', 'node', 'video']
    async def send_group(*, group_id, msg):
        return group_id == 1
    plugin.tools.send_group_msg_by_bots.side_effect = send_group
    assert not await plugin.api.tweet_handle(prepared, 'alice', '101', state)
    receipts = plugin.api.read_twitter_list()['alice']['pending_deliveries']['101']
    assert receipts['group:1'] == ['image:0', 'image:1', 'video:0']
    assert receipts['group:2'] == ['image:0', 'image:1']
    assert receipts['private:3'] == ['image:0', 'image:1', 'video:0']
    plugin.tools.send_group_msg_by_bots.reset_mock(side_effect=True)
    plugin.tools.send_group_msg_by_bots.return_value = True
    forward_calls = plugin.tools.send_group_forward_msg_by_bots.await_count
    assert await plugin.api.tweet_handle(prepared, 'alice', '101', state)
    calls = plugin.tools.send_group_msg_by_bots.await_args_list
    assert len(calls) == 1 and calls[0].kwargs['group_id'] == 2 and calls[0].kwargs['msg'][0].type == 'video'
    assert plugin.tools.send_group_forward_msg_by_bots.await_count == forward_calls
    download.assert_awaited_once()


@pytest.mark.asyncio
async def test_single_http_attempt_has_a_total_deadline(modules):
    async def handler(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={})
    route = modules.llm_client.Route("https://proxy.test", "fake-secret", "model")
    with pytest.raises(modules.llm_client.LLMError, match="timeout"):
        await modules.llm_client.request_translation(route, "system", "prompt", timeout=0.01, transport=httpx.MockTransport(handler))


def test_source_view_versions_do_not_overwrite_translation_cache(modules, tmp_path):
    pipeline, state, _ = make_pipeline(modules, tmp_path)
    entry = state["user0"]["pending_tweets"][0]
    source = view(modules)
    before = pipeline.write_view("user0", entry, source)
    source.main.translation = modules.tweet_model.Translation(True, "英语", "你好", "gemini", "model")
    translated = pipeline.write_view("user0", entry, source)
    assert before != translated
    assert json.loads(Path(before).read_text())["main"]["translation"] is None
    assert json.loads(Path(translated).read_text())["main"]["translation"]["text"] == "你好"
