import importlib
import json
from unittest.mock import AsyncMock

import pytest


def model(plugin):
    return importlib.import_module(f"{plugin.api.__package__}.tweet_model")


def subscribe(plugin, *, groups=("1",), private=()):
    settings = dict(status=True, r18=True, media=False)
    state = {"alice": dict(screen_name="Alice", group={key: dict(settings) for key in groups},
                           private={key: dict(settings) for key in private})}
    plugin.api.update_twitter_list(lambda current: current.update(state))
    return state


def packed(plugin, part, content, mode="direct"):
    return dict(part=part, mode=mode, message=plugin.api.serialize_message(plugin.api.Message(content)))


def source_view(plugin):
    schema = model(plugin)
    return schema.TweetView(schema.Post("101", parts=[
        schema.TextPart("Body "), schema.TextPart("https://main.test/a?q=1", "url", "https://main.test/a?q=1"),
        schema.TextPart(" https://second.test/path https://main.test/a?q=1")], media=[
            schema.Media("image", "main.jpg"), schema.Media("gif", "main-gif.mp4"),
            schema.Media("video", "main-video.mp4"), schema.Media("video", "second-video.mp4")],
        quote=schema.Post("102", parts=[schema.TextPart("https://quote.test", "url", "https://quote.test")],
                          media=[schema.Media("image", "quote.jpg"), schema.Media("gif", "quote-gif.mp4"),
                                 schema.Media("video", "quote-video.mp4")])),
        replies=[schema.Post("103", media=[schema.Media("image", "reply.jpg"),
            schema.Media("gif", "reply-gif.mp4"), schema.Media("video", "reply-video.mp4")])])


def test_legacy_fields_expose_only_main_attachments_and_ordered_body_urls(plugin):
    source = source_view(plugin)
    fields = source.legacy_fields()
    assert fields["pic_url_list"] == ["main.jpg"]
    assert fields["gif_url_list"] == ["main-gif.mp4"]
    assert fields["video_url_list"] == ["main-video.mp4", "second-video.mp4"]
    assert fields["video_url"] == "main-video.mp4"
    assert fields["url_list"] == ["https://main.test/a?q=1", "https://second.test/path"]
    assert fields["view"]["main"]["quote"]["media"] and fields["view"]["replies"][0]["media"]


@pytest.mark.asyncio
async def test_automatic_packs_all_rendered_and_main_media_before_urls_and_each_video_file(plugin, tmp_path, monkeypatch):
    state = subscribe(plugin)
    source = source_view(plugin)
    paths = []
    for index in range(2):
        path = tmp_path / f"preview{index}.png"
        path.write_bytes(f"preview{index}".encode())
        paths.append(str(path))
    gif = tmp_path / "loop.gif"
    gif.write_bytes(b"GIF89a")
    photo = AsyncMock(return_value=plugin.api.MessageSegment.image(b"main-photo"))
    monkeypatch.setattr(plugin.api, "get_pic", photo)
    monkeypatch.setattr(plugin.api, "get_gif_path", AsyncMock(return_value=str(gif)))
    downloads = AsyncMock(side_effect=[[str(tmp_path / "first.mp4"), str(tmp_path / "second.mp4")],
                                       [str(tmp_path / "third.mp4")]])
    monkeypatch.setattr(plugin.api, "get_video_path", downloads)
    async def video(path):
        return plugin.api.MessageSegment.video(path)
    monkeypatch.setattr(plugin.api, "get_video", video)
    plugin.config.twitter_node = True
    # Simulate a legacy caller supplying quote/reply media lists alongside its parsed view.
    info = dict(source.legacy_fields(), rendered_images=paths,
                pic_url_list=["quote.jpg"], gif_url_list=["reply-gif.mp4"], video_url="quote-video.mp4")
    prepared = await plugin.api.prepare_tweet_delivery(info, "alice", "101", state)
    assert [(part["part"], part["mode"]) for part in prepared["delivery_parts"]] == [
        ("main", "direct"), ("video:0", "video"), ("video:1", "video"), ("video:2", "video")]
    main = prepared["delivery_parts"][0]["message"]
    assert [segment["type"] for segment in main] == ["image"] * 4 + ["text"]
    assert [segment["data"]["file"] for segment in main[:4]] == [
        plugin.api.MessageSegment.image(data).data["file"] for data in (b"preview0", b"preview1", b"main-photo", b"GIF89a")]
    assert main[-1]["data"]["text"] == "\nhttps://main.test/a?q=1\nhttps://second.test/path"
    photo.assert_awaited_once_with("main.jpg")
    plugin.api.get_gif_path.assert_awaited_once_with("main-gif.mp4")
    assert [call.args[0] for call in downloads.await_args_list] == ["main-video.mp4", "second-video.mp4"]
    assert prepared["pinned_files"] == [str(gif.resolve()), *[str((tmp_path / name).resolve())
                                                           for name in ("first.mp4", "second.mp4", "third.mp4")]]
    assert await plugin.api.tweet_handle(prepared, "alice", "101", state)
    assert [call.kwargs["msg"][0].type for call in plugin.tools.send_group_msg_by_bots.await_args_list] == [
        "image", "video", "video", "video"]
    plugin.tools.send_group_forward_msg_by_bots.assert_not_called()


@pytest.mark.asyncio
async def test_each_target_must_receive_main_before_its_videos(plugin):
    state = subscribe(plugin, groups=("1", "2"), private=("3",))
    prepared = dict(status=True, media=True, r18=False, delivery_version=2, delivery_parts=[
        packed(plugin, "main", plugin.api.MessageSegment.text("main")),
        packed(plugin, "video:0", plugin.api.MessageSegment.video("video.mp4"), "video")])
    plugin.tools.send_group_msg_by_bots.side_effect = lambda *, group_id, msg: group_id == 1
    assert not await plugin.api.tweet_handle(prepared, "alice", "101", state)
    group_calls = plugin.tools.send_group_msg_by_bots.await_args_list
    assert [(call.kwargs["group_id"], call.kwargs["msg"][0].type) for call in group_calls] == [
        (1, "text"), (2, "text"), (1, "video")]
    assert [call.kwargs["msg"][0].type for call in plugin.tools.send_private_msg_by_bots.await_args_list] == ["text", "video"]
    plugin.tools.send_group_msg_by_bots.reset_mock(side_effect=True)
    plugin.tools.send_group_msg_by_bots.return_value = True
    plugin.tools.send_private_msg_by_bots.reset_mock()
    assert await plugin.api.tweet_handle(prepared, "alice", "101", state)
    assert [(call.kwargs["group_id"], call.kwargs["msg"][0].type)
            for call in plugin.tools.send_group_msg_by_bots.await_args_list] == [(2, "text"), (2, "video")]
    plugin.tools.send_private_msg_by_bots.assert_not_called()


@pytest.mark.asyncio
async def test_without_main_videos_only_first_message_is_sent_even_when_quote_has_video(plugin, tmp_path):
    state = subscribe(plugin)
    source = source_view(plugin)
    source.main.media = []
    preview = tmp_path / "preview.png"
    preview.write_bytes(b"preview")
    prepared = await plugin.api.prepare_tweet_delivery(dict(source.legacy_fields(), rendered_images=[str(preview)]),
                                                      "alice", "101", state)
    assert [part["part"] for part in prepared["delivery_parts"]] == ["main"]
    assert await plugin.api.tweet_handle(prepared, "alice", "101", state)
    plugin.tools.send_group_msg_by_bots.assert_awaited_once()


@pytest.mark.asyncio
async def test_old_frozen_cache_filters_context_media_and_does_not_repeat_successful_images(plugin, tmp_path, monkeypatch):
    state = subscribe(plugin, groups=("1", "2"))
    source = source_view(plugin)
    view_cache, delivery_cache = tmp_path / "view.json", tmp_path / "delivery.json"
    view_cache.write_text(json.dumps(source.to_dict()))
    preview = packed(plugin, "image:0", plugin.api.MessageSegment.node_custom(
        user_id=1, nickname="Alice", content=plugin.api.Message(plugin.api.MessageSegment.image(b"preview"))), "node")
    old = dict(status=True, media=True, r18=False, pinned_files=[], delivery_parts=[preview,
        packed(plugin, "gif:0", plugin.api.MessageSegment.image(b"main-gif")),
        packed(plugin, "gif:1", plugin.api.MessageSegment.image(b"quote-gif")),
        packed(plugin, "gif:2", plugin.api.MessageSegment.image(b"reply-gif")),
        packed(plugin, "video:0", plugin.api.MessageSegment.video("frozen-first.mp4"), "video")])
    delivery_cache.write_text(json.dumps(old))
    def pending(current):
        current["alice"]["pending_tweets"] = [dict(tweet_id="101", processing=dict(
            view_cache=str(view_cache), delivery_cache=str(delivery_cache))) ]
        current["alice"]["pending_deliveries"] = {"101": {"group:1": ["image:0", "gif:0", "video:0"]}}
    plugin.api.update_twitter_list(pending)
    monkeypatch.setattr(plugin.api, "get_pic", AsyncMock(return_value=plugin.api.MessageSegment.image(b"main-photo")))
    monkeypatch.setattr(plugin.api, "get_gif_path", AsyncMock())
    monkeypatch.setattr(plugin.api, "get_video_path", AsyncMock(return_value=[str(tmp_path / "second.mp4")]))
    monkeypatch.setattr(plugin.api, "get_video", AsyncMock(return_value=plugin.api.MessageSegment.video("second.mp4")))
    assert await plugin.api.tweet_handle(old, "alice", "101", plugin.api.read_twitter_list())
    calls = plugin.tools.send_group_msg_by_bots.await_args_list
    first = next(call.kwargs["msg"] for call in calls if call.kwargs["group_id"] == 1)
    assert [segment.type for segment in first] == ["image", "text"]
    assert first[0].data["file"] == plugin.api.MessageSegment.image(b"main-photo").data["file"]
    second = next(call.kwargs["msg"] for call in calls if call.kwargs["group_id"] == 2)
    assert [segment.type for segment in second] == ["image", "image", "image", "text"]
    assert second[2].data["file"] == plugin.api.MessageSegment.image(b"main-gif").data["file"]
    plugin.api.get_gif_path.assert_not_called()
    plugin.api.get_video_path.assert_awaited_once_with("second-video.mp4")
    migrated = json.loads(delivery_cache.read_text())
    assert migrated["delivery_version"] == 2
    assert [part["part"] for part in migrated["delivery_parts"]] == ["main", "video:main:0", "video:main:1"]
    plugin.tools.send_group_msg_by_bots.reset_mock()
    assert await plugin.api.tweet_handle(migrated, "alice", "101", plugin.api.read_twitter_list())
    plugin.tools.send_group_msg_by_bots.assert_not_called()
    plugin.tools.send_group_forward_msg_by_bots.assert_not_called()


@pytest.mark.asyncio
async def test_untraceable_frozen_forward_retries_as_normal_message_with_old_receipt(plugin):
    state = subscribe(plugin, groups=("1", "2"))
    plugin.api.update_twitter_list(lambda current: current["alice"].update(
        pending_deliveries={"101": {"group:1": ["image:0"]}}))
    old = dict(status=True, media=False, delivery_parts=[packed(plugin, "image:0",
        plugin.api.MessageSegment.node_custom(user_id=1, nickname="Alice",
            content=plugin.api.Message(plugin.api.MessageSegment.image(b"frozen"))), "node")])
    assert await plugin.api.tweet_handle(old, "alice", "101", state)
    call = plugin.tools.send_group_msg_by_bots.await_args
    assert call.kwargs["group_id"] == 2 and call.kwargs["msg"][0].type == "image"
    plugin.tools.send_group_msg_by_bots.assert_awaited_once()
    plugin.tools.send_group_forward_msg_by_bots.assert_not_called()


@pytest.mark.asyncio
async def test_migrating_quote_only_media_keeps_rendered_preview_without_extra_media(plugin, tmp_path, monkeypatch):
    state = subscribe(plugin)
    source = source_view(plugin)
    source.main.media = []
    view_cache = tmp_path / "view.json"
    view_cache.write_text(json.dumps(source.to_dict()))
    plugin.api.update_twitter_list(lambda current: current["alice"].update(pending_tweets=[dict(
        tweet_id="101", processing=dict(view_cache=str(view_cache)))]))
    old = dict(status=True, media=True, delivery_parts=[
        packed(plugin, "image:0", plugin.api.MessageSegment.image(b"preview")),
        packed(plugin, "gif:0", plugin.api.MessageSegment.image(b"quote-gif")),
        packed(plugin, "gif:1", plugin.api.MessageSegment.image(b"reply-gif")),
        packed(plugin, "video:0", plugin.api.MessageSegment.video("quote.mp4"), "video")])
    monkeypatch.setattr(plugin.api, "get_video_path", AsyncMock())
    monkeypatch.setattr(plugin.api, "get_gif_path", AsyncMock())
    assert await plugin.api.tweet_handle(old, "alice", "101", plugin.api.read_twitter_list())
    message = plugin.tools.send_group_msg_by_bots.await_args.kwargs["msg"]
    assert [segment.type for segment in message] == ["image", "text"]
    assert message[0].data["file"] == plugin.api.MessageSegment.image(b"preview").data["file"]
    plugin.tools.send_group_msg_by_bots.assert_awaited_once()
    plugin.api.get_video_path.assert_not_called()
    plugin.api.get_gif_path.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("already_sent", [False, True])
async def test_migrating_original_fallback_reuses_only_main_photo_and_its_receipt(plugin, tmp_path, monkeypatch, already_sent):
    state = subscribe(plugin)
    source = source_view(plugin)
    source.main.media = [media for media in source.main.media if media.kind == "image"]
    view_cache = tmp_path / "view.json"
    view_cache.write_text(json.dumps(source.to_dict()))
    def pending(current):
        current["alice"]["pending_tweets"] = [dict(tweet_id="101", processing=dict(view_cache=str(view_cache)))]
        if already_sent:
            current["alice"]["pending_deliveries"] = {"101": {"group:1": ["original"]}}
    plugin.api.update_twitter_list(pending)
    original = [plugin.api.MessageSegment.text(source.main.text),
                plugin.api.MessageSegment.image(b"main-photo"), plugin.api.MessageSegment.image(b"quote-photo")]
    old = dict(status=True, media=True, delivery_parts=[packed(plugin, "original", original)])
    monkeypatch.setattr(plugin.api, "get_pic", AsyncMock())
    monkeypatch.setattr(plugin.api, "get_video_path", AsyncMock())
    monkeypatch.setattr(plugin.api, "get_gif_path", AsyncMock())
    assert await plugin.api.tweet_handle(old, "alice", "101", plugin.api.read_twitter_list())
    if already_sent:
        plugin.tools.send_group_msg_by_bots.assert_not_called()
    else:
        message = plugin.tools.send_group_msg_by_bots.await_args.kwargs["msg"]
        assert [segment.type for segment in message] == ["text", "image", "text"]
        assert message[1].data["file"] == plugin.api.MessageSegment.image(b"main-photo").data["file"]
    assert plugin.api.read_twitter_list()["alice"]["pending_deliveries"]["101"]["group:1"][-1] == "main"
    plugin.api.get_pic.assert_not_called()
    plugin.api.get_video_path.assert_not_called()
    plugin.api.get_gif_path.assert_not_called()
