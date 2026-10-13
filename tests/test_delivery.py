import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest


def entries(*ids):
    return [dict(signature=f'status:{i}', tweet_id=str(i), source_user_name='alice', is_retweet=False) for i in ids]


def setup_state(plugin, groups=('1',), private=()):
    settings = dict(status=True, r18=True, media=False)
    state = {'alice': dict(screen_name='Alice', since_id='100', timeline_seen=['status:100'],
                          group={i: dict(settings) for i in groups}, private={i: dict(settings) for i in private})}
    plugin.api.update_twitter_list(lambda current: current.update(state))
    return state


def tweet(**changes):
    info = dict(status=True, html=b'', text=['content'], pic_url_list=[], video_url='', r18=False, media=False)
    info.update(changes)
    return info


async def process(plugin, visible, fetch=None, deliver=None):
    return await plugin.delivery.process_timeline(
        'alice', visible, None, read_state=plugin.api.read_twitter_list,
        update_state=plugin.api.update_twitter_list,
        fetch_tweet=fetch or AsyncMock(return_value=tweet()),
        deliver_tweet=deliver or plugin.api.tweet_handle,
    )


@pytest.mark.asyncio
async def test_failed_fetch_keeps_cursor_and_durable_queue(plugin):
    setup_state(plugin)
    assert not await process(plugin, entries(102, 101, 100), fetch=AsyncMock(return_value=tweet(status=False)))
    user = plugin.api.read_twitter_list()['alice']
    assert user['since_id'] == '100'
    assert user['timeline_seen'] == ['status:100']
    assert [e['tweet_id'] for e in user['pending_tweets']] == ['101', '102']
    plugin.tools.send_group_msg_by_bots.assert_not_called()


@pytest.mark.asyncio
async def test_failed_send_is_not_acknowledged(plugin):
    setup_state(plugin)
    plugin.tools.send_group_msg_by_bots.return_value = False
    assert not await process(plugin, entries(101, 100))
    user = plugin.api.read_twitter_list()['alice']
    assert user['since_id'] == '100'
    assert user['timeline_seen'] == ['status:100']
    assert len(user['pending_tweets']) == 1


@pytest.mark.asyncio
async def test_retry_only_failed_recipient(plugin):
    setup_state(plugin, groups=('1', '2'), private=('3',))
    async def send_group(*, group_id, msg):
        return group_id == 1
    plugin.tools.send_group_msg_by_bots.side_effect = send_group
    assert not await process(plugin, entries(101, 100))
    assert plugin.tools.send_private_msg_by_bots.await_count == 1
    plugin.tools.send_group_msg_by_bots.reset_mock(side_effect=True)
    plugin.tools.send_group_msg_by_bots.return_value = True
    assert await process(plugin, entries(101, 100))
    assert [call.kwargs['group_id'] for call in plugin.tools.send_group_msg_by_bots.await_args_list] == [2]
    assert plugin.tools.send_private_msg_by_bots.await_count == 1
    user = plugin.api.read_twitter_list()['alice']
    assert user['since_id'] == '101'
    assert user['pending_tweets'] == []
    assert user['pending_deliveries'] == {}


@pytest.mark.asyncio
async def test_retry_only_failed_message_part(plugin, monkeypatch):
    setup_state(plugin)
    monkeypatch.setattr(plugin.api, 'get_video_path', AsyncMock(return_value=['test.mp4']))
    monkeypatch.setattr(plugin.api, 'get_video', AsyncMock(return_value=plugin.api.MessageSegment.video('test.mp4')))
    async def send(*, group_id, msg):
        return msg[0].type != 'video'
    plugin.tools.send_group_msg_by_bots.side_effect = send
    assert not await process(plugin, entries(101, 100), fetch=AsyncMock(return_value=tweet(media=True, video_url='movie.mp4')))
    plugin.tools.send_group_msg_by_bots.reset_mock(side_effect=True)
    plugin.tools.send_group_msg_by_bots.return_value = True
    assert await process(plugin, entries(101, 100), fetch=AsyncMock(return_value=tweet(media=True, video_url='movie.mp4')))
    calls = plugin.tools.send_group_msg_by_bots.await_args_list
    assert len(calls) == 1 and calls[0].kwargs['msg'][0].type == 'video'


@pytest.mark.asyncio
async def test_cancelled_send_keeps_success_receipts_and_queue(plugin):
    setup_state(plugin, groups=('1', '2'))
    plugin.tools.send_group_msg_by_bots.side_effect = [True, asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await process(plugin, entries(101, 100))
    user = plugin.api.read_twitter_list()['alice']
    assert user['since_id'] == '100'
    assert user['pending_deliveries']['101']['group:1'] == ['main']
    assert len(user['pending_tweets']) == 1


@pytest.mark.asyncio
async def test_partial_batch_acknowledges_only_completed_tweets(plugin):
    setup_state(plugin)
    deliver = AsyncMock(side_effect=[True, False, True])
    assert not await process(plugin, entries(103, 102, 101, 100), deliver=deliver)
    user = plugin.api.read_twitter_list()['alice']
    assert user['since_id'] == '103'
    assert user['timeline_seen'] == ['status:103', 'status:101', 'status:100']
    assert [e['tweet_id'] for e in user['pending_tweets']] == ['102']
    deliver = AsyncMock(return_value=True)
    assert await process(plugin, entries(103, 102, 101, 100), deliver=deliver)
    assert [call.args[2] for call in deliver.await_args_list] == ['102']


@pytest.mark.asyncio
async def test_failed_tweet_can_retry_after_it_leaves_timeline(plugin):
    setup_state(plugin)
    assert not await process(plugin, entries(102, 101, 100), deliver=AsyncMock(return_value=False))
    deliver = AsyncMock(return_value=True)
    assert await process(plugin, entries(106, 105, 104), deliver=deliver)
    assert [call.args[2] for call in deliver.await_args_list] == ['101', '102', '104', '105', '106']


@pytest.mark.asyncio
async def test_no_overlap_recovers_visible_backlog(plugin):
    setup_state(plugin)
    deliver = AsyncMock(return_value=True)
    assert await process(plugin, entries(104, 103, 102), deliver=deliver)
    assert [call.args[2] for call in deliver.await_args_list] == ['102', '103', '104']


@pytest.mark.asyncio
async def test_new_subscription_initializes_without_replay(plugin):
    setup_state(plugin)
    plugin.api.update_twitter_list(lambda state: state['alice'].pop('timeline_seen'))
    deliver = AsyncMock()
    assert await process(plugin, entries(102, 101), deliver=deliver)
    deliver.assert_not_called()
    assert plugin.api.read_twitter_list()['alice']['timeline_seen'] == ['status:102', 'status:101']


def http_response(plugin, monkeypatch, status, content):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text=content))
    monkeypatch.setattr(plugin.api.httpx, 'AsyncClient', lambda **kwargs: real_client(transport=transport))


@pytest.mark.asyncio
async def test_http_503_does_not_look_like_empty_healthy_timeline(plugin, monkeypatch):
    setup_state(plugin)
    http_response(plugin, monkeypatch, 503, 'Unavailable')
    assert not await plugin.ns['get_status']('alice', plugin.api.read_twitter_list(), None)
    assert plugin.api.read_twitter_list()['alice']['since_id'] == '100'


@pytest.mark.asyncio
async def test_error_page_200_not_accepted_as_timeline(plugin, monkeypatch):
    setup_state(plugin)
    http_response(plugin, monkeypatch, 200, '<div class="error-panel">Instance has no auth tokens</div>')
    assert not await plugin.ns['get_status']('alice', plugin.api.read_twitter_list(), None)


@pytest.mark.asyncio
async def test_error_page_200_not_accepted_as_tweet(plugin, monkeypatch):
    setup_state(plugin)
    http_response(plugin, monkeypatch, 200, '<div class="error-panel">Instance has no auth tokens</div>')
    result = await plugin.api.get_tweet(None, 'alice', '101')
    assert not result['status']
    assert not await plugin.api.tweet_handle(result, 'alice', '101', plugin.api.read_twitter_list())


@pytest.mark.asyncio
async def test_valid_empty_timeline_is_healthy(plugin, monkeypatch):
    setup_state(plugin)
    http_response(plugin, monkeypatch, 200, '<a class="profile-card-fullname">Alice</a><div class="timeline"></div>')
    assert await plugin.ns['get_status']('alice', plugin.api.read_twitter_list(), None)
    assert plugin.api.read_twitter_list()['alice']['timeline_seen'] == ['status:100']


@pytest.mark.asyncio
async def test_new_recipient_does_not_reset_existing_progress(plugin):
    setup_state(plugin)
    plugin.ns.update(
        ensure_twitter_url=AsyncMock(return_value='http://nitter.test'),
        get_user_info=AsyncMock(return_value=dict(status=True, screen_name='Alice', bio='')),
        get_user_newtimeline=AsyncMock(return_value='102'),
        get_user_timeline_entries=AsyncMock(return_value=entries(102, 101, 100)),
        GroupMessageEvent=type('GroupMessageEvent', (), {}),
    )
    class Finished(Exception): pass
    class Matcher:
        async def finish(self, *args): raise Finished
    with pytest.raises(Finished):
        await plugin.ns['save_handle'](None, SimpleNamespace(user_id=3), Matcher(), SimpleNamespace(extract_plain_text=lambda: 'alice'))
    user = plugin.api.read_twitter_list()['alice']
    assert user['since_id'] == '100'
    assert user['timeline_seen'] == ['status:100']
    assert '3' in user['private']


@pytest.mark.asyncio
async def test_one_unavailable_tweet_does_not_block_new_pushes(plugin):
    setup_state(plugin)
    fetch = AsyncMock(side_effect=[ValueError('test unavailable'), tweet()])
    deliver = AsyncMock(return_value=True)
    assert not await process(plugin, entries(102, 101, 100), fetch=fetch, deliver=deliver)
    user = plugin.api.read_twitter_list()['alice']
    assert user['since_id'] == '102'
    assert [e['tweet_id'] for e in user['pending_tweets']] == ['101']
    assert [call.args[2] for call in deliver.await_args_list] == ['102']


@pytest.mark.asyncio
async def test_empty_initial_timeline_pushes_first_new_tweet(plugin):
    setup_state(plugin)
    plugin.api.update_twitter_list(lambda state: state['alice'].update(timeline_seen=[], since_id='0'))
    assert await process(plugin, entries(1))
    assert plugin.api.read_twitter_list()['alice']['since_id'] == '1'
    plugin.tools.send_group_msg_by_bots.assert_awaited_once()


def test_login_alert_excludes_unconfirmed_chrome_failures(plugin):
    status = dict(accounts=[dict(id='1', needs_relogin=True, chrome_logged_in=False),
                            dict(id='2', needs_relogin=False, chrome_logged_in=False)])
    message = plugin.ns['build_nitter_sync_alert_message'](status)
    assert '失效账号：1' in message and '1, 2' not in message
