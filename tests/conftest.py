import ast
import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sync(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / 'nitter'))
    spec = importlib.util.spec_from_file_location('sync_under_test', ROOT / 'nitter/session_sync.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, filename in [('SESSIONS_PATH', 'sessions.jsonl'), ('SEED_SESSIONS_PATH', 'seeds.jsonl'),
                           ('STATUS_PATH', 'status.json'), ('LOCK_PATH', 'sync.lock'), ('PROFILES_DIR', 'profiles')]:
        monkeypatch.setattr(module, name, tmp_path / filename)
    module.PROFILES_DIR.mkdir()
    account = dict(kind='cookie', id='1', username='test', auth_token='test-token', ct0='test-ct0', cookies=[])
    module.write_sessions_to(module.SESSIONS_PATH, [account])
    module.write_sessions_to(module.SEED_SESSIONS_PATH, [account])
    module._real_restart_nitter = module.restart_nitter
    monkeypatch.setattr(module, 'restart_nitter', lambda: None)
    return module


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    # Import the real API/delivery modules without starting a NoneBot app or scheduled jobs.
    monkeypatch.chdir(tmp_path)
    pkg = ModuleType('twitter_under_test')
    pkg.__path__ = [str(ROOT / 'nonebot_plugin_twitter')]
    monkeypatch.setitem(sys.modules, pkg.__name__, pkg)
    config = ModuleType(f'{pkg.__name__}.config')
    config.plugin_config = SimpleNamespace(
        twitter_url='http://nitter.test', twitter_proxy=None, twitter_htmlmode=False,
        twitter_node=False, twitter_no_text=False, twitter_qq=1, twitter_original=False,
    )
    config.Config = object
    config.SetCookieParam = dict
    config.get_browser_launch_kwargs = lambda: {}
    config.get_plugin_config = lambda: config.plugin_config
    for name in ('twitter_login', 'twitter_post', 'nitter_foot', 'nitter_head'):
        setattr(config, name, '')
    monkeypatch.setitem(sys.modules, config.__name__, config)
    send_plugin = ModuleType('nonebot_plugin_sendmsg_by_bots')
    send_plugin.tools = SimpleNamespace(**{
        name: AsyncMock(return_value=True) for name in (
            'send_group_msg_by_bots', 'send_private_msg_by_bots',
            'send_group_forward_msg_by_bots', 'send_private_forward_msg_by_bots',
        )
    })
    monkeypatch.setitem(sys.modules, send_plugin.__name__, send_plugin)
    for name in ('api', 'timeline', 'delivery'):
        sys.modules.pop(f'{pkg.__name__}.{name}', None)
    api = importlib.import_module(f'{pkg.__name__}.api')
    delivery = importlib.import_module(f'{pkg.__name__}.delivery')
    tree = ast.parse((ROOT / 'nonebot_plugin_twitter/__init__.py').read_text())
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name in {'get_status', 'save_handle', 'build_nitter_sync_alert_message'}]
    for node in nodes:
        node.decorator_list = []
    ns = dict(api.__dict__, process_timeline=delivery.process_timeline, CommandArg=lambda: None)
    selected = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    ast.fix_missing_locations(selected)
    exec(compile(selected, str(ROOT / 'nonebot_plugin_twitter/__init__.py'), 'exec'), ns)
    return SimpleNamespace(api=api, delivery=delivery, config=config.plugin_config, tools=send_plugin.tools, ns=ns)
