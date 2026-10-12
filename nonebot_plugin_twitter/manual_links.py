"""Durable manual requests, processed by the subscription translation worker."""

from contextlib import contextmanager
import fcntl
from hashlib import sha256
import json
import logging
from pathlib import Path
from threading import RLock

logger = logging.getLogger(__name__)
PREFIX = "manual:"


class ManualLinkQueue:
    def __init__(self, path: Path, *, prepare_delivery, send_part):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.prepare_delivery = prepare_delivery
        self.send_part = send_part
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
        for part in info["delivery_parts"]:
            if part["part"] in request.get("sent_parts", []):
                continue
            try:
                await self.send_part(request["target"], part["mode"], part["message"])
            except Exception as error:
                logger.warning("手动推文 %s 投递失败：%s", tweet_id, type(error).__name__)
                return False
            def acknowledge(latest):
                if user in latest:
                    latest[user].setdefault("sent_parts", []).append(part["part"])
            self.update(acknowledge)
        return True
