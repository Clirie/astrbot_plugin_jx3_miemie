"""豆包（火山方舟）图片生成客户端

支持：
    · 文生图  doubao-seedream-*        t2i
    · 图生图  doubao-seedream-4-0-*    t2i with `image` 字段（参考图）

接口：
    POST https://ark.cn-beijing.volces.com/api/v3/images/generations
    Authorization: Bearer <ARK_API_KEY>

插件配置项：
    doubao_api_key   方舟 API Key（必填）
    doubao_model     模型 ID，默认 doubao-seedream-4-0-250828
    doubao_size      图片尺寸，默认 1024x1024
    doubao_base_url  可选，默认 https://ark.cn-beijing.volces.com/api/v3
"""

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger

DEFAULT_BASE = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = "doubao-seedream-5-0-flash-260915"
DEFAULT_SIZE = "1024x1024"
TIMEOUT = 180


class ImageGenError(Exception):
    """图片生成失败（带用户可读原因）。"""


class DoubaoImageGen:
    def __init__(self, config: Any, save_dir: Path):
        self.conf = config
        self.save_dir = Path(save_dir)
        self.save_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 配置 ----------
    @property
    def api_key(self) -> str:
        return str(self.conf.get("doubao_api_key", "") or "").strip()

    @property
    def model(self) -> str:
        return str(self.conf.get("doubao_model", "") or "").strip() or DEFAULT_MODEL

    @property
    def size(self) -> str:
        return str(self.conf.get("doubao_size", "") or "").strip() or DEFAULT_SIZE

    @property
    def base(self) -> str:
        return (str(self.conf.get("doubao_base_url", "") or "").strip()
                or DEFAULT_BASE).rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    # ---------- 主入口 ----------
    async def generate(
        self,
        prompt: str,
        reference_image: bytes | None = None,
        size: str | None = None,
        model: str | None = None,
        negative_prompt: str | None = None,
    ) -> Path:
        """生成图片，返回本地文件路径。

        Args:
            prompt: 提示词
            reference_image: 图生图时的参考图字节（None = 文生图）
            size: 覆盖默认尺寸
            model: 覆盖默认模型
        """
        if not self.enabled:
            raise ImageGenError(
                "还没配置豆包 API Key\n"
                "请在插件配置里填写「豆包图片生成的 API Key」后再试"
            )
        prompt = (prompt or "").strip()
        if not prompt:
            raise ImageGenError("提示词不能为空")

        payload: dict[str, Any] = {
            "model": model or self.model,
            "prompt": prompt,
            "size": size or self.size,
            "response_format": "url",
            "watermark": False,
        }
        if negative_prompt:
            payload["negative_prompt"] = str(negative_prompt)
        if reference_image:
            payload["image"] = ("data:image/png;base64,"
                               + base64.b64encode(reference_image).decode())

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        url = f"{self.base}/images/generations"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=TIMEOUT)
            ) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    raw = await resp.text()
                    status = resp.status
        except asyncio.TimeoutError:
            raise ImageGenError(f"豆包接口超时（{TIMEOUT} 秒），稍后再试")
        except aiohttp.ClientError as exc:
            raise ImageGenError(f"连不上豆包接口：{exc}")

        try:
            body = json.loads(raw)
        except Exception:
            raise ImageGenError(f"豆包返回不是 JSON（HTTP {status}）：{raw[:150]}")

        if status != 200 or body.get("error"):
            err = body.get("error") or {}
            msg = err.get("message") or body.get("message") or raw[:150]
            code = err.get("code") or status
            # 常见错误给出可操作提示
            hint = ""
            if "Authentication" in str(code) or status == 401:
                hint = "（API Key 不对或过期，去火山方舟控制台重新复制）"
            elif "PermissionDenied" in str(code) or status == 403:
                hint = "（模型没开通，去方舟控制台开通该图像模型）"
            elif "RateLimit" in str(code) or status == 429:
                hint = "（请求太频繁，等一会儿再试）"
            elif "ModelNotFound" in str(code):
                hint = "（模型 ID 不存在，检查 doubao_model 配置）"
            raise ImageGenError(f"生成失败：{msg}{hint}")

        items = body.get("data") or []
        if not items:
            raise ImageGenError(f"豆包没返回图片：{raw[:150]}")

        first = items[0] or {}
        if first.get("b64_json"):
            data = base64.b64decode(first["b64_json"])
            return self._save(data, "png")

        img_url = first.get("url")
        if not img_url:
            raise ImageGenError(f"返回里没有图片地址：{raw[:150]}")
        return await self._download(img_url)

    async def _download(self, img_url: str) -> Path:
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            ) as session:
                async with session.get(img_url) as resp:
                    if resp.status != 200:
                        raise ImageGenError(f"下载生成图失败（HTTP {resp.status}）")
                    data = await resp.read()
        except ImageGenError:
            raise
        except Exception as exc:
            raise ImageGenError(f"下载生成图失败：{exc}")
        return self._save(data, "jpeg")

    def _save(self, data: bytes, ext: str) -> Path:
        name = f"aigen_{int(time.time() * 1000)}.{ext}"
        path = self.save_dir / name
        path.write_bytes(data)
        logger.info(f"豆包生图完成：{path}（{len(data)/1024:.0f} KB）")
        return path
