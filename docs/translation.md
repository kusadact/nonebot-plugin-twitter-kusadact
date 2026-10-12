# 正式图片模板、翻译与投票


新模板保留主帖、引用以及当前 Nitter 页面返回的全部回复。原文在上，译文在下，正文链接使用完整 `href`。
主帖昵称和 `@id` 分两行，正文及媒体位于头部下方，与头像左侧对齐；回复保留右侧缩进，回复与引用使用紧凑头部。直接回复主帖时省略重复的“回复 @作者”，回复其他人的上下文保留。认证、统计、语言和播放标记使用本地 Tabler SVG 图标。
图片按原比例展示，GIF 与视频封面分别标注。主帖、引用及已返回回复中的 GIF 单独发送循环动图图片；X 的 MP4 动图通过本地 `ffmpeg` 转为 GIF（最高 640 像素宽、15 fps、保留时长和比例）。真实视频沿用视频消息链路。运行插件的主机需要安装 `ffmpeg`。
截图按内容实际高度生成；超过高度上限时优先在段落、引用或回复处拆分，超长单段使用行边界，所有片段均保存。

先设置 `twitter_template_enabled=true` 验证原文图片；再填写两条线路并开启 `twitter_translation_enabled=true`。
开启翻译会自动使用新模板，不受 `twitter_htmlmode`、`twitter_original` 或 `twitter_no_text` 控制。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `twitter_template_enabled` | `false` | 正式原文图片模板 |
| `twitter_translation_enabled` | `false` | 自动订阅翻译，并使用正式模板 |
| `twitter_translation_primary` | 未设置 | JSON 对象，仅 `url`、`key`、`model` |
| `twitter_translation_fallback` | 未设置 | 同上，主力耗尽后使用 |
| `twitter_translation_timeout` | `60` | 一次 HTTP 请求超时，秒 |
| `twitter_translation_max_chars` | `30000` | 整个结构化输入和 Prompt 的字符预算；超限使用完整原文 |
| `twitter_render_width` | `640` | 图片宽度，像素 |
| `twitter_render_max_height` | `8000` | 单张图片的高度上限，像素 |

环境变量配置格式见 [.env.example](.env.example)。模型文字直接使用成功线路的 `model`，Gemini、DeepSeek 显示各自 Logo，其他服务显示通用图标。
标准 `generativelanguage.googleapis.com` 地址使用 [Gemini 原生 generateContent 协议](https://ai.google.dev/api/generate-content)；
其 `/v1beta/openai` 地址及普通代理使用 [Chat Completions 兼容协议](https://developers.openai.com/api/reference/resources/chat)。
兼容接口可填写根地址、`/v1` 基址或完整 `/chat/completions` 地址；带自定义前缀的地址会在该前缀后追加 `/chat/completions`。
自建 Gemini 原生代理需要填写完整 `.../models/<model>:generateContent` 地址；实际路径中的模型取配置 `model`。
URL 不含鉴权参数，密钥通过 `key` 配置，并经 HTTP header 发送。

主帖、引用、文字回复和投票选项在一个请求里完成判断与翻译。空正文、纯表情、纯链接和纯数字跳过请求；中文交给同次请求判断，
模型判定无需翻译也视为成功。响应必须覆盖全部 ID，且保留 URL、用户名和数字，校验失败不会发布部分译文。
输入明确标注主帖、引用关系和会话内回复；纯媒体或纯链接帖仍可作为上下文，附带媒体类型和已有链接标题、摘要。
模型按实际语境理解词义，采用对应中文语境中通行的叫法：常用外文名称、术语、缩写和固定表达保留原文，有成熟中文说法的采用中文。
Prompt v7 沿用 v6 的翻译与校验反馈规则，补充投票选项及所属帖子的关系，不预设行业词表或统一文风。中文句子自然夹用外文表达也可判为无需翻译；原文阿拉伯数字与数字格式保留，周围单位及描述正常翻译。原文数字词可以正常译成阿拉伯数字，例如 `five-hour` 译成“5小时”；新增数字不会单独触发数字保护错误，原文已有数字仍须逐处保留。不推测媒体内容或具体回复父帖。完整 Prompt 位于 `nonebot_plugin_twitter/translation.py`，当前版本为 7。译文缓存同时区分 Prompt 版本与实际内容，新请求重新处理，已有测试图片仍保留原版本。

投票保留 Nitter 返回的选项、整数百分比、领先选项、总票数和结束状态或剩余时间。图片显示原文选项，并在开启翻译时在各选项下面显示译文；比例、票数和状态由程序保留，不交给模型改写。投票选项沿用整帖校验、错误反馈、缓存和重试预算。没有返回的比例或票数不补成零，原文文字回退也保留投票内容。

自动推送使用独立后台任务：轮询只发现并入队，主力最多 10 次、保底最多 3 次，首次立即请求，失败后等待 60 秒。
主力耗尽立即切换保底；两条都耗尽后推送原文。次数在请求前落盘，重启不会归零；同样的正文、上下文、Prompt 版本和线路配置共享预算与译文缓存。
JSON、ID、译文或受保护内容校验失败时，下一次请求带上 `validation_feedback`，包含错误类别、修正要求以及可定位的帖子 ID 和数字差异。修正仍使用完整正文与上下文，返回全部条目并重新校验，计入原有 10＋3 次预算。反馈随待处理队列及共享状态保存，重启或切换保底后仍可继续；超时和接口错误不会生成新的校验反馈。
等待任务不占用浏览器，不阻塞其他到期任务，LLM 并发为 1。后台渲染使用自己的浏览器，退出时关闭并保留恢复状态。

处理阶段保存在原来的 `pending_tweets[*].processing`，缓存位于 `data/twitter/cache/pipeline/`。
准备好的图片和消息部分固定保存，发送失败仅补发缺少回执的目标和部分，不再次调用模型或下载、转换媒体。
新模板失败时重试一次，再使用已有的原文文字/媒体能力；译文和 LLM 预算保留。
每日旧媒体清理跳过待发送任务引用的媒体。新流水线缓存暂时保留，避免重启或补发时缺少产物；长期缓存清理尚需后续按活动任务引用实现。

手动推文链接复用解析器与新模板，首版立即输出原文，不等待整套翻译重试。关闭翻译可以继续输出原文图片；
同时关闭模板开关回到旧路径。已开始投递的任务继续使用原缓存及回执。


## 独立本地测试网页


独立测试入口提供本地网页和 HTTP API，复用正式 Nitter 抓取、结构化解析、当前 Prompt、翻译缓存和 PNG 渲染器。
它使用自己的测试目录，不启动 NoneBot，不读取订阅队列，不发送 QQ 消息。

```sh
uv run --python 3.11 --with-requirements requirements-preview.txt \
  python tools/serve_tweet_preview.py \
  --nitter http://你的Nitter地址 \
  --llm-url https://你的模型服务/v1 --model 你的模型名
```

启动时在终端输入本次测试的 API Key（隐藏输入，仅保存在进程内存），然后打开 `http://127.0.0.1:8765/`。
要启用三种模式，复制 `preview-routes.example.json` 为 Git 忽略的 `.env.preview.json`，填入两条线路并设置权限 `chmod 600 .env.preview.json`，改用以下启动方式：

```sh
uv run --python 3.11 --with-requirements requirements-preview.txt \
  python tools/serve_tweet_preview.py \
  --nitter http://你的Nitter地址 --routes-file .env.preview.json
```

网页可选择当前链路、Gemini 单次、DeepSeek 单次。当前链路使用 `primary`，失败最多请求 10 次，再使用 `fallback` 最多 3 次；
成功即停止，同一线路失败间隔 60 秒，主模型耗尽时立即切换保底，全部失败则生成原文图片并明确标注状态。
单次模式按配置模型识别对应供应商，首次请求一次；校验失败时立即带上错误反馈再修正一次，最多共 2 次。连接、超时或接口错误直接显示原因；修正后仍失败也停止，不切换供应商或生成成功图片。
示例中 DeepSeek 使用官方兼容地址 `https://api.deepseek.com/v1` 与 `deepseek-flash`；Gemini 使用 `gemini-3.8-flash`，填写自己的服务地址。
密钥仅由后端读取，网页配置接口、任务状态及产物不包含密钥。
输入完整 `https://x.com/用户/status/推文ID` 或 `https://twitter.com/用户/status/推文ID` 链接，点击“生成图片”，
即可查看进度、最终 PNG、分片下载、原文数据、上下文关系和译文数据。分享链接的 query 与 fragment 会被忽略。
抓取并解析后，翻译和素材下载立即并行开始，排版截图等待两者完成，并复用下载结果。
网页显示素材完成数量、下载耗时和排版截图耗时；下载仍按现有并发与单请求超时处理，没有整批下载总时限。
Nitter 返回的同组回复保留头像竖线连接；独立回复之间保留分隔线，不根据相同作者或相邻顺序推断回复关系。
使用 `--original-only` 可只测试原文；`--browser /path/to/chrome` 指定浏览器，macOS 默认寻找本机 Chrome。
没有本机浏览器时可用 Playwright 安装 Chromium。`--out` 可指定独立目录，默认 `previews/generated/preview-server/`。
本地服务仅监听回环地址，`--port` 可更换端口。单次模型请求默认等待 60 秒；慢线路可用 `--llm-timeout 120` 调整，范围为 1–300 秒。

HTTP 接口文档位于 `http://127.0.0.1:8765/docs`：

```sh
curl http://127.0.0.1:8765/api/previews \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://x.com/xsser_w/status/2109492334429962632","translate":true,"mode":"deepseek"}'
```

POST 返回 `202`、任务 `id` 和 `status_url`。GET `status_url` 可查询 `queued`、`fetching`、`translating`、
`retry_wait`、`downloading_assets`、`rendering`、`completed` 或 `failed`；`rendering` 仅表示排版截图。
`asset_status`、`asset_completed`、`asset_total`、`asset_failed`、`asset_seconds` 与 `render_seconds` 提供下载和截图进度；完成时 `images` 是全部 PNG 的下载 URL，
`files` 提供原文、译文和 LLM 上下文 JSON。`/?job=<id>` 可重新打开已完成的结果。
`source_status`、`source_attempts`、`source_attempt_limit`、`source_timeout_seconds`、`source_started_at`、`source_seconds` 提供抓取阶段、预算和耗时。
每轮最多尝试 5 次，每次完整 HTTP 请求最多 10 秒（包含连接和正文读取）；成功立即结束，超时、连接中断或无等待指令的 502/503/504 间隔 2 秒重试，5 次均超时约 58 秒结束。
404、403、认证失败和限流不立即重试，503 的 `Retry-After` 也不忽略；网页分别提示连接、超时、认证与限流原因。这个抓取预算不改变翻译请求和素材下载的超时。
原文、翻译与图片使用当前正式代码，测试任务只保存自己的阶段和产物。`mode` 可为 `pipeline`（默认）、`gemini` 或 `deepseek`。
状态包含实际模型、`primary_attempts`、`fallback_attempts`、`translation_attempts` 与下次重试时间，耗尽状态为 `translation_status=original_only`。
翻译失败时，`last_translation_details` 标记被拒的帖子 ID 和原因：`empty_translation` 表示译文为空，`no_chinese_text` 表示标为需要翻译但译文不含汉字；受保护内容校验还指出 URL、用户名或数字中的哪些项目被改动。
`translation_failures` 和可下载的 `translation-errors.json` 保存本轮各次失败的模型、计数、错误码及上述定位信息，不保存原始模型响应、密钥或请求头。整批校验仍要求所有正文通过，未通过的结果不进入译文缓存。
`translation_feedback` 保存最近一次可修正的校验反馈，`feedback_applied` 标记本次请求是否携带反馈；成功后清空反馈。反馈与完整正文共同受输入字符预算限制；先减少反馈定位元数据，仍超限则使用完整原文，不截断帖子或持续消耗模型请求。
可修正配置后重新提交，或取消勾选翻译输出原文。正在处理的相同链接、翻译开关及模式复用同一任务，切换模式会创建独立任务；
已结束的任务再次提交会重新抓取页面，内容一致时复用正式译文和渲染缓存。
图片中 GIF/视频显示正式封面与标签；这个接口测试 PNG 产物，循环 GIF 附件和 QQ 视频投递仍由机器人发送链路处理。
