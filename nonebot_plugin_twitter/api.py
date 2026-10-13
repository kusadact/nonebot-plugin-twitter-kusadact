import json
import random
import sys
import typing
import asyncio
import httpx
import os
import shutil
import subprocess
from threading import RLock
from typing import Optional,Literal
from pathlib import Path
from datetime import datetime
from urllib.parse import quote, unquote, urljoin, urlparse
from uuid import uuid4
from bs4 import BeautifulSoup
from nonebot import logger
from nonebot.adapters.onebot.v11 import MessageSegment,Message
from playwright.async_api import Browser
from nonebot_plugin_sendmsg_by_bots import tools
from .config import (
    Config,
    SetCookieParam,
    get_browser_launch_kwargs,
    get_plugin_config,
    plugin_config,
    twitter_login,
    twitter_post,
    nitter_foot,
    nitter_head,
)
from .nitter_parser import parse_nitter
from .tweet_fetcher import fetch_nitter_view
from .tweet_model import TweetView, text_urls
from .renderer import TweetRenderer
from .gif_media import convert_gif, gif_cache_path
from .timeline import (
    build_timeline_signature,
    get_new_timeline_entries,
    get_recent_timeline_signatures,
    normalize_timeline_signature,
    normalize_tweet_href,
)

# Path
dirpath = Path() / "data" / "twitter"
dirpath.mkdir(parents=True, exist_ok=True)
dirpath = Path() / "data" / "twitter" / "cache"
dirpath.mkdir(parents=True, exist_ok=True)
dirpath = Path() / "data" / "twitter" / "twitter_list.json"
dirpath.touch()
if not dirpath.stat().st_size:
    dirpath.write_text("{}")
linkpath = Path() / "data" / "twitter" / "twitter_link.json"
linkpath.touch()
if not linkpath.stat().st_size:
    linkpath.write_text("{}")

twitter_list_lock = RLock()
link_list_lock = RLock()
    
header = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
    }

def _read_json_dict(path: Path) -> dict:
    try:
        raw = path.read_text("utf8").strip()
        if not raw:
            return {}
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
        logger.warning(f"{path} 内容不是字典，已按空字典处理")
    except json.JSONDecodeError as e:
        logger.warning(f"读取 {path} 失败，JSON 损坏：{e}")
    except FileNotFoundError:
        return {}
    return {}


def _write_json_dict_atomic(path: Path, data: dict) -> None:
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{random.randint(0, 999999):06d}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False), "utf8")
    tmp_path.replace(path)


def read_twitter_list() -> dict:
    with twitter_list_lock:
        return _read_json_dict(dirpath)


def update_twitter_list(mutator):
    with twitter_list_lock:
        twitter_list = _read_json_dict(dirpath)
        result = mutator(twitter_list)
        _write_json_dict_atomic(dirpath, twitter_list)
        return result


def ensure_twitter_user_entry(twitter_list: dict, user_name: str) -> dict:
    user_entry = twitter_list.setdefault(user_name, {})
    user_entry.setdefault("group", {})
    user_entry.setdefault("private", {})
    return user_entry


def read_link_list() -> dict:
    with link_list_lock:
        return _read_json_dict(linkpath)


def update_link_list(mutator):
    with link_list_lock:
        link_list = _read_json_dict(linkpath)
        result = mutator(link_list)
        _write_json_dict_atomic(linkpath, link_list)
        return result


def build_httpx_client_kwargs(*, http2: bool = False, timeout: Optional[float] = None) -> dict:
    kwargs = {}
    if plugin_config.twitter_proxy:
        kwargs["proxy"] = plugin_config.twitter_proxy
    if http2:
        kwargs["http2"] = True
    if timeout is not None:
        kwargs["timeout"] = timeout
    return kwargs


async def close_page_context(page, context) -> None:
    if page is not None:
        try:
            await page.close()
        except Exception as e:
            logger.debug(f"关闭 Playwright 页面失败：{e}")

    if context is not None:
        try:
            await context.close()
        except Exception as e:
            logger.debug(f"关闭 Playwright 上下文失败：{e}")


async def close_browser_resources(playwright, browser) -> None:
    try:
        if playwright is not None and browser is not None:
            await browser.close()
    except Exception as e:
        logger.debug(f"关闭 Playwright 浏览器失败：{e}")
    finally:
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception as e:
                logger.debug(f"停止 Playwright driver 失败：{e}")


def parse_timeline_entries(soup: BeautifulSoup, follow_user_name: str) -> list[dict]:
    entries = []
    for timeline_item in soup.find_all("div", class_="timeline-item"):
        if timeline_item.find("div", class_="pinned") is not None:
            continue

        tweet_link = timeline_item.find("a", class_="tweet-link")
        if not tweet_link or "href" not in tweet_link.attrs:
            continue

        href = normalize_tweet_href(str(tweet_link.attrs["href"]))
        parts = href.strip("/").split("/")
        if len(parts) < 3 or parts[1] != "status":
            continue

        source_user_name = parts[0]
        tweet_id = parts[2]
        is_retweet = timeline_item.find("div", class_="retweet-header") is not None
        is_quote = timeline_item.find("div", class_="quote") is not None

        if not is_retweet and source_user_name != follow_user_name:
            continue

        entries.append(
            {
                "follow_user_name": follow_user_name,
                "source_user_name": source_user_name,
                "tweet_id": tweet_id,
                "href": href,
                "signature": build_timeline_signature(href),
                "is_retweet": is_retweet,
                "is_quote": is_quote,
            }
        )

    return entries


async def get_user_info(user_name:str) -> dict:
    '''通过 user_name 获取信息详情,
    return:
    result["status"],
    result["user_name"],
    result["screen_name"],
    result["bio"]
    '''
    result ={}
    result["status"] = False
    try:
        async with httpx.AsyncClient(**build_httpx_client_kwargs(http2=True, timeout=120)) as client:
            res = await client.get(url=f"{plugin_config.twitter_url}/{user_name}",headers=header)
            
            if res.status_code == 200:
                result["status"] = True
                result["user_name"] = user_name
                soup = BeautifulSoup(res.text,"html.parser")
                result["screen_name"] = match[0].text if (match := soup.find_all('a', class_='profile-card-fullname')) else ""
                result["bio"] = match[0].text if (match := soup.find_all('p')) else ""
            else:
                logger.warning(f"通过 user_name {user_name} 获取信息详情失败：{res.status_code} {res.text} ")
                result["status"] = False
    except Exception as e:
        logger.warning(f"通过 user_name {user_name} 获取信息详情出错：{e}")

    return result

async def get_user_timeline_entries(user_name: str) -> list[dict]:
    async with httpx.AsyncClient(**build_httpx_client_kwargs(http2=True, timeout=120)) as client:
        res = await client.get(url=f"{plugin_config.twitter_url}/{user_name}",headers=header)
        if res.status_code ==200:
            soup = BeautifulSoup(res.text,"html.parser")
            if soup.select_one(".error-panel") or not soup.select_one(".profile-card-fullname"):
                raise ValueError(f"{user_name} 的响应不是有效 Nitter 时间线")
            return parse_timeline_entries(soup, user_name)
        res.raise_for_status()
        raise ValueError(f"{user_name} 的时间线响应状态异常：{res.status_code}")


async def get_user_timeline(user_name:str,since_id: str = "0"):
    entries = await get_user_timeline_entries(user_name)
    new_line = []
    for entry in entries:
        if entry["source_user_name"] != user_name:
            continue

        tweet_id = entry["tweet_id"]
        if since_id != "0":
            if int(tweet_id) > int(since_id):
                logger.trace(f"通过 user_name {user_name} 获取时间线成功：{tweet_id}")
                new_line.append(tweet_id)
        else:
            new_line.append(tweet_id)
    return new_line

async def get_user_newtimeline(user_name:str,since_id: str = "0") -> str:
    ''' 通过 user_name 获取推文id列表,
    有 since_id return 最近的新的推文id,
    无 since_id return 最新的推文id'''
    try:
        new_line = await get_user_timeline(user_name, since_id)
        if since_id == "0":
            if new_line == []:
                new_line.append("1")
            else:
                new_line = [str(max(map(int,new_line)))]
        if new_line == []:
            new_line = ["not found"]
        return new_line[-1]
    except Exception as e:
        logger.warning(f"通过 user_name {user_name} 获取时间线失败：{e}")
        raise e

def build_pbs_fallback_urls(raw_url: str, *, image_size: str = "large") -> list[str]:
    decoded_url = unquote(raw_url)
    parsed = urlparse(decoded_url)
    path = parsed.path if parsed.scheme else decoded_url

    if not path.startswith("/pic/"):
        return []

    media_path = path.removeprefix("/pic/")
    if media_path.startswith(("https://", "http://")):
        return [media_path]

    fallbacks: list[str] = []
    if media_path.startswith("orig/media/"):
        filename = media_path.removeprefix("orig/media/")
        stem, ext = os.path.splitext(filename)
        ext = ext.lstrip(".").lower()
        if stem and ext:
            fallbacks.append(f"{plugin_config.twitter_img_url}{stem}?format={ext}&name={image_size}")
        fallbacks.append(f"https://pbs.twimg.com/media/{filename}")
        return fallbacks

    if media_path:
        fallbacks.append(f"https://pbs.twimg.com/{media_path.lstrip('/')}")
    return fallbacks


def build_image_candidates(raw_url: str, *, include_original: bool = True, image_size: str = "large") -> list[str]:
    candidates: list[str] = []
    if include_original:
        if raw_url.startswith("/"):
            candidates.append(f"{plugin_config.twitter_url}{raw_url}")
        else:
            candidates.append(raw_url)
    candidates.extend(build_pbs_fallback_urls(raw_url, image_size=image_size))

    deduped: list[str] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return deduped


async def prepare_nitter_screenshot_page(page, *, block_nitter_media: bool = False) -> None:

    async def _route_handler(route):
        url = route.request.url
        if route.request.resource_type in {"image", "media"} and "/pic/" in url:
            if block_nitter_media:
                await route.abort()
                return

            candidates = build_image_candidates(url, include_original=False, image_size="small")
            if candidates:
                try:
                    async with httpx.AsyncClient(**build_httpx_client_kwargs(http2=True, timeout=20)) as client:
                        for candidate in candidates:
                            res = await client.get(candidate, headers=header)
                            content_type = res.headers.get("content-type", "")
                            if res.status_code == 200 and content_type.startswith("image/"):
                                await route.fulfill(
                                    status=200,
                                    headers={"content-type": content_type},
                                    body=res.content,
                                )
                                return
                except Exception as e:
                    logger.debug(f"Nitter 截图图片代理回填失败：{url}，{e}")
            await route.continue_()
            return
        await route.continue_()

    try:
        await page.route("**/*", _route_handler)
    except Exception as e:
        logger.debug(f"Nitter 页面资源拦截设置失败：{e}")


async def wait_for_nitter_media_images(page, timeout: int = 8000) -> None:
    try:
        await page.wait_for_function(
            """() => {
                const images = Array.from(document.images)
                    .filter(img => img.src.includes("/pic/"));
                return images.every(img => img.complete && img.naturalWidth > 0);
            }""",
            timeout=timeout,
        )
    except Exception:
        logger.debug("等待 Nitter 截图图片加载完成超时，继续截图")

async def prepare_nitter_page(page, wait_selector: str = "") -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        logger.debug("Nitter 页面等待 DOMContentLoaded 超时，继续处理")
    if wait_selector:
        try:
            await page.wait_for_selector(wait_selector, timeout=5000)
        except Exception:
            logger.debug(f"Nitter 页面等待选择器超时：{wait_selector}")
    await page.evaluate(nitter_head)
    await page.evaluate(nitter_foot)

    timezone_label = plugin_config.twitter_html_timezone_label or plugin_config.twitter_html_timezone
    if not timezone_label:
        return

    try:
        await page.wait_for_function(
            """label => {
                const published = document.querySelector(".tweet-published");
                if (published) {
                    const text = published.textContent || "";
                    return text.includes(label) || !/UTC/i.test(text);
                }

                const nodes = Array.from(document.querySelectorAll(".tweet-date a"));
                if (!nodes.length) {
                    return true;
                }

                return nodes.every(node => {
                    const title = node.getAttribute("title") || "";
                    return !/UTC/i.test(title) || title.includes(label);
                });
            }""",
            arg=timezone_label,
            timeout=1500,
        )
    except Exception:
        logger.debug("等待 Nitter 页面时区修正完成超时，继续截图")
    await wait_for_nitter_media_images(page)

async def get_timeline_screen(browser: Browser,user_name: str,length: int = 5):
    url=f"{plugin_config.twitter_url}/{user_name}"
    context = None
    page = None

    async def fallback_screenshot(reason: str):
        logger.warning(f"Nitter 时间线截图使用整页兜底：{user_name}，原因：{reason}")
        try:
            return await page.screenshot(full_page=True, timeout=15000)
        except Exception as e:
            logger.warning(f"Nitter 时间线整页兜底截图失败：{user_name}，{e}")
            return None

    try:
        context = await browser.new_context()
        page = await context.new_page()
        await prepare_nitter_screenshot_page(page)
        await page.goto(url,timeout=60000,wait_until="domcontentloaded")
        try:
            await page.wait_for_selector('.timeline-item', timeout=10000)
        except Exception as e:
            logger.warning(f"Nitter 时间线等待条目超时：{user_name}，{e}")
        await prepare_nitter_page(page, '.timeline-item')

        tweets = await page.query_selector_all('.timeline-item')
        if not tweets:
            return await fallback_screenshot("未找到 timeline-item")

        visible_count = min(max(length, 1), 5, len(tweets))
        visible_tweets = tweets[:visible_count]

        try:
            timeline_screen = await page.locator(".timeline").nth(0).screenshot(timeout=15000)
            if timeline_screen:
                return timeline_screen
            logger.warning(f"Nitter 时间线整体截图为空：{user_name}")
        except Exception as e:
            logger.debug(f"Nitter 时间线整体截图失败，转为裁剪截图：{e}")

        first_bbox = await visible_tweets[0].bounding_box()
        last_bbox = await visible_tweets[-1].bounding_box()
        if first_bbox and last_bbox:
            # 计算截图区域高度，兼容不足 5 条推文的时间线
            total_height = last_bbox['y'] + last_bbox['height'] - first_bbox['y']
            return await page.screenshot(
                full_page=True,
                clip={
                    'x': first_bbox['x'],
                    'y': first_bbox['y'],
                    'width': first_bbox['width'],
                    'height': total_height,
                },
            )
        logger.warning(f"Nitter 时间线截图失败：{user_name} bbox 为空")
        return await fallback_screenshot("bbox 为空")
    finally:
        await close_page_context(page, context)
    
def use_tweet_template() -> bool:
    return bool(getattr(plugin_config, "twitter_template_enabled", False) or
                getattr(plugin_config, "twitter_translation_enabled", False))


def make_tweet_renderer() -> TweetRenderer:
    return TweetRenderer(Path("data/twitter/cache/pipeline/rendering"), get_browser_launch_kwargs(),
                         proxy=plugin_config.twitter_proxy,
                         width=getattr(plugin_config, "twitter_render_width", 640),
                         max_height=getattr(plugin_config, "twitter_render_max_height", 8000),
                         timezone=getattr(plugin_config, "twitter_html_timezone", "Asia/Shanghai") or "Asia/Shanghai",
                         label=getattr(plugin_config, "twitter_html_timezone_label", "UTC+8") or "UTC+8")


async def fetch_tweet_view(user_name: str, tweet_id: str) -> TweetView:
    return await fetch_nitter_view(plugin_config.twitter_url, user_name, tweet_id,
                                   proxy=plugin_config.twitter_proxy, headers=header)


async def get_timeline_images(user_name: str, tweet_ids: list[str]) -> list[bytes]:
    """Render up to five listed tweets, preserving tweet and image-slice order."""
    selected = tweet_ids[:5]
    if not selected:
        return []
    renderer = make_tweet_renderer()
    images = []
    # Listing remains read-only; do not enqueue subscription or translation work.
    for tweet_id in selected:
        view = await fetch_tweet_view(user_name, tweet_id)
        paths = await renderer.render(view)
        if not paths:
            raise ValueError("timeline_render_empty")
        images.extend(Path(path).read_bytes() for path in paths)
    return images


async def get_tweet(browser: Browser,user_name:str,tweet_id: str = "0") -> dict:
    '''通过 user_name 和 tweet_id 获取推文详情,
    return:
    result["status"],
    result["text"],
    result["pic_url_list"],
    result["video_url"],
    result["r18"]
    result["html"]
    '''
    if use_tweet_template():
        view = await fetch_tweet_view(user_name, tweet_id)
        info = view.legacy_fields()
        try:
            info["rendered_images"] = await make_tweet_renderer().render(view)
        except Exception as error:
            logger.warning(f"推文 {user_name}/{tweet_id} 模板渲染失败：{type(error).__name__}")
        return info
    try:
        result = {}
        result["status"] = False
        result["html"] = b""
        result["media"] = False
        url=f"{plugin_config.twitter_url}/{user_name}/status/{tweet_id}"

        if plugin_config.twitter_htmlmode:
            context = None
            page = None
            try:
                context = await browser.new_context()
                page = await context.new_page()
                cookie: typing.List[SetCookieParam] = [{
                    "url": plugin_config.twitter_url,
                    "name": "hlsPlayback",
                    "value": "on"}]
                await context.add_cookies(cookie)
                if plugin_config.twitter_original:
                    # 原版 twitter
                    url=f"https://twitter.com/{user_name}/status/{tweet_id}"
                    await page.goto(url,timeout=60000)
                    await page.wait_for_load_state("load",timeout=60000)
                    await page.evaluate(twitter_login)
                    await page.evaluate(twitter_post)
                    screenshot_bytes = await page.locator("xpath=/html/body/div[1]/div/div/div[2]/main/div/div/div/div[1]/div/section/div/div/div[1]").screenshot()
                else:
                    await prepare_nitter_screenshot_page(page)
                    await page.goto(url,timeout=60000,wait_until="domcontentloaded")
                    await prepare_nitter_page(page, ".tweet-published, .tweet-date a")
                    screenshot_bytes = await page.locator("xpath=/html/body/div[1]/div").screenshot(timeout=60000)
                logger.info(f"使用浏览器截图获取 {url} 推文信息成功")
                result["html"] = screenshot_bytes
            finally:
                await close_page_context(page, context)

        async with httpx.AsyncClient(**build_httpx_client_kwargs(http2=True, timeout=120)) as client:
            res = await client.get(f"{plugin_config.twitter_url}/{user_name}/status/{tweet_id}",cookies={"hlsPlayback": "on"},headers=header)
            if res.status_code ==200:
                soup = BeautifulSoup(res.text,"html.parser")
                if soup.select_one(".error-panel") or not soup.select_one(".main-thread .main-tweet"):
                    logger.warning(f"{user_name} 的推文 {tweet_id} 响应不是有效 Nitter 推文")
                    return result

                screenshot = result["html"]
                view = parse_nitter(res.text, plugin_config.twitter_url, user_name, tweet_id)
                result.update(view.legacy_fields())
                result["html"] = screenshot
                result["status"] = True
                logger.info(f"推主 {user_name} 的推文 {tweet_id} 获取成功")
            else:
                logger.warning(f"获取 {user_name} 的推文 {tweet_id} 失败：{res.status_code} {res.text}")
        return result
    except Exception as e:
        logger.warning(f"获取 {user_name} 的推文 {tweet_id} 异常：{e}")
        raise e


async def get_video_path(url: str) -> list:
    try:
        async def download_video_file(client: httpx.AsyncClient, download_url: str, *, extra_headers: Optional[dict] = None) -> list[str]:
            filename = f"{int(datetime.now().timestamp())}.{uuid4().hex}.mp4"
            path = Path() / "data" / "twitter" / "cache" / filename
            abs_path = f"{os.getcwd()}/{str(path)}"

            headers_to_use = dict(header)
            if extra_headers:
                headers_to_use.update(extra_headers)

            async with client.stream("GET", download_url, headers=headers_to_use, timeout=240) as response:
                if response.status_code != 200:
                    raise ValueError(f"视频下载失败: {download_url} ({response.status_code})")
                with open(abs_path, "wb") as file:
                    async for chunk in response.aiter_bytes():
                        file.write(chunk)
            return [abs_path]

        async def download_playlist(client: httpx.AsyncClient, playlist_url: str) -> str:
            res = await client.get(
                playlist_url,
                headers=header,
                cookies={"hlsPlayback": "on"},
                timeout=120,
            )
            if res.status_code != 200:
                raise ValueError(f"播放列表下载失败: {playlist_url} ({res.status_code})")
            return res.text

        def normalize_video_url(raw_url: str) -> str:
            if raw_url.startswith("/"):
                return f"{plugin_config.twitter_url}{raw_url}"
            return raw_url

        def extract_direct_video_url(raw_url: str) -> str:
            parsed = urlparse(raw_url)
            path = parsed.path if parsed.scheme else raw_url
            if path.startswith("/pic/"):
                decoded = unquote(path.removeprefix("/pic/"))
                if decoded.startswith("video.twimg.com/"):
                    return "https://" + decoded
                if decoded.startswith(("https://video.twimg.com/", "http://video.twimg.com/")):
                    return decoded
            if not path.startswith("/video/"):
                return raw_url

            parts = path.split("/", 3)
            if len(parts) < 4:
                return raw_url
            decoded = unquote(parts[3])
            return decoded if decoded.startswith(("http://", "https://")) else raw_url

        def resolve_nitter_url(raw_url: str) -> str:
            if raw_url.startswith("http://") or raw_url.startswith("https://"):
                return raw_url
            return urljoin(plugin_config.twitter_url.rstrip("/") + "/", raw_url.lstrip("/"))

        def choose_best_variant(lines: list[str]) -> str:
            best_variant = ""
            best_area = -1
            for index, line in enumerate(lines):
                if not line.startswith("#EXT-X-STREAM-INF:") or index + 1 >= len(lines):
                    continue
                area = 0
                if "RESOLUTION=" in line:
                    resolution = line.split("RESOLUTION=", 1)[1].split(",", 1)[0]
                    width, height = resolution.split("x", 1)
                    area = int(width) * int(height)
                if area >= best_area:
                    best_area = area
                    best_variant = lines[index + 1].strip()
            return best_variant

        def parse_media_playlist(lines: list[str]) -> tuple[str, list[str]]:
            init_url = ""
            segment_urls: list[str] = []
            for line in lines:
                if line.startswith('#EXT-X-MAP:URI="'):
                    init_url = line.split('URI="', 1)[1].split('"', 1)[0]
                elif line and not line.startswith("#"):
                    segment_urls.append(line.strip())
            return init_url, segment_urls

        async def download_nitter_hls_video(client: httpx.AsyncClient, master_url: str) -> list[str]:
            master_url = normalize_video_url(master_url)
            master_text = await download_playlist(client, master_url)
            master_lines = master_text.splitlines()

            variant_path = choose_best_variant(master_lines)
            variant_url = resolve_nitter_url(variant_path) if variant_path else master_url
            playlist_text = await download_playlist(client, variant_url)
            init_url, segment_urls = parse_media_playlist(playlist_text.splitlines())
            if not init_url or not segment_urls:
                raise ValueError("未找到可下载的视频分片")

            filename = f"{int(datetime.now().timestamp())}.{uuid4().hex}.mp4"
            path = Path() / "data" / "twitter" / "cache" / filename
            abs_path = f"{os.getcwd()}/{str(path)}"

            with open(abs_path, "wb") as file:
                init_res = await client.get(
                    resolve_nitter_url(init_url),
                    headers=header,
                    cookies={"hlsPlayback": "on"},
                    timeout=120,
                )
                if init_res.status_code != 200:
                    raise ValueError(f"视频初始化片段下载失败: {init_res.status_code}")
                file.write(init_res.content)

                for segment_url in segment_urls:
                    segment_res = await client.get(
                        resolve_nitter_url(segment_url),
                        headers=header,
                        cookies={"hlsPlayback": "on"},
                        timeout=120,
                    )
                    if segment_res.status_code != 200:
                        raise ValueError(f"视频分片下载失败: {segment_res.status_code}")
                    file.write(segment_res.content)

            logger.info(f"通过 Nitter HLS 下载视频成功：{master_url}")
            return [abs_path]

        async def download_direct_hls_video(master_url: str) -> list[str]:
            direct_url = extract_direct_video_url(master_url)
            if direct_url == master_url and not urlparse(direct_url).netloc.endswith("video.twimg.com"):
                raise ValueError("未找到可直连的 video.twimg.com 视频地址")

            filename = f"{int(datetime.now().timestamp())}.{uuid4().hex}.mp4"
            path = Path() / "data" / "twitter" / "cache" / filename
            abs_path = f"{os.getcwd()}/{str(path)}"
            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-headers",
                f"User-Agent: {header['User-Agent']}\r\n",
                "-protocol_whitelist",
                "file,http,https,tcp,tls,crypto,httpproxy",
                "-i",
                direct_url,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                abs_path,
            ]
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=240,
            )
            if result.returncode != 0 or not os.path.exists(abs_path) or os.path.getsize(abs_path) == 0:
                if os.path.exists(abs_path):
                    os.remove(abs_path)
                raise RuntimeError(result.stderr.strip() or "ffmpeg failed")

            logger.info(f"通过直连 X HLS 下载视频成功：{direct_url}")
            return [abs_path]

        async with httpx.AsyncClient(**build_httpx_client_kwargs(timeout=120)) as client:
            if urlparse(extract_direct_video_url(url)).path.lower().endswith(".mp4"):
                return await download_video_file(client, extract_direct_video_url(url))
            if "/video/" in url:
                try:
                    return await download_direct_hls_video(url)
                except Exception as e:
                    logger.warning(f"通过直连 X HLS 下载视频异常：url {url}，{e}")
                if plugin_config.twitter_video_mux_api:
                    mux_headers = {}
                    if plugin_config.twitter_video_mux_token:
                        mux_headers["X-Auth-Token"] = plugin_config.twitter_video_mux_token
                    try:
                        mux_url = f"{plugin_config.twitter_video_mux_api}?video_url={quote(url, safe='')}"
                        files = await download_video_file(client, mux_url, extra_headers=mux_headers)
                        logger.info(f"通过远程视频转码接口下载视频成功：{url}")
                        return files
                    except Exception as e:
                        logger.warning(f"通过远程视频转码接口下载视频异常：url {url}，{e}")
                try:
                    return await download_nitter_hls_video(client, url)
                except Exception as e:
                    logger.warning(f"通过 Nitter HLS 下载视频异常：url {url}，{e}")

            res = await client.get(f"https://twitterxz.com/parse?url={url}",headers=header,timeout=120)
            if res.status_code != 200:
                raise ValueError("视频下载失败")
            
            soup = BeautifulSoup(res.text, 'html.parser')
            hidden_div = soup.find('div', id='S:1')
            if not hidden_div:
                return []

            video_urls = []
            video_tag = hidden_div.find('video')
            if video_tag and video_tag.get('src'):
                video_urls = [video_tag['src']]

            if not video_urls:
                return []

            download_files = []
            for video_url in video_urls:
                filename = f"{int(datetime.now().timestamp())}.{uuid4().hex}.mp4"
                path = Path() / "data" / "twitter" / "cache" /  filename
                path = f"{os.getcwd()}/{str(path)}"
                async with client.stream("GET", video_url) as response:
                    if response.status_code != 200:
                        logger.warning(f"下载视频失败: {video_url}")
                        continue
                    with open(path, 'wb') as f:
                        async for chunk in response.aiter_bytes():
                            f.write(chunk)
                download_files.append(path)
            return download_files

    except Exception as e:
        logger.warning(f"下载视频异常：url {url}，{e}")
        raise e

async def get_gif_path(url: str) -> str:
    target = gif_cache_path(url, Path("data/twitter/cache/pipeline/gifs")).resolve()
    if target.is_file() and target.read_bytes()[:6] in {b"GIF87a", b"GIF89a"}:
        return str(target)
    files = await get_video_path(url)
    if not files:
        raise ValueError("gif_download_empty")
    await convert_gif(Path(files[0]), target)
    return str(target)


async def get_video(file_path: str) -> MessageSegment:
    '修改为返回视频消息，而非合并视频消息'
    # return MessageSegment.node_custom(user_id=user_id, nickname=name,
    #                                       content=Message(MessageSegment.video(f"file:///{task}"))) 
    try:
        if plugin_config.twitter_video_send_base_url:
            filename = os.path.basename(file_path)
            video_url = (
                f"{plugin_config.twitter_video_send_base_url.rstrip('/')}"
                f"/twitter/cache/{quote(filename)}"
            )
            return MessageSegment.video(video_url)

        send_path = file_path
        host_path = plugin_config.twitter_video_send_host_path
        container_path = plugin_config.twitter_video_send_container_path
        if host_path and container_path:
            filename = os.path.basename(file_path)
            target_path = os.path.join(host_path, filename)
            if os.path.abspath(file_path) != os.path.abspath(target_path):
                Path(host_path).mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file_path, target_path)
            send_path = os.path.join(container_path, filename)
        return MessageSegment.video(f"file://{send_path}")
    except Exception as e:
        logger.debug(f"缓存视频异常：file {file_path}，{e}")
        return MessageSegment.text("获取视频出错啦")
        
async def get_pic(url: str) -> MessageSegment:
    '修改为返回图片消息，而非合并图片消息'
    per_request_timeout = 20
    retry_times = 2

    candidates = build_image_candidates(url)
    async with httpx.AsyncClient(**build_httpx_client_kwargs(http2=True)) as client:
        last_error = ""
        for candidate in candidates:
            for attempt in range(1, retry_times + 1):
                try:
                    res = await client.get(candidate, headers=header, timeout=per_request_timeout)
                    if res.status_code != 200:
                        last_error = f"HTTP {res.status_code}"
                        logger.warning(
                            f"图片下载失败:{candidate}，状态码：{res.status_code}，尝试 {attempt}/{retry_times}"
                        )
                        continue
                    tmp = bytes(random.randint(0,255))
                    return MessageSegment.image(file=(res.read()+tmp))
                except Exception as e:
                    last_error = str(e)
                    logger.warning(f"获取图片出现异常 {candidate}，尝试 {attempt}/{retry_times}：{e}")

        failed_url = candidates[0] if candidates else url
        if last_error:
            logger.warning(f"图片全部下载失败，最终错误：{last_error}")
        return MessageSegment.text(f"图片加载失败 X_X 图片链接 {failed_url}")



# 发送
async def send_msg(
    twitter_list: dict, user_name: str, line_new_tweet_id: str, tweet_info: dict,
    msg: Message, mode: Optional[Literal["node", "direct", "video"]] = "direct", *, part: str = "main",
    requires_main: bool = False, legacy_receipts: list[str] | None = None,
    receipt_aliases: list[str] | None = None,
) -> bool:
    if mode not in {"node", "direct", "video"}:
        raise ValueError(f"未知发送模式：{mode}")
    if mode == "node":
        msg = deserialize_message(unwrap_message_segments(serialize_message(msg)))
    success = True
    user = read_twitter_list().get(user_name, {})
    for kind in ("group", "private"):
        for recipient, settings in user.get(kind, {}).items():
            if not settings.get("status", False):
                continue
            if not settings.get("r18", False) and tweet_info.get("r18", False):
                continue
            if settings.get("media", False) and not tweet_info.get("media", False):
                continue
            target = f"{kind}:{recipient}"
            receipts = read_twitter_list().get(user_name, {}).get("pending_deliveries", {})
            completed = receipts.get(str(line_new_tweet_id), {}).get(target, [])
            if part in completed or any(alias in completed for alias in receipt_aliases or []):
                continue
            if requires_main and "main" not in completed:
                success = False
                continue
            target_msg = Message(segment for index, segment in enumerate(msg)
                                 if not legacy_receipts or not legacy_receipts[index]
                                 or legacy_receipts[index] not in completed)
            try:
                # Older frozen nodes are unwrapped before arriving here. All automatic
                # deliveries use ordinary messages, including when twitter_node is set.
                sent = True
                if target_msg:
                    if kind == "group":
                        sent = await tools.send_group_msg_by_bots(group_id=int(recipient), msg=target_msg)
                    else:
                        sent = await tools.send_private_msg_by_bots(user_id=int(recipient), msg=target_msg)
                if not sent:
                    success = False
                    logger.warning(f"推文 {user_name}/status/{line_new_tweet_id} 未发送到 {target}")
                    continue

                def remember(state):
                    if user_name not in state:
                        return
                    pending = state[user_name].setdefault("pending_deliveries", {})
                    completed = pending.setdefault(str(line_new_tweet_id), {}).setdefault(target, [])
                    if part not in completed:
                        completed.append(part)

                update_twitter_list(remember)
                logger.info(f"推文 {user_name}/status/{line_new_tweet_id} 发送到 {target} 成功")
            except Exception as e:
                success = False
                logger.warning(f"推文 {user_name}/status/{line_new_tweet_id} 发送到 {target} 失败：{e}")
    return success

def get_next_element(my_list, current_element):

    # 获取当前元素在列表中的索引
    index = my_list.index(current_element)
    
    # 计算下一个元素的索引
    next_index = (index + 1) % len(my_list)
    
    # 返回下一个元素
    return my_list[next_index]


async def get_tweet_context(tweet_info: dict,user_name: str,line_new_tweet_id: str):
    all_msg = []
        
    # html模式
    if tweet_info.get("rendered_images"):
        all_msg.extend(MessageSegment.image(Path(path).read_bytes()) for path in tweet_info["rendered_images"])
    elif plugin_config.twitter_htmlmode and tweet_info.get("html"):
        all_msg.append(MessageSegment.image(tweet_info["html"]))
    
    # 返回图片
    if tweet_info["pic_url_list"] and not tweet_info.get("rendered_images"):
        for url in tweet_info["pic_url_list"]:
            # print(f"打印图片链接: {url}")
            # if ".jpg" in url:
            #     url = url.replace(".jpg")
            all_msg.append(await get_pic(url))

    for url in tweet_info.get("gif_url_list", []):
        path = await get_gif_path(url)
        all_msg.append(MessageSegment.image(Path(path).read_bytes()))
            
    # 视频，返回本地视频路径
    if tweet_info["video_url"]:
        #print("打印tweet_info[video_url]:", tweet_info["video_url"])
        video_files = await get_video_path(tweet_info["video_url"])
        #print("打印video_files:", video_files)
        for file in video_files:
            #print(f"遍历打印file: {file}")
            #print(f"遍历打印get_video(file): {get_video(file)}")
            all_msg.append(await get_video(file))
        
    return all_msg


def split_video_messages(all_msg: list[MessageSegment]) -> tuple[list[MessageSegment], list[MessageSegment]]:
    media_msgs: list[MessageSegment] = []
    video_msgs: list[MessageSegment] = []
    for msg in all_msg:
        if msg.type == "video":
            video_msgs.append(msg)
        else:
            media_msgs.append(msg)
    return media_msgs, video_msgs


def should_send_nitter_first(tweet_info: dict) -> bool:
    return bool(tweet_info.get("is_retweet") or tweet_info.get("quote_text"))


def split_nitter_preview_messages(
    tweet_info: dict, media_msgs: list[MessageSegment]
) -> tuple[list[MessageSegment], list[MessageSegment]]:
    if plugin_config.twitter_htmlmode and tweet_info.get("html") and media_msgs:
        first_msg = media_msgs[0]
        if first_msg.type == "image":
            return [first_msg], media_msgs[1:]
    return [], media_msgs


def serialize_message(value):
    if isinstance(value, MessageSegment):
        return {"type": value.type, "data": serialize_message(value.data)}
    if isinstance(value, dict):
        return {key: serialize_message(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, Message)):
        return [serialize_message(item) for item in value]
    return value


def deserialize_message(segments: list[dict]) -> Message:
    message = Message()
    for segment in segments:
        data = dict(segment["data"])
        if segment["type"] == "node" and isinstance(data.get("content"), list):
            data["content"] = deserialize_message(data["content"])
        message.append(MessageSegment(segment["type"], data))
    return message


async def prepare_manual_photos(urls: list[str]) -> list[dict]:
    parts = []
    for index, url in enumerate(dict.fromkeys(urls)):
        photo = await get_pic(url)
        parts.append(dict(part=f"photo:{index}", mode="direct", message=serialize_message(Message(photo))))
    return parts


async def prepare_manual_tweet_delivery(info: dict, user: str, tweet_id: str, state: dict) -> dict:
    return await prepare_tweet_delivery(info, user, tweet_id, state, manual=True)


def delivery_source_fields(info: dict) -> dict:
    """Use the parsed main post even when old callers carry broader legacy lists."""
    if info.get("view"):
        source = TweetView.from_dict(info["view"]).legacy_fields()
        return {name: source[name] for name in ("pic_url_list", "gif_url_list", "video_url_list", "url_list")}
    text = info.get("text", [])
    main_text = text[0] if text else ""
    return dict(pic_url_list=list(dict.fromkeys(info.get("pic_url_list", []))),
                gif_url_list=list(dict.fromkeys(info.get("gif_url_list", []))),
                video_url_list=list(dict.fromkeys(info.get("video_url_list") or
                                                 ([info["video_url"]] if info.get("video_url") else []))),
                url_list=list(dict.fromkeys(info.get("url_list", text_urls(main_text)))))


async def prepare_video_parts(urls: list[str], *, prefix="video", start=0) -> tuple[list[dict], list[str]]:
    parts, pinned = [], []
    for url in dict.fromkeys(urls):
        files = await get_video_path(url)
        if not files:
            raise ValueError("video_download_empty")
        for path in files:
            video = await get_video(path)
            if video.type != "video":
                raise ValueError("video_prepare_failed")
            pinned.append(str(Path(path).resolve()))
            parts.append(dict(part=f"{prefix}:{start + len(parts)}", mode="video",
                              message=serialize_message(Message(video))))
    return parts, pinned


async def prepare_tweet_delivery(info: dict, user: str, tweet_id: str, state: dict, *, manual=False) -> dict:
    source = delivery_source_fields(info)
    segments = [MessageSegment.image(Path(path).read_bytes()) for path in info.get("rendered_images", [])]
    if not segments and plugin_config.twitter_htmlmode and info.get("html"):
        segments.append(MessageSegment.image(info["html"]))
    if not segments and info.get("text") and (manual or not plugin_config.twitter_no_text):
        # Exhausted rendering still keeps the existing original-text fallback.
        segments.append(MessageSegment.text("\n\n".join(info["text"])))
    for url in source["pic_url_list"]:
        segments.append(await get_pic(url))
    pinned_files = []
    for url in source["gif_url_list"]:
        path = await get_gif_path(url)
        pinned_files.append(str(Path(path).resolve()))
        segments.append(MessageSegment.image(Path(path).read_bytes()))
    if source["url_list"]:
        segments.append(MessageSegment.text("\n" + "\n".join(source["url_list"])))
    if not segments:
        segments.append(MessageSegment.text(f"https://x.com/{user}/status/{tweet_id}"))
    parts = [dict(part="main", mode="reply" if manual else "direct",
                  message=serialize_message(Message(segments)))]
    videos, video_files = await prepare_video_parts(source["video_url_list"])
    parts.extend(videos)
    pinned_files.extend(video_files)
    prepared = dict(status=True, r18=info.get("r18", False), media=info.get("media", False),
                    html="", delivery_version=2, delivery_parts=parts,
                    pinned_files=list(dict.fromkeys(pinned_files)))
    if manual:
        prepared["manual_delivery_version"] = 2
    return prepared


def unwrap_message_segments(segments: list[dict]) -> list[dict]:
    result = []
    for segment in segments:
        if segment["type"] == "node" and isinstance(segment["data"].get("content"), list):
            result.extend(unwrap_message_segments(segment["data"]["content"]))
        else:
            result.append(segment)
    return result


async def upgrade_automatic_delivery(info: dict, user: str, tweet_id: str, state: dict) -> dict:
    """Upgrade traceable frozen artifacts without repeating acknowledged segments."""
    entry = next((item for item in state.get(user, {}).get("pending_tweets", [])
                  if str(item["tweet_id"]) == str(tweet_id)), {})
    processing = entry.get("processing", {})
    try:
        view = TweetView.from_dict(json.loads(Path(processing["view_cache"]).read_text("utf8")))
    except (KeyError, OSError, ValueError):
        # No source ownership can be recovered: retain old receipts and attachments,
        # but unwrap frozen forward nodes so compatible retries send ordinary messages.
        return dict(info, delivery_parts=[dict(part, mode="video" if part["part"].startswith("video:") else "direct",
                                              message=unwrap_message_segments(part["message"]))
                                          for part in info["delivery_parts"]])
    fields = view.legacy_fields()
    old_photos = list(dict.fromkeys(media.url for post in view.main.walk() for media in post.media
                                   if media.kind == "image" and media.url))
    old_gifs = list(dict.fromkeys(media.url for post in view.walk() for media in post.media
                                 if media.kind == "gif" and media.url))
    old_videos = [media.url for post in view.main.walk() for media in post.media
                  if media.kind == "video" and media.url]
    cached = {part["part"]: part for part in info["delivery_parts"]}
    segments, legacy_receipts, photos = [], [], {}
    def append(message, receipt=""):
        plain = unwrap_message_segments(message)
        segments.extend(plain)
        legacy_receipts.extend([receipt] * len(plain))
    for part in info["delivery_parts"]:
        name = part["part"]
        if name.startswith("image:"):
            append(part["message"], name)
        elif name == "original":
            original = unwrap_message_segments(part["message"])
            # The old text fallback had one initial text segment, followed by photos.
            had_text = bool(fields["text"] or entry.get("is_retweet"))
            offset = 1 if had_text and original and original[0]["type"] == "text" else 0
            append(original[:offset], name)
            photos.update(zip(old_photos, original[offset:]))
    for url in fields["pic_url_list"]:
        if url in photos:
            append([photos[url]], "original")
        else:
            append(serialize_message(Message(await get_pic(url))))
    pinned = list(info.get("pinned_files", []))
    for url in fields["gif_url_list"]:
        name = f"gif:{old_gifs.index(url)}"
        if name in cached:
            append(cached[name]["message"], name)
        else:
            path = await get_gif_path(url)
            pinned.append(str(Path(path).resolve()))
            append(serialize_message(Message(MessageSegment.image(Path(path).read_bytes()))))
    if fields["url_list"]:
        append(serialize_message(Message(MessageSegment.text("\n" + "\n".join(fields["url_list"])))),
               "original" if "original" in cached else "")
    # Distinct migrated IDs prevent an old quote-video receipt from suppressing a
    # different main-post video. Matching frozen files keep their original aliases.
    videos = []
    first_video = old_videos[0] if old_videos else ""
    if first_video in fields["video_url_list"]:
        for part in info["delivery_parts"]:
            if part["part"].startswith("video:"):
                videos.append(dict(part=f"video:main:{len(videos)}", mode="video", message=part["message"],
                                   receipt_aliases=[part["part"]]))
    missing = [url for url in fields["video_url_list"] if url != first_video or not videos]
    extra, video_files = await prepare_video_parts(missing, prefix="video:main", start=len(videos))
    videos.extend(extra)
    pinned.extend(video_files)
    if not segments:
        append(serialize_message(Message(MessageSegment.text(f"https://x.com/{user}/status/{tweet_id}"))))
    migrated = dict(info, delivery_version=2, pinned_files=list(dict.fromkeys(pinned)),
                    delivery_parts=[dict(part="main", mode="direct", message=segments,
                                         legacy_receipts=legacy_receipts), *videos])
    if processing.get("delivery_cache"):
        _write_json_dict_atomic(Path(processing["delivery_cache"]), migrated)
    return migrated


async def tweet_handle(tweet_info: dict, user_name: str, line_new_tweet_id: str, twitter_list: dict) -> bool:
    if "delivery_parts" not in tweet_info:
        if not tweet_info.get("status") and not (tweet_info.get("html") and plugin_config.twitter_htmlmode):
            logger.warning(f"{user_name} 的推文 {line_new_tweet_id} 获取失败")
            return False
        tweet_info = await prepare_tweet_delivery(tweet_info, user_name, line_new_tweet_id, twitter_list)
    elif tweet_info.get("delivery_version", 0) < 2 and not any(part["part"] == "main" for part in tweet_info["delivery_parts"]):
        tweet_info = await upgrade_automatic_delivery(tweet_info, user_name, line_new_tweet_id, twitter_list)
    has_main = any(part["part"] == "main" for part in tweet_info["delivery_parts"])
    success = True
    for part in tweet_info["delivery_parts"]:
        success = await send_msg(
            twitter_list, user_name, line_new_tweet_id, tweet_info,
            deserialize_message(unwrap_message_segments(part["message"])),
            "video" if part["part"].startswith("video:") else "direct", part=part["part"],
            requires_main=has_main and part["part"].startswith("video:"),
            legacy_receipts=part.get("legacy_receipts"), receipt_aliases=part.get("receipt_aliases"),
        ) and success
    return success

async def tweet_handle_link(tweet_info: dict,user_name: str,line_new_tweet_id: str):
    if not tweet_info["status"] and not tweet_info["html"]:
        # 啥都没获取到
        logger.warning(f"{user_name} 的推文 {line_new_tweet_id} 获取失败")
        return Message(f"{user_name} 的推文 {line_new_tweet_id} 获取失败")
    elif not tweet_info["status"] and tweet_info["html"]:
        # 起码有个截图
        logger.debug(f"{user_name} 的推文 {line_new_tweet_id} 获取失败，但截图成功，准备发送截图")
        msg = []
        if plugin_config.twitter_htmlmode:
            # 有截图
            bytes_size = sys.getsizeof(tweet_info["html"]) / (1024 * 1024)
            msg.append(MessageSegment.image(tweet_info["html"]))
            if plugin_config.twitter_node:
                # 合并转发
                msg.append(MessageSegment.node_custom(
                    user_id=plugin_config.twitter_qq,
                    nickname=user_name,
                    content=Message(MessageSegment.image(tweet_info["html"]))
                ))
                # await send_msg(twitter_list,user_name,line_new_tweet_id,tweet_info,Message(msg))
            
            return Message(msg)
        return Message("")
    # elif tweet_info["status"] and not tweet_info["html"]:
    #     # 只没有截图？不应该啊
    #     pass
    # elif tweet_info["status"] and tweet_info["html"]:
    else:
        # 有没有截图不知道，内容信息是真有
        all_msg = await get_tweet_context(tweet_info,user_name,line_new_tweet_id)
        has_text = bool(tweet_info["text"]) and not plugin_config.twitter_no_text and not tweet_info.get("rendered_images")
        media_msgs, video_msgs = split_video_messages(all_msg)
        prefer_nitter_first = should_send_nitter_first(tweet_info)
        nitter_msgs, content_media_msgs = split_nitter_preview_messages(tweet_info, media_msgs)
            
        # 准备发送消息
        if plugin_config.twitter_node and not video_msgs:
            # 以合并方式发送
            msg = []
            if prefer_nitter_first:
                for value in nitter_msgs:
                    msg.append(
                        MessageSegment.node_custom(
                            user_id=plugin_config.twitter_qq,
                            nickname=user_name,
                            content=Message(value)
                        )
                    )
            if has_text:
                # 开启了媒体文字
                for x in tweet_info["text"]:
                    msg.append(MessageSegment.node_custom(
                        user_id=plugin_config.twitter_qq,
                        nickname=user_name,
                        content=
                        Message(x)
                    ))
            media_to_append = content_media_msgs if prefer_nitter_first else media_msgs
            for value in  media_to_append:
                msg.append(
                    MessageSegment.node_custom(
                        user_id=plugin_config.twitter_qq,
                        nickname=user_name,
                        content=Message(value)
                    )
                )
            return Message(msg) if msg else Message("")
        else:
            direct_msg = []
            if prefer_nitter_first:
                direct_msg.extend(nitter_msgs)
                if has_text:
                    direct_msg.append(MessageSegment.text('\n\n'.join(tweet_info["text"])))
                direct_msg.extend(content_media_msgs)
            else:
                if has_text:
                    direct_msg.append(MessageSegment.text('\n\n'.join(tweet_info["text"])))
                direct_msg.extend(media_msgs)
            direct_msg.extend(video_msgs)
            return Message(direct_msg) if direct_msg else Message("")
