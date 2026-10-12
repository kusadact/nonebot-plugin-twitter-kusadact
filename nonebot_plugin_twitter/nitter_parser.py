"""Extract Nitter's returned posts without importing source HTML into the template."""

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

from .tweet_model import Author, LinkPreview, Media, Poll, PollOption, Post, TextPart, TweetView


def safe_url(value: str, base: str) -> str:
    url = urljoin(base, value) if value else ""
    return url if urlparse(url).scheme in {"http", "https"} else ""


def owned(root: Tag, selector: str) -> list[Tag]:
    result = []
    for node in root.select(selector):
        parent = node.parent
        while isinstance(parent, Tag) and parent is not root:
            if {"quote", "timeline-item", "main-tweet"}.intersection(parent.get("class", [])):
                break
            parent = parent.parent
        if parent is root:
            result.append(node)
    return result


def first(root: Tag, selector: str) -> Tag | None:
    nodes = owned(root, selector)
    return nodes[0] if nodes else None


def node_text(node: Tag | None) -> str:
    return node.get_text("", strip=True) if node else ""


def parse_text(root: Tag | None, base: str) -> list[TextPart]:
    parts: list[TextPart] = []

    def visit(node):
        if isinstance(node, NavigableString):
            parts.append(TextPart(str(node)))
        elif isinstance(node, Tag):
            if node.name in {"script", "style"}:
                return
            if node.name == "br":
                parts.append(TextPart("\n"))
            elif node.name == "a":
                text = node.get_text()
                href = str(node.get("href", ""))
                if text.startswith("@") and re.fullmatch(r"@[\w]+", text):
                    parts.append(TextPart(text, "mention"))
                elif text.startswith("#"):
                    parts.append(TextPart(text, "hashtag"))
                else:
                    url = safe_url(href, base)
                    parts.append(TextPart(url or text, "url" if url else "text", url))
            else:
                block = node.name in {"p", "div", "blockquote"}
                if block and parts and not parts[-1].text.endswith("\n"):
                    parts.append(TextPart("\n"))
                for child in node.children:
                    visit(child)
                if block:
                    parts.append(TextPart("\n"))

    if root:
        for child in root.children:
            visit(child)
    return parts


def post_id(root: Tag, default: str) -> str:
    link = first(root, "a.tweet-link, a.quote-link, .tweet-date a, a.unavailable-box, a.unavailable-quote")
    match = re.search(r"/status/(\d+)", str(link.get("href", ""))) if link else None
    return match[1] if match else default


def parse_post(root: Tag, base: str, default_id: str, default_user: str = "") -> Post:
    username = node_text(first(root, "a.username")).lstrip("@") or root.get("data-username", default_user)
    avatar = first(root, ".tweet-avatar img, .tweet-name-row img")
    date = first(root, ".tweet-date a")
    published = first(root, ".tweet-published")
    unavailable = first(root, ".unavailable-box, .unavailable-quote")
    post = Post(
        id=post_id(root, default_id),
        author=Author(node_text(first(root, ".fullname")) or username, str(username),
                      safe_url(str(avatar.get("src", "")), base) if avatar else "",
                      bool(first(root, ".icon-ok, .icon-verified, .verified-icon"))),
        parts=parse_text(first(root, ".tweet-content, .quote-text"), base),
        timestamp=node_text(published) or (str(date.get("title", "")) if date else ""),
        relative_time=node_text(date),
        replying_to=[node_text(a).lstrip("@") for a in owned(root, ".replying-to a")],
        unavailable=node_text(unavailable),
    )
    poll = first(root, ".poll")
    if poll:
        options = []
        for meter in poll.select(".poll-meter"):
            label = meter.select_one(".poll-choice-option")
            if label is None:
                continue
            value = node_text(meter.select_one(".poll-choice-value"))
            match = re.fullmatch(r"(\d{1,3})\s*%", value)
            percentage = int(match[1]) if match and int(match[1]) <= 100 else None
            options.append(PollOption(node_text(label), percentage, "leader" in meter.get("class", [])))
        info = node_text(poll.select_one(".poll-info"))
        match = re.fullmatch(r"([\d,]+) votes(?:\s*•\s*(.+))?", info)
        if options:
            votes = match[1] if match else ""
            status = (match[2] or "") if match else info
            post.poll = Poll(options, votes, status)
    for stat in owned(root, ".tweet-stat"):
        for kind, key in [("comment", "replies"), ("retweet", "retweets"), ("heart", "likes"), ("views", "views")]:
            if stat.select_one(f".icon-{kind}"):
                post.stats[key] = stat.get_text(strip=True) or "0"
    for a in owned(root, "a.still-image"):
        img = a.find("img")
        def size(key):
            value = str(img.get(key, "")) if img else ""
            return int(value) if value.isdigit() else None
        post.media.append(Media("image", safe_url(str(a.get("href", "")), base),
                                safe_url(str(img.get("src", "")), base) if img else "",
                                alt=str(img.get("alt", "")) if img else "", width=size("width"), height=size("height")))
    for video in owned(root, "video"):
        source = video.find("source")
        url = str(video.get("data-url") or video.get("src") or (source.get("src", "") if source else ""))
        post.media.append(Media("gif" if "gif" in video.get("class", []) else "video",
                                safe_url(url, base), poster=safe_url(str(video.get("poster", "")), base)))
    # Disabled/unavailable videos still have a returned thumbnail, without a <video> tag.
    for gallery in owned(root, ".gallery-video, .media-gif"):
        if gallery.find("video"):
            continue
        img = gallery.find("img")
        if img:
            post.media.append(Media("video", poster=safe_url(str(img.get("src", "")), base)))
    for card in owned(root, ".card:not(.attachments), .article-card"):
        container = card.select_one("a.card-container, a.card-content-container")
        img = card.select_one(".card-image img")
        url = safe_url(str(container.get("href", "")), base) if container else ""
        post.links.append(LinkPreview(url, node_text(card.select_one(".card-title")),
                                      node_text(card.select_one(".card-description")),
                                      node_text(card.select_one(".card-destination")) or urlparse(url).netloc,
                                      safe_url(str(img.get("src", "")), base) if img else ""))
    quote = first(root, ".quote")
    if quote:
        post.quote = parse_post(quote, base, f"{post.id}:quote")
    return post


def parse_nitter(html: str, base: str, username: str, tweet_id: str) -> TweetView:
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one(".main-thread .main-tweet")
    if soup.select_one(".error-panel") or main is None:
        raise ValueError("invalid_nitter_tweet")
    # Some Nitter versions wrap the main timeline-item instead of applying main-tweet to it.
    content_root = main.select_one(".timeline-item") if not main.select_one(":scope > .tweet-body") else main
    main_post = parse_post(content_root or main, base, tweet_id, username)
    replies = []
    groups = {}
    seen = {main_post.id}
    for root in soup.select(".timeline-item"):
        if root is main or main in root.parents or root.select_one(".main-tweet"):
            continue
        if root.find_parent(class_="quote"):
            continue
        if "more-replies" in root.get("class", []):
            continue
        post = parse_post(root, base, f"{tweet_id}:reply:{len(replies)}")
        if post.id in seen:
            continue
        seen.add(post.id)
        replies.append(post)
        group = root.find_parent(class_="reply") or root.find_parent(class_="main-thread")
        if group is not None:
            groups.setdefault(id(group), []).append(post)
    for posts in groups.values():
        if len(posts) > 1:
            for post in posts:
                post.reply_group = posts[0].id
    return TweetView(main_post, replies, f"https://x.com/{username}/status/{tweet_id}",
                     bool(soup.select_one(".unavailable-box")))
