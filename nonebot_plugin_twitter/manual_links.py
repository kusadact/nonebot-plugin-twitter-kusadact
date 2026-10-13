"""Durable manual requests, processed by the subscription translation worker."""

from contextlib import contextmanager
import fcntl
from hashlib import sha256
import json
import logging
from pathlib import Path
from threading import RLock

from .tweet_model import TweetView

logger = logging.getLogger(__name__)
PREFIX = "manual:"


class ManualLinkQueue:
    def __init__(self, path: Path, *, prepare_delivery, send_part, prepare_photos=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.prepare_delivery = prepare_delivery
        self.send_part = send_part
        self.prepare_photos = prepare_photos
        self.lock = RLock()

    @staticmethod
    def owns(user):
        return user.startswith(PREFIX)

    @contextmanager
    def locked(self):
        with self.lock, self.path.with_suffix(".lock").open("a") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)

    def _read(self):
        if not self.path.exists():
            return {}
        # Do not discard requests if a state file is damaged.
        state = json.loads(self.path.read_text("utf8"))
        if not isinstance(state, dict):
            raise ValueError("invalid_manual_link_state")
        return state

    def read(self):
        with self.locked():
            return self._read()

    def update(self, mutator):
        with self.locked():
            state = self._read()
            result = mutator(state)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, ensure_ascii=False), "utf8")
            temporary.replace(self.path)
            return result

    def enqueue(self, *, source_user_name, tweet_id, target):
        # Duplicate handling of one incoming message must not enqueue another delivery.
        identity = dict(target=target, source_user_name=source_user_name.lower(), tweet_id=tweet_id)
        user = PREFIX + sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        entry = dict(signature=f"status:{tweet_id}", tweet_id=tweet_id,
                     source_user_name=source_user_name, is_retweet=False)
        self.update(lambda state: state.setdefault(user, dict(target=dict(target),
                    source_user_name=source_user_name, pending_tweets=[entry], sent_parts=[])))
        return user

    def complete(self, user):
        self.update(lambda state: state.pop(user, None))

    async def prepare(self, info, user, tweet_id, state):
        source = state[user]["source_user_name"]
        return await self.prepare_delivery(info, source, tweet_id, {source: dict(screen_name=source)})

    async def _upgrade_delivery(self, info, request, tweet_id):
        """Regroup old frozen parts, reusing their files and delivery receipts."""
        processing = request["pending_tweets"][0]["processing"]
        view = TweetView.from_dict(json.loads(Path(processing["view_cache"]).read_text("utf8")))
        parts = info["delivery_parts"]
        sent = set(request.get("sent_parts", []))
        # These are the indexes used before legacy_fields became main-post-only.
        old_photos = list(dict.fromkeys(media.url for post in view.main.walk()
                         for media in post.media if media.kind == "image" and media.url))
        old_gifs = list(dict.fromkeys(media.url for post in view.walk()
                       for media in post.media if media.kind == "gif" and media.url))
        old_videos = [media.url for post in view.main.walk()
                      for media in post.media if media.kind == "video" and media.url]
        source = view.legacy_fields()
        main_photos = source["pic_url_list"]
        main_gifs = source["gif_url_list"]
        main_videos = source["video_url_list"]
        by_name = {part["part"]: part for part in parts}
        pending = [part for part in parts if part["part"] not in sent
                   and (part["part"].startswith("image:") or part["part"] == "original")]
        photo_parts = []
        for url in main_photos:
            name = f"photo:{old_photos.index(url)}"
            if name in sent:
                continue
            if name in by_name:
                photo_parts.append(by_name[name])
            else:
                photo_parts.extend(await self.prepare_photos([url]))
        gif_parts = [by_name[f"gif:{old_gifs.index(url)}"] for url in main_gifs
                     if f"gif:{old_gifs.index(url)}" in by_name
                     and f"gif:{old_gifs.index(url)}" not in sent]
        missing_gifs = [url for url in main_gifs if f"gif:{old_gifs.index(url)}" not in by_name
                        and f"gif:{old_gifs.index(url)}" not in sent]
        video_parts = [part for part in parts if part["part"].startswith("video:")
                       and old_videos and old_videos[0] in main_videos]
        missing_videos = [url for url in main_videos if not old_videos or url != old_videos[0]
                          or not video_parts]
        extra = None
        if missing_gifs or missing_videos:
            # Prepare only absent main-post media; translation, rendering and cached
            # attachments are retained. Quote/reply media never enter this view.
            subset = TweetView.from_dict(view.to_dict())
            subset.main.parts, subset.main.links, subset.main.quote = [], [], None
            subset.replies = []
            subset.main.media = [media for media in subset.main.media
                                 if (media.kind == "gif" and media.url in missing_gifs)
                                 or (media.kind == "video" and media.url in missing_videos)]
            extra = await self.prepare_delivery(subset.legacy_fields(), request["source_user_name"], tweet_id, {})
            for part in extra["delivery_parts"]:
                if part["part"] == "main":
                    gif_parts.append(dict(message=[segment for segment in part["message"]
                                                  if segment["type"] == "image"]))
                elif part["part"].startswith("video:"):
                    video_parts.append(dict(part=f"video:{len(video_parts)}", mode="video", message=part["message"]))
        message = [segment for part in [*pending, *photo_parts, *gif_parts] for segment in part["message"]]
        urls = source["url_list"]
        # The old original-text fallback already delivered its URLs successfully.
        if urls and "original" not in sent:
            message.append(dict(type="text", data=dict(text="\n" + "\n".join(urls))))
        main = [dict(part="main", mode="reply", message=message)] if message and "main" not in sent else []
        info["delivery_parts"] = [*main, *video_parts]
        info["delivery_version"] = 2
        info["manual_delivery_version"] = 2
        if extra:
            info["pinned_files"] = list(dict.fromkeys([*info.get("pinned_files", []), *extra.get("pinned_files", [])]))
        path = Path(processing["delivery_cache"])
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(info, ensure_ascii=False), "utf8")
        temporary.replace(path)

    async def deliver(self, info, user, tweet_id, state):
        request = self.read().get(user)
        if request is None:
            return True
        if self.prepare_photos and info.get("manual_delivery_version", 0) < 2:
            if not any(part["part"] == "main" for part in info["delivery_parts"]):
                await self._upgrade_delivery(info, request, tweet_id)
        for part in info["delivery_parts"]:
            if part["part"] in request.get("sent_parts", []):
                continue
            try:
                # Also apply the new reply policy to already frozen older messages.
                if part["part"] in {"main", "original"} or part["part"].startswith("image:"):
                    mode = "reply"
                elif part["part"].startswith("video:"):
                    mode = "video"
                else:
                    mode = "direct"
                await self.send_part(request["target"], mode, part["message"])
            except Exception as error:
                logger.warning("手动推文 %s 投递失败：%s", tweet_id, type(error).__name__)
                return False
            def acknowledge(latest):
                if user in latest:
                    latest[user].setdefault("sent_parts", []).append(part["part"])
            self.update(acknowledge)
        return True
