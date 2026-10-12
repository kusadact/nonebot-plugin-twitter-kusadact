"""Loopback preview API using the production parser, translator and PNG renderer."""

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil
import time
from typing import Literal
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
import httpx

from .llm_client import LLMError, Route
from .renderer import TweetRenderer
from .translation import PROMPT_VERSION, Translator, build_payload, validation_feedback
from .tweet_fetcher import SourceError, fetch_nitter_view, parse_tweet_url
from .tweet_model import TweetView

PAGE = Path(__file__).with_name("templates") / "preview.html"
ROUTE_LIMITS = {"primary": 10, "fallback": 3}
MODE_LABELS = {"pipeline": "当前链路 · 主模型 10 次＋保底 3 次", "gemini": "Gemini · 单次＋一次修正", "deepseek": "DeepSeek · 单次＋一次修正"}
SOURCE_ERRORS = {
    "source_rate_limited": "Nitter / X 上游正在限流，请等待恢复后再试。",
    "source_auth_unavailable": "Nitter 暂无可用认证会话，请检查会话恢复服务。",
}


@dataclass
class PreviewSettings:
    nitter_url: str
    cache_dir: Path
    route: Route | None = field(default=None, repr=False)
    launch_kwargs: dict = field(default_factory=dict)
    proxy: str | None = None
    width: int = 640
    max_height: int = 8000
    translation_timeout: float = 60
    max_chars: int = 30000
    fallback_route: Route | None = field(default=None, repr=False)
    retry_seconds: float = 60

    def routes_for(self, mode):
        routes = {"primary": self.route, "fallback": self.fallback_route}
        if mode == "pipeline":
            return [(name, route, ROUTE_LIMITS[name]) for name, route in routes.items() if route]
        return [(name, route, 2) for name, route in routes.items() if route and route.provider == mode][:1]

    def public_modes(self):
        return [dict(id=mode, label=label, available=bool(self.routes_for(mode)),
                     routes=[dict(name=name, model=route.model, attempts=limit) for name, route, limit in self.routes_for(mode)])
                for mode, label in MODE_LABELS.items()]


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=1024)
    translate: bool = True
    mode: Literal["pipeline", "gemini", "deepseek"] = "pipeline"


class PreviewError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(code)


def write_json(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf8")
    tmp.replace(path)


class PreviewService:
    def __init__(self, settings, *, fetch=None, translator=None, renderer=None):
        self.settings = settings
        self.root = Path(settings.cache_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs_root = self.root / "jobs"
        self.jobs_root.mkdir(exist_ok=True)
        self.fetch = fetch or fetch_nitter_view
        self.translator = translator or Translator(self.root / "translations",
            timeout=settings.translation_timeout, max_chars=settings.max_chars, proxy=settings.proxy)
        self.renderer = renderer or TweetRenderer(self.root / "rendering", settings.launch_kwargs,
            proxy=settings.proxy, width=settings.width, max_height=settings.max_height)
        self.jobs = {}
        self.queue = asyncio.Queue(maxsize=8)
        self.worker_task = None
        for path in self.jobs_root.glob("*/job.json"):
            try:
                job = json.loads(path.read_text("utf8"))
                if job["id"] != path.parent.name:
                    continue
                if job["status"] not in {"completed", "failed"}:
                    job.update(status="failed", error=dict(code="interrupted", message="服务已重启，请重新提交链接。"))
                    write_json(path, job)
                self.jobs[job["id"]] = job
            except (OSError, ValueError, KeyError):
                continue

    def save(self, job, **changes):
        job.update(changes, updated_at=time.time())
        write_json(self.jobs_root / job["id"] / "job.json", job)

    def submit(self, request):
        location = parse_tweet_url(request.url)
        routes = self.settings.routes_for(request.mode) if request.translate else []
        if request.translate and not routes:
            raise PreviewError("translation_unconfigured", "所选模式尚未配置翻译线路，可选择其他模式或输出原文。")
        for job in self.jobs.values():
            if (job["url"] == location.url and job["translate"] == request.translate
                    and (not request.translate or job.get("mode", "pipeline") == request.mode)
                    and job["status"] not in {"completed", "failed"}):
                return job
        if self.queue.full():
            raise PreviewError("queue_full", "等待中的测试较多，请稍后再试。")
        job_id = uuid4().hex
        folder = self.jobs_root / job_id
        folder.mkdir()
        job = dict(id=job_id, url=location.url, username=location.username, tweet_id=location.tweet_id,
                   translate=request.translate, status="queued", created_at=time.time(),
                   prompt_version=PROMPT_VERSION, mode=request.mode,
                   model=routes[0][1].model if routes else None,
                   routes=[dict(name=name, model=route.model, attempts=limit) for name, route, limit in routes],
                   translation_attempts=0, primary_attempts=0, fallback_attempts=0,
                   asset_status="pending", asset_total=0, asset_completed=0,
                   images=[], files=[], error=None)
        self.jobs[job_id] = job
        self.save(job)
        self.queue.put_nowait(job_id)
        return job

    async def start(self):
        self.worker_task = asyncio.create_task(self.worker())

    async def stop(self):
        if self.worker_task:
            self.worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.worker_task

    async def worker(self):
        while True:
            job = self.jobs[await self.queue.get()]
            try:
                await self.run(job)
            except asyncio.CancelledError:
                self.save(job, status="failed", error=dict(code="interrupted", message="测试已中断，请重新提交链接。"))
                raise
            except PreviewError as error:
                self.save(job, status="failed", error=dict(code=error.code, message=error.message))
            except SourceError as error:
                code = str(error)
                self.save(job, status="failed", error=dict(code=code, message=SOURCE_ERRORS[code]))
            except httpx.TimeoutException:
                self.save(job, status="failed", error=dict(code="source_timeout", message="抓取推文超时，已达到本轮尝试上限，请稍后再试。"))
            except httpx.HTTPStatusError as error:
                code = error.response.status_code
                self.save(job, status="failed", error=dict(code=f"source_http_{code}", message=f"抓取服务返回 HTTP {code}。"))
            except httpx.RequestError:
                self.save(job, status="failed", error=dict(code="source_network_error", message="抓取连接失败，自动重试后仍未恢复，请检查 Nitter 连接。"))
            except Exception as error:
                # HTTP responses and exception bodies can contain credentials.
                code = "invalid_nitter_tweet" if str(error) == "invalid_nitter_tweet" else type(error).__name__
                self.save(job, status="failed", error=dict(code=code, message="测试失败，请检查抓取服务或浏览器配置。"))
            finally:
                self.queue.task_done()

    async def run(self, job):
        folder = self.jobs_root / job["id"]
        self.save(job, status="fetching", source_started_at=time.time())
        started = time.monotonic()
        async def progress(details):
            self.save(job, **details)
        try:
            view = await self.fetch(self.settings.nitter_url, job["username"], job["tweet_id"],
                                    proxy=self.settings.proxy, source_path=folder / "source.html", progress=progress)
        finally:
            self.save(job, source_seconds=round(time.monotonic() - started, 2))
        write_json(folder / "source.json", view.to_dict())
        self.save(job, files=["source.json"])
        assets_task = (asyncio.create_task(self.download_assets(job, view))
                       if hasattr(self.renderer, "prepare_assets") else None)
        try:
            await self.finish(job, view, folder, assets_task)
        finally:
            if assets_task:
                if not assets_task.done():
                    assets_task.cancel()
                    self.save(job, asset_status="cancelled")
                await asyncio.gather(assets_task, return_exceptions=True)

    async def download_assets(self, job, view):
        started = time.monotonic()
        self.save(job, asset_status="downloading", asset_started_at=time.time())
        async def progress(counts):
            self.save(job, **{f"asset_{key}": value for key, value in counts.items()})
        try:
            assets = await self.renderer.prepare_assets(view, progress=progress)
        except asyncio.CancelledError:
            self.save(job, asset_status="cancelled", asset_seconds=round(time.monotonic() - started, 2))
            raise
        except Exception:
            self.save(job, asset_status="failed", asset_seconds=round(time.monotonic() - started, 2))
            raise
        self.save(job, asset_status="ready", asset_seconds=round(time.monotonic() - started, 2))
        return assets

    async def finish(self, job, view, folder, assets_task):
        if job["translate"]:
            try:
                payload = build_payload(view, self.settings.max_chars)
            except LLMError as error:
                raise PreviewError(str(error), "正文及上下文超出预算，或来源 ID 冲突；可选择原文输出。") from None
            write_json(folder / "llm-input.json", payload)
            self.save(job, files=[*job["files"], "llm-input.json"])
            succeeded = await self.translate(job, view) if payload["items"] else True
            translation_status = ("original_only" if not succeeded else
                                  "translated" if any(p.has_translation for p in view.walk()) else "not_needed")
        else:
            translation_status = "disabled"
        write_json(folder / "translated.json", view.to_dict())
        translated_posts = sum(p.has_translation for p in view.walk())
        self.save(job, status="downloading_assets", translation_status=translation_status, translated_posts=translated_posts,
                  files=[*job["files"], "translated.json"])
        assets = await assets_task if assets_task else None
        self.save(job, status="rendering", render_started_at=time.time())
        started = time.monotonic()
        paths = await self.renderer.render(view, assets=assets) if assets_task else await self.renderer.render(view)
        if not paths:
            raise PreviewError("no_images", "渲染未返回图片。")
        names = []
        for index, source in enumerate(paths, 1):
            name = f"tweet-{index:02d}.png"
            shutil.copyfile(source, folder / name)
            names.append(name)
        self.save(job, status="completed", images=names, files=[*job["files"], *names],
                  render_seconds=round(time.monotonic() - started, 2))

    async def translate(self, job, view):
        for name, route, limit in self.settings.routes_for(job["mode"]):
            for attempt in range(1, limit + 1):
                self.save(job, status="translating", active_route=name, provider=route.provider,
                          model=route.model, translation_attempts=job["translation_attempts"] + 1,
                          next_attempt_at=None, feedback_applied=bool(job.get("translation_feedback")),
                          **{f"{name}_attempts": attempt})
                try:
                    kwargs = dict(feedback=job["translation_feedback"]) if job.get("translation_feedback") else {}
                    await self.translator.translate(view, route, **kwargs)
                    self.save(job, translation_feedback=None)
                    return True
                except LLMError as error:
                    code = str(error)
                    # Failed attempts must not leave partial translations in an original image.
                    for _, target in view.translation_targets():
                        target.translation = None
                    details = error.details
                    feedback = validation_feedback(error)
                    if feedback:
                        job["translation_feedback"] = feedback
                    failures = [*job.get("translation_failures", []), dict(attempt=job["translation_attempts"],
                        route=name, model=route.model, code=code, details=details)]
                    write_json(self.jobs_root / job["id"] / "translation-errors.json", failures)
                    files = list(dict.fromkeys([*job["files"], "translation-errors.json"]))
                    self.save(job, last_translation_error=code, last_translation_details=details,
                              translation_failures=failures, files=files)
                    if job["mode"] != "pipeline":
                        if attempt < limit and feedback:
                            continue  # One immediate correction on the selected model, inside the two-attempt limit.
                        label = "修正后仍未通过" if attempt > 1 else "单次翻译未通过"
                        raise PreviewError(code, f"{label}（{code}）；可重新提交或选择原文输出。") from None
                    if code == "input_budget_exceeded":
                        self.save(job, original_reason=code, next_attempt_at=None)
                        return False
                    if attempt < limit:
                        self.save(job, status="retry_wait", next_attempt_at=time.time() + self.settings.retry_seconds)
                        await asyncio.sleep(self.settings.retry_seconds)
            # As in the bot pipeline, switch to fallback immediately on primary exhaustion.
        self.save(job, original_reason="routes_exhausted", next_attempt_at=None)
        return False

    def public_job(self, job):
        result = dict(job)
        result["images"] = [f"/api/previews/{job['id']}/files/{name}" for name in job["images"]]
        result["files"] = {name: f"/api/previews/{job['id']}/files/{name}" for name in job["files"]}
        return result


def create_app(settings: PreviewSettings, *, fetch=None, translator=None, renderer=None):
    service = PreviewService(settings, fetch=fetch, translator=translator, renderer=renderer)

    @asynccontextmanager
    async def lifespan(app):
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(title="推文图片测试", lifespan=lifespan)
    app.state.preview_service = service

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and urlparse(origin).netloc != request.headers.get("host"):
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=403, content={"detail": "仅接受当前测试网页提交。"})
        return await call_next(request)

    @app.get("/")
    async def page():
        return FileResponse(PAGE, media_type="text/html")

    @app.get("/api/config")
    async def config():
        return dict(translation_available=bool(settings.route or settings.fallback_route),
                    model=settings.route.model if settings.route else None, prompt_version=PROMPT_VERSION,
                    modes=settings.public_modes(), retry_seconds=settings.retry_seconds)

    @app.post("/api/previews", status_code=202)
    async def submit(body: PreviewRequest):
        try:
            job = service.submit(body)
        except ValueError as error:
            if isinstance(error, PreviewError):
                raise HTTPException(429 if error.code == "queue_full" else 409, detail=error.message) from None
            raise HTTPException(422, detail="请输入完整的 https://x.com/用户/status/推文ID 链接。") from None
        result = service.public_job(job)
        result["status_url"] = f"/api/previews/{job['id']}"
        return result

    @app.get("/api/previews/{job_id}")
    async def status(job_id: str):
        job = service.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, detail="未找到测试任务。")
        return service.public_job(job)

    @app.get("/api/previews/{job_id}/files/{name}")
    async def artifact(job_id: str, name: str):
        job = service.jobs.get(job_id)
        if job is None or name not in job["files"]:
            raise HTTPException(404, detail="未找到测试产物。")
        path = service.jobs_root / job_id / name
        if not path.is_file():
            raise HTTPException(404, detail="测试产物已不存在，请重新提交链接。")
        return FileResponse(path, media_type="image/png" if name.endswith(".png") else "application/json")

    return app
