import ast
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from nonebot.exception import FinishedException
import pytest


ROOT = Path(__file__).resolve().parents[1]


def handler_namespace(plugin):
    tree = ast.parse((ROOT / "nonebot_plugin_twitter/__init__.py").read_text())
    node = next(node for node in tree.body
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "twitter_timeline_handle")
    node.decorator_list = []
    selected = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node],
                          type_ignores=[])
    ast.fix_missing_locations(selected)
    ns = dict(plugin.api.__dict__, CommandArg=lambda: None, FinishedException=FinishedException,
              ensure_twitter_url=AsyncMock(return_value=True),
              get_user_info=AsyncMock(return_value={"status": True, "user_name": "alice"}),
              get_user_timeline=AsyncMock(return_value=["105", "104", "103", "102", "101", "100"]),
              get_timeline_images=AsyncMock(return_value=[b"first slice", b"second slice"]),
              create_browser=AsyncMock(), get_timeline_screen=AsyncMock(), close_browser_resources=AsyncMock())
    exec(compile(selected, str(ROOT / "nonebot_plugin_twitter/__init__.py"), "exec"), ns)
    return ns


async def invoke_handler(ns, matcher, user_name="alice"):
    arg = SimpleNamespace(extract_plain_text=lambda: user_name)
    with pytest.raises(FinishedException):
        await ns["twitter_timeline_handle"](None, None, matcher, arg)


def image_payloads(matcher):
    images = []
    for call in matcher.send.await_args_list:
        message = call.args[0]
        if not isinstance(message, str):
            assert message.type == "image"
            images.append(base64.b64decode(message.data["file"].removeprefix("base64://")))
    return images


@pytest.mark.asyncio
async def test_listing_uses_formal_renderer_without_htmlmode_and_sends_all_slices(plugin):
    plugin.config.twitter_htmlmode = False
    plugin.config.twitter_translation_enabled = True
    ns = handler_namespace(plugin)
    matcher = SimpleNamespace(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException()))

    await invoke_handler(ns, matcher, " alice ")

    ns["get_user_info"].assert_awaited_once_with("alice")
    ns["get_user_timeline"].assert_awaited_once_with("alice")
    ns["get_timeline_images"].assert_awaited_once_with("alice", ["105", "104", "103", "102", "101"])
    assert image_payloads(matcher) == [b"first slice", b"second slice"]
    ns["create_browser"].assert_not_awaited()
    ns["get_timeline_screen"].assert_not_awaited()
    ns["close_browser_resources"].assert_not_awaited()
    matcher.finish.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeline", [[], ["not found"]])
async def test_listing_empty_timeline_does_not_render_or_send_images(plugin, timeline):
    ns = handler_namespace(plugin)
    ns["get_user_timeline"].return_value = timeline
    matcher = SimpleNamespace(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException()))

    await invoke_handler(ns, matcher)

    ns["get_timeline_images"].assert_not_awaited()
    assert image_payloads(matcher) == []
    matcher.finish.assert_awaited_once_with("未找到 alice 的推文时间线")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["ensure_twitter_url", "get_user_info", "get_user_timeline", "get_timeline_images"])
async def test_listing_reports_fetch_or_render_failure_without_partial_results(plugin, operation):
    ns = handler_namespace(plugin)
    ns[operation].side_effect = RuntimeError("internal source failure")
    matcher = SimpleNamespace(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException()))

    await invoke_handler(ns, matcher)

    assert image_payloads(matcher) == []
    matcher.finish.assert_awaited_once_with("推文列表处理失败，请稍后重试。")


@pytest.mark.asyncio
async def test_listing_empty_render_has_explicit_failure(plugin):
    ns = handler_namespace(plugin)
    ns["get_timeline_images"].return_value = []
    matcher = SimpleNamespace(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException()))

    await invoke_handler(ns, matcher)

    assert image_payloads(matcher) == []
    matcher.finish.assert_awaited_once_with("推文列表渲染失败，请稍后重试。")


@pytest.mark.asyncio
async def test_listing_missing_user_does_not_query_timeline(plugin):
    ns = handler_namespace(plugin)
    ns["get_user_info"].return_value = {"status": False}
    matcher = SimpleNamespace(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException()))

    await invoke_handler(ns, matcher)

    ns["get_user_timeline"].assert_not_awaited()
    matcher.finish.assert_awaited_once_with("未找到 alice")


@pytest.mark.asyncio
async def test_listing_missing_argument_does_not_contact_source(plugin):
    ns = handler_namespace(plugin)
    matcher = SimpleNamespace(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException()))

    await invoke_handler(ns, matcher, " ")

    ns["ensure_twitter_url"].assert_not_awaited()
    matcher.send.assert_not_awaited()
    matcher.finish.assert_awaited_once_with("用法：推文列表 <推主id>")


@pytest.mark.asyncio
async def test_timeline_images_keep_tweet_and_slice_order_with_five_tweet_limit(plugin, monkeypatch, tmp_path):
    views = [SimpleNamespace(id=str(tweet_id)) for tweet_id in [105, 104, 103, 102, 101]]
    rendered_paths = []
    expected = []
    for view in views:
        paths = []
        for slice_id in range(2):
            content = f"{view.id}:{slice_id}".encode()
            path = tmp_path / f"{view.id}-{slice_id}.png"
            path.write_bytes(content)
            paths.append(str(path))
            expected.append(content)
        rendered_paths.append(paths)
    fetch = AsyncMock(side_effect=views)
    renderer = SimpleNamespace(render=AsyncMock(side_effect=rendered_paths))
    factory = Mock(return_value=renderer)
    monkeypatch.setattr(plugin.api, "fetch_tweet_view", fetch)
    monkeypatch.setattr(plugin.api, "make_tweet_renderer", factory)

    result = await plugin.api.get_timeline_images("alice", ["105", "104", "103", "102", "101", "100"])

    assert result == expected
    assert [call.args for call in fetch.await_args_list] == [("alice", view.id) for view in views]
    assert [call.args[0] for call in renderer.render.await_args_list] == views
    factory.assert_called_once_with()


@pytest.mark.asyncio
async def test_timeline_images_empty_list_never_creates_renderer(plugin, monkeypatch):
    factory = Mock()
    fetch = AsyncMock()
    monkeypatch.setattr(plugin.api, "make_tweet_renderer", factory)
    monkeypatch.setattr(plugin.api, "fetch_tweet_view", fetch)

    assert await plugin.api.get_timeline_images("alice", []) == []

    factory.assert_not_called()
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeline_images_rejects_empty_tweet_render(plugin, monkeypatch):
    monkeypatch.setattr(plugin.api, "fetch_tweet_view", AsyncMock(return_value=object()))
    monkeypatch.setattr(plugin.api, "make_tweet_renderer", lambda: SimpleNamespace(render=AsyncMock(return_value=[])))

    with pytest.raises(ValueError, match="timeline_render_empty"):
        await plugin.api.get_timeline_images("alice", ["105"])


@pytest.mark.asyncio
async def test_timeline_images_does_not_hide_failed_second_tweet(plugin, monkeypatch, tmp_path):
    path = tmp_path / "first.png"
    path.write_bytes(b"first tweet")
    fetch = AsyncMock(side_effect=[object(), RuntimeError("source failed")])
    renderer = SimpleNamespace(render=AsyncMock(return_value=[str(path)]))
    monkeypatch.setattr(plugin.api, "fetch_tweet_view", fetch)
    monkeypatch.setattr(plugin.api, "make_tweet_renderer", lambda: renderer)

    with pytest.raises(RuntimeError, match="source failed"):
        await plugin.api.get_timeline_images("alice", ["105", "104"])

    assert fetch.await_count == 2
    renderer.render.assert_awaited_once()
