import json
import random
import sys
import typing
import httpx
import os
import shutil
from typing import Optional,Literal
from pathlib import Path
from datetime import datetime
from urllib.parse import quote, unquote, urljoin
from bs4 import BeautifulSoup
from nonebot import logger
from nonebot.adapters.onebot.v11 import MessageSegment,Message
from playwright.async_api import async_playwright,Browser
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

TIMELINE_SEEN_LIMIT = 50


def build_httpx_client_kwargs(*, http2: bool = False, timeout: Optional[float] = None) -> dict:
    kwargs = {}
    if plugin_config.twitter_proxy:
        kwargs["proxy"] = plugin_config.twitter_proxy
    if http2:
        kwargs["http2"] = True
    if timeout is not None:
        kwargs["timeout"] = timeout
    return kwargs


def normalize_tweet_href(href: str) -> str:
    return href.split("#", 1)[0]


def parse_timeline_entries(soup: BeautifulSoup, follow_user_name: str) -> list[dict]:
    entries = []
    for timeline_item in soup.find_all("div", class_="timeline-item"):
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
                "signature": f"{'retweet' if is_retweet else 'tweet'}:{href}",
                "is_retweet": is_retweet,
                "is_quote": is_quote,
            }
        )

    return entries


def get_recent_timeline_signatures(entries: list[dict], limit: int = TIMELINE_SEEN_LIMIT) -> list[str]:
    return [entry["signature"] for entry in entries[:limit]]


def get_new_timeline_entries(entries: list[dict], seen_signatures: list[str]) -> list[dict]:
    if not entries or not seen_signatures:
        return []

    current_signatures = {entry["signature"] for entry in entries}
    if not current_signatures.intersection(seen_signatures):
        return []

    seen_signature_set = set(seen_signatures)
    new_entries = []
    for entry in entries:
        if entry["signature"] in seen_signature_set:
            break
        new_entries.append(entry)

    new_entries.reverse()
    return new_entries

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
            return parse_timeline_entries(soup, user_name)
        else:
            logger.warning(f"通过 user_name {user_name} 获取时间线失败：{res.status_code} {res.text}")
            return []


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
    
async def get_timeline_screen(browser: Browser,user_name: str,length: int = 5):
    url=f"{plugin_config.twitter_url}/{user_name}"
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await page.goto(url,timeout=60000)
        await page.wait_for_load_state("networkidle")
        await page.wait_for_selector('.timeline-item')

        tweets = await page.query_selector_all('.timeline-item')
        if not tweets:
            return None

        visible_count = min(max(length, 1), 5, len(tweets))
        visible_tweets = tweets[:visible_count]

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
        return None
    finally:
        await page.close()
        await context.close()
    
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
    try:
        result = {}
        result["status"] = False
        result["html"] = b""
        result["media"] = False
        url=f"{plugin_config.twitter_url}/{user_name}/status/{tweet_id}"

        if plugin_config.twitter_htmlmode:
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
                await page.goto(url,timeout=60000)
                await page.wait_for_load_state("load",timeout=60000)
                await page.evaluate(nitter_head)
                await page.evaluate(nitter_foot)
                screenshot_bytes = await page.locator("xpath=/html/body/div[1]/div").screenshot(timeout=60000)
            logger.info(f"使用浏览器截图获取 {url} 推文信息成功")
            result["html"] = screenshot_bytes
            await page.close()
            await context.close()

        async with httpx.AsyncClient(**build_httpx_client_kwargs(http2=True, timeout=120)) as client:
            res = await client.get(url,cookies={"hlsPlayback": "on"},headers=header)
            if res.status_code ==200:
                soup = BeautifulSoup(res.text,"html.parser")

                # text && pic && video
                result["text"] = []
                result["pic_url_list"] = []
                result["video_url"] = ""
                result["quote_text"] = ""
                result["quote_user_name"] = ""
                if main_thread_div := soup.find('div', class_='main-thread'):
                    # pic
                    if pic_list := main_thread_div.find_all('a', class_='still-image'): # type: ignore
                        result["pic_url_list"] = [x.attrs["href"] for x in pic_list]
                    # video
                    if video_list := main_thread_div.find_all('video'): # type: ignore
                        if video_url := video_list[0].attrs.get("data-url"):
                            result["video_url"] = video_url
                        else:
                            try:
                                video_url = video_list[0].parent.parent.parent.parent.parent.contents[1].attrs["href"].replace("#m","")
                            except Exception as e:
                                logger.info(f"获取视频推文链接出错，转为获取自身链接，{e}")
                                video_url = url.split(plugin_config.twitter_url)[1]
                            result["video_url"] = f"https://x.com{video_url}"
                    # text
                    if match := main_thread_div.find_all('div', class_='tweet-content media-body'): # type: ignore
                        for x in match:
                            if x.parent.attrs["class"] == "replying-to":
                                continue
                            result["text"].append(x.text)
                    if quote_div := main_thread_div.find('div', class_='quote'): # type: ignore
                        if quote_text := quote_div.find('div', class_='quote-text'):
                            result["quote_text"] = quote_text.text.strip()
                        if quote_user := quote_div.find('a', class_='username'):
                            result["quote_user_name"] = quote_user.text.strip().lstrip("@")
                        if result["quote_text"]:
                            quote_prefix = "引用"
                            if result["quote_user_name"]:
                                quote_prefix = f"引用 @{result['quote_user_name']}"
                            result["text"].append(f"{quote_prefix}：\n{result['quote_text']}")
                # r18
                result["r18"] = bool(r18 := soup.find_all('div', class_='unavailable-box'))
                if result["video_url"] or result["pic_url_list"]:
                    result["media"] = True
                    logger.info(f"推主 {user_name} 的推文 {tweet_id} 存在媒体")
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
            filename = f"{int(datetime.now().timestamp())}.mp4"
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

            filename = f"{int(datetime.now().timestamp())}.mp4"
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

        async with httpx.AsyncClient(**build_httpx_client_kwargs(timeout=120)) as client:
            if "/video/" in url:
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
                filename = str(int(datetime.now().timestamp())) + ".mp4"
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

async def get_video(file_path: str) -> MessageSegment:
    '修改为返回视频消息，而非合并视频消息'
    # return MessageSegment.node_custom(user_id=user_id, nickname=name,
    #                                       content=Message(MessageSegment.video(f"file:///{task}"))) 
    try:
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
    def build_image_candidates(raw_url: str) -> list[str]:
        candidates: list[str] = []
        if raw_url.startswith("/"):
            candidates.append(f"{plugin_config.twitter_url}{raw_url}")

            decoded = unquote(raw_url)
            if decoded.startswith("/pic/orig/media/"):
                filename = decoded.removeprefix("/pic/orig/media/")
                stem, ext = os.path.splitext(filename)
                ext = ext.lstrip(".").lower()
                if stem and ext:
                    candidates.append(
                        f"{plugin_config.twitter_img_url}{stem}?format={ext}&name=large"
                    )
        else:
            candidates.append(raw_url)

        deduped: list[str] = []
        for candidate in candidates:
            if candidate not in deduped:
                deduped.append(candidate)
        return deduped

    candidates = build_image_candidates(url)
    async with httpx.AsyncClient(**build_httpx_client_kwargs(http2=True)) as client:
        last_error = ""
        for candidate in candidates:
            try:
                res = await client.get(candidate, headers=header, timeout=120)
                if res.status_code != 200:
                    last_error = f"HTTP {res.status_code}"
                    logger.warning(f"图片下载失败:{candidate}，状态码：{res.status_code}")
                    continue
                tmp = bytes(random.randint(0,255))
                return MessageSegment.image(file=(res.read()+tmp))
            except Exception as e:
                last_error = str(e)
                logger.warning(f"获取图片出现异常 {candidate} ：{e}")

        failed_url = candidates[0] if candidates else url
        if last_error:
            logger.warning(f"图片全部下载失败，最终错误：{last_error}")
        return MessageSegment.text(f"图片加载失败 X_X 图片链接 {failed_url}")



async def is_chromium_installed():
    '''check chromium install | 检测Chromium是否已经安装'''
    try:
        playwright_manager = async_playwright()
        playwright = await playwright_manager.start()
        browser = await playwright.chromium.launch(**get_browser_launch_kwargs())
        await browser.close()
        await playwright.stop()
        return True
    except Exception:
        return False
    
    
# 发送
async def send_msg(twitter_list: dict,user_name: str,line_new_tweet_id: str,tweet_info: dict,msg: Message,mode:Optional[Literal["node","direct","video"]] = "node"):
    for group_num in twitter_list[user_name]["group"]:
        # 群聊
        if twitter_list[user_name]["group"][group_num]["status"]:
            if twitter_list[user_name]["group"][group_num]["r18"] == False and tweet_info["r18"] == True:
                logger.info(f"根据r18设置，群 {group_num} 的推文 {user_name}/status/{line_new_tweet_id} 跳过发送")
                continue
            if twitter_list[user_name]["group"][group_num]["media"] == True and tweet_info["media"] == False:
                logger.info(f"根据媒体设置，群 {group_num} 的推文 {user_name}/status/{line_new_tweet_id} 跳过发送")
                continue
            try:
                if mode == "node":
                    # 以合并方式发送
                    if await tools.send_group_forward_msg_by_bots(group_id=int(group_num), node_msg=msg):
                        logger.info(f"群 {group_num} 的推文 {user_name}/status/{line_new_tweet_id} 合并发送成功")
                elif mode == "direct":
                    if await tools.send_group_msg_by_bots(group_id=int(group_num), msg=msg):
                        logger.info(f"群 {group_num} 的推文 {user_name}/status/{line_new_tweet_id} 直接发送成功")
                elif mode == "video":
                    if await tools.send_group_msg_by_bots(group_id=int(group_num), msg=msg):
                        logger.info(f"群 {group_num} 的推文 {user_name}/status/{line_new_tweet_id} 视频发送成功")
            except Exception as e:
                logger.warning(f"发送消息出现失败,目标群：{group_num},推文 {user_name}/status/{line_new_tweet_id}，发送模式 {'截图' if tweet_info['html'] else '内容'}，异常{e}")
        else:
            logger.info(f"根据通知设置，群 {group_num} 的推文 {user_name}/status/{line_new_tweet_id} 跳过发送")
            
    for qq in twitter_list[user_name]["private"]:
        # 私聊
        if twitter_list[user_name]["private"][qq]["status"]:
            if twitter_list[user_name]["private"][qq]["r18"] == False and tweet_info["r18"] == True:
                logger.info(f"根据r18设置，qq {qq} 的推文 {user_name}/status/{line_new_tweet_id} 跳过发送")   
                continue
            if twitter_list[user_name]["private"][qq]["media"] == True and tweet_info["media"] == False:
                logger.info(f"根据媒体设置，qq {qq} 的推文 {user_name}/status/{line_new_tweet_id} 跳过发送")   
                continue
            try:
                if mode == "node":
                    if await tools.send_private_forward_msg_by_bots(user_id=int(qq), node_msg=msg):
                        logger.info(f"qq {qq} 的推文 {user_name}/status/{line_new_tweet_id} 合并发送成功")
                elif mode == "direct":
                    if await tools.send_private_msg_by_bots(user_id=int(qq), msg=msg):
                        logger.info(f"qq {qq} 的推文 {user_name}/status/{line_new_tweet_id} 直接发送成功")
                elif mode == "video":
                    if await tools.send_private_msg_by_bots(user_id=int(qq), msg=msg):
                        logger.info(f"qq {qq} 的推文 {user_name}/status/{line_new_tweet_id} 视频发送成功")
            except Exception as e:
                logger.warning(f"发送消息出现失败,目标qq：{qq},推文 {user_name}/status/{line_new_tweet_id}，发送模式 {'截图' if tweet_info['html'] else '内容'}，异常{e}")
        else:
            logger.info(f"根据通知设置，qq {qq} 的推文 {user_name}/status/{line_new_tweet_id} 跳过发送")                    

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
    if plugin_config.twitter_htmlmode:
        bytes_size = sys.getsizeof(tweet_info["html"]) / (1024 * 1024)
        all_msg.append(MessageSegment.image(tweet_info["html"]))
    
    # 返回图片
    if tweet_info["pic_url_list"]:
        for url in tweet_info["pic_url_list"]:
            # print(f"打印图片链接: {url}")
            # if ".jpg" in url:
            #     url = url.replace(".jpg")
            all_msg.append(await get_pic(url))
            
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


async def tweet_handle(tweet_info: dict,user_name: str,line_new_tweet_id: str,twitter_list: dict) -> bool:
    if not tweet_info["status"] and not tweet_info["html"]:
        # 啥都没获取到
        logger.warning(f"{user_name} 的推文 {line_new_tweet_id} 获取失败")
        return False
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
                    nickname=twitter_list[user_name]["screen_name"],
                    content=Message(MessageSegment.image(tweet_info["html"]))
                ))
                await send_msg(twitter_list,user_name,line_new_tweet_id,tweet_info,Message(msg))
            else:
                # 直接发送
                await send_msg(twitter_list,user_name,line_new_tweet_id,tweet_info,Message(msg),"direct")
                
            return True
        return False
    # elif tweet_info["status"] and not tweet_info["html"]:
    #     # 只没有截图？不应该啊
    #     pass
    # elif tweet_info["status"] and tweet_info["html"]:
    else:
        # 有没有截图不知道，内容信息是真有
        all_msg = await get_tweet_context(tweet_info,user_name,line_new_tweet_id)
        has_text = bool(tweet_info["text"]) and not plugin_config.twitter_no_text
        media_msgs, video_msgs = split_video_messages(all_msg)
        prefer_nitter_first = should_send_nitter_first(tweet_info)
        nitter_msgs, content_media_msgs = split_nitter_preview_messages(tweet_info, media_msgs)
            
        # 准备发送消息
        if plugin_config.twitter_node and not video_msgs:
            # 以合并方式发送
            msg = []
            ordered_media_msgs = media_msgs
            if prefer_nitter_first:
                ordered_media_msgs = [*nitter_msgs, *content_media_msgs]
                for value in nitter_msgs:
                    msg.append(
                        MessageSegment.node_custom(
                            user_id=plugin_config.twitter_qq,
                            nickname=twitter_list[user_name]["screen_name"],
                            content=Message(value)
                        )
                    )
            if has_text:
                # 开启了媒体文字
                for x in tweet_info["text"]:
                    msg.append(MessageSegment.node_custom(
                        user_id=plugin_config.twitter_qq,
                        nickname=twitter_list[user_name]["screen_name"],
                        content=
                        Message(x)
                    ))
            for value in ordered_media_msgs[len(nitter_msgs):] if prefer_nitter_first else ordered_media_msgs:
                msg.append(
                    MessageSegment.node_custom(
                        user_id=plugin_config.twitter_qq,
                        nickname=twitter_list[user_name]["screen_name"],
                        content=Message(value)
                    )
                )
            if msg:
                # 发送合并消息
                await send_msg(twitter_list,user_name,line_new_tweet_id,tweet_info,Message(msg))
            else:
                logger.info(f"推文 {user_name}/status/{line_new_tweet_id} 根据配置过滤后无可发送内容")
        else:
            if video_msgs:
                # 合并转发不稳定支持视频，视频推文改为拆开发送
                if prefer_nitter_first and nitter_msgs:
                    await send_msg(twitter_list,user_name,line_new_tweet_id,tweet_info,Message(nitter_msgs),"direct")
                if has_text:
                    await send_msg(
                        twitter_list,
                        user_name,
                        line_new_tweet_id,
                        tweet_info,
                        Message(MessageSegment.text('\n\n'.join(tweet_info["text"]))),
                        "direct",
                    )
                media_to_send = content_media_msgs if prefer_nitter_first else media_msgs
                if media_to_send:
                    await send_msg(twitter_list,user_name,line_new_tweet_id,tweet_info,Message(media_to_send),"direct")
                for video_msg in video_msgs:
                    await send_msg(twitter_list,user_name,line_new_tweet_id,tweet_info,Message(video_msg),"video")
            else:
                # 以直接发送的方式
                if prefer_nitter_first:
                    if nitter_msgs:
                        await send_msg(twitter_list,user_name,line_new_tweet_id,tweet_info,Message(nitter_msgs),"direct")
                    if has_text:
                        await send_msg(
                            twitter_list,
                            user_name,
                            line_new_tweet_id,
                            tweet_info,
                            Message(MessageSegment.text('\n\n'.join(tweet_info["text"]))),
                            "direct",
                        )
                    if content_media_msgs:
                        await send_msg(twitter_list,user_name,line_new_tweet_id,tweet_info,Message(content_media_msgs),"direct")
                    if not nitter_msgs and not has_text and not content_media_msgs:
                        logger.info(f"推文 {user_name}/status/{line_new_tweet_id} 根据配置过滤后无可发送内容")
                else:
                    if has_text:
                        # 开启了媒体文字
                        media_msgs.insert(0, MessageSegment.text('\n\n'.join(tweet_info["text"])))
                    if media_msgs:
                        # 剩余部分直接发送
                        await send_msg(twitter_list,user_name,line_new_tweet_id,tweet_info,Message(media_msgs),"direct")
                    else:
                        logger.info(f"推文 {user_name}/status/{line_new_tweet_id} 根据配置过滤后无可发送内容")
            
            
        # 更新本地缓存
        current_since_id = str(twitter_list[user_name].get("since_id", "0"))
        try:
            twitter_list[user_name]["since_id"] = str(max(int(current_since_id), int(line_new_tweet_id)))
        except Exception:
            twitter_list[user_name]["since_id"] = current_since_id
        dirpath.write_text(json.dumps(twitter_list))
        return True
    
    
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
        has_text = bool(tweet_info["text"]) and not plugin_config.twitter_no_text
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
