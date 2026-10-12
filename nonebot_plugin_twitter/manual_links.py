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

    async def deliver(self, info, user, tweet_id, state):
        request = self.read().get(user)
        if request is None:
            return True
        if self.prepare_photos and not info.get("manual_delivery_version"):
            # Older frozen deliveries omitted photos after a rendered preview. Add only
            # those missing attachments, retaining existing images, GIFs and receipts.
            if any(part["part"].startswith("image:") for part in info["delivery_parts"]):
                processing = request["pending_tweets"][0]["processing"]
                view = TweetView.from_dict(json.loads(Path(processing["view_cache"]).read_text("utf8")))
                photos = await self.prepare_photos(view.legacy_fields()["pic_url_list"])
                preview = [part for part in info["delivery_parts"] if part["part"].startswith("image:")]
                attachments = [part for part in info["delivery_parts"] if not part["part"].startswith("image:")]
                info["delivery_parts"] = [*preview, *photos, *attachments]
            info["manual_delivery_version"] = 1
            path = Path(request["pending_tweets"][0]["processing"]["delivery_cache"])
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(info, ensure_ascii=False), "utf8")
            temporary.replace(path)
        for part in info["delivery_parts"]:
            if part["part"] in request.get("sent_parts", []):
                continue
            try:
                # Also apply the new reply policy to already frozen older messages.
                mode = "reply" if part["part"].startswith("image:") or part["part"] == "original" else "direct"
                await self.send_part(request["target"], mode, part["message"])
            except Exception as error:
                logger.warning("手动推文 %s 投递失败：%s", tweet_id, type(error).__name__)
                return False
            def acknowledge(latest):
                if user in latest:
                    latest[user].setdefault("sent_parts", []).append(part["part"])
            self.update(acknowledge)
        return True
