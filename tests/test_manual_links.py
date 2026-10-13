import ast
import asyncio
from copy import deepcopy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nonebot.exception import FinishedException
import pytest

ROOT = Path(__file__).resolve().parents[1]
TARGET = dict(bot_id="99", kind="group", group_id=1, user_id=2, message_id=3)


def make_worker(plugin, tmp_path, *, translator=None, now=None, state=None, queue=None, assets=None, render=None):
    package = plugin.api.__package__
    model = importlib.import_module(f"{package}.tweet_model")
    links = importlib.import_module(f"{package}.manual_links")
    pipeline = importlib.import_module(f"{package}.pipeline")
    view = model.TweetView(model.Post("101", model.Author("Alice", "alice"), [model.TextPart("Hello 2")]))
    state = state if state is not None else {}
    queue = queue or links.ManualLinkQueue(tmp_path / "manual.json", prepare_delivery=AsyncMock(
        return_value=dict(manual_delivery_version=2, delivery_parts=[dict(part="main", mode="reply", message=[dict(type="text", data=dict(text="translated"))])])),
        send_part=AsyncMock())
    config = SimpleNamespace(twitter_template_enabled=True, twitter_translation_enabled=True,
        twitter_translation_primary=dict(url="https://primary.test", key="fake-key", model="gemini-test"),
        twitter_translation_fallback=dict(url="https://fallback.test", key="fake-key", model="deepseek-test"))
    translator = translator or SimpleNamespace(timeout=60, translate=AsyncMock())
    worker = pipeline.TweetPipeline(read_state=lambda: deepcopy(state), update_state=lambda fn: fn(state),
        fetch=AsyncMock(return_value=view), render=render or AsyncMock(return_value=[]), translator=translator,
        prepare=AsyncMock(return_value=dict(delivery_parts=[])), deliver=AsyncMock(return_value=True),
        config=config, cache_dir=tmp_path / "tasks", clock=lambda: (now or [0])[0],
        prepare_assets=assets, manual_queue=queue)
    return worker, queue, translator, view


def enqueue(queue, **changes):
    return queue.enqueue(source_user_name="alice", tweet_id="101", target=dict(TARGET, **changes))


@pytest.mark.asyncio
async def test_manual_translation_renders_translated_view_and_keeps_subscriptions_separate(plugin, tmp_path):
    worker, queue, translator, view = make_worker(plugin, tmp_path)
    model = importlib.import_module(f"{plugin.api.__package__}.tweet_model")
    async def translate(view, route):
        view.main.translation = model.Translation(True, "英语", "你好 2", route.provider, route.model)
    translator.translate.side_effect = translate
    enqueue(queue)
    await worker.tick()
    assert worker.render.await_args.args[0].main.translation.text == "你好 2"
    translator.translate.assert_awaited_once()
    queue.send_part.assert_awaited_once()
    worker.prepare.assert_not_awaited()
    worker.deliver.assert_not_awaited()
    assert not queue.read() and not plugin.api.read_twitter_list()


def test_queue_deduplicates_one_message_but_keeps_other_messages_and_groups(plugin, tmp_path):
    worker, queue, _, _ = make_worker(plugin, tmp_path)
    key = enqueue(queue)
    assert enqueue(queue) == key
    enqueue(queue, message_id=4)
    enqueue(queue, group_id=5)
    recovered = type(queue)(queue.path, prepare_delivery=queue.prepare_delivery, send_part=queue.send_part)
    assert len(recovered.read()) == 3
    assert worker.is_manual(key)
    assert not worker.is_manual("alice")


@pytest.mark.asyncio
async def test_manual_retries_survive_restart_with_feedback_and_share_subscription_budget(plugin, tmp_path):
    now = [0]
    llm = importlib.import_module(f"{plugin.api.__package__}.llm_client")
    translator = SimpleNamespace(timeout=60, translate=AsyncMock(side_effect=llm.LLMError(
        "protected_content_changed", details=dict(post_id="101", changed=["numbers"],
        number_changes=dict(missing={"2": 1}, added={"3": 1})))))
    subscriptions = {"alice": dict(pending_tweets=[dict(signature="status:101", tweet_id="101", source_user_name="alice", is_retweet=False)])}
    worker, queue, _, _ = make_worker(plugin, tmp_path, translator=translator, now=now, state=subscriptions)
    key = enqueue(queue)
    await worker.tick()
    assert translator.translate.await_count == 1  # Shared with the subscription, even in this first tick.
    saved = queue.read()[key]["pending_tweets"][0]["processing"]
    assert saved["primary_attempts"] == 1 and saved["next_attempt_at"] == 60
    assert saved["translation_feedback"]["code"] == "protected_content_changed"
    recovered = type(queue)(queue.path, prepare_delivery=queue.prepare_delivery, send_part=queue.send_part)
    fresh, _, _, _ = make_worker(plugin, tmp_path, queue=recovered, translator=translator, now=now, state=subscriptions)
    await fresh.tick()
    assert translator.translate.await_count == 1
    translator.translate.side_effect = None
    now[0] = 60
    await fresh.tick()
    assert translator.translate.await_count == 2
    assert translator.translate.await_args.kwargs["feedback"]["code"] == "protected_content_changed"
    assert not recovered.read() and not subscriptions["alice"]["pending_tweets"]


@pytest.mark.asyncio
async def test_manual_thirteen_failures_fall_back_to_original(plugin, tmp_path):
    now = [0]
    llm = importlib.import_module(f"{plugin.api.__package__}.llm_client")
    translator = SimpleNamespace(timeout=60, translate=AsyncMock(side_effect=llm.LLMError("timeout")))
    worker, queue, _, _ = make_worker(plugin, tmp_path, translator=translator, now=now)
    enqueue(queue)
    for _ in range(12):
        await worker.tick()
        now[0] += 60
    assert translator.translate.await_count == 13
    assert [call.args[1].model for call in translator.translate.await_args_list] == ["gemini-test"] * 10 + ["deepseek-test"] * 3
    assert worker.render.await_args.args[0].main.translation is None
    assert not queue.read()


@pytest.mark.asyncio
async def test_manual_asset_download_overlaps_translation(plugin, tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    async def download(view):
        started.set()
        await release.wait()
        return {"asset": "cached"}
    async def translate(view, route):
        await started.wait()
        release.set()
    worker, queue, _, _ = make_worker(plugin, tmp_path, assets=AsyncMock(side_effect=download),
        translator=SimpleNamespace(timeout=60, translate=AsyncMock(side_effect=translate)))
    enqueue(queue)
    await asyncio.wait_for(worker.tick(), timeout=2)
    assert worker.render.await_args.kwargs["assets"] == {"asset": "cached"}
    assert not worker.asset_tasks


@pytest.mark.asyncio
async def test_delivery_restart_skips_successful_parts_and_reuses_frozen_media(plugin, tmp_path):
    now = [0]
    worker, queue, translator, _ = make_worker(plugin, tmp_path, now=now)
    queue.prepare_delivery.return_value = dict(manual_delivery_version=2, delivery_parts=[
        dict(part=part, mode="direct", message=[dict(type="text", data=dict(text=part))])
        for part in ("main", "video:0", "video:1")])
    queue.send_part.side_effect = [None, ValueError("failed")]
    key = enqueue(queue)
    await worker.tick()
    assert queue.read()[key]["sent_parts"] == ["main"]
    assert [call.args[1] for call in queue.send_part.await_args_list] == ["reply", "video"]
    recovered = type(queue)(queue.path, prepare_delivery=queue.prepare_delivery, send_part=AsyncMock())
    fresh, _, _, _ = make_worker(plugin, tmp_path, queue=recovered, translator=translator, now=now)
    now[0] = 60
    await fresh.tick()
    assert [call.args[2][0]["data"]["text"] for call in recovered.send_part.await_args_list] == ["video:0", "video:1"]
    assert all(call.args[1] == "video" for call in recovered.send_part.await_args_list)
    assert translator.translate.await_count == 1
    queue.prepare_delivery.assert_awaited_once()
    fresh.fetch.assert_not_awaited()
    fresh.render.assert_not_awaited()
    assert not recovered.read()


@pytest.mark.asyncio
async def test_disconnected_bot_keeps_manual_delivery_without_retranslation(plugin, tmp_path):
    now = [0]
    worker, queue, translator, _ = make_worker(plugin, tmp_path, now=now)
    queue.send_part.side_effect = ValueError("bot_disconnected")
    key = enqueue(queue)
    await worker.tick()
    assert queue.read()[key]["pending_tweets"][0]["processing"]["stage"] == "delivering"
    queue.send_part.side_effect = None
    now[0] = 60
    await worker.tick()
    assert not queue.read() and translator.translate.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", ["translation", "both", "routes", "empty"])
async def test_manual_original_path_when_disabled_unconfigured_or_no_text(plugin, tmp_path, disabled):
    worker, queue, translator, view = make_worker(plugin, tmp_path)
    if disabled in {"translation", "both"}:
        worker.config.twitter_translation_enabled = False
        if disabled == "both":
            worker.config.twitter_template_enabled = False
    elif disabled == "routes":
        worker.config.twitter_translation_primary = worker.config.twitter_translation_fallback = None
    else:
        view.main.parts = []
    enqueue(queue)
    await worker.tick()
    translator.translate.assert_not_awaited()
    queue.send_part.assert_awaited_once()
    assert not queue.read()


def handler_namespace(plugin):
    tree = ast.parse((ROOT / "nonebot_plugin_twitter/__init__.py").read_text())
    names = {"pat_twitter_handle", "send_manual_tweet_part", "clean_pic_cache"}
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    group_type = type("GroupEvent", (), {})
    ns = dict(plugin.api.__dict__, RegexStr=lambda: None, FinishedException=FinishedException, GroupMessageEvent=group_type,
        ensure_twitter_url=AsyncMock(return_value=True), create_browser=AsyncMock(), close_browser_resources=AsyncMock(),
        twitter_pipeline=None, twitter_manual_queue=None)
    selected = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    ast.fix_missing_locations(selected)
    exec(compile(selected, str(ROOT / "nonebot_plugin_twitter/__init__.py"), "exec"), ns)
    return ns, group_type


@pytest.mark.asyncio
async def test_handler_enqueues_without_browser_fetch_or_llm_wait(plugin, tmp_path):
    worker, queue, translator, _ = make_worker(plugin, tmp_path)
    ns, group_type = handler_namespace(plugin)
    plugin.config.twitter_translation_enabled = True
    ns.update(twitter_pipeline=worker, twitter_manual_queue=queue)
    event = group_type()
    event.group_id, event.user_id, event.message_id = 1, 2, 3
    matcher = SimpleNamespace(finish=AsyncMock(side_effect=FinishedException()), send=AsyncMock())
    with pytest.raises(FinishedException):
        await ns["pat_twitter_handle"](SimpleNamespace(self_id="99"), event, matcher, "x.com/alice/status/101")
    assert len(queue.read()) == 1 and worker.wake.is_set()
    assert "翻译队列" in matcher.finish.await_args_list[0].args[0]
    ns["create_browser"].assert_not_awaited()
    ns["ensure_twitter_url"].assert_not_awaited()
    translator.translate.assert_not_awaited()
    worker.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_group_never_enqueues_or_fetches(plugin, tmp_path):
    worker, queue, _, _ = make_worker(plugin, tmp_path)
    ns, group_type = handler_namespace(plugin)
    plugin.config.twitter_translation_enabled = True
    ns.update(twitter_pipeline=worker, twitter_manual_queue=queue)
    plugin.api.update_link_list(lambda state: state.update({"1": dict(link=False)}))
    event = group_type()
    event.group_id, event.user_id, event.message_id = 1, 2, 3
    with pytest.raises(FinishedException):
        await ns["pat_twitter_handle"](SimpleNamespace(self_id="99"), event,
            SimpleNamespace(finish=AsyncMock(side_effect=FinishedException()), send=AsyncMock()), "x.com/alice/status/101")
    assert not queue.read()
    ns["create_browser"].assert_not_awaited()
    ns["ensure_twitter_url"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,mode,wrapped", [
    (kind, mode, wrapped) for kind in ("group", "private")
    for mode, wrapped in (("reply", False), ("direct", False), ("video", False), ("reply", True))])
async def test_background_sender_replies_only_to_preview_and_never_forwards(plugin, kind, mode, wrapped):
    ns, _ = handler_namespace(plugin)
    bot = SimpleNamespace(**{name: AsyncMock() for name in (
        "send_group_msg", "send_private_msg", "send_group_forward_msg", "send_private_forward_msg")})
    ns["get_bots"] = lambda: {"99": bot}
    target = dict(TARGET, kind=kind)
    segment = plugin.api.MessageSegment.video("video.mp4") if mode == "video" else plugin.api.MessageSegment.image(b"image")
    message = plugin.api.Message(segment)
    if wrapped:
        message = plugin.api.Message(plugin.api.MessageSegment.node_custom(user_id=1, nickname="Alice", content=message))
    await ns["send_manual_tweet_part"](target, mode, plugin.api.serialize_message(message))
    name = f"send_{kind}_msg"
    sent = getattr(bot, name)
    sent.assert_awaited_once()
    kwargs = sent.await_args.kwargs
    assert kwargs["group_id" if kind == "group" else "user_id"] == (1 if kind == "group" else 2)
    bot.send_group_forward_msg.assert_not_awaited()
    bot.send_private_forward_msg.assert_not_awaited()
    assert not any(segment.type == "node" for segment in kwargs["message"])
    if mode == "reply":
        assert kwargs["message"][0].type == "reply" and kwargs["message"][0].data["id"] == "3"
    else:
        assert not any(segment.type == "reply" for segment in kwargs["message"])
        assert kwargs["message"][0].type == segment.type


def test_daily_cleanup_preserves_media_pinned_by_manual_request(plugin, tmp_path):
    worker, queue, _, _ = make_worker(plugin, tmp_path)
    ns, _ = handler_namespace(plugin)
    ns["twitter_manual_queue"] = queue
    cache = Path("data/twitter/cache")
    pinned, old = cache / "1.mp4", cache / "2.mp4"
    pinned.write_bytes(b"pinned")
    old.write_bytes(b"old")
    delivery = tmp_path / "delivery.json"
    delivery.write_text(json.dumps(dict(pinned_files=[str(pinned.resolve())])))
    key = enqueue(queue)
    queue.update(lambda state: state[key]["pending_tweets"][0].update(processing=dict(delivery_cache=str(delivery))))
    ns["clean_pic_cache"]()
    assert pinned.exists() and not old.exists()


@pytest.mark.asyncio
async def test_manual_reply_groups_previews_main_photos_gifs_and_urls_then_sends_main_videos(plugin, tmp_path, monkeypatch):
    plugin.config.twitter_node = True
    preview = tmp_path / "preview.png"
    preview.write_bytes(b"preview")
    second = tmp_path / "second.png"
    second.write_bytes(b"second")
    gif, video = tmp_path / "loop.gif", tmp_path / "clip.mp4"
    gif.write_bytes(b"GIF89a")
    video.write_bytes(b"mp4")
    photo = AsyncMock(side_effect=lambda url: plugin.api.MessageSegment.image(url.encode()))
    monkeypatch.setattr(plugin.api, "get_pic", photo)
    monkeypatch.setattr(plugin.api, "get_gif_path", AsyncMock(return_value=str(gif)))
    monkeypatch.setattr(plugin.api, "get_video_path", AsyncMock(return_value=[str(video)]))
    monkeypatch.setattr(plugin.api, "get_video", AsyncMock(return_value=plugin.api.MessageSegment.video("clip.mp4")))
    model = importlib.import_module(f"{plugin.api.__package__}.tweet_model")
    view = model.TweetView(model.Post("101", parts=[model.TextPart("hello "),
        model.TextPart("https://main.test", "url", "https://main.test")],
        media=[model.Media("image", "main.jpg"), model.Media("gif", "gif.mp4"),
               model.Media("video", "video.mp4"), model.Media("video", "second-video.mp4")],
        quote=model.Post("102", parts=[model.TextPart("https://quote.test", "url", "https://quote.test")],
                         media=[model.Media("image", "quote.jpg"), model.Media("gif", "quote-gif.mp4"), model.Media("video", "quote-video.mp4")])) ,
        replies=[model.Post("103", media=[model.Media("image", "reply.jpg"),
                 model.Media("gif", "reply-gif.mp4"), model.Media("video", "reply-video.mp4")])])
    info = dict(view.legacy_fields(), rendered_images=[str(preview), str(second)])
    prepared = await plugin.api.prepare_manual_tweet_delivery(info, "alice", "101", {})
    assert [(part["part"], part["mode"]) for part in prepared["delivery_parts"]] == [
        ("main", "reply"), ("video:0", "video"), ("video:1", "video")]
    assert all(part["message"][0]["type"] != "node" for part in prepared["delivery_parts"])
    assert [call.args[0] for call in photo.await_args_list] == ["main.jpg"]
    plugin.api.get_gif_path.assert_awaited_once_with("gif.mp4")
    assert [call.args[0] for call in plugin.api.get_video_path.await_args_list] == ["video.mp4", "second-video.mp4"]
    main = prepared["delivery_parts"][0]["message"]
    assert [segment["type"] for segment in main] == ["image"] * 4 + ["text"]
    assert [segment["data"]["file"] for segment in main[:4]] == [
        plugin.api.MessageSegment.image(content).data["file"] for content in (b"preview", b"second", b"main.jpg", b"GIF89a")]
    assert main[-1]["data"]["text"].strip() == "https://main.test"
    assert prepared["manual_delivery_version"] == 2
    # The shared packer differs only in whether its first message is a reply.
    automatic = await plugin.api.prepare_tweet_delivery(info, "alice", "101", {})
    assert automatic["delivery_parts"][0]["mode"] == "direct"
    assert [part["message"] for part in automatic["delivery_parts"]] == [part["message"] for part in prepared["delivery_parts"]]


@pytest.mark.asyncio
async def test_manual_render_failure_groups_original_text_and_photos_in_one_reply(plugin, monkeypatch):
    monkeypatch.setattr(plugin.api, "get_pic", AsyncMock(return_value=plugin.api.MessageSegment.image(b"photo")))
    prepared = await plugin.api.prepare_manual_tweet_delivery(
        dict(status=True, text=["hello"], pic_url_list=["photo.jpg"]), "alice", "101", {})
    assert [(part["part"], part["mode"]) for part in prepared["delivery_parts"]] == [("main", "reply")]
    assert prepared["delivery_parts"][0]["message"][0]["type"] == "text"
    assert prepared["delivery_parts"][0]["message"][1]["type"] == "image"


@pytest.mark.asyncio
async def test_manual_legacy_html_preview_groups_photos_in_same_reply(plugin, monkeypatch):
    plugin.config.twitter_htmlmode = True
    plugin.config.twitter_node = True
    monkeypatch.setattr(plugin.api, "get_pic", AsyncMock(return_value=plugin.api.MessageSegment.image(b"photo")))
    prepared = await plugin.api.prepare_manual_tweet_delivery(
        dict(status=True, text=["hello"], html=b"screenshot", pic_url_list=["photo.jpg"]), "alice", "101", {})
    assert [(part["part"], part["mode"]) for part in prepared["delivery_parts"]] == [("main", "reply")]
    assert [segment["type"] for segment in prepared["delivery_parts"][0]["message"]] == ["image", "image"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["group", "private"])
async def test_url_handler_without_translation_uses_same_grouped_reply_and_normal_video_policy(plugin, tmp_path, monkeypatch, kind):
    plugin.config.twitter_translation_enabled = False
    plugin.config.twitter_template_enabled = True
    plugin.config.twitter_node = True
    preview = tmp_path / "preview.png"
    preview.write_bytes(b"preview")
    monkeypatch.setattr(plugin.api, "get_pic", AsyncMock(return_value=plugin.api.MessageSegment.image(b"photo")))
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(plugin.api, "get_video_path", AsyncMock(return_value=[str(video)]))
    monkeypatch.setattr(plugin.api, "get_video", AsyncMock(return_value=plugin.api.MessageSegment.video(str(video))))
    ns, group_type = handler_namespace(plugin)
    ns["get_tweet"] = AsyncMock(return_value=dict(status=True, text=["hello"], rendered_images=[str(preview)],
        pic_url_list=["photo.jpg"], video_url="video.mp4", url_list=["https://main.test"]))
    ns["send_manual_tweet_part"] = AsyncMock()
    event = group_type() if kind == "group" else SimpleNamespace()
    event.group_id, event.user_id, event.message_id = 1, 2, 3
    with pytest.raises(FinishedException):
        await ns["pat_twitter_handle"](SimpleNamespace(self_id="99"), event,
            SimpleNamespace(finish=AsyncMock(side_effect=FinishedException()), send=AsyncMock()), "x.com/alice/status/101")
    assert [call.args[1] for call in ns["send_manual_tweet_part"].await_args_list] == ["reply", "video"]
    expected = dict(TARGET, kind=kind)
    if kind == "private":
        expected.pop("group_id")
    assert all(call.args[0] == expected for call in ns["send_manual_tweet_part"].await_args_list)
    main = ns["send_manual_tweet_part"].await_args_list[0].args[2]
    assert [segment["type"] for segment in main] == ["image", "image", "text"]
    assert main[-1]["data"]["text"].strip() == "https://main.test"
    ns["create_browser"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["group", "private"])
async def test_translated_link_queue_sends_grouped_reply_then_normal_main_video(plugin, tmp_path, monkeypatch, kind):
    worker, queue, translator, view = make_worker(plugin, tmp_path)
    model = importlib.import_module(f"{plugin.api.__package__}.tweet_model")
    view.main.parts.append(model.TextPart(" https://main.test"))
    view.main.media = [model.Media("image", "main.jpg"), model.Media("gif", "main-gif.mp4"),
                       model.Media("video", "main-video.mp4")]
    view.main.quote = model.Post("102", media=[model.Media("image", "quote.jpg"),
        model.Media("gif", "quote-gif.mp4"), model.Media("video", "quote-video.mp4")])
    preview, gif, video = tmp_path / "preview.png", tmp_path / "main.gif", tmp_path / "main.mp4"
    preview.write_bytes(b"preview")
    gif.write_bytes(b"GIF89a")
    video.write_bytes(b"video")
    worker.render.return_value = [str(preview)]
    queue.prepare_delivery = AsyncMock(wraps=plugin.api.prepare_manual_tweet_delivery)
    queue.prepare_photos = plugin.api.prepare_manual_photos
    monkeypatch.setattr(plugin.api, "get_pic", AsyncMock(return_value=plugin.api.MessageSegment.image(b"main-photo")))
    monkeypatch.setattr(plugin.api, "get_gif_path", AsyncMock(return_value=str(gif)))
    monkeypatch.setattr(plugin.api, "get_video_path", AsyncMock(return_value=[str(video)]))
    monkeypatch.setattr(plugin.api, "get_video", AsyncMock(return_value=plugin.api.MessageSegment.video("main.mp4")))
    target = dict(TARGET, kind=kind)
    if kind == "private":
        target.pop("group_id")
    queue.enqueue(source_user_name="alice", tweet_id="101", target=target)

    await worker.tick()

    translator.translate.assert_awaited_once()
    assert [call.args[0] for call in queue.send_part.await_args_list] == [target, target]
    assert [call.args[1] for call in queue.send_part.await_args_list] == ["reply", "video"]
    message = queue.send_part.await_args_list[0].args[2]
    assert [segment["type"] for segment in message] == ["image", "image", "image", "text"]
    assert message[-1]["data"]["text"] == "\nhttps://main.test"
    plugin.api.get_pic.assert_awaited_once_with("main.jpg")
    plugin.api.get_gif_path.assert_awaited_once_with("main-gif.mp4")
    plugin.api.get_video_path.assert_awaited_once_with("main-video.mp4")
    assert not queue.read()
    worker.deliver.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_frozen_delivery_groups_only_main_media_and_keeps_receipts_across_retries(plugin, tmp_path, monkeypatch):
    now = [0]
    worker, queue, translator, view = make_worker(plugin, tmp_path, now=now)
    model = importlib.import_module(f"{plugin.api.__package__}.tweet_model")
    view.main.parts.append(model.TextPart(" https://main.test https://main.test"))
    view.main.media = [model.Media("image", "main.jpg"), model.Media("gif", "main-gif.mp4"),
                       model.Media("video", "main-video.mp4"), model.Media("video", "second-video.mp4")]
    view.main.quote = model.Post("102", media=[model.Media("image", "quote.jpg"),
        model.Media("gif", "quote-gif.mp4"), model.Media("video", "quote-video.mp4")])
    view.replies = [model.Post("103", media=[model.Media("image", "reply.jpg"),
        model.Media("gif", "reply-gif.mp4"), model.Media("video", "reply-video.mp4")])]
    frozen_preview = plugin.api.Message(plugin.api.MessageSegment.node_custom(
        user_id=1, nickname="Alice", content=plugin.api.Message(plugin.api.MessageSegment.image(b"frozen"))))
    queue.prepare_delivery.return_value = dict(delivery_parts=[
        dict(part="image:0", mode="node", message=plugin.api.serialize_message(frozen_preview)),
        dict(part="gif:0", mode="direct", message=plugin.api.serialize_message(plugin.api.Message(plugin.api.MessageSegment.image(b"main-gif")))),
        dict(part="gif:1", mode="direct", message=plugin.api.serialize_message(plugin.api.Message(plugin.api.MessageSegment.image(b"quote-gif")))),
        dict(part="gif:2", mode="direct", message=plugin.api.serialize_message(plugin.api.Message(plugin.api.MessageSegment.image(b"reply-gif")))),
        dict(part="video:0", mode="video", message=plugin.api.serialize_message(plugin.api.Message(plugin.api.MessageSegment.video("frozen.mp4"))))])
    queue.send_part.side_effect = [None, ValueError("gif_failed")]
    key = enqueue(queue)
    await worker.tick()
    assert queue.read()[key]["sent_parts"] == ["image:0"]
    photos = AsyncMock(side_effect=lambda url: plugin.api.MessageSegment.image(url.encode()))
    monkeypatch.setattr(plugin.api, "get_pic", photos)
    monkeypatch.setattr(plugin.api, "get_gif_path", AsyncMock())
    video = tmp_path / "second.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(plugin.api, "get_video_path", AsyncMock(return_value=[str(video)]))
    monkeypatch.setattr(plugin.api, "get_video", AsyncMock(return_value=plugin.api.MessageSegment.video("second.mp4")))
    upgrade = AsyncMock(wraps=plugin.api.prepare_manual_tweet_delivery)
    recovered = type(queue)(queue.path, prepare_delivery=upgrade, send_part=AsyncMock(side_effect=ValueError("main_failed")),
        prepare_photos=plugin.api.prepare_manual_photos)
    fresh, _, _, _ = make_worker(plugin, tmp_path, now=now, queue=recovered, translator=translator)
    now[0] = 60
    await fresh.tick()
    assert recovered.read()[key]["sent_parts"] == ["image:0"]
    recovered.send_part.assert_awaited_once()
    assert recovered.send_part.await_args.args[1] == "reply"
    main = recovered.send_part.await_args.args[2]
    assert [segment["type"] for segment in main] == ["image", "image", "text"]
    assert [segment["data"]["file"] for segment in main[:2]] == [
        plugin.api.MessageSegment.image(content).data["file"] for content in (b"main.jpg", b"main-gif")]
    assert main[-1]["data"]["text"] == "\nhttps://main.test"
    recovered.send_part.reset_mock(side_effect=True)
    recovered.send_part.side_effect = [None, ValueError("video_failed")]
    now[0] = 120
    await fresh.tick()
    assert recovered.read()[key]["sent_parts"] == ["image:0", "main"]
    assert [call.args[1] for call in recovered.send_part.await_args_list] == ["reply", "video"]
    recovered.send_part.reset_mock(side_effect=True)
    now[0] = 180
    await fresh.tick()
    assert [call.args[2][0]["data"]["file"] for call in recovered.send_part.await_args_list] == ["frozen.mp4", "second.mp4"]
    assert all(call.args[1] == "video" for call in recovered.send_part.await_args_list)
    photos.assert_awaited_once_with("main.jpg")
    plugin.api.get_gif_path.assert_not_awaited()
    plugin.api.get_video_path.assert_awaited_once_with("second-video.mp4")
    assert translator.translate.await_count == 1
    queue.prepare_delivery.assert_awaited_once()
    upgrade.assert_awaited_once()
    fresh.fetch.assert_not_awaited()
    fresh.render.assert_not_awaited()
    assert not recovered.read()
