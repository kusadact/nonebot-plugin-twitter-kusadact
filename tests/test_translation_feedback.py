import asyncio
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


@pytest.fixture
def feature(monkeypatch):
    package = ModuleType("translation_feedback_test")
    package.__path__ = [str(ROOT / "nonebot_plugin_twitter")]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    return SimpleNamespace(**{name: importlib.import_module(f"{package.__name__}.{name}") for name in
                             ("tweet_model", "translation", "llm_client", "pipeline", "preview_server")})


def source(feature, text="2 times"):
    model = feature.tweet_model
    return model.TweetView(model.Post("101", model.Author("Alice", "alice"), [model.TextPart(text)]))


def output(text="2 次", post_id="101"):
    return dict(id=post_id, needs_translation=True, source_language="英语", translation=text)


def api_response(raw, native=False):
    if native:
        return httpx.Response(200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": raw}]}}]})
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": raw}}]})


@pytest.mark.parametrize(("original", "translated"), [
    ("Twice the weekly allowance doesn't help if a five-hour cap interrupts my work.", "每周额度增加2倍也没用，5小时上限仍会打断工作。"),
    ("2 tasks under a five-hour cap", "2项任务受到5小时上限限制"),
    ("$9k in 2 years is $375 a month; a $375 deduction; a tenner less!", "$9k在2年里相当于每月$375，每月扣$375，少10英镑都能发现！"),
    ("Moving from Oct 14th to Oct 18th", "从10月14日改到10月18日"),
    ("2 slots, twice a week", "2个名额，每周2次"),
])
def test_translated_number_words_do_not_violate_original_digit_protection(feature, original, translated):
    items = feature.translation.build_input(source(feature, original))
    result = feature.translation.validate_response(json.dumps(dict(items=[output(translated)])), items)
    assert result["101"]["text"] == translated


@pytest.mark.parametrize(("original", "translated", "missing"), [
    ("2 minutes", "两分钟", {"2": 1}),
    ("42 times", "43次", {"42": 1}),
    ("1,000 units", "1000个单位", {"1,000": 1}),
    ("375 then 375", "375次", {"375": 1}),
])
def test_original_digits_formats_and_repeated_occurrences_stay_protected(feature, original, translated, missing):
    items = feature.translation.build_input(source(feature, original))
    with pytest.raises(feature.llm_client.LLMError, match="protected_content_changed") as caught:
        feature.translation.validate_response(json.dumps(dict(items=[output(translated)])), items)
    assert caught.value.details["number_changes"]["missing"] == missing


@pytest.mark.parametrize("code", ["timeout", "network_error", "http_400", "http_401", "http_429", "invalid_response", "invalid_endpoint", "input_budget_exceeded"])
def test_transport_and_configuration_errors_do_not_become_model_feedback(feature, code):
    error = feature.llm_client.LLMError(code, details=dict(raw_response="fake-secret"))
    assert feature.translation.validation_feedback(error) is None


def test_feedback_only_copies_safe_validator_fields(feature):
    error = feature.llm_client.LLMError("protected_content_changed", details=dict(post_id="101", changed=["numbers"],
        raw_response="fake-secret", headers={"Authorization": "fake-secret"},
        number_changes=dict(missing={"2": 1, "fake-secret": 1}, added={"5": 1, "6": True})))
    feedback = feature.translation.validation_feedback(error)
    assert feedback["number_changes"] == dict(missing={"2": 1}, added={"5": 1})
    assert "fake-secret" not in json.dumps(feedback)


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("failure", ["numbers", "empty", "no_chinese", "invalid_json", "missing_ids"])
async def test_feedback_reaches_both_protocols_and_only_corrected_results_are_cached(feature, tmp_path, native, failure):
    view = source(feature)
    view.replies = [feature.tweet_model.Post("102", parts=[feature.tweet_model.TextPart("five-hour cap")])]
    valid = [output(), output("5小时上限", "102")]
    bad = json.loads(json.dumps(valid))
    if failure == "numbers":
        bad[0]["translation"] = "两次"
    elif failure == "empty":
        bad[1]["translation"] = ""
    elif failure == "no_chinese":
        bad[1]["translation"] = "fake-secret"
    elif failure == "missing_ids":
        bad.pop()
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(json.loads(body["contents"][0]["parts"][0]["text"] if native else body["messages"][1]["content"]))
        raw = json.dumps(dict(items=bad if len(requests) == 1 else valid))
        return api_response("fake-secret" if failure == "invalid_json" and len(requests) == 1 else raw, native)
    route = feature.llm_client.Route("https://generativelanguage.googleapis.com" if native else "https://llm.test", "fake-secret", "test-model")
    translator = feature.translation.Translator(tmp_path, transport=httpx.MockTransport(handler))
    with pytest.raises(feature.llm_client.LLMError) as caught:
        await translator.translate(view, route)
    feedback = feature.translation.validation_feedback(caught.value)
    path = translator.cache_path(feature.translation.build_payload(view), route)
    assert not path.exists() and all(post.translation is None for post in view.walk())
    await translator.translate(view, route, feedback=feedback)
    assert requests[1]["validation_feedback"] == feedback
    assert requests[0] == {key: value for key, value in requests[1].items() if key != "validation_feedback"}
    assert view.replies[0].translation.text == "5小时上限"
    await translator.translate(view, route, feedback=feedback)
    assert len(requests) == 2  # Feedback does not fragment the successful cache.
    saved = json.loads(path.read_text())
    assert set(saved) == {"items"} and "fake-secret" not in json.dumps(saved)


def test_feedback_budget_reduces_metadata_without_truncating_source(feature):
    payload = feature.translation.build_payload(source(feature, "Whole paragraph with 2 tasks"))
    feedback = feature.translation.validation_feedback(feature.llm_client.LLMError("protected_content_changed",
        details=dict(post_id="101", changed=["numbers"], number_changes=dict(missing={str(index): 1 for index in range(32)}, added={}))))
    essential = {key: value for key, value in feedback.items() if key in {"code", "instruction", "post_id"}}
    limit = len(feature.translation.SYSTEM_PROMPT) + len(json.dumps(dict(**payload, validation_feedback=essential), ensure_ascii=False))
    prompt = json.loads(feature.translation.request_prompt(payload, feedback, limit))
    assert prompt["validation_feedback"] == essential and prompt["items"] == payload["items"]
    with pytest.raises(feature.llm_client.LLMError, match="input_budget_exceeded"):
        feature.translation.request_prompt(payload, feedback, len(feature.translation.SYSTEM_PROMPT) + len(json.dumps(payload, ensure_ascii=False)))


def pipeline_for(feature, tmp_path, translator, now, users=1, config=None):
    state = {f"user{index}": dict(timeline_seen=["status:100"], since_id="100", group={}, private={}, pending_tweets=[
        dict(signature="status:101", tweet_id="101", source_user_name="alice", is_retweet=False)]) for index in range(users)}
    def read():
        return json.loads(json.dumps(state))
    def update(function):
        function(state)
    config = config or SimpleNamespace(twitter_template_enabled=True, twitter_translation_enabled=True,
        twitter_translation_primary=dict(url="https://primary.test", key="fake-secret", model="gemini-test"),
        twitter_translation_fallback=dict(url="https://fallback.test", key="fake-secret", model="deepseek-test"))
    pipeline = feature.pipeline.TweetPipeline(read_state=read, update_state=update, config=config,
        fetch=AsyncMock(return_value=source(feature)), translator=translator, render=AsyncMock(return_value=[]),
        prepare=AsyncMock(return_value=dict(status=True, delivery_parts=[])), deliver=AsyncMock(return_value=True),
        cache_dir=tmp_path / "pipeline", clock=lambda: now[0])
    return pipeline, state


@pytest.mark.asyncio
@pytest.mark.parametrize("users", [1, 2])
async def test_feedback_survives_restart_shared_subscriptions_and_a_network_failure(feature, tmp_path, users):
    requests = []
    def handler(request):
        requests.append(json.loads(json.loads(request.content)["messages"][1]["content"]))
        if len(requests) == 2:
            return httpx.Response(503)
        return api_response(json.dumps(dict(items=[output("两次" if len(requests) == 1 else "2 次")])))
    translator = feature.translation.Translator(tmp_path / "translations", transport=httpx.MockTransport(handler))
    now = [0]
    pipeline, state = pipeline_for(feature, tmp_path, translator, now, users)
    await pipeline.tick()
    assert len(requests) == 1
    processing = state["user0"]["pending_tweets"][0]["processing"]
    assert processing["primary_attempts"] == 1 and processing["translation_feedback"]["post_id"] == "101"
    fresh = feature.pipeline.TweetPipeline(read_state=pipeline.read_state, update_state=pipeline.update_state, config=pipeline.config,
        fetch=pipeline.fetch, translator=translator, render=pipeline.render, prepare=pipeline.prepare, deliver=pipeline.deliver,
        cache_dir=pipeline.cache_dir, clock=lambda: now[0])
    await fresh.tick()
    assert len(requests) == 1  # The persisted 60-second wait still applies.
    now[0] = 60
    await fresh.tick()
    processing = state["user0"]["pending_tweets"][0]["processing"]
    assert processing["primary_attempts"] == 2 and processing["last_error"] == "http_503"
    assert processing["translation_feedback"]["code"] == "protected_content_changed"
    now[0] = 120
    await fresh.tick()
    assert len(requests) == 3 and requests[1]["validation_feedback"] == requests[2]["validation_feedback"]
    assert all(not user["pending_tweets"] for user in state.values())
    assert pipeline.render.await_args.args[0].main.translation.text == "2 次"
    shared = [json.loads(path.read_text()) for path in pipeline.cache_dir.glob('*-translation-state.json')]
    assert shared[0]["primary_attempts"] == 3 and shared[0]["translation_feedback"] is None
    assert "fake-secret" not in json.dumps(state) + json.dumps(shared)


@pytest.mark.asyncio
async def test_feedback_does_not_expand_ten_plus_three_budget_or_publish_rejected_text(feature, tmp_path):
    requests = []
    def handler(request):
        requests.append((request.url.host, json.loads(json.loads(request.content)["messages"][1]["content"])))
        return api_response(json.dumps(dict(items=[output("两次")])))
    translator = feature.translation.Translator(tmp_path / "translations", transport=httpx.MockTransport(handler))
    now = [0]
    pipeline, state = pipeline_for(feature, tmp_path, translator, now)
    for _ in range(12):
        await pipeline.tick()
        now[0] += 60
    assert [host for host, _ in requests] == ["primary.test"] * 10 + ["fallback.test"] * 3
    assert "validation_feedback" not in requests[0][1]
    assert all(payload["validation_feedback"]["code"] == "protected_content_changed" for _, payload in requests[1:])
    assert not state["user0"]["pending_tweets"] and pipeline.render.await_args.args[0].main.translation is None
    assert not list((tmp_path / "translations").glob('*.json'))


@pytest.mark.asyncio
async def test_feedback_over_budget_outputs_complete_original_without_repeating_models(feature, tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return api_response(json.dumps(dict(items=[output("两次")])))
    payload = feature.translation.build_payload(source(feature))
    limit = len(feature.translation.SYSTEM_PROMPT) + len(json.dumps(payload, ensure_ascii=False))
    translator = feature.translation.Translator(tmp_path / "translations", max_chars=limit, transport=httpx.MockTransport(handler))
    now = [0]
    pipeline, state = pipeline_for(feature, tmp_path, translator, now)
    await pipeline.tick()
    now[0] = 60
    await pipeline.tick()
    assert len(requests) == 1 and not state["user0"]["pending_tweets"]
    assert pipeline.render.await_args.args[0].main.text == "2 times"
    assert pipeline.render.await_args.args[0].main.translation is None


@asynccontextmanager
async def client_for(app):
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            yield client


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["gemini", "deepseek"])
@pytest.mark.parametrize("outcome,attempts,status", [("corrected", 2, "completed"), ("invalid", 2, "failed"),
                                                     ("network_first", 1, "failed"), ("network_second", 2, "failed")])
async def test_single_preview_corrects_once_with_feedback_and_never_switches_models(feature, tmp_path, mode, outcome, attempts, status):
    requests = []
    def handler(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        requests.append((request.url.host, payload))
        if outcome == "network_first" or outcome == "network_second" and len(requests) == 2:
            return httpx.Response(503)
        text = "2 次" if outcome == "corrected" and len(requests) == 2 else "两次"
        return api_response(json.dumps(dict(items=[output(text)])))
    settings = feature.preview_server.PreviewSettings("http://nitter.test", tmp_path / "previews",
        feature.llm_client.Route("https://gemini.test", "fake-secret", "gemini-test"),
        fallback_route=feature.llm_client.Route("https://deepseek.test", "fake-secret", "deepseek-test"), retry_seconds=0)
    translator = feature.translation.Translator(tmp_path / "translations", transport=httpx.MockTransport(handler))
    image = tmp_path / 'tweet.png'
    image.write_bytes(b'png')
    renderer = SimpleNamespace(render=AsyncMock(return_value=[str(image)]))
    app = feature.preview_server.create_app(settings, fetch=AsyncMock(return_value=source(feature)), translator=translator, renderer=renderer)
    async with client_for(app) as client:
        response = await client.post('/api/previews', json=dict(url='https://x.com/alice/status/101', mode=mode))
        async with asyncio.timeout(3):
            while True:
                job = (await client.get(response.json()['status_url'])).json()
                if job['status'] in {'completed', 'failed'}:
                    break
                await asyncio.sleep(.005)
    assert job['status'] == status and job['translation_attempts'] == len(requests) == attempts
    assert all(host == mode + '.test' for host, _ in requests)
    if attempts == 2:
        feedback = requests[1][1]['validation_feedback']
        assert feedback['post_id'] == '101' and feedback['number_changes']['missing'] == {'2': 1}
        assert job['feedback_applied'] is True
    if status == 'completed':
        assert renderer.render.await_args.args[0].main.translation.text == '2 次'
        assert job['translation_feedback'] is None
    else:
        renderer.render.assert_not_awaited()
    assert 'fake-secret' not in json.dumps(job)
