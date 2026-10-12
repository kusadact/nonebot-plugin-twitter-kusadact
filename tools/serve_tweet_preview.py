"""Start a local URL → translation → production PNG preview without starting NoneBot."""

import argparse
import getpass
import importlib
import json
from pathlib import Path
import shutil
import sys
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
pkg = ModuleType("twitter_preview")
pkg.__path__ = [str(ROOT / "nonebot_plugin_twitter")]
sys.modules[pkg.__name__] = pkg


def load_routes(path):
    """Read credentials only from a private server-side JSON file."""
    route_type = importlib.import_module("twitter_preview.llm_client").Route
    try:
        if path.stat().st_mode & 0o077:
            raise ValueError("private_permissions")
        data = json.loads(path.read_text("utf8"))
        if not isinstance(data, dict) or set(data) - {"primary", "fallback"}:
            raise ValueError("invalid_routes")
        routes = []
        for name in ("primary", "fallback"):
            item = data.get(name)
            if item is None:
                routes.append(None)
                continue
            if not isinstance(item, dict) or set(item) != {"url", "key", "model"}:
                raise ValueError("invalid_route")
            if any(not isinstance(item[key], str) or not item[key].strip() for key in item):
                raise ValueError("invalid_route")
            route = route_type.from_config(item)
            route.endpoint()
            routes.append(route)
        if not any(routes):
            raise ValueError("empty_routes")
        return tuple(routes)
    except (OSError, ValueError, TypeError):
        # JSON parser errors and route contents may contain API credentials.
        raise ValueError("线路文件需为权限 0600 的 JSON；primary/fallback 每项包含 url、key、model。") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nitter", required=True, help="可访问的 Nitter 基址")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", choices=["127.0.0.1", "::1"], default="127.0.0.1")
    parser.add_argument("--out", type=Path, default=ROOT / "previews/generated/preview-server")
    parser.add_argument("--llm-url")
    parser.add_argument("--model")
    parser.add_argument("--routes-file", type=Path, help="私有 JSON 线路配置：primary 为主模型，fallback 为保底")
    parser.add_argument("--llm-timeout", type=int, default=60, help="单次模型请求最长等待秒数（默认 60）")
    parser.add_argument("--key-stdin", action="store_true", help="从标准输入获取本次进程使用的密钥")
    parser.add_argument("--original-only", action="store_true", help="不配置 LLM，测试原文图片")
    parser.add_argument("--browser")
    parser.add_argument("--proxy")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--max-height", type=int, default=8000)
    args = parser.parse_args()
    if args.routes_file and (args.original_only or args.llm_url or args.model or args.key_stdin):
        parser.error("--routes-file 单独配置翻译线路，不与 --original-only/--llm-url/--model/--key-stdin 混用")
    if not args.original_only and not (args.routes_file or args.llm_url and args.model):
        parser.error("翻译测试需要 --routes-file 或 --llm-url 和 --model；原文测试使用 --original-only")
    if not 320 <= args.width <= 1600 or not 1000 <= args.max_height <= 16000:
        parser.error("width 范围为 320–1600，max-height 范围为 1000–16000")
    if not 1 <= args.llm_timeout <= 300:
        parser.error("llm-timeout 范围为 1–300 秒")
    route = fallback_route = None
    if args.routes_file:
        try:
            route, fallback_route = load_routes(args.routes_file)
        except ValueError as error:
            parser.error(str(error))
    elif not args.original_only:
        key = sys.stdin.read().strip() if args.key_stdin else getpass.getpass("本次测试 API Key：")
        if not key:
            parser.error("API Key 为空")
        route_type = importlib.import_module("twitter_preview.llm_client").Route
        route = route_type(args.llm_url.rstrip("/"), key, args.model)
        route.endpoint()
    server = importlib.import_module("twitter_preview.preview_server")
    browser = args.browser
    mac_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not browser and mac_chrome.is_file():
        browser = str(mac_chrome)
    if not browser:
        browser = shutil.which("chromium") or shutil.which("google-chrome")
    kwargs = {"executable_path": browser} if browser else {}
    if args.proxy:
        kwargs["proxy"] = {"server": args.proxy}
    settings = server.PreviewSettings(args.nitter, args.out, route, kwargs, proxy=args.proxy,
                                     width=args.width, max_height=args.max_height,
                                     translation_timeout=args.llm_timeout, fallback_route=fallback_route)
    import uvicorn
    uvicorn.run(server.create_app(settings), host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
