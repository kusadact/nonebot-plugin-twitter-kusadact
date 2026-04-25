import json

from pydantic import BaseModel
from typing import Literal, Optional
from typing_extensions import TypedDict
from nonebot import get_driver, get_plugin_config


class Config(BaseModel):
    # 自定义镜像站
    twitter_website: Optional[str] = ""
    # 代理
    twitter_proxy: Optional[str] = None
    # 内部当前使用url
    twitter_url: Optional[str] = ""
    # 内部当前使用url
    twitter_img_url: Optional[str] = "https://pbs.twimg.com/media/"
    # 自定义转发消息来源qq
    twitter_qq: int = 2854196310
    # 自定义事件响应等级
    command_priority: int = 10
    # 插件开关
    plugin_enabled: bool = True
    # 网页截图模式
    twitter_htmlmode: bool = False
    # 截取源地址网页
    twitter_original: bool = False
    # Nitter 截图时间显示时区
    twitter_html_timezone: Optional[str] = "Asia/Shanghai"
    # Nitter 截图时间显示标签
    twitter_html_timezone_label: Optional[str] = "UTC+8"
    # Playwright 浏览器通道
    twitter_browser_channel: Optional[str] = None
    # Playwright 浏览器可执行文件路径
    twitter_browser_executable_path: Optional[str] = None
    # 可选：视频文件发送时复制到宿主机可见目录
    twitter_video_send_host_path: Optional[str] = None
    # 可选：视频文件发送时在协议中使用的容器内路径
    twitter_video_send_container_path: Optional[str] = None
    # 可选：远程视频转码接口
    twitter_video_mux_api: Optional[str] = None
    # 可选：远程视频转码接口鉴权 token
    twitter_video_mux_token: Optional[str] = None
    # 可选：Nitter Chrome 会话同步状态文件
    twitter_nitter_sync_status_file: Optional[str] = None
    # 可选：Nitter Chrome 会话失效告警 QQ
    twitter_nitter_alert_qq: int = 0
    # 媒体无文字
    twitter_no_text: bool = False
    # 使用转发消息
    twitter_node: bool = True
           
plugin_config = get_plugin_config(Config)
global_config = get_driver().config


def get_browser_launch_kwargs() -> dict:
    launch_kwargs = {"slow_mo": 50}

    if plugin_config.twitter_proxy:
        launch_kwargs["proxy"] = {"server": plugin_config.twitter_proxy}

    executable_path = (
        plugin_config.twitter_browser_executable_path
        or getattr(global_config, "htmlrender_browser_executable_path", None)
    )
    if executable_path:
        launch_kwargs["executable_path"] = executable_path
        return launch_kwargs

    channel = (
        plugin_config.twitter_browser_channel
        or getattr(global_config, "htmlrender_browser_channel", None)
    )
    if channel:
        launch_kwargs["channel"] = channel

    return launch_kwargs

website_list = [
    "https://nitter.net", # 403
    "https://nitter.poast.org",
    
]

twitter_post = '''() => {
            const elementXPath = '/html/body/div[1]/div/div/div[2]/main/div/div/div/div[1]/div/div[1]/div[1]/div/div/div/div';
            const element = document.evaluate(elementXPath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
            
            if (element) {
                element.remove();
            }
        }'''
twitter_login = '''() => {
            const elementXPath = '/html/body/div[1]/div/div/div[1]/div/div[1]/div';
            const element = document.evaluate(elementXPath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
            
            if (element) {
                element.remove();
            }
        }'''
        
nitter_head = '''() => {
            const elementXPath = '/html/body/nav';
            const element = document.evaluate(elementXPath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
            
            if (element) {
                element.remove();
            }
        }'''


def build_nitter_foot_script() -> str:
    timezone = plugin_config.twitter_html_timezone or ""
    timezone_label = plugin_config.twitter_html_timezone_label or timezone
    return f"""() => {{
            const elementXPath = '/html/body/div[1]/div/div[3]/div';
            const element = document.evaluate(elementXPath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;

            if (element) {{
                element.remove();
            }}

            const timezone = {json.dumps(timezone)};
            const timezoneLabel = {json.dumps(timezone_label)};
            if (!timezone) {{
                return;
            }}

            const parseUtcText = (value) => {{
                if (!value || !/UTC/i.test(value)) {{
                    return null;
                }}

                const normalized = value
                    .replace(/\\s*·\\s*/g, " ")
                    .replace(/\\s+/g, " ")
                    .trim();
                const parsed = new Date(normalized);
                return Number.isNaN(parsed.getTime()) ? null : parsed;
            }};

            const formatTimestamp = (date) => {{
                const datePart = new Intl.DateTimeFormat("en-US", {{
                    timeZone: timezone,
                    month: "short",
                    day: "numeric",
                    year: "numeric",
                }}).format(date);
                const timePart = new Intl.DateTimeFormat("en-US", {{
                    timeZone: timezone,
                    hour: "numeric",
                    minute: "2-digit",
                    hour12: true,
                }}).format(date);
                return `${{datePart}} · ${{timePart}} ${{timezoneLabel}}`;
            }};

            const patchTimestamp = (node) => {{
                const textDate = parseUtcText(node.textContent?.trim() ?? "");
                if (textDate) {{
                    node.textContent = formatTimestamp(textDate);
                }}

                if (typeof node.getAttribute === "function" && typeof node.setAttribute === "function") {{
                    const titleDate = parseUtcText(node.getAttribute("title") ?? "");
                    if (titleDate) {{
                        node.setAttribute("title", formatTimestamp(titleDate));
                    }}
                }}
            }};

            const patchAllTimestamps = () => {{
                document
                    .querySelectorAll(".tweet-published, .tweet-date a")
                    .forEach(patchTimestamp);
            }};

            patchAllTimestamps();

            if (!window.__nbTwitterTimezoneObserverAttached && document.body) {{
                const observer = new MutationObserver(() => {{
                    patchAllTimestamps();
                }});
                observer.observe(document.body, {{
                    childList: true,
                    subtree: true,
                    characterData: true,
                }});
                window.__nbTwitterTimezoneObserverAttached = true;
            }}
        }}"""


nitter_foot = build_nitter_foot_script()
        
class SetCookieParam(TypedDict, total=False):
    name: str
    value: str
    url: Optional[str]
    domain: Optional[str]
    path: Optional[str]
    expires: Optional[float]
    httpOnly: Optional[bool]
    secure: Optional[bool]
    sameSite: Optional[Literal["Lax", "None", "Strict"]]
