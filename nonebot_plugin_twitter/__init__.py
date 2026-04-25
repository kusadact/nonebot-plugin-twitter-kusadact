import os
import sys
from nonebot import on_regex, require,on_command,get_driver
require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler
from nonebot.adapters.onebot.v11 import Message,MessageEvent,Bot,GroupMessageEvent,MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg,RegexStr
from nonebot.log import logger
from nonebot.adapters.onebot.v11.adapter import Adapter
from nonebot.exception import FinishedException
from nonebot.plugin import PluginMetadata
from pathlib import Path
from importlib.metadata import version
import random
from httpx import AsyncClient,Client
import asyncio
from playwright.async_api import async_playwright
from .config import (
    Config,
    get_browser_launch_kwargs,
    get_plugin_config,
    plugin_config,
    website_list,
)
from .api import *


__plugin_meta__ = PluginMetadata(
    name="twitter 推特订阅",
    description="订阅 twitter 推文",
    usage="""
| 指令 | 权限 | 需要@ | 范围 | 说明 |
|:-----:|:----:|:----:|:----:|:----:|
| 关注推主 | 无 | 否 | 群聊/私聊 | 关注，指令格式：“关注推主 <推主id> [r18] [媒体]”|
| 取关推主 | 无 | 否 | 群聊/私聊 | 取关切割 |
| 推主列表 | 无 | 否 | 群聊/私聊 | 展示列表 |
| 推文列表 | 无 | 否 | 群聊/私聊 | 展示最多5条时间线推文，指令格式：“推文列表 <推主id>” |
| 推文推送关闭 | 无 | 否 | 群聊/私聊 | 关闭推送 |
| 推文推送开启 | 无 | 否 | 群聊/私聊 | 开启推送 |
| 推文链接识别关闭 | 无 | 否 | 群聊 | 关闭链接识别 |
| 推文链接识别开启 | 无 | 否 | 群聊 | 开启链接识别 |
    """,
    type="application",
    config=Config,
    homepage="https://github.com/nek0us/nonebot-plugin-twitter",
    supported_adapters={"~onebot.v11"},
    extra={
        "author":"nek0us",
        "version": version("nonebot_plugin_twitter"),
        "priority":plugin_config.command_priority
    }
)

web_list = []


def normalize_website_url(url: str) -> str:
    return url.rstrip("/")


def is_valid_website_response(content: str) -> bool:
    markers = (
        "profile-card-fullname",
        "timeline-item",
        "tweet-link",
        "main-thread",
        "tweet-content media-body",
    )
    return any(marker in content for marker in markers)


def pick_website(client: Client) -> str:
    probe_paths = ("/elonmusk", "/jack/status/20")
    for raw_url in web_list:
        url = normalize_website_url(raw_url)
        for path in probe_paths:
            full_url = f"{url}{path}"
            try:
                res = client.get(full_url, timeout=60)
                if res.status_code == 200 and is_valid_website_response(res.text):
                    logger.info(f"website: {url} ok! ({path})")
                    return url
            except Exception as e:
                logger.debug(f"website选择异常：{e}")
        logger.info(f"website: {url} failed!")
    return ""


if plugin_config.twitter_website:
    logger.info("使用自定义 website")
    web_list.append(normalize_website_url(plugin_config.twitter_website))
for url in website_list:
    normalized_url = normalize_website_url(url)
    if normalized_url not in web_list:
        web_list.append(normalized_url)

get_driver = get_driver()
@get_driver.on_startup
async def pywt_init():
    if plugin_config.twitter_htmlmode and not await is_chromium_installed():
        logger.warning("Chromium browser is not installed for Playwright")
        
async def create_browser():
    playwright_manager = async_playwright()
    playwright = await playwright_manager.start()
    launch_kwargs = get_browser_launch_kwargs()
    browser = await playwright.chromium.launch(**launch_kwargs)
    return playwright,browser
        

with Client(**build_httpx_client_kwargs(http2=True)) as client:
    plugin_config.twitter_url = pick_website(client)

if plugin_config.twitter_url:
    logger.info(f"当前使用推文站点：{plugin_config.twitter_url}")
else:
    logger.warning("未找到可用的推文站点，请检查自定义 website 或代理配置")
        
# 清理垃圾
@scheduler.scheduled_job("cron",hour="5")
def clean_pic_cache():
    path = Path() / "data" / "twitter" / "cache"
    filenames = [f for f in os.listdir(path) if os.path.isfile(os.path.join(path,f))]
    timeline = int(datetime.now().timestamp()) - 60 * 60 * 5
    [os.remove(path / f) for f in filenames if int(f.split(".")[0]) <= timeline]

    
        
if plugin_config.plugin_enabled:
    if not plugin_config.twitter_url:
        logger.debug(f"website 推文服务器为空，跳过推文定时检索")
    else:
        @scheduler.scheduled_job("interval", minutes=30, id="twitter", misfire_grace_time=179)
        async def now_twitter():
            playwright, browser = await create_browser()
            twitter_list = read_twitter_list()
            results = []
            try:
                for user_name in twitter_list:
                    # 检查单个用户状态
                    result = await get_status(user_name, twitter_list, browser)
                    results.append(result)
                    # 检查完一个用户后等待5秒再检查下一个
                    await asyncio.sleep(5)
                    
                if plugin_config.twitter_website == "":
                    true_count = sum(1 for elem in results if elem)
                    if true_count < len(results) / 2:
                        plugin_config.twitter_url = get_next_element(website_list, plugin_config.twitter_url)
                        logger.debug(f"检测到当前镜像站出错过多，切换镜像站至：{plugin_config.twitter_url}")
            except Exception as e:
                logger.warning(f"twitter 任务出错{e}")
            finally:
                await browser.close()
                await playwright.stop()
                
async def get_status(user_name,twitter_list,browser:Browser) -> bool:
    # 获取推文
    try:
        timeline_entries = await get_user_timeline_entries(user_name)
        timeline_seen = twitter_list[user_name].get("timeline_seen", [])
        current_signatures = get_recent_timeline_signatures(timeline_entries)

        if not current_signatures:
            return True

        if not timeline_seen:
            def init_timeline_seen(latest_twitter_list: dict):
                if user_name not in latest_twitter_list:
                    return
                latest_twitter_list[user_name]["timeline_seen"] = current_signatures

            update_twitter_list(init_timeline_seen)
            logger.info(f"初始化 {user_name} 的时间线游标")
            return True

        new_entries = get_new_timeline_entries(timeline_entries, timeline_seen)
        result = True

        for entry in new_entries:
            tweet_info = await get_tweet(browser, entry["source_user_name"], entry["tweet_id"])
            if entry["is_retweet"]:
                tweet_info["is_retweet"] = True
                retweet_text = f"@{user_name} 转帖了 @{entry['source_user_name']}"
                tweet_info["text"] = [retweet_text, *tweet_info.get("text", [])]
            result = await tweet_handle(tweet_info, user_name, entry["tweet_id"], twitter_list) and result

        def persist_timeline_seen(latest_twitter_list: dict):
            if user_name not in latest_twitter_list:
                return
            latest_twitter_list[user_name]["timeline_seen"] = current_signatures

        update_twitter_list(persist_timeline_seen)
        return result
    except Exception as e:
        logger.debug(f"获取 {user_name} 的推文出现异常：{e}")
        return False


save = on_command("关注推主",block=True,priority=plugin_config.command_priority)
@save.handle()
async def save_handle(bot:Bot,event: MessageEvent,matcher: Matcher,arg: Message = CommandArg()):
    if not plugin_config.twitter_url:
        await matcher.finish("website 推文服务器访问失败，请检查连通性或代理")
    data = []
    if " " in arg.extract_plain_text():
        data = arg.extract_plain_text().split(" ")
    else:
        data.append(arg.extract_plain_text())
        data.append("")
    user_info = await get_user_info(data[0])
    
    if not user_info["status"]:
        await matcher.finish(f"未找到 {data[0]}")

    tweet_id = await get_user_newtimeline(data[0])
    timeline_entries = await get_user_timeline_entries(data[0])
    timeline_seen = get_recent_timeline_signatures(timeline_entries)
    
    def save_subscription(latest_twitter_list: dict):
        user_entry = ensure_twitter_user_entry(latest_twitter_list, data[0])
        subscription = {
            "status":True,
            "r18":True if 'r18' in data[1:] else False,
            "media":True if '媒体' in data[1:] else False
        }
        if isinstance(event,GroupMessageEvent):
            user_entry["group"][str(event.group_id)] = subscription
        else:
            user_entry["private"][str(event.user_id)] = subscription

        current_since_id = str(user_entry.get("since_id", "0"))
        try:
            user_entry["since_id"] = str(max(int(current_since_id), int(tweet_id)))
        except Exception:
            user_entry["since_id"] = tweet_id
        user_entry["timeline_seen"] = timeline_seen
        user_entry["screen_name"] = user_info["screen_name"]

    update_twitter_list(save_subscription)
    await matcher.finish(f"id:{data[0]}\nname:{user_info['screen_name']}\n{user_info['bio']}\n订阅成功")
        

delete = on_command("取关推主",block=True,priority=plugin_config.command_priority)
@delete.handle()
async def delete_handle(bot:Bot,event: MessageEvent,matcher: Matcher,arg: Message = CommandArg()):
    user_name = arg.extract_plain_text()

    def delete_subscription(latest_twitter_list: dict):
        if user_name not in latest_twitter_list:
            return f"未找到 {arg}"

        user_entry = ensure_twitter_user_entry(latest_twitter_list, user_name)
        if isinstance(event,GroupMessageEvent):
            group_id = str(event.group_id)
            if group_id not in user_entry["group"]:
                return f"本群未订阅 {arg}"
            user_entry["group"].pop(group_id)
        else:
            private_id = str(event.user_id)
            if private_id not in user_entry["private"]:
                return f"未订阅 {arg}"
            user_entry["private"].pop(private_id)

        if user_entry["group"] == {} and user_entry["private"] == {}:
            latest_twitter_list.pop(user_name, None)
        return ""

    error_message = update_twitter_list(delete_subscription)
    if error_message:
        await matcher.finish(error_message)
    
    await matcher.finish(f"取关 {arg.extract_plain_text()} 成功")
    
follow_list = on_command("推主列表",block=True,priority=plugin_config.command_priority)
@follow_list.handle()
async def follow_list_handle(bot:Bot,event: MessageEvent,matcher: Matcher):
    
    twitter_list = read_twitter_list()
    msg = []
    
    if isinstance(event,GroupMessageEvent):
        for user_name in twitter_list:
            if str(event.group_id) in twitter_list[user_name]["group"]:
                msg += [
                    MessageSegment.node_custom(
                        user_id=plugin_config.twitter_qq, nickname=twitter_list[user_name]["screen_name"], content=Message(
                            f"{user_name}  {'r18' if twitter_list[user_name]['group'][str(event.group_id)]['r18'] else ''}  {'媒体' if twitter_list[user_name]['group'][str(event.group_id)]['media'] else ''}"
                            )
                    )
                ]
        await bot.send_group_forward_msg(group_id=event.group_id, messages=msg)
    else:
        for user_name in twitter_list:
            if str(event.user_id) in twitter_list[user_name]["private"]:
                msg += [
                    MessageSegment.node_custom(
                        user_id=plugin_config.twitter_qq, nickname=twitter_list[user_name]["screen_name"], content=Message(
                            f"{user_name}  {'r18' if twitter_list[user_name]['private'][str(event.user_id)]['r18'] else ''}  {'媒体' if twitter_list[user_name]['private'][str(event.user_id)]['media'] else ''}"
                            )
                    )
                ]
        await bot.send_private_forward_msg(user_id=event.user_id, messages=msg)          
    
    await matcher.finish()


async def is_rule(event:MessageEvent) -> bool:
    if isinstance(event,GroupMessageEvent):
        if event.sender.role in ["owner","admin"]:
            return True
        return False
    else:
        return True
    
twitter_status = on_command("推文推送",block=True,rule=is_rule,priority=plugin_config.command_priority)
@twitter_status.handle()
async def twitter_status_handle(bot:Bot,event: MessageEvent,matcher: Matcher,arg: Message = CommandArg()):
    try:
        desired_status = None
        if arg.extract_plain_text() == "开启":
            desired_status = True
        elif arg.extract_plain_text() == "关闭":
            desired_status = False
        else:
            await matcher.finish("错误指令")

        def update_push_status(latest_twitter_list: dict):
            if isinstance(event,GroupMessageEvent):
                group_id = str(event.group_id)
                for user_name in latest_twitter_list:
                    user_entry = ensure_twitter_user_entry(latest_twitter_list, user_name)
                    if group_id in user_entry["group"]:
                        user_entry["group"][group_id]["status"] = desired_status
            else:
                private_id = str(event.user_id)
                for user_name in latest_twitter_list:
                    user_entry = ensure_twitter_user_entry(latest_twitter_list, user_name)
                    if private_id in user_entry["private"]:
                        user_entry["private"][private_id]["status"] = desired_status

        update_twitter_list(update_push_status)
        await matcher.finish(f"推送已{arg.extract_plain_text()}")
    except FinishedException:
        pass
    except Exception as e:
        await matcher.finish(f"异常:{e}")

pat_twitter = on_regex(r'(twitter\.com|x\.com)/[a-zA-Z0-9_]+/status/\d+',priority=plugin_config.command_priority)
@pat_twitter.handle()
async def pat_twitter_handle(bot: Bot,event: MessageEvent,matcher: Matcher,text: str = RegexStr()):
    logger.info(f"检测到推文链接 {text}")
    link_list = read_link_list()
    playwright,browser = await create_browser()
    try:
        if isinstance(event,GroupMessageEvent):
            # 是群，处理一下先
            if str(event.group_id) not in link_list:
                def init_group_link_setting(latest_link_list: dict):
                    latest_link_list.setdefault(str(event.group_id), {"link":True})

                update_link_list(init_group_link_setting)
                link_list = read_link_list()
            
            if not link_list[str(event.group_id)]["link"]:
                # 关闭了链接识别
                logger.info(f"根据群设置，不获取推文链接内容 {text}")
                await matcher.finish()
        # 处理完了 继续
        
        # x.com/username/status/tweet_id     
        tmp = text.split("/")
        user_name = tmp[1]
        tweet_id = tmp[-1]
        
        tweet_info = await get_tweet(browser,user_name,tweet_id)
        msg = await tweet_handle_link(tweet_info,user_name,tweet_id)
        if plugin_config.twitter_node and all(segment.type == "node" for segment in msg):
            if isinstance(event,GroupMessageEvent):
                await bot.send_group_forward_msg(group_id=int(event.group_id), messages=msg)
            else:
                await bot.send_private_forward_msg(user_id=int(event.user_id), messages=msg)
        else:
            if any(segment.type == "video" for segment in msg):
                for segment in msg:
                    await matcher.send(segment, reply_message=True)
            else:
                await matcher.send(msg, reply_message=True)
    except FinishedException:
        pass            
    except Exception as e:
        await matcher.send(f"异常:{e}")
    finally:
        await browser.close()
        await playwright.stop()
        await matcher.finish()
        
twitter_link = on_command("推文链接识别",priority=plugin_config.command_priority)
@twitter_link.handle()
async def twitter_link_handle(event: GroupMessageEvent,matcher: Matcher,arg: Message = CommandArg()):
    if "开启" in arg.extract_plain_text():
        desired_status = True
    elif "关闭" in arg.extract_plain_text():
        desired_status = False
    else:
        await matcher.finish("仅支持“开启”和“关闭”操作")

    def update_group_link_setting(latest_link_list: dict):
        latest_link_list.setdefault(str(event.group_id), {"link":True})
        latest_link_list[str(event.group_id)]["link"] = desired_status

    update_link_list(update_group_link_setting)
    await matcher.finish(f"推文链接识别已{arg.extract_plain_text()}")    
    
twitter_timeline = on_command("推文列表",priority=plugin_config.command_priority)
@twitter_timeline.handle()
async def twitter_timeline_handle(bot: Bot,event: MessageEvent,matcher: Matcher,arg: Message = CommandArg()):
    if not plugin_config.twitter_htmlmode:
        await matcher.finish(f"暂时仅支持html模式，请先联系超级管理员开启")
    
    await matcher.send(f"获取中, 请稍等一下..")
    
    user_info = await get_user_info(arg.extract_plain_text())
    
    if not user_info["status"]:
        await matcher.finish(f"未找到 {arg.extract_plain_text()}")
    new_line = await get_user_timeline(user_info["user_name"])
    if "not found" in new_line:
        await matcher.finish(f"未找到 {arg.extract_plain_text()} 存在推文时间线")
    if len(new_line) > 5:
        new_line = new_line[:5]
    playwright,browser = await create_browser()
    try:
        screen = await get_timeline_screen(browser,user_info["user_name"],len(new_line))
        if not screen:
            await matcher.finish("好像失败了...")
        await matcher.send(MessageSegment.image(file=screen))
    except FinishedException:
        pass            
    except Exception as e:
        await matcher.send(f"异常:{e}")
    finally:
        await browser.close()
        await playwright.stop()
        await matcher.finish()
