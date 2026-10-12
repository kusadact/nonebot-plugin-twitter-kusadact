"""Render escaped structured content using an independent, short-lived browser."""

import asyncio
import base64
from datetime import datetime
from hashlib import sha256
from html import escape
import json
import math
from pathlib import Path
import re
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from playwright.async_api import async_playwright

from .tweet_model import Poll, Post, Translation, TweetView
from .media_urls import image_candidates

TEMPLATE_VERSION = "11"
TEMPLATES = Path(__file__).with_name("templates")
ICONS = {
    "replies": "message-circle",
    "retweets": "repeat",
    "likes": "heart",
    "views": "chart-bar",
    "language": "language",
    "verified": "rosette-discount-check",
    "play": "player-play",
}


def icon(name: str) -> str:
    svg = (TEMPLATES / "icons" / f"{ICONS[name]}.svg").read_text("utf8")
    return svg.replace("<svg", '<svg class="icon" aria-hidden="true"', 1)


def display_time(text: str, timezone: str, label: str) -> str:
    normalized = text.replace(" · ", " ").strip()
    try:
        date = datetime.strptime(normalized, "%b %d, %Y %I:%M %p UTC").replace(tzinfo=ZoneInfo("UTC"))
        local = date.astimezone(ZoneInfo(timezone))
        return f"{local:%Y年%m月%d日 · %H:%M} {label}"
    except (ValueError, KeyError):
        return text


def image_html(url: str, assets: dict[str, str], alt: str = "图片") -> str:
    src = assets.get(url, "")
    if src:
        return f'<img src="{escape(src, quote=True)}" alt="{escape(alt, quote=True)}">'
    return '<div class="media-placeholder">' + escape(f"{alt} · 加载失败") + '</div>'


def text_html(post: Post) -> str:
    # Retain entity styles and original line breaks, without making static images interactive.
    paragraphs = [""]
    for part in post.parts:
        for index, text in enumerate(part.text.split("\n\n")):
            if index:
                paragraphs.append("")
            kind = part.kind if part.kind in {"url", "mention", "hashtag"} else "plain"
            paragraphs[-1] += f'<span class="{kind}">{escape(text)}</span>'
    return '<div class="body-text">' + "".join(f'<p data-split>{p}</p>' for p in paragraphs) + '</div>' if post.text else ""


def translation_label_html(trans: Translation) -> str:
    logo_path = TEMPLATES / f"{trans.provider}.svg"
    logo = icon("language")
    if trans.provider in {"gemini", "deepseek"} and logo_path.is_file():
        data = base64.b64encode(logo_path.read_bytes()).decode()
        logo = f'<img src="data:image/svg+xml;base64,{data}" alt="{trans.provider}">'
    label = f"翻译自{trans.source_language} · 简体中文 · {trans.model}"
    return f'<div class="translation-label">{logo}<span>{escape(label)}</span></div>'


def poll_html(poll: Poll, *, preceding_translation: Translation | None = None) -> str:
    choices = []
    translations = []
    for option in poll.options:
        trans = option.translation
        translated = ""
        if trans and trans.needs_translation and trans.text:
            translated = f'<div class="poll-translated">{escape(trans.text)}</div>'
            translations.append(trans)
        valid_percentage = type(option.percentage) is int and 0 <= option.percentage <= 100
        bar = f'<span class="poll-fill" style="width:{option.percentage}%"></span>' if valid_percentage else ""
        value = f'<span class="poll-percentage">{option.percentage}%</span>' if valid_percentage else ""
        cls = "poll-option leader" if option.leader else "poll-option"
        choices.append(f'<div class="{cls}">{bar}<div class="poll-label"><div class="poll-original">{escape(option.text)}</div>{translated}</div>{value}</div>')
    info = [f"{poll.votes} 票"] if poll.votes else []
    if poll.status == "Final results":
        info.append("已结束")
    elif poll.status:
        remaining = re.fullmatch(r"(\d+) (years?|months?|weeks?|days?|hours?|minutes?|seconds?)", poll.status)
        units = dict(year="年", month="个月", week="周", day="天", hour="小时", minute="分钟", second="秒")
        info.append(f"剩余 {remaining[1]} {units[remaining[2].rstrip('s')]}" if remaining else poll.status)
    label = ""
    if translations and (preceding_translation is None or any(
            (item.provider, item.model, item.source_language) !=
            (preceding_translation.provider, preceding_translation.model, preceding_translation.source_language)
            for item in translations)):
        label = translation_label_html(translations[0])
    footer = f'<div class="poll-info">{escape(" · ".join(info))}</div>' if info else ""
    return '<section class="poll" data-split aria-label="投票">' + label + "".join(choices) + footer + '</section>'


def build_html(view: TweetView, assets: dict[str, str] | None = None, *, timezone="Asia/Shanghai", label="UTC+8", retweet="") -> str:
    assets = assets or {}
    def render_post(post: Post, *, quote=False, reply=False):
        author = post.author
        avatar = (f'<img class="avatar" src="{escape(assets[author.avatar], quote=True)}" alt="">'
                  if author.avatar in assets else f'<div class="avatar initial">{escape((author.name or author.username or "?")[:1])}</div>')
        verified = f'<span class="verified" aria-label="已认证">{icon("verified")}</span>' if author.verified else ""
        handle = f'<span class="handle">@{escape(author.username)}</span>'
        name = f'<span class="display-name">{escape(author.name)}</span>{verified}'
        compact = quote or reply
        author_info = f'<div class="author-line">{name}{handle if compact else ""}</div>{handle if not compact else ""}'
        header = f'<div class="author">{avatar}<div class="author-info">{author_info}</div><span class="relative-time">{escape(post.relative_time)}</span></div>'
        targets = [u for u in post.replying_to if not reply or u.casefold() != view.main.author.username.casefold()]
        relation = '<div class="replying-to">回复 ' + escape(" ".join("@" + u for u in targets)) + '</div>' if targets else ""
        content = relation + text_html(post)
        trans = post.translation
        if trans and trans.needs_translation and trans.text and post.text:
            translated = "".join(f'<p data-split>{escape(p)}</p>' for p in trans.text.split("\n\n"))
            content += f'<section class="translation">{translation_label_html(trans)}<div class="body-text">{translated}</div></section>'
        if post.unavailable:
            content += f'<div class="unavailable" data-split>{escape(post.unavailable)}</div>'
        for link in post.links:
            thumb = image_html(link.image, assets) if link.image else ""
            content += f'<div class="link-card" data-split>{"<div class=media-frame>" + thumb + "</div>" if thumb else ""}<h2 class="link-title">{escape(link.title)}</h2><p class="link-description">{escape(link.description)}</p><div class="link-domain">{escape(link.domain)}</div><div class="link-url">{escape(link.url)}</div></div>'
        if post.media:
            media = []
            for item in post.media:
                url = item.display_url or item.url if item.kind == "image" else item.poster
                media_label = "GIF" if item.kind == "gif" else "视频"
                badge = f'<span class="video-badge">{icon("play")} {media_label}</span>' if item.kind != "image" else ""
                alt = item.alt or ("图片" if item.kind == "image" else f"{media_label}封面")
                media.append(f'<div class="media-frame" data-split>{image_html(url, assets, alt)}{badge}</div>')
            content += '<div class="post-media">' + "".join(media) + '</div>'
        if post.poll:
            preceding = trans if trans and trans.needs_translation and trans.text and post.text else None
            content += poll_html(post.poll, preceding_translation=preceding)
        if post.quote:
            content += render_post(post.quote, quote=True)
        timestamp = display_time(post.timestamp, timezone, label)
        footer = f'<div class="timestamp">{escape(timestamp)}</div>' if timestamp else ""
        stats = "".join(f'<span>{icon(kind)}{escape(value)}</span>' for kind, value in post.stats.items() if kind in ICONS)
        if stats:
            footer += '<div class="stats">' + stats + '</div>'
        cls = "quote-card" if quote else "reply" if reply else "main-post"
        return f'<section class="{cls}" data-post-id="{escape(post.id, quote=True)}">{header}<div class="post-content">{content}<div data-split>{footer}</div></div></section>'

    body = ('<div class="retweet">' + escape(retweet) + '</div>') if retweet else ""
    body += render_post(view.main)
    if view.replies:
        groups = []
        for post in view.replies:
            if groups and post.reply_group and groups[-1][0].reply_group == post.reply_group:
                groups[-1].append(post)
            else:
                groups.append([post])
        rendered = []
        for posts in groups:
            cls = "reply-group reply-chain" if len(posts) > 1 else "reply-group"
            rendered.append(f'<div class="{cls}">' + "".join(render_post(post, reply=True) for post in posts) + '</div>')
        body += '<section class="replies">' + "".join(rendered) + '</section>'
    css = (TEMPLATES / "tweet.css").read_text("utf8")
    return f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>{css}</style></head><body><article id="tweet-card" class="tweet-card">{body}</article></body></html>'


class TweetRenderer:
    def __init__(self, cache_dir: Path, launch_kwargs: dict | None = None, *, proxy=None,
                 width=640, max_height=8000, timezone="Asia/Shanghai", label="UTC+8", transport=None):
        self.cache_dir = Path(cache_dir)
        self.launch_kwargs = dict(launch_kwargs or {})
        self.proxy = proxy
        self.width = width
        self.max_height = max_height
        self.timezone, self.label = timezone, label
        self.transport = transport
        self.asset_semaphore = asyncio.Semaphore(6)

    async def prepare_assets(self, view: TweetView, *, progress=None) -> dict[str, str]:
        urls = set()
        for post in view.walk():
            urls.add(post.author.avatar)
            urls.update(link.image for link in post.links)
            urls.update((m.display_url or m.url) if m.kind == "image" else m.poster for m in post.media)
        asset_dir = self.cache_dir / "assets"
        asset_dir.mkdir(parents=True, exist_ok=True)
        assets = {}
        urls = {url for url in urls if url and urlparse(url).scheme in {"http", "https"}}
        counts = dict(total=len(urls), completed=0, loaded=0, failed=0)
        async def report():
            if progress:
                await progress(dict(counts))
        await report()
        async with httpx.AsyncClient(proxy=self.proxy, timeout=12, follow_redirects=True, transport=self.transport) as client:
            async def download(url):
                path = asset_dir / (sha256(url.encode()).hexdigest() + ".json")
                if path.is_file():
                    try:
                        assets[url] = json.loads(path.read_text())["data"]
                        return
                    except (OSError, ValueError, KeyError):
                        pass
                async with self.asset_semaphore:
                    for candidate in image_candidates(url):
                        try:
                            async with client.stream("GET", candidate) as response:
                                response.raise_for_status()
                                mime = response.headers.get("content-type", "").split(";")[0]
                                if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif", "image/avif"}:
                                    continue
                                data = bytearray()
                                async for chunk in response.aiter_bytes():
                                    data.extend(chunk)
                                    if len(data) > 20 * 1024 * 1024:
                                        break
                                if len(data) > 20 * 1024 * 1024:
                                    continue
                                src = "data:" + mime + ";base64," + base64.b64encode(data).decode()
                                path.write_text(json.dumps({"data": src}))
                                assets[url] = src
                                return
                        except (httpx.HTTPError, OSError):
                            pass  # Exhaustion renders a placeholder; text and replies remain intact.
            async def load(url):
                await download(url)
                counts["completed"] += 1
                counts["loaded" if url in assets else "failed"] += 1
                await report()
            tasks = [asyncio.create_task(load(url)) for url in urls]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        return assets

    async def render(self, view: TweetView, *, retweet="", assets=None) -> list[str]:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        params = dict(template=TEMPLATE_VERSION, width=self.width, max_height=self.max_height,
                      timezone=self.timezone, label=self.label, retweet=retweet, view=view.to_dict())
        key = sha256(json.dumps(params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        manifest = self.cache_dir / f"{key}.json"
        if manifest.is_file():
            paths = json.loads(manifest.read_text())["images"]
            if paths and all(Path(path).is_file() for path in paths):
                return paths
        if assets is None:
            assets = await self.prepare_assets(view)
        html = build_html(view, assets, timezone=self.timezone, label=self.label, retweet=retweet)
        (self.cache_dir / f"{key}.html").write_text(html, "utf8")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(**self.launch_kwargs)
            try:
                page = await browser.new_page(viewport={"width": self.width, "height": 800}, device_scale_factor=1)
                # Only embedded resources are needed, so source text cannot trigger external requests.
                await page.route("**/*", lambda route: route.abort())
                await page.set_content(html, wait_until="load", timeout=30000)
                await page.evaluate("async () => { await document.fonts.ready; await Promise.all(Array.from(document.images).map(img => img.complete ? Promise.resolve() : new Promise(resolve => { img.onload = img.onerror = resolve; setTimeout(resolve, 5000); }))); }")
                await page.evaluate("() => { for (const img of document.images) if (!img.naturalWidth) { const box = document.createElement('div'); box.className='media-placeholder'; box.textContent=img.alt ? img.alt + ' · 加载失败' : '图片加载失败'; img.replaceWith(box); } }")
                metrics = await page.evaluate("""() => {
                    const card = document.querySelector('#tweet-card');
                    const box = card.getBoundingClientRect();
                    const boundaries = Array.from(card.querySelectorAll('[data-split]')).map(n => n.getBoundingClientRect().bottom);
                    // Line boundaries are used only if one paragraph exceeds the image limit.
                    const lines = [];
                    for (const p of card.querySelectorAll('.body-text p, .link-description')) {
                        const r = p.getBoundingClientRect(), lh = parseFloat(getComputedStyle(p).lineHeight);
                        if (Number.isFinite(lh)) for (let y=r.top+lh; y<r.bottom; y+=lh) lines.push(y);
                    }
                    const media = Array.from(card.querySelectorAll('.media-frame img')).map(img => ({width:img.width,height:img.height,naturalWidth:img.naturalWidth,naturalHeight:img.naturalHeight}));
                    return {height: Math.ceil(box.height), width:box.width, overflow:card.scrollWidth>box.width, boundaries, lines, media};
                }""")
                if metrics["overflow"]:
                    raise ValueError("template_horizontal_overflow")
                # Media ratios can leave a fractional final pixel. Chromium clips to the
                # document extent, so round the canvas up before taking integer slices.
                await page.evaluate("height => { document.querySelector('#tweet-card').style.minHeight = height + 'px'; }", metrics["height"])
                paths = []
                start = 0
                while start < metrics["height"]:
                    limit = min(start + self.max_height, metrics["height"])
                    end = limit
                    if limit < metrics["height"]:
                        options = [math.floor(y) for y in metrics["boundaries"] if start + 80 < y <= limit]
                        if not options:
                            options = [math.floor(y) for y in metrics["lines"] if start + 80 < y <= limit]
                        if options:
                            end = max(options)
                    path = (self.cache_dir / f"{key}-{len(paths)}.png").resolve()
                    await page.screenshot(path=str(path), full_page=True, clip={"x": 0, "y": start, "width": self.width, "height": end - start}, timeout=30000)
                    paths.append(str(path))
                    start = end
                tmp = manifest.with_suffix(".tmp")
                tmp.write_text(json.dumps({"images": paths, "height": metrics["height"], "media": metrics["media"]}))
                tmp.replace(manifest)
                return paths
            finally:
                await browser.close()
