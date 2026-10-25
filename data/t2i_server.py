"""本地 T2I 渲染服务（自建，替代公共的 t2i.soulter.top）

协议与平台渲染策略对齐：
    POST /text2img/generate
        body: {"tmpl": str, "json": bool, "tmpldata": dict, "options": dict}
        json=False -> 直接返回图片字节
        json=True  -> 返回 {"code":0,"data":{"id": "xxx.jpg"}}，图片可通过 GET /text2img/{id} 获取

设计要点：
    1. 用 Jinja2 渲染模板（模板里含 {{ }} / {# #} 语法）
    2. 用 Playwright + Chromium 真实渲染，不做 5 秒硬超时
    3. 渲染失败时返回明确的错误，绝不把错误内容当成图片返回
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import os
import re
import ssl
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web
from jinja2 import Environment
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)

HOST = os.environ.get("T2I_HOST", "127.0.0.1")
PORT = int(os.environ.get("T2I_PORT", "8999"))
CONCURRENCY = max(1, int(os.environ.get("T2I_CONCURRENCY", "1")))
VERSION = "2026.09.27.1"
SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
MAX_PENDING = max(CONCURRENCY, int(os.environ.get("T2I_MAX_PENDING", "8")))
TEMPLATE_CACHE_BYTES = max(0, int(os.environ.get("T2I_TEMPLATE_CACHE_MB", "64"))) * 1024 * 1024
# 浏览器闲置多久后关闭以释放内存（宿主机内存紧张，这个很关键）
IDLE_SHUTDOWN_SECONDS = int(os.environ.get("T2I_IDLE_SHUTDOWN", "300"))
DEFAULT_WIDTH = int(os.environ.get("T2I_WIDTH", "1280"))
DEFAULT_HEIGHT = int(os.environ.get("T2I_HEIGHT", "900"))
# 直接用系统安装的 Chromium，避开 playwright 官方 CDN 的龟速下载
HTTPS_PORT = int(os.environ.get("T2I_HTTPS_PORT", "443"))
CERT_FILE = BASE_DIR / "t2i.crt"
KEY_FILE = BASE_DIR / "t2i.key"
# 直接用系统安装的 Chromium，避开 playwright 官方 CDN 的龟速下载
CHROMIUM_PATH = os.environ.get("T2I_CHROMIUM", "/usr/bin/chromium")
RENDER_TIMEOUT_MS = int(os.environ.get("T2I_RENDER_TIMEOUT_MS", "60000"))
RESOURCE_TIMEOUT_MS = int(os.environ.get("T2I_RESOURCE_TIMEOUT_MS", "10000"))
QUEUE_TIMEOUT_MS = int(os.environ.get("T2I_QUEUE_TIMEOUT_MS", "10000"))
OPTIONAL_IMAGE_TIMEOUT_MS = int(os.environ.get("T2I_OPTIONAL_IMAGE_TIMEOUT_MS", "1400"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("local-t2i")

# 模板本身已是 HTML，不能做自动转义
env = Environment(autoescape=False)

_semaphore = asyncio.Semaphore(CONCURRENCY)
_browser_lock = asyncio.Lock()
_browser = None
_playwright = None
_last_activity = time.monotonic()
_inflight = 0
_active_renders = 0
_template_lock = asyncio.Lock()

# Serve immutable template assets over loopback instead of serializing font
# bytes through Playwright/CDP for every new browser context. Query data and
# rendered HTML remain isolated per request and never enter this asset store.
_ASSET_ORIGIN = f"http://127.0.0.1:{PORT}/assets/"
_active_assets: dict[str, tuple[tuple[str, bytes], int]] = {}
_DATA_URI = re.compile(
    r"url\(\s*(?P<quote>['\"]?)(?P<uri>data:(?P<mime>(?:font|image|application)/[\w.+-]+);"
    r"base64,(?P<blob>[A-Za-z0-9+/=]{4096,}))(?P=quote)\s*\)"
)


@dataclass
class PreparedTemplate:
    template: object
    assets: dict[str, tuple[str, bytes]]
    weight: int
    reference: str = ""


class TemplateCache:
    def __init__(self, max_bytes: int = TEMPLATE_CACHE_BYTES, max_entries: int = 16):
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.entries = OrderedDict()
        self.bytes = 0

    def get_reference(self, reference):
        if not isinstance(reference, str) or not re.fullmatch(r"[0-9a-f]{64}", reference):
            return None
        key = bytes.fromhex(reference)
        cached = self.entries.get(key)
        if cached is not None:
            self.entries.move_to_end(key)
        return cached

    def prepare(self, source: str) -> tuple[PreparedTemplate, bool]:
        key = hashlib.sha256(source.encode("utf-8")).digest()
        cached = self.entries.get(key)
        if cached is not None:
            self.entries.move_to_end(key)
            return cached, True
        assets = {}

        def replace_asset(match):
            url = _ASSET_ORIGIN + hashlib.sha256(match["uri"].encode("ascii")).hexdigest()
            if url not in assets:
                try:
                    assets[url] = (match["mime"], base64.b64decode(match["blob"], validate=True))
                except (ValueError, binascii.Error):
                    return match[0]
            return "url('" + url + "')"

        compact = _DATA_URI.sub(replace_asset, source)
        # Count decoded resources and conservatively budget compiled source.
        weight = sum(len(blob) for _, blob in assets.values()) + 4 * len(compact.encode("utf-8")) + 4096
        prepared = PreparedTemplate(env.from_string(compact), assets, weight, key.hex())
        if weight <= self.max_bytes and self.max_entries > 0:
            while self.entries and (self.bytes + weight > self.max_bytes or len(self.entries) >= self.max_entries):
                _, old = self.entries.popitem(last=False)
                self.bytes -= old.weight
            self.entries[key] = prepared
            self.bytes += weight
        return prepared, False


_template_cache = TemplateCache()

_SCALE_BY_LEVEL = {"normal": 1.0, "high": 1.3, "ultra": 1.8}


async def get_browser():
    """复用同一个 Chromium 实例。"""
    global _browser, _playwright
    async with _browser_lock:
        if _browser is None or not _browser.is_connected():
            if _playwright is None:
                _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(
                executable_path=CHROMIUM_PATH,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--font-render-hinting=none",
                    "--renderer-process-limit=1",
                    "--js-flags=--max-old-space-size=128",
                    "--disable-extensions",
                    "--disable-background-networking",
                ],
            )
            logger.info("Chromium 已启动")
        return _browser


def _parse_options(options: dict | None) -> tuple[str, float, bool, int, int | None, int]:
    options = options or {}
    fmt = str(options.get("type") or "jpeg").lower()
    if fmt not in {"jpeg", "jpg", "png"}:
        fmt = "jpeg"

    scale = options.get("device_scale_factor")
    if scale is None:
        scale = _SCALE_BY_LEVEL.get(
            str(options.get("device_scale_factor_level") or "high").lower(),
            1.3,
        )
    try:
        scale = float(scale)
    except (TypeError, ValueError):
        scale = 1.0
    scale = min(max(scale, 1.0), 3.0)

    full_page = bool(options.get("full_page", True))
    try:
        width = int(options.get("width") or DEFAULT_WIDTH)
    except (TypeError, ValueError):
        width = DEFAULT_WIDTH
    width = min(max(width, 320), 4096)

    try:
        # 视口高度：内容比它矮时，截出来的图就会有空白，所以允许调用方指定
        height = int(options.get("height") or DEFAULT_HEIGHT)
    except (TypeError, ValueError):
        height = DEFAULT_HEIGHT
    height = min(max(height, 64), 8192)

    quality = None
    if fmt in {"jpeg", "jpg"}:
        try:
            quality = int(options.get("quality") or 95)
        except (TypeError, ValueError):
            quality = 95
        quality = min(max(quality, 1), 100)
    return fmt, scale, full_page, width, quality, height


async def render_html(html: str, options: dict | None, assets=None, timings=None) -> tuple[bytes, str]:
    """Render in a fresh context; static assets and compilation can be reused."""
    global _active_renders, _last_activity
    fmt, scale, full_page, width, quality, height = _parse_options(options)
    timings = timings if timings is not None else {}
    last = time.perf_counter()

    def mark(name):
        nonlocal last
        now = time.perf_counter()
        timings[name] = (now - last) * 1000
        last = now

    _active_renders += 1
    context = None
    for url, asset in (assets or {}).items():
        current = _active_assets.get(url)
        _active_assets[url] = (asset, (current[1] if current else 0) + 1)
    try:
        async with asyncio.timeout(RENDER_TIMEOUT_MS / 1000):
            browser = await get_browser()
            context = await browser.new_context(
                viewport={"width": width, "height": height}, device_scale_factor=scale,
            )
            page = await context.new_page()
            page.set_default_timeout(RENDER_TIMEOUT_MS)
            mark("browser")
            # The window load event also waits for third-party avatars. Wait for
            # the DOM first, then give optional images their own short deadline.
            await page.set_content(html, wait_until="domcontentloaded",
                                   timeout=min(RENDER_TIMEOUT_MS, RESOURCE_TIMEOUT_MS))
            mark("load")
            # Explicit readiness also covers decoded images and the final layout
            # after web-font loading. Charts may expose a Promise as __T2I_READY__.
            async with asyncio.timeout(RESOURCE_TIMEOUT_MS / 1000):
                fallback_count = await page.evaluate("""async (optionalTimeout) => {
                    let fallbacks = 0;
                    const images = Array.from(document.images, async image => {
                        // Full-page screenshots never scroll lazy images into view.
                        // Start their requests before waiting for decode readiness.
                        image.loading = 'eager';
                        if (!image.hasAttribute('data-render-optional')) {
                            await image.decode().catch(() => {});
                            return;
                        }
                        let timer;
                        const loaded = await Promise.race([
                            image.decode().then(() => true, () => false),
                            new Promise(resolve => {timer = setTimeout(() => resolve(false), optionalTimeout);})
                        ]);
                        clearTimeout(timer);
                        if (!loaded) {
                            fallbacks++;
                            const fallback = image.dataset.renderFallback;
                            // Changing the source abandons the delayed avatar.
                            image.removeAttribute('srcset');
                            if (fallback) {
                                image.src = fallback;
                                await image.decode().catch(() => {});
                                image.classList.add('avatar-fallback');
                            } else {
                                image.removeAttribute('src');
                                image.style.visibility = 'hidden';
                            }
                        }
                    });
                    await Promise.all([document.fonts.ready, ...images]);
                    return fallbacks;
                }""", OPTIONAL_IMAGE_TIMEOUT_MS)
                # Required CSS backgrounds and load handlers keep their original
                # readiness contract after delayed optional avatars are replaced.
                await page.wait_for_load_state("load", timeout=RESOURCE_TIMEOUT_MS)
                await page.evaluate("""async () => {
                    if (window.__T2I_READY__) await window.__T2I_READY__;
                    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
                }""")
            if fallback_count:
                logger.info("optional image fallback count=%s", fallback_count)
            # Keep the old settle interval for unrelated legacy templates, whose
            # scripts may start asynchronously. Ink templates are synchronous.
            if not assets:
                await page.wait_for_timeout(300)
            mark("ready")
            image = await page.screenshot(full_page=full_page,
                type="png" if fmt == "png" else "jpeg", quality=quality,
                animations="disabled", timeout=RENDER_TIMEOUT_MS)
            mark("screenshot")
            return image, ("image/png" if fmt == "png" else "image/jpeg")
    finally:
        try:
            if context is not None:
                await context.close()
        finally:
            _active_renders -= 1
            for url in assets or {}:
                asset, count = _active_assets[url]
                if count == 1:
                    del _active_assets[url]
                else:
                    _active_assets[url] = (asset, count - 1)
            _last_activity = time.monotonic()


async def handle_generate(request: web.Request) -> web.Response:
    global _inflight, _last_activity
    if _inflight >= MAX_PENDING:
        return web.json_response({"code": 1, "message": "render queue is full", "data": {}},
                                 status=503, headers={"Retry-After": "2"})
    _inflight += 1
    _last_activity = time.monotonic()
    start = time.perf_counter()
    timings = {}
    try:
        try:
            payload = await request.json()
        except web.HTTPRequestEntityTooLarge:
            raise
        except Exception:
            return web.json_response({"code": 1, "message": "invalid JSON", "data": {}}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"code": 1, "message": "JSON object required", "data": {}}, status=400)
        tmpl = payload.get("tmpl") or ""
        reference = payload.get("tmpl_id")
        data = payload.get("tmpldata") or {}
        options = payload.get("options") or {}
        want_json = bool(payload.get("json"))
        if not isinstance(tmpl, str) or not (tmpl.strip() or isinstance(reference, str)) or not isinstance(data, dict) or not isinstance(options, dict):
            return web.json_response({"code": 1, "message": "invalid template, tmpldata or options", "data": {}}, status=400)
        timings["request"] = (time.perf_counter() - start) * 1000
        queued = time.perf_counter()
        try:
            async with asyncio.timeout(QUEUE_TIMEOUT_MS / 1000):
                await _semaphore.acquire()
        except TimeoutError:
            return web.json_response({"code": 1, "message": "render queue timeout", "data": {}},
                                     status=503, headers={"Retry-After": "2"})
        try:
            timings["queue"] = (time.perf_counter() - queued) * 1000
            template_start = time.perf_counter()
            try:
                # Only compile static templates in the worker. Per-request data
                # is rendered anew and is never included in the template cache.
                async with _template_lock:
                    if tmpl.strip():
                        prepared, hit = await asyncio.to_thread(_template_cache.prepare, tmpl)
                    else:
                        prepared = _template_cache.get_reference(reference)
                        hit = prepared is not None
                        if prepared is None:
                            return web.json_response({"code": 2, "message": "template_missing", "data": {}}, status=409)
                html = prepared.template.render(**data)
            except Exception as exc:
                logger.warning("模板渲染失败: %s", type(exc).__name__)
                return web.json_response({"code": 1, "message": "template render failed", "data": {}}, status=400)
            timings["template"] = (time.perf_counter() - template_start) * 1000
            try:
                image, content_type = await render_html(html, options, prepared.assets, timings)
            except (TimeoutError, PlaywrightTimeoutError):
                logger.warning("图片资源或渲染超时 resource_budget=%sms render_budget=%sms",
                               RESOURCE_TIMEOUT_MS, RENDER_TIMEOUT_MS)
                return web.json_response({"code": 1, "message": "render timeout", "data": {}}, status=504)
            except Exception as exc:
                logger.error("图片渲染失败: %s", type(exc).__name__)
                return web.json_response({"code": 1, "message": "render failed: " + type(exc).__name__, "data": {}}, status=500)
        finally:
            _semaphore.release()
        timings["total"] = (time.perf_counter() - start) * 1000
        headers = {"Server-Timing": ", ".join(f"{key};dur={value:.1f}" for key, value in timings.items()),
                   "X-T2I-Version": VERSION, "X-T2I-Template-Id": prepared.reference}
        logger.info("render cache=%s bytes=%s %s", "hit" if hit else "miss", len(image),
                    " ".join(f"{key}={value:.0f}ms" for key, value in timings.items()))
        if want_json:
            name = f"{int(time.time())}_{uuid.uuid4().hex[:8]}." + ("png" if content_type == "image/png" else "jpg")
            await asyncio.to_thread((OUT_DIR / name).write_bytes, image)
            return web.json_response({"code": 0, "data": {"id": name}}, headers=headers)
        return web.Response(body=image, content_type=content_type, headers=headers)
    finally:
        _inflight -= 1
        _last_activity = time.monotonic()


async def handle_output(request: web.Request) -> web.Response:
    name = os.path.basename(request.match_info["name"])
    path = OUT_DIR / name
    if not path.is_file():
        raise web.HTTPNotFound()
    content_type = "image/png" if name.endswith(".png") else "image/jpeg"
    return web.Response(body=await asyncio.to_thread(path.read_bytes), content_type=content_type)


async def handle_asset(request: web.Request) -> web.Response:
    if request.remote not in {"127.0.0.1", "::1"}:
        raise web.HTTPForbidden()
    name = request.match_info['name']
    if not re.fullmatch(r'[0-9a-f]{64}', name):
        raise web.HTTPNotFound()
    url = _ASSET_ORIGIN + name
    active = _active_assets.get(url)
    asset = active[0] if active else None
    if asset is None:
        # Template preparation updates the LRU in a worker thread. Hold its
        # lock while looking through entries that have no active render lease.
        async with _template_lock:
            for prepared in reversed(_template_cache.entries.values()):
                asset = prepared.assets.get(url)
                if asset is not None:
                    break
    if asset is None:
        raise web.HTTPNotFound()
    mime, body = asset
    return web.Response(body=body, content_type=mime, headers={
        'Access-Control-Allow-Origin': '*',
        'Cache-Control': 'public, max-age=31536000, immutable',
        'X-Content-Type-Options': 'nosniff',
    })


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response(
        {"code": 0, "message": "ok", "data": {"browser": bool(_browser and _browser.is_connected()), "version": VERSION, "pid": os.getpid(), "source_sha256": SOURCE_SHA256, "capabilities": ["template_ref"], "inflight": _inflight, "active": _active_renders, "template_cache_entries": len(_template_cache.entries), "template_cache_bytes": _template_cache.bytes}}
    )


async def idle_monitor() -> None:
    """闲置一段时间后关掉浏览器，把内存还给宿主机。"""
    global _browser
    while True:
        await asyncio.sleep(60)
        if _browser is None:
            continue
        if time.monotonic() - _last_activity < IDLE_SHUTDOWN_SECONDS or _inflight or _active_renders:
            continue
        async with _browser_lock:
            if _browser is not None and not _inflight and not _active_renders and time.monotonic() - _last_activity >= IDLE_SHUTDOWN_SECONDS:
                try:
                    await _browser.close()
                except Exception as exc:
                    logger.warning('关闭浏览器失败: %s', exc)
                _browser = None
                logger.info('浏览器闲置超过 %s 秒，已关闭释放内存', IDLE_SHUTDOWN_SECONDS)


async def cleanup_loop() -> None:
    """清理 30 分钟前的临时出图文件。"""
    while True:
        await asyncio.sleep(300)
        deadline = time.time() - 1800
        for path in OUT_DIR.glob("*"):
            try:
                if path.is_file() and path.stat().st_mtime < deadline:
                    path.unlink()
            except OSError:
                pass


async def on_startup(app: web.Application) -> None:
    # 不预启浏览器：首次渲染时才拉起，空闲 5 分钟后自动关闭，
    # 这样宿主机能一直保有这几百 MB 内存。
    app["cleanup_task"] = asyncio.create_task(cleanup_loop())
    app["idle_task"] = asyncio.create_task(idle_monitor())
    logger.info("本地 T2I 服务就绪: http://%s:%s/text2img", HOST, PORT)


async def on_cleanup(app: web.Application) -> None:
    for key in ("cleanup_task", "idle_task"):
        task = app.get(key)
        if task:
            task.cancel()
    await asyncio.gather(*(app[key] for key in ("cleanup_task", "idle_task") if app.get(key)), return_exceptions=True)
    if _browser is not None:
        await _browser.close()
    if _playwright is not None:
        await _playwright.stop()


def build_app() -> web.Application:
    app = web.Application(client_max_size=32 * 1024 * 1024)
    app.router.add_post("/text2img/generate", handle_generate)
    app.router.add_get("/assets/{name}", handle_asset)
    app.router.add_get("/text2img/{name}", handle_output)
    app.router.add_get("/health", handle_health)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":

    async def _serve() -> None:
        app = build_app()
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, HOST, PORT).start()
        logger.info("HTTP  监听 http://%s:%s/text2img", HOST, PORT)

        if CERT_FILE.is_file() and KEY_FILE.is_file():
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
            try:
                await web.TCPSite(
                    runner, "0.0.0.0", HTTPS_PORT, ssl_context=context
                ).start()
                logger.info("HTTPS 监听 https://0.0.0.0:%s/text2img", HTTPS_PORT)
            except OSError as exc:
                logger.error("HTTPS 监听失败: %s", exc)

        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    asyncio.run(_serve())
