"""JSON-safe content shared by parsing, translation and rendering."""

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json


@dataclass
class Author:
    name: str = ""
    username: str = ""
    avatar: str = ""
    verified: bool = False


@dataclass
class TextPart:
    text: str
    kind: str = "text"
    url: str = ""


@dataclass
class Media:
    kind: str
    url: str = ""
    display_url: str = ""
    poster: str = ""
    alt: str = ""
    width: int | None = None
    height: int | None = None


@dataclass
class LinkPreview:
    url: str = ""
    title: str = ""
    description: str = ""
    domain: str = ""
    image: str = ""


@dataclass
class Translation:
    needs_translation: bool
    source_language: str
    text: str = ""
    provider: str = "other"
    model: str = ""


@dataclass
class PollOption:
    text: str
    percentage: int | None = None
    leader: bool = False
    translation: Translation | None = None


@dataclass
class Poll:
    options: list[PollOption] = field(default_factory=list)
    votes: str = ""
    status: str = ""


@dataclass
class Post:
    id: str
    author: Author = field(default_factory=Author)
    parts: list[TextPart] = field(default_factory=list)
    timestamp: str = ""
    relative_time: str = ""
    stats: dict[str, str] = field(default_factory=dict)
    replying_to: list[str] = field(default_factory=list)
    media: list[Media] = field(default_factory=list)
    links: list[LinkPreview] = field(default_factory=list)
    quote: "Post | None" = None
    translation: Translation | None = None
    unavailable: str = ""
    # Nitter's returned reply wrapper; this does not assert an exact parent post ID.
    reply_group: str = ""
    poll: Poll | None = None

    @property
    def text(self) -> str:
        return "".join(part.text for part in self.parts).strip()

    def walk(self):
        yield self
        if self.quote:
            yield from self.quote.walk()

    def translation_targets(self):
        yield self.id, self
        if self.poll:
            for index, option in enumerate(self.poll.options, 1):
                yield f"{self.id}:poll:{index}", option

    @property
    def has_translation(self) -> bool:
        return any(target.translation and target.translation.needs_translation
                   for _, target in self.translation_targets())

    @property
    def fallback_text(self) -> str:
        lines = [self.text] if self.text else []
        if self.poll:
            lines.append("[投票]")
            for option in self.poll.options:
                percentage = f" · {option.percentage}%" if option.percentage is not None else ""
                lines.append(option.text + percentage)
            if self.poll.votes:
                lines.append(f"{self.poll.votes} 票")
            if self.poll.status:
                lines.append("已结束" if self.poll.status == "Final results" else self.poll.status)
        return "\n".join(lines)

    @classmethod
    def from_dict(cls, data: dict) -> "Post":
        data = dict(data)
        data["author"] = Author(**data.get("author", {}))
        data["parts"] = [TextPart(**part) for part in data.get("parts", [])]
        data["media"] = [Media(**item) for item in data.get("media", [])]
        data["links"] = [LinkPreview(**item) for item in data.get("links", [])]
        data["quote"] = cls.from_dict(data["quote"]) if data.get("quote") else None
        data["translation"] = Translation(**data["translation"]) if data.get("translation") else None
        poll = data.get("poll")
        if poll:
            options = []
            for option in poll.get("options", []):
                option = dict(option)
                option["translation"] = Translation(**option["translation"]) if option.get("translation") else None
                options.append(PollOption(**option))
            data["poll"] = Poll(options=options, votes=poll.get("votes", ""), status=poll.get("status", ""))
        else:
            data["poll"] = None
        return cls(**data)


@dataclass
class TweetView:
    main: Post
    replies: list[Post] = field(default_factory=list)
    url: str = ""
    r18: bool = False

    def walk(self):
        yield from self.main.walk()
        for post in self.replies:
            yield from post.walk()

    def translation_targets(self):
        for post in self.walk():
            yield from post.translation_targets()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TweetView":
        return cls(main=Post.from_dict(data["main"]),
                   replies=[Post.from_dict(post) for post in data.get("replies", [])],
                   url=data.get("url", ""), r18=data.get("r18", False))

    def content_digest(self) -> str:
        data = self.to_dict()
        # Relative timestamps and translations do not change the source content.
        def strip(post):
            post.pop("translation", None)
            post.pop("relative_time", None)
            if post.get("poll"):
                for option in post["poll"]["options"]:
                    option.pop("translation", None)
            if post.get("quote"):
                strip(post["quote"])
        strip(data["main"])
        for post in data["replies"]:
            strip(post)
        return sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def legacy_fields(self) -> dict:
        # Replies belong in the rendered view; keep the existing main/quote media messages.
        posts = list(self.main.walk())
        images = list(dict.fromkeys(m.url for p in posts for m in p.media if m.kind == "image" and m.url))
        videos = [m for p in posts for m in p.media if m.kind == "video" and m.url]
        gifs = list(dict.fromkeys(m.url for p in self.walk() for m in p.media if m.kind == "gif" and m.url))
        texts = [self.main.fallback_text] if self.main.fallback_text else []
        quote = self.main.quote
        if quote and quote.fallback_text:
            texts.append(f"引用 @{quote.author.username}：\n{quote.fallback_text}")
        return dict(status=True, html=b"", text=texts, pic_url_list=images,
                    video_url=videos[0].url if videos else "", gif_url_list=gifs, r18=self.r18,
                    media=any(p.media for p in posts),
                    quote_text=quote.text if quote else "",
                    quote_user_name=quote.author.username if quote else "",
                    view=self.to_dict())
