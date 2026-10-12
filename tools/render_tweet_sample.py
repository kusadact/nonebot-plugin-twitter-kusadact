"""Render a saved Nitter response without starting a bot or sending messages."""
import argparse
import asyncio
import importlib
from pathlib import Path
import sys
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
pkg = ModuleType('twitter_sample')
pkg.__path__ = [str(ROOT / 'nonebot_plugin_twitter')]
sys.modules[pkg.__name__] = pkg
parse_nitter = importlib.import_module('twitter_sample.nitter_parser').parse_nitter
TweetRenderer = importlib.import_module('twitter_sample.renderer').TweetRenderer
Translation = importlib.import_module('twitter_sample.tweet_model').Translation

async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('html', type=Path)
    parser.add_argument('--user', default='sharpnelsound')
    parser.add_argument('--id', required=True)
    parser.add_argument('--base', default='http://nitter.test')
    parser.add_argument('--out', type=Path, default=Path('data/twitter/cache/sample'))
    parser.add_argument('--browser')
    parser.add_argument('--max-height', type=int, default=8000)
    parser.add_argument('--mock-translations', action='store_true', help='Add clearly synthetic layout-only translations')
    args = parser.parse_args()
    view = parse_nitter(args.html.read_text(), args.base, args.user, args.id)
    if args.mock_translations:
        texts = {'2108953577679122591': '动作好可爱🤩', '2108578344031678712': '刚刚做完',
                 '2108969900849209772': '天哪……谢谢😭😭😭😭😭😭😭😭😭😭😭😭😭😭😭🥰🥰',
                 '2109152716954222849': 'Pro用户，你更希望采用哪种方案？',
                 '2109152716954222849:poll:1': '5小时上限，每周额度2倍',
                 '2109152716954222849:poll:2': '没有5小时上限，额度不变',
                 '2109152716954222849:poll:3': '查看结果'}
        for item_id, target in view.translation_targets():
            if item_id in texts:
                target.translation = Translation(True, '英语', texts[item_id], 'gemini', 'mock-layout-only')
    kwargs = {'executable_path': args.browser} if args.browser else {}
    renderer = TweetRenderer(args.out, kwargs, max_height=args.max_height)
    for path in await renderer.render(view):
        print(path)

if __name__ == '__main__':
    asyncio.run(main())
