"""A resumable worker over pending_tweets, with one LLM request at a time."""

import asyncio
import fcntl
from hashlib import sha256
import json
import logging
from pathlib import Path
import time

from .delivery import acknowledge_tweet
from .llm_client import LLMError, Route
from .translation import PROMPT_VERSION, build_input, build_payload, validation_feedback
from .tweet_model import TweetView

logger = logging.getLogger(__name__)
RETRY_SECONDS = 60
ROUTE_LIMITS = {"primary": 10, "fallback": 3}


class TweetPipeline:
    def __init__(self, *, read_state, update_state, fetch, render, translator, prepare, deliver,
                 config, cache_dir: Path, clock=time.time, prepare_assets=None, manual_queue=None):
        self.read_state, self.update_state = read_state, update_state
        self.fetch, self.render, self.translator = fetch, render, translator
        self.prepare, self.deliver, self.config = prepare, deliver, config
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.prepare_assets = prepare_assets
        self.asset_tasks = {}
        self.manual_queue = manual_queue

    def is_manual(self, user):
        return self.manual_queue is not None and self.manual_queue.owns(user)

    def state_for(self, user):
        return self.manual_queue.read() if self.is_manual(user) else self.read_state()

    def update_for(self, user, mutator):
        update = self.manual_queue.update if self.is_manual(user) else self.update_state
        return update(mutator)

    def all_state(self):
        state = self.read_state()
        if self.manual_queue is not None:
            state.update(self.manual_queue.read())
        return state

    def enabled(self):
        return bool(getattr(self.config, "twitter_template_enabled", False) or
                    getattr(self.config, "twitter_translation_enabled", False))

    def task(self, user, signature):
        return next((entry for entry in self.state_for(user).get(user, {}).get("pending_tweets", [])
                     if entry["signature"] == signature), None)

    def save(self, user, entry, processing):
        def update(state):
            for item in state.get(user, {}).get("pending_tweets", []):
                if item["signature"] == entry["signature"]:
                    item["processing"] = dict(processing)
                    break
        self.update_for(user, update)

    def write_view(self, user, entry, view):
        serialized = json.dumps(view.to_dict(), ensure_ascii=False, sort_keys=True)
        key = sha256(f"{user}:{entry['signature']}:{serialized}".encode()).hexdigest()
        path = self.cache_dir / f"{key}-view.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(serialized, "utf8")
        tmp.replace(path)
        return str(path.resolve())

    def route(self, processing):
        for name in ("primary", "fallback"):
            route = Route.from_config(getattr(self.config, f"twitter_translation_{name}", None))
            if route and processing.get(f"{name}_attempts", 0) < ROUTE_LIMITS[name]:
                return name, route
        return None, None

    def shared_translation_path(self, view):
        routes = {}
        for name in ROUTE_LIMITS:
            route = Route.from_config(getattr(self.config, f"twitter_translation_{name}", None))
            routes[name] = dict(url=route.url, model=route.model) if route else None
        payload = dict(input=build_payload(view, getattr(self.config, "twitter_translation_max_chars", 30000)),
                       routes=routes, prompt=PROMPT_VERSION)
        key = sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return self.cache_dir / f"{key}-translation-state.json"

    def save_translation_state(self, path, state):
        fields = ("stage", "primary_attempts", "fallback_attempts", "request_inflight", "next_attempt_at",
                  "last_error", "provider", "model", "view_cache", "original_reason", "translation_feedback")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({name: state[name] for name in fields if name in state}), "utf8")
        tmp.replace(path)

    async def tick(self):
        if self.lock.locked():
            return
        async with self.lock:
            # Also prevent a second bot process from claiming the same persisted tasks.
            with (self.cache_dir / "worker.lock").open("a") as file:
                try:
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return
                try:
                    snapshot = self.all_state()
                    live_keys = {(user, entry["signature"]) for user, data in snapshot.items()
                                 for entry in data.get("pending_tweets", [])}
                    abandoned = [self.asset_tasks.pop(key) for key in list(self.asset_tasks) if key not in live_keys]
                    for task in abandoned:
                        task.cancel()
                    await asyncio.gather(*abandoned, return_exceptions=True)
                    for user, data in snapshot.items():
                        for entry in data.get("pending_tweets", []):
                            if not self.enabled() and "processing" not in entry and not self.is_manual(user):
                                continue
                            current = self.task(user, entry["signature"])
                            if current is None:
                                continue
                            processing = current.get("processing", {})
                            if processing.get("next_attempt_at", 0) > self.clock():
                                continue
                            try:
                                await self.step(user, current)
                            except Exception as error:
                                # No raw exceptions from HTTP or credentials reach persisted state/logs.
                                latest = self.task(user, entry["signature"])
                                if latest:
                                    state = latest.get("processing", {})
                                    state.update(next_attempt_at=self.clock() + RETRY_SECONDS,
                                                 last_error=type(error).__name__)
                                    self.save(user, latest, state)
                                logger.warning("推文 %s/%s 后台处理失败：%s", user, entry["tweet_id"], type(error).__name__)
                finally:
                    fcntl.flock(file, fcntl.LOCK_UN)

    async def step(self, user, entry):
        state = dict(entry.get("processing") or {"stage": "discovered", "primary_attempts": 0, "fallback_attempts": 0})
        state.pop("last_error", None)
        stage = state["stage"]
        if stage == "discovered":
            self.save(user, entry, state)
            view = await self.fetch(entry["source_user_name"], entry["tweet_id"])
            state.update(stage="fetched", view_cache=self.write_view(user, entry, view), input_digest=view.content_digest())
            self.save(user, entry, state)
            stage = "fetched"
        else:
            try:
                view = TweetView.from_dict(json.loads(Path(state["view_cache"]).read_text("utf8")))
            except (OSError, ValueError, KeyError):
                # Persisted source views are necessary for retrying with the same content/budget.
                # Do not silently refetch after delivery has started and change the artifact.
                if stage == "delivering":
                    view = None
                else:
                    raise ValueError("missing_view_cache") from None
        asset_key = (user, entry["signature"])
        if self.prepare_assets and stage in {"fetched", "translation_wait", "translated", "original_only"}:
            if asset_key not in self.asset_tasks:
                self.asset_tasks[asset_key] = asyncio.create_task(self.prepare_assets(view))
        if stage == "fetched":
            try:
                items = build_input(view, getattr(self.config, "twitter_translation_max_chars", 30000))
            except LLMError as error:
                items = []
                state["original_reason"] = str(error)
            translate = getattr(self.config, "twitter_translation_enabled", False) and bool(items)
            state.update(stage="translation_wait" if translate else "original_only", next_attempt_at=0)
            self.save(user, entry, state)
            stage = state["stage"]
        if stage in {"translation_wait", "translated"} and not getattr(self.config, "twitter_translation_enabled", False):
            state.update(stage="original_only", next_attempt_at=0)
            self.save(user, entry, state)
            stage = "original_only"
        if stage == "translation_wait":
            shared_path = self.shared_translation_path(view)
            if shared_path.is_file():
                shared = json.loads(shared_path.read_text())
                for name in ROUTE_LIMITS:
                    field = f"{name}_attempts"
                    state[field] = max(state.get(field, 0), shared.get(field, 0))
                if "translation_feedback" in shared:
                    state["translation_feedback"] = shared["translation_feedback"]
                if shared.get("stage") == "translated":
                    translated_view = TweetView.from_dict(json.loads(Path(shared["view_cache"]).read_text("utf8")))
                    translations = {item_id: target.translation for item_id, target in translated_view.translation_targets()}
                    for item_id, target in view.translation_targets():
                        target.translation = translations.get(item_id)
                    state.update(stage="translated", provider=shared["provider"], model=shared["model"],
                                 view_cache=self.write_view(user, entry, view), next_attempt_at=0)
                elif shared.get("stage") == "original_only":
                    state.update(stage="original_only", original_reason=shared.get("original_reason", "routes_exhausted"), next_attempt_at=0)
                elif shared.get("next_attempt_at", 0) > self.clock():
                    state["next_attempt_at"] = shared["next_attempt_at"]
                    self.save(user, entry, state)
                    return
            if state["stage"] != "translation_wait":
                pass
            elif not getattr(self.config, "twitter_translation_enabled", False):
                state["stage"] = "original_only"
            else:
                while True:
                    name, route = self.route(state)
                    if route is None:
                        state.update(stage="original_only", original_reason="routes_exhausted")
                        break
                    # Count before sending: interruption during the request consumes this attempt.
                    state[f"{name}_attempts"] = state.get(f"{name}_attempts", 0) + 1
                    state.update(request_inflight=True, next_attempt_at=self.clock() + self.translator.timeout + RETRY_SECONDS)
                    self.save(user, entry, state)
                    self.save_translation_state(shared_path, state)
                    started = self.clock()
                    try:
                        kwargs = dict(feedback=state["translation_feedback"]) if state.get("translation_feedback") else {}
                        await self.translator.translate(view, route, **kwargs)
                    except LLMError as error:
                        feedback = validation_feedback(error)
                        if feedback:
                            state["translation_feedback"] = feedback
                        state.update(request_inflight=False, last_error=str(error), next_attempt_at=self.clock() + RETRY_SECONDS)
                        logger.info("推文 %s/%s 翻译失败：%s %s 第 %s 次 %s 耗时 %.1fs", user, entry["tweet_id"], name, route.model,
                                    state[f"{name}_attempts"], str(error), self.clock() - started)
                        self.save(user, entry, state)
                        self.save_translation_state(shared_path, state)
                        if str(error) == "input_budget_exceeded":
                            state.update(stage="original_only", next_attempt_at=0, original_reason="input_budget_exceeded")
                            break
                        # Primary exhaustion switches immediately to the fallback's first attempt.
                        if state[f"{name}_attempts"] >= ROUTE_LIMITS[name]:
                            if self.route(state)[1] is None:
                                state.update(stage="original_only", next_attempt_at=0, original_reason="routes_exhausted")
                                break
                            continue
                        return
                    else:
                        state.update(stage="translated", request_inflight=False, next_attempt_at=0,
                                     translation_feedback=None,
                                     provider=route.provider, model=route.model,
                                     view_cache=self.write_view(user, entry, view))
                        logger.info("推文 %s/%s 翻译成功：%s %s 第 %s 次 耗时 %.1fs", user, entry["tweet_id"], name, route.model,
                                    state[f"{name}_attempts"], self.clock() - started)
                        break
            state["next_attempt_at"] = 0
            self.save(user, entry, state)
            if getattr(self.config, "twitter_translation_enabled", False):
                self.save_translation_state(shared_path, state)
            stage = state["stage"]
        if stage in {"translated", "original_only"}:
            if stage == "original_only":
                for _, target in view.translation_targets():
                    target.translation = None
            retweet = f"@{user} 转帖了 @{entry['source_user_name']}" if entry.get("is_retweet") else ""
            try:
                assets_task = self.asset_tasks.get(asset_key)
                if assets_task:
                    assets = await assets_task
                    paths = await self.render(view, retweet=retweet, assets=assets)
                else:
                    paths = await self.render(view, retweet=retweet)
            except Exception as error:
                state["render_attempts"] = state.get("render_attempts", 0) + 1
                state.update(last_error=type(error).__name__, next_attempt_at=self.clock() + RETRY_SECONDS)
                self.save(user, entry, state)
                if state["render_attempts"] < 2:
                    return
                paths = []
                state["render_fallback"] = True
            finally:
                self.asset_tasks.pop(asset_key, None)
            state.update(stage="rendered", next_attempt_at=0, images=paths)
            self.save(user, entry, state)
            stage = "rendered"
        if stage == "rendered":
            if not self.task(user, entry["signature"]):
                return
            info = view.legacy_fields()
            info["rendered_images"] = state.get("images", [])
            if entry.get("is_retweet"):
                info["text"] = [f"@{user} 转帖了 @{entry['source_user_name']}", *info["text"]]
            prepare = self.manual_queue.prepare if self.is_manual(user) else self.prepare
            prepared = await prepare(info, user, entry["tweet_id"], self.state_for(user))
            key = sha256(f"{user}:{entry['signature']}".encode()).hexdigest()
            path = self.cache_dir / f"{key}-delivery.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(prepared, ensure_ascii=False), "utf8")
            tmp.replace(path)
            state.update(stage="delivering", delivery_cache=str(path.resolve()), next_attempt_at=0)
            self.save(user, entry, state)
            stage = "delivering"
        if stage == "delivering":
            info = json.loads(Path(state["delivery_cache"]).read_text("utf8"))
            deliver = self.manual_queue.deliver if self.is_manual(user) else self.deliver
            success = await deliver(info, user, entry["tweet_id"], self.state_for(user))
            if success:
                if self.is_manual(user):
                    self.manual_queue.complete(user)
                else:
                    self.update_state(lambda latest: acknowledge_tweet(latest, user, entry))
            else:
                state["next_attempt_at"] = self.clock() + RETRY_SECONDS
                self.save(user, entry, state)

    async def run(self):
        try:
            while True:
                self.wake.clear()
                try:
                    await self.tick()
                except Exception as error:
                    logger.warning("Twitter 后台任务轮询失败：%s", type(error).__name__)
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
        finally:
            tasks = list(self.asset_tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.asset_tasks.clear()
