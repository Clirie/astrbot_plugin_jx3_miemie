"""Manage the local renderer, recover failures, and reuse template references."""
import asyncio
import contextlib
import hashlib
import os
import shutil
import signal
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger

CHROMIUM_CANDIDATES = ("/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/snap/bin/chromium")


class RenderServiceError(RuntimeError):
    """A renderer failure with a short user-facing explanation."""


class LocalT2I:
    def __init__(self, workdir: Path, server_template: Path, conf: Any = None, port: int = 8999):
        self.workdir = Path(workdir)
        self.server_template = Path(server_template)
        self.server_path = self.workdir / "t2i_server.py"
        self.log_path = self.workdir / "t2i.log"
        self.conf = conf or {}
        self.port = port
        self.proc = None
        self.chromium = ""
        self._lock = asyncio.Lock()
        self._session = None
        self._task = None
        self._closed = False
        self._ready = False
        self._health = {}
        self._original_strategy = None
        self._template_ids = OrderedDict()

    def endpoint(self):
        return f"http://127.0.0.1:{self.port}/text2img"

    async def session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=130))
        return self._session

    async def health(self, timeout=3.0):
        try:
            session = await self.session()
            async with session.get(f"http://127.0.0.1:{self.port}/health", timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                payload = await response.json()
                if response.status == 200 and payload.get("code") == 0:
                    return payload.get("data") or {}
        except (aiohttp.ClientError, TimeoutError, ValueError):
            pass
        return None

    async def healthy(self, timeout=3.0):
        return await self.health(timeout) is not None

    def find_chromium(self):
        for path in CHROMIUM_CANDIDATES:
            if Path(path).is_file() and os.access(path, os.X_OK):
                return path
        return shutil.which("chromium") or shutil.which("chromium-browser") or ""

    async def _run(self, *cmd, timeout=600, env=None):
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode, (out or b"").decode("utf-8", "ignore")[-500:]
        except TimeoutError:
            return -1, "超时"
        finally:
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()

    async def ensure_playwright(self):
        try:
            import playwright  # noqa: F401
            return True
        except ImportError:
            cmd = [sys.executable, "-m", "pip", "install", "playwright"]
            index = str(self.conf.get("pypi_index_url") or "").strip()
            if index:
                cmd += ["-i", index]
            code, _ = await self._run(*cmd, timeout=300)
            return code == 0

    async def ensure_browser(self):
        self.chromium = self.find_chromium()
        if self.chromium:
            return True
        env = dict(os.environ)
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(self.workdir / "browsers"))
        code, _ = await self._run(sys.executable, "-m", "playwright", "install", "chromium", timeout=900, env=env)
        return code == 0

    def write_server(self):
        self.workdir.mkdir(parents=True, exist_ok=True)
        temp = self.server_path.with_suffix(".py.tmp")
        shutil.copy2(self.server_template, temp)
        temp.replace(self.server_path)

    async def spawn(self):
        env = dict(os.environ)
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(self.workdir / "browsers"))
        env["T2I_PORT"] = str(self.port)
        if self.chromium:
            env["T2I_CHROMIUM"] = self.chromium
        with self.log_path.open("a") as log:
            self.proc = await asyncio.create_subprocess_exec(sys.executable, str(self.server_path), cwd=str(self.workdir), env=env, stdout=log, stderr=asyncio.subprocess.STDOUT, start_new_session=True)

    def _managed_pid(self):
        if self.proc is not None and self.proc.returncode is None:
            return self.proc.pid
        proc_root = Path("/proc")
        if proc_root.is_dir():
            for path in proc_root.iterdir():
                if not path.name.isdigit():
                    continue
                try:
                    if str(self.server_path).encode() in (path / "cmdline").read_bytes().split(b"\0"):
                        return int(path.name)
                except OSError:
                    continue
        return None

    async def _stop_managed(self):
        pid = self._managed_pid()
        if pid is None:
            return True
        # Drain active work before an upgrade/unload. Never signal an unknown
        # service just because it uses the configured port.
        deadline = time.monotonic() + 65
        while time.monotonic() < deadline:
            info = await self.health(timeout=1)
            if info is None or not info.get("inflight", 0):
                break
            await asyncio.sleep(.25)
        else:
            return False
        # Recheck process ownership after waiting, guarding against PID reuse.
        if self._managed_pid() != pid:
            return False
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGINT)
        for _ in range(40):
            if self._managed_pid() != pid:
                if self.proc is not None and self.proc.pid == pid:
                    await self.proc.wait()
                return True
            await asyncio.sleep(.25)
        return False

    async def ensure(self):
        async with self._lock:
            if self._closed:
                return False
            expected = hashlib.sha256(self.server_template.read_bytes()).hexdigest()
            info = await self.health()
            owned = self._managed_pid() is not None
            if info is not None and info.get("source_sha256") == expected and owned:
                self._health = info
                self._ready = True
                return True
            if info is not None and not owned:
                logger.warning("本地渲染端口由其他服务占用，保留原渲染策略")
                return False
            self._ready = False
            if not await self.ensure_playwright() or not await self.ensure_browser():
                return False
            if owned and not await self._stop_managed():
                return False
            self.write_server()
            await self.spawn()
            self._template_ids.clear()
            for _ in range(48):
                if self._closed:
                    return False
                info = await self.health(timeout=1)
                if info is not None and info.get("source_sha256") == expected:
                    self._health = info
                    self._ready = True
                    logger.info("本地渲染服务已就绪，版本 %s", info.get("version"))
                    return True
                if self.proc.returncode is not None:
                    break
                await asyncio.sleep(.5)
            return False

    def take_over(self):
        from astrbot.core import html_renderer
        strategy = getattr(html_renderer, "network_strategy", None)
        if strategy is None:
            return False
        if self._original_strategy is None:
            self._original_strategy = (strategy, strategy.BASE_RENDER_URL, list(strategy.endpoints))
        strategy.BASE_RENDER_URL = self.endpoint()
        strategy.endpoints = [self.endpoint()]
        return True

    def restore(self):
        if self._original_strategy is None:
            return
        strategy, url, endpoints = self._original_strategy
        if strategy.BASE_RENDER_URL == self.endpoint() and strategy.endpoints == [self.endpoint()]:
            strategy.BASE_RENDER_URL = url
            strategy.endpoints = list(endpoints)
        self._original_strategy = None

    async def bootstrap(self):
        if await self.ensure():
            return self.take_over()
        return False

    async def _supervise(self):
        delay = 15
        while not self._closed:
            try:
                if await self.bootstrap():
                    delay = 15
                else:
                    self._ready = False
                    self.restore()
                    delay = min(delay * 2, 60)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._ready = False
                self.restore()
                delay = min(delay * 2, 60)
                logger.warning("本地渲染恢复失败: %s", type(exc).__name__)
            await asyncio.sleep(delay)

    async def bootstrap_background(self):
        if self._task is None or self._task.done():
            self._closed = False
            self._task = asyncio.create_task(self._supervise(), name="jx3-render-supervisor")

    async def render_image(self, template, data, options):
        if not self._ready or "template_ref" not in self._health.get("capabilities", []):
            return None
        reference = self._template_ids.get(template)
        session = await self.session()
        for attempt in range(2):
            payload = {"tmpldata": data, "options": options, "json": True}
            if reference:
                payload["tmpl_id"] = reference
            else:
                payload["tmpl"] = template
            try:
                async with session.post(self.endpoint() + "/generate", json=payload) as response:
                    result = await response.json()
                    if response.status == 409 and reference:
                        reference = None
                        continue
                    if response.status != 200 or result.get("code") != 0:
                        messages = {503: "图片队列繁忙，请稍后重试。", 504: "图片资源加载超时，请稍后重试。"}
                        raise RenderServiceError(messages.get(response.status, "图片生成失败，请稍后重试。"))
                    reference = response.headers.get("X-T2I-Template-Id")
                name = str(result.get("data", {}).get("id", ""))
                if Path(name).name != name or Path(name).suffix not in {".png", ".jpg"}:
                    raise RuntimeError("渲染服务返回了无效图片路径")
                path = self.workdir / "output" / name
                if not path.is_file():
                    raise RuntimeError("渲染图片已过期，请重新查询")
                if reference:
                    self._template_ids[template] = reference
                    self._template_ids.move_to_end(template)
                    while len(self._template_ids) > 16:
                        self._template_ids.popitem(last=False)
                return str(path)
            except aiohttp.ClientConnectionError:
                self._ready = False
                if attempt == 0 and await self.ensure():
                    reference = None
                    continue
                self.restore()
                return None
        return None

    async def stop(self):
        self._closed = True
        self._ready = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        # If the previous strategy already used this service, keep the shared
        # process alive. Otherwise restore the original strategy before stopping.
        shared = self._original_strategy is not None and self._original_strategy[1] == self.endpoint()
        self.restore()
        try:
            if not shared:
                await self._stop_managed()
        finally:
            if self._session is not None:
                await self._session.close()
                self._session = None
            self._template_ids.clear()
