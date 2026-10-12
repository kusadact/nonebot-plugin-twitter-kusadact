<div align="center">
  <a href="https://v2.nonebot.dev/store"><img src="https://github.com/A-kirami/nonebot-plugin-template/blob/resources/nbp_logo.png" width="180" height="180" alt="NoneBotPluginLogo"></a>
  <br>
  <p><img src="https://github.com/A-kirami/nonebot-plugin-template/blob/resources/NoneBotPlugin.svg" width="240" alt="NoneBotPluginText"></p>
</div>

<div align="center">

# nonebot-plugin-twitter

_✨ 推文订阅推送插件 ✨_


<a href="./LICENSE">
    <img src="https://img.shields.io/github/license/nek0us/nonebot-plugin-twitter.svg" alt="license">
</a>
<a href="https://pypi.python.org/pypi/nonebot-plugin-twitter">
    <img src="https://img.shields.io/pypi/v/nonebot-plugin-twitter.svg" alt="pypi">
</a>
<img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="python">

</div>

⚠ 需要可用的 Nitter 镜像站或自建实例。公开实例稳定性通常较差，建议自建。

## 📖 介绍

订阅推送 Twitter 推文。支持主帖、引用、回复和投票的完整图片模板、按需简体中文翻译、GIF 附件、超长图片分片，以及持久推送队列和按回执补发。

正式模板和翻译默认关闭；开启翻译后，自动订阅及群聊/私聊中的手动推文链接均会翻译。手动链接先确认入队，完成后发送图片，重试不占用群消息处理。配置与重试规则见 [图片、翻译与投票说明](docs/translation.md)，线路配置格式见 [.env.example](.env.example)。翻译主力最多 10 次、保底最多 3 次；校验失败的下一次请求带错误反馈修正，次数仍计入原预算。

运行环境需要 Python 3.10+、Playwright 可启动的 Chrome/Chromium、中文字体；发送 X 的 MP4 动图还需要 ffmpeg。投票保留原始选项、比例、票数和状态，选项随整帖翻译。

## 💿 安装

<details>
<summary>使用包管理器安装</summary>
在 nonebot2 项目的插件目录下, 打开命令行, 根据你使用的包管理器, 输入相应的安装命令

<details>
<summary>pip</summary>

    pip install nonebot-plugin-twitter
</details>
<details>
<summary>pdm</summary>

    pdm add nonebot-plugin-twitter
</details>
<details>
<summary>poetry</summary>

    poetry add nonebot-plugin-twitter
</details>
<details>
<summary>conda</summary>

    conda install nonebot-plugin-twitter
</details>

打开 nonebot2 项目根目录下的 `pyproject.toml` 文件, 在 `[tool.nonebot]` 部分追加写入

    plugins = ["nonebot_plugin_twitter"]

</details>

本仓库的 Git 版本可直接安装：

```sh
uv add "nonebot-plugin-twitter @ git+https://github.com/kusadact/nonebot-plugin-twitter-kusadact.git@master"
```

已使用该 Git 依赖的项目，更新时刷新该包的锁定提交，再同步环境：

```sh
uv lock --upgrade-package nonebot-plugin-twitter
uv sync --inexact
```

## ⚙️ 配置

 
在 nonebot2 项目的`.env`文件中添加下表中的必填配置

| 配置项 | 必填 | 默认值 | 说明 |
|:-----:|:----:|:----:|:----:|
| twitter_website | 否 | 无 | 自定义website |
| twitter_proxy | 否 | 无 | proxy |
| twitter_qq | 否 | 2854196310 | 合并消息头像来源 |
| command_priority | 否 | 10 | 命令优先级 |
| twitter_htmlmode | 否 | false | 网页截图模式 |
| twitter_template_enabled | 否 | false | 独立启用正式图片模板 |
| twitter_translation_enabled | 否 | false | 自动订阅翻译并启用正式模板 |
| twitter_translation_primary | 否 | 无 | 主力线路 JSON：url、key、model |
| twitter_translation_fallback | 否 | 无 | 保底线路 JSON：url、key、model |
| twitter_translation_timeout | 否 | 60 | 单次模型请求期限，秒，最大 300 |
| twitter_translation_max_chars | 否 | 30000 | 完整输入和 Prompt 的字符预算 |
| twitter_render_width | 否 | 640 | 正式图片宽度 |
| twitter_render_max_height | 否 | 8000 | 单张图片高度上限，超长内容分片 |
| twitter_original | 否 | false | 使用 X 官网截图 |
| twitter_html_timezone | 否 | Asia/Shanghai | Nitter 截图时间显示时区 |
| twitter_html_timezone_label | 否 | UTC+8 | Nitter 截图时间显示标签 |
| twitter_browser_channel | 否 | 无 | Playwright 浏览器通道，如 `chrome` |
| twitter_browser_executable_path | 否 | 无 | Playwright 浏览器可执行文件路径 |
| twitter_video_send_host_path | 否 | 无 | 视频发送时复制到宿主机可见目录 |
| twitter_video_send_container_path | 否 | 无 | 视频发送时在协议中使用的容器内路径 |
| twitter_video_mux_api | 否 | 无 | 远程视频转码接口 |
| twitter_video_mux_token | 否 | 无 | 远程视频转码接口鉴权 token |
| twitter_nitter_sync_status_file | 否 | 无 | Nitter Chrome 会话同步状态文件路径 |
| twitter_nitter_alert_qq | 否 | 0 | Nitter Chrome 登录态失效时接收告警的 QQ |
| twitter_no_text | 否 | false | 开启媒体过滤后彻底不输出文字 |
| twitter_node | 否 | true | 使用合并转发消息发送 |

配置格式示例
```bash
# twitter
twitter_website="http://127.0.0.1:18645"
twitter_proxy="http://127.0.0.1:1090"
twitter_qq=2854196306
command_priority=10

# 复用系统 Chrome
twitter_browser_channel="chrome"
# 或直接指定路径
# twitter_browser_executable_path="/usr/bin/google-chrome"

# 使用截图纯图模式示例
twitter_htmlmode=true
twitter_original=false
twitter_no_text=true
twitter_node=false

# Nitter 会话同步告警
twitter_nitter_sync_status_file="/opt/nitter/session_sync_status.json"
twitter_nitter_alert_qq=959472968
```

## 自建 Nitter

推荐将 `twitter_website` 指向你自己的 Nitter 实例，例如：

```bash
twitter_website="http://127.0.0.1:18645"
```

如果你在 Nitter 侧额外部署了 Chrome 会话自动续期脚本，并持续写出一个状态文件，那么插件可以读取这个文件，在检测到 `chrome_login_required=true` 时向指定 QQ 发送一次私聊告警。

这部分配置只负责“读状态并发告警”，不负责替你维护 Nitter 的 cookie。

## 🎉 使用
### 指令表
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


[] 为可选参数，

r18 : 开启r18推文推送

媒体 : 仅推送媒体消息

### 效果图
[![pCPuhWV.png](https://s1.ax1x.com/2023/06/05/pCPuhWV.png)](https://imgse.com/i/pCPuhWV)
[![pCPu4zT.png](https://s1.ax1x.com/2023/06/05/pCPu4zT.png)](https://imgse.com/i/pCPu4zT)
### 注意事项
1.推主id：
[![pCPMu36.png](https://s1.ax1x.com/2023/06/05/pCPMu36.png)](https://imgse.com/i/pCPMu36)

2.消息为合并转发发送，存在延迟和发送失败的可能

3.新的0.1.0版本为破坏性更新：代理配置格式更改，关注列表需重新关注。

4.已知bug，视频无法发送（可能为gocq bug）

5.链接识别发送方式与配置文件配置有关

6.推文列表暂时仅在 网页截图模式 开启时支持

### 更新记录
2026.10.13 0.3.0
1. 正式图片模板完整展示主帖、引用、回复和投票，使用 SVG 图标并按内容高度分片。
2. 主力/保底整帖翻译、校验反馈修正、持久重试和缓存，支持正常数字词翻译。
3. 抓取每次最多 10 秒、最多 5 次；抓取解析后并行翻译与素材下载。
4. 循环 GIF 附件与持久推送队列，失败仅补发未成功的目标和消息部分。
5. 独立本地预览网页提供当前链路、Gemini、DeepSeek 三模式。

2024.01.20 0.2.4
1. 优化代理设置
2. 添加链接识别功能
3. 添加查看时间线截图功能
   

2024.01.14 0.2.3
1. 修复内存溢出bug
2. 修复代理未完全生效bug


2024.01.06 0.2.2
1. 更新默认镜像站列表
2. 调整文字输出，不再会输出评论区文字
3. 调整合并转发消息内，图片的优先级（其实是上次更新内容，但忘写了）
4. 调整自动切换镜像站（非指定website的情况下）


2024.01.01 0.2.0
1. 增加截图模式
2. 增加无文字的媒体过滤
3. 增加非合并转发发送方式
4. 调整缓存删除方式为每天早上删除（没什么用，现在发不出视频）
5. 调整媒体图片输出不再会输出评论区他人的图片视频
6. 优化了日志输出
7. 还有什么有点忘了，一口气改到0点，祝大家2024新年快乐吧


2023.10.28 0.1.14
1. 更新可用站点列表


2023.09.16 0.1.13
1. 暂无更新，可在env配置文件中添加以下参数来解决不可用问题
```bash
# twitter
twitter_website="https://nitter.privacydev.net"
```

最近找工作忙，更新慢了请见谅


2023.07.28 0.1.13
1. 修复bug


2023.07.25

1. 优化推送消息发送方式
2. 修复bug

2023.07.20

1. 增加了仅媒体推送
2. 修复了该插件与若干问题


2023.06.27

1. 临时解决回复原推文时，无法推送全部推文的问题
