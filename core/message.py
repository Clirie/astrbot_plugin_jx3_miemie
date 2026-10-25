import math
import pathlib
import hashlib
import asyncio
import re
import time
from functools import lru_cache
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

try:  # aiocqhttp 是可选依赖：QQ 官方机器人等平台并不需要它
    from aiocqhttp.exceptions import ActionFailed
except Exception:  # pragma: no cover - 仅在缺少 aiocqhttp 时生效

    class ActionFailed(Exception):
        """占位异常，保证非 aiocqhttp 平台也能正常加载本插件。"""

        retcode = None

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.core import html_renderer
from astrbot.core.utils.session_waiter import (
    SessionController,
    session_waiter,
)

from .bilei_data import BiLeidata
from .local_t2i import RenderServiceError
from .cache import CacheService
from .event_push import EventPushService
from .jx3api_data import JX3APIService
from .jx3box_data import JX3BOXService
from .team_manager import TeamManager
from .market_choices import ChoiceEvent, MarketChoices, canonical_name, normalize_query
from .market_card import market_card
from .market_buttons import MarketButtons
from .tarot import tarot_reading
from .fortune import LoveBook, fortune_reading, love_reading
from .horoscope import HoroscopeService, parse_sign
from .birth_chart import ziwei_reading
from .daily_tarot import daily_card, load_secret
from .template import load_template


def _is_image_component(component) -> bool:
    """不直接 import Image，避免对消息组件模块的硬依赖。"""
    return type(component).__name__ == "Image"


class MessageBuilder:
    """回复消息构建"""

    IMAGE_RENDER_HANDLERS = frozenset(
        {
            "helps", "hangqing", "richangyuche", "qiongyewei", "pifenghui", "yunchongshe",
            "chutianshe", "guanaishouling", "zhenyingevent", "yanhuachaxun",
            "zhanji", "mingjianpaihang", "mingjiantongji", "kuafumingjian",
            "wulinzhengba", "bukairongyu", "jianghulangke", "juedoutiaozhan",
            "banghuipaihang", "zhenyingpaihang", "qitapaihang", "shilianpaixing",
            "zhengyingpaimai", "dilujilu", "jinjia", "wujia", "chengbeng",
            "bangzhanjilu", "shapan", "zhueevent", "qiyuhuizong", "weizuoqiyu",
            "jinqiqiyu", "juesheqiyu", "qiyutongji", "qiyugonglue", "jingnai",
            "baizhan", "chengjiu", "zilipaixing", "jineng", "qixue", "liaotian",
            "xiaoyao", "huajia", "zhuangshi", "qiwu", "baishi", "shoutu",
            "tuanduizhaomu", "tuanzhang", "tuanpai", "zhuangtai", "fubeng",
            "diaoluo", "hong", "zili", "jiaoyihang", "bilei_all", "bilei_select",
            "horoscope", "tarot_daily",
        }
    )

    _RENDER_FORMATS = {"jpeg", "png"}
    _DATA_TIME_MARKER = "data-jx3-data-time"
    _DATA_TIME_ZONE = ZoneInfo("Asia/Shanghai")
    _SESSION_SCOPED_IMAGE_NAMES = frozenset({"避雷查看", "避雷查询"})
    _DEVICE_SCALE_FACTOR_LEVELS = {
        1.0: "normal",
        1.3: "high",
        1.8: "ultra",
    }

    # QQ 官方机器人 / 官方 webhook：一条消息只支持一张图片
    _SINGLE_IMAGE_PLATFORMS = frozenset({"qq_official", "qqofficial_webhook"})

    @classmethod
    def _adapt_chain_for_platform(cls, event, chain):
        """按平台能力裁剪消息链（QQ 官方一条消息只能带一张图）。"""
        if not isinstance(chain, list):
            return chain
        try:
            platform_name = event.get_platform_name()
        except Exception:
            return chain
        if platform_name not in cls._SINGLE_IMAGE_PLATFORMS:
            return chain

        image_count = sum(1 for item in chain if _is_image_component(item))
        if image_count <= 1:
            return chain

        from astrbot.api.message_components import Plain

        kept_image = False
        trimmed = []
        for item in chain:
            if _is_image_component(item):
                if kept_image:
                    continue
                kept_image = True
            trimmed.append(item)
        trimmed.append(
            Plain("\n（QQ 官方机器人一条消息只能带一张图，此处只保留了第一张）")
        )
        return trimmed

    def __init__(
        self,
        jx3api: JX3APIService,
        jx3box: JX3BOXService,
        bilei: BiLeidata,
        event_push: EventPushService,
        team_manager: TeamManager,
        icons: dict[str, dict[str, str]],
        render_config: dict[str, Any] | None = None,
        cache: CacheService | None = None,
        server_binding: Any = None,
    ):
        self.jx3api = jx3api
        self.jx3box = jx3box
        self.bilei = bilei
        self.event_push = event_push
        self.team_manager = team_manager
        self.icons = icons
        self.render_config = render_config if isinstance(render_config, dict) else {}
        self.cache = cache
        self.server_binding = server_binding
        self.local_t2i = None


    def _build_render_options(
        self,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """根据插件配置构造 HTML 截图参数。"""
        image_format = str(self.render_config.get("format", "jpeg")).lower()
        if image_format not in self._RENDER_FORMATS:
            image_format = "jpeg"

        try:
            device_scale_factor = float(
                self.render_config.get("device_scale_factor", 1.3)
            )
        except (TypeError, ValueError):
            device_scale_factor = 1.3
        scale_factor_level = self._DEVICE_SCALE_FACTOR_LEVELS.get(
            device_scale_factor,
            "high",
        )

        try:
            jpeg_quality = int(self.render_config.get("jpeg_quality", 100))
        except (TypeError, ValueError):
            jpeg_quality = 100
        jpeg_quality = max(1, min(100, jpeg_quality))

        options: dict[str, Any] = {
            "quality": jpeg_quality,
            "device_scale_factor_level": scale_factor_level,
            "scale": "device",
            "full_page": True,
            "omit_background": False,
            "type": image_format,
        }
        options.update(overrides or {})
        if options.get("type") == "png":
            options.pop("quality", None)
        return options


    async def html_render(
        self,
        tmpl: str,
        data: dict,
        return_url=True,
        options: dict | None = None,
    ) -> str:
        """渲染 HTML（带产物校验）"""
        if self.local_t2i is not None and not return_url:
            result = await self.local_t2i.render_image(tmpl, data, self._build_render_options(options))
            if result is not None:
                self._assert_real_image(result)
                return result
        result = await html_renderer.render_custom_template(
            tmpl,
            data,
            return_url=return_url,
            options=self._build_render_options(options),
        )
        # ★ 关键防护：渲染服务故障时会返回 JSON 文本而不是图片
        #   （例如容器内缺 chromium），必须在此拦下，
        #   否则会被当成图片发给用户 → 平台报"富媒体文件格式不支持"。
        if not return_url:
            self._assert_real_image(result)
        return result

    @staticmethod
    def _assert_real_image(buff) -> None:
        """确认渲染产物是真图片；否则抛出带原因的异常。"""
        head = b""
        try:
            if isinstance(buff, (bytes, bytearray)):
                head = bytes(buff[:16])
            elif hasattr(buff, "read"):
                pos = buff.tell() if hasattr(buff, "tell") else 0
                head = buff.read(16)
                if hasattr(buff, "seek"):
                    buff.seek(pos)
            else:
                with open(str(buff), "rb") as fh:
                    head = fh.read(16)
        except OSError as exc:
            raise RuntimeError(f"渲染产物无法读取：{exc}") from exc

        if head.startswith((b"\xff\xd8", b"\x89PNG", b"GIF8", b"RIFF", b"BM")):
            return

        detail = head.decode("utf-8", "ignore").strip() or "未知内容"
        try:
            if not isinstance(buff, (bytes, bytearray)) and not hasattr(buff, "read"):
                detail = pathlib.Path(str(buff)).read_text(
                    encoding="utf-8", errors="ignore")[:200].strip() or detail
        except Exception:
            pass
        raise RuntimeError(
            "渲染服务没有返回图片，返回的是：" + detail +
            "。通常是渲染服务所在环境缺少浏览器（chromium），"
            "请检查容器内是否安装了 chromium。"
        )

    @staticmethod
    def _format_data_time(result: dict[str, Any] | None = None) -> str:
        """优先使用接口缓存创建时间，否则使用本次数据生成时间。"""
        cache_metadata = (result or {}).get("_cache") or {}
        timestamp = cache_metadata.get("created_at")
        try:
            numeric_timestamp = float(timestamp)
            if numeric_timestamp > 10_000_000_000:
                numeric_timestamp /= 1000
            if numeric_timestamp > 0:
                return datetime.fromtimestamp(
                    numeric_timestamp,
                    tz=MessageBuilder._DATA_TIME_ZONE,
                ).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError, OverflowError):
            pass
        return datetime.now(MessageBuilder._DATA_TIME_ZONE).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    @classmethod
    def _ensure_data_time_footer(cls, template: str) -> str:
        """为不使用公共布局的动态 HTML 补上统一的数据时间区域。"""
        if cls._DATA_TIME_MARKER in template:
            return template
        footer = """
<style>
.jx3-data-time {
    box-sizing: border-box;
    width: 100%;
    margin-top: 18px;
    padding: 12px 24px 4px;
    border-top: 1px solid rgba(148, 163, 184, 0.35);
    color: #64748b;
    font: 500 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    letter-spacing: 0.02em;
    text-align: right;
}
</style>
<footer class="jx3-data-time" data-jx3-data-time>
    数据时间：<time>{{ data_time }}</time>
</footer>
"""
        body_end = template.lower().rfind("</body>")
        if body_end >= 0:
            return f"{template[:body_end]}{footer}{template[body_end:]}"
        return f"{template}{footer}"

    @staticmethod
    @lru_cache(maxsize=64)
    def _icon_groups(template):
        known = {"img", "sand", "sect", "serendipity"}
        groups = set(re.findall(r"icons\.(img|sand|sect|serendipity)\b", template))
        groups.update(re.findall(r"icons\[\s*['\"](img|sand|sect|serendipity)['\"]\s*\]", template))
        # Unknown/dynamic access must retain the complete mapping.
        remaining = re.sub(r"icons\.(?:img|sand|sect|serendipity)\b|icons\[\s*['\"](?:img|sand|sect|serendipity)['\"]\s*\]", "", template)
        return frozenset(known if re.search(r"\bicons\b", remaining) else groups)

    @classmethod
    def select_icons(cls, template, icons):
        groups = cls._icon_groups(template)
        return {name: values if name in groups else {} for name, values in icons.items()}

    async def _render_image_file(
        self,
        template: str,
        render_data: dict[str, Any],
        cache_name: str = "",
        render_options: dict[str, Any] | None = None,
        include_icons: bool = False,
        source_signature: str = "",
        variant_signature: str = "",
        cache_key_override: str = "",
        cache_lock_held: bool = False,
        message_text: str = "",
        data_time: str = "",
        disable_cache: bool = False,
    ) -> str:
        """命中时返回持久化图片，未命中时渲染一次并写入缓存。"""
        effective_cache_name = "" if disable_cache else cache_name or (
            self.cache.current_command() if self.cache else ""
        )
        options = self._build_render_options(render_options)
        cache_key = cache_key_override
        if self.cache and effective_cache_name:
            if not cache_key:
                cache_key = self.cache.build_image_key(
                    effective_cache_name,
                    template,
                    render_data,
                    options,
                    source_signature=source_signature,
                    variant_signature=(
                        variant_signature or self.cache.current_command_signature()
                    ),
                )
            cached_path = await self.cache.get_image(cache_key, effective_cache_name)
            if cached_path:
                return str(cached_path)

        async def render_and_save() -> str:
            payload = dict(render_data)
            payload["data_time"] = data_time or self._format_data_time()
            if include_icons:
                payload["icons"] = self.select_icons(template, self.icons)
            rendered_path = await self.html_render(
                self._ensure_data_time_footer(template),
                payload,
                return_url=False,
                options=options,
            )
            if self.cache and cache_key:
                saved_path = await self.cache.save_image(
                    cache_key,
                    effective_cache_name,
                    rendered_path,
                    str(options.get("type") or "jpeg"),
                    message_text=message_text,
                )
                if saved_path:
                    return str(saved_path)
            return rendered_path

        if not self.cache or not cache_key:
            return await render_and_save()
        if cache_lock_held:
            return await render_and_save()

        async with self.cache.image_lock(cache_key):
            cached_path = await self.cache.get_image(cache_key, effective_cache_name)
            if cached_path:
                return str(cached_path)
            return await render_and_save()

    def _image_request_identity(
        self,
        event: AstrMessageEvent,
        cache_name: str,
        render_options: dict[str, Any] | None,
        cache_variant: str,
    ) -> tuple[str, str]:
        if not self.cache:
            return cache_name, ""
        effective_cache_name = cache_name or self.cache.current_command()
        if (
            not effective_cache_name
            or self.cache.get_ttl("image", effective_cache_name) <= 0
        ):
            return effective_cache_name, ""
        variant_signature = cache_variant or self.cache.current_command_signature()
        scope_signature = ""
        if effective_cache_name in self._SESSION_SCOPED_IMAGE_NAMES:
            scope_signature = self.cache.value_signature(event.unified_msg_origin)
        return effective_cache_name, self.cache.build_image_request_key(
            effective_cache_name,
            self._build_render_options(render_options),
            variant_signature,
            scope_signature,
        )


    async def plain_msg(self, event: AstrMessageEvent, action):
        """最终将数据整理成文本发送"""
        data= await action()
        try:
            if data["code"] == 200:
                await event.send( event.plain_result(data["data"]))
            else:
                await event.send(event.plain_result(data["msg"])) 
        except Exception as e:
            logger.error(f"功能函数执行错误: {e}")
            await event.send(event.plain_result("猪脑过载，请稍后再试"))


    async def T2I_image_msg(
        self,
        event: AstrMessageEvent,
        action,
        render_options: dict | None = None,
        cache_name: str = "",
        cache_variant: str = "",
        disable_cache: bool = False,
    ):
        """最终将数据渲染成图片发送"""
        phase_times = {}
        started = time.perf_counter()
        effective_cache_name = cache_name
        phase = "cache"
        phase_start = started
        cache_hit = False
        try:
            if disable_cache:
                effective_cache_name, request_key = "", ""
            else:
                effective_cache_name, request_key = self._image_request_identity(
                    event,
                    cache_name,
                    render_options,
                    cache_variant,
                )
            data = None
            image_path = ""

            async def request_and_render(cache_lock_held: bool = False):
                nonlocal data, phase, phase_start
                phase = "query"
                phase_start = time.perf_counter()
                data = await action()
                phase_times[phase] = (time.perf_counter() - phase_start) * 1000
                if data["code"] != 200:
                    return ""
                phase = "render"
                phase_start = time.perf_counter()
                path = await self._render_image_file(
                    data["temp"],
                    data["data"],
                    cache_name=effective_cache_name,
                    render_options=render_options,
                    include_icons=True,
                    source_signature=str(
                        (data.get("_cache") or {}).get("data_hash") or ""
                    ),
                    variant_signature=cache_variant,
                    cache_key_override=request_key,
                    cache_lock_held=cache_lock_held,
                    data_time=self._format_data_time(data),
                    disable_cache=disable_cache,
                )
                phase_times[phase] = (time.perf_counter() - phase_start) * 1000
                return path

            if self.cache and request_key:
                async with self.cache.image_lock(request_key):
                    cached_path = await self.cache.get_image(
                        request_key,
                        effective_cache_name,
                    )
                    cache_hit = bool(cached_path)
                    image_path = (
                        str(cached_path)
                        if cached_path
                        else await request_and_render(cache_lock_held=True)
                    )
            else:
                image_path = await request_and_render()

            phase = "send"
            phase_start = time.perf_counter()
            if image_path:
                await event.send(event.image_result(image_path))
            else:
                await event.send(
                    event.plain_result(
                        (data or {}).get("msg") or "获取接口信息失败"
                    )
                )

        except RenderServiceError as e:
            phase_times.setdefault(phase, (time.perf_counter() - phase_start) * 1000)
            phase, phase_start = "send", time.perf_counter()
            logger.warning("图片指令未完成 command=%s reason=%s", effective_cache_name, e)
            await event.send(event.plain_result(str(e)))
        except ActionFailed as e:
            if e.retcode == 1200:
                logger.warning(
                    "图片消息发送回执超时，消息可能已经成功送达，"
                    "不再发送错误提示。"
                )
                return
            logger.error(f"功能函数执行错误: {e}")
            await event.send(event.plain_result("猪脑过载，请稍后再试"))
        except Exception as e:
            logger.error(f"功能函数执行错误: {e}")
            await event.send(event.plain_result("猪脑过载，请稍后再试"))
        finally:
            phase_times.setdefault(phase, (time.perf_counter() - phase_start) * 1000)
            logger.info("图片指令耗时 command=%s cache_hit=%s query_ms=%.0f render_ms=%.0f send_ms=%.0f total_ms=%.0f",
                        effective_cache_name or "query", cache_hit,
                        phase_times.get("query", 0), phase_times.get("render", 0), phase_times.get("send", 0),
                        (time.perf_counter() - started) * 1000)


    async def adaptive_msg(self, event: AstrMessageEvent, action):
        """Send a standard result as text or as a freshly rendered image."""
        try:
            data = await action()
            if data["code"] != 200:
                await event.send(event.plain_result(data["msg"]))
                return
            if not data.get("temp"):
                await event.send(event.plain_result(data["data"]))
                return
            image_path = await self._render_image_file(
                data["temp"],
                data["data"],
                include_icons=True,
                data_time=self._format_data_time(data),
                disable_cache=True,
            )
            await event.send(event.image_result(image_path))
        except ActionFailed as exc:
            if exc.retcode == 1200:
                logger.warning(
                    "图片消息发送回执超时，消息可能已经成功送达，"
                    "不再发送错误提示。"
                )
                return
            logger.error(f"功能函数执行错误: {exc}")
            await event.send(event.plain_result("猪脑过载，请稍后再试"))
        except Exception as exc:
            logger.error(f"功能函数执行错误: {exc}")
            await event.send(event.plain_result("猪脑过载，请稍后再试"))


    async def image_msg(self, event: AstrMessageEvent, action):
        """最终将数据整理成图片发送"""
        data = await action()
        try:
            if data["code"] == 200:
                await event.send(event.image_result(data["data"])) 
            else:
                await event.send(event.plain_result(data["msg"])) 

        except Exception as e:
            logger.error(f"功能函数执行错误: {e}")
            await event.send(event.plain_result("猪脑过载，请稍后再试")) 


    async def plain_chain(self, event: AstrMessageEvent, action):
        """富媒体消息"""
        data= await action()
        try:
            if data["code"] == 200:
                chain = self._adapt_chain_for_platform(
                    event, data["data"]
                )
                await event.send(event.chain_result(chain))
            else:
                await event.send(event.plain_result(data["msg"])) 
        except Exception as e:
            logger.error(f"功能函数执行错误: {e}")
            await event.send(event.plain_result("猪脑过载，请稍后再试")) 


    async def plain_image_msg(
        self,
        event: AstrMessageEvent,
        action,
        cache_name: str = "",
        cache_variant: str = "",
    ):
        """发送正文文本，并把可选 HTML 正文渲染为附图。"""
        try:
            effective_cache_name, request_key = self._image_request_identity(
                event,
                cache_name,
                None,
                cache_variant,
            )
            data = None
            image_path = ""
            message_text = ""

            async def request_and_render(cache_lock_held: bool = False):
                nonlocal data, message_text
                data = await action()
                if data.get("code") != 200:
                    return ""
                message_text = str(data.get("data") or "")
                if not data.get("temp"):
                    return ""
                return await self._render_image_file(
                    data["temp"],
                    {},
                    cache_name=effective_cache_name,
                    variant_signature=cache_variant,
                    cache_key_override=request_key,
                    cache_lock_held=cache_lock_held,
                    message_text=message_text,
                    data_time=self._format_data_time(data),
                )

            if self.cache and request_key:
                async with self.cache.image_lock(request_key):
                    cached_entry = await self.cache.get_image_entry(
                        request_key,
                        effective_cache_name,
                    )
                    if cached_entry:
                        image_path = str(cached_entry[0])
                        message_text = cached_entry[1]
                    else:
                        image_path = await request_and_render(cache_lock_held=True)
            else:
                image_path = await request_and_render()

            if data is not None and data.get("code") != 200:
                await event.send(
                    event.plain_result(data.get("msg") or "获取详细数据失败")
                )
                return

            chain = MessageChain()
            if message_text:
                chain.message(message_text)
            if image_path:
                chain.file_image(image_path)
            await event.send(chain)
        except Exception as e:
            logger.error(f"功能函数执行错误: {e}")
            await event.send(event.plain_result("猪脑过载，请稍后再试"))


    async def handler_plain_image_msg(self, event: AstrMessageEvent, action1, action2, result_handler=None, timeout: int = 15):
        """通用两轮会话：首轮数字键值对裁剪后交给次轮函数。"""
        try:
            option_map = await action1()
            if not isinstance(option_map, dict) or not option_map:
                await event.send(event.plain_result("未搜索到可选内容"))
                return

            options = {str(key): value for key, value in option_map.items()}
            if not all(key.isdigit() for key in options):
                await event.send(event.plain_result("选项序号格式异常"))
                return

            menu_lines = [f"请选择序号（{timeout}后自动选择第一条）"]
            menu_lines.extend(
                f"{key}、{label}"
                for key, label in options.items()
            )
            await event.send(event.plain_result("\n".join(menu_lines)))
            user_id = event.get_sender_id()
            send_result = result_handler or self.T2I_image_msg
            cache_name = self.cache.current_command() if self.cache else ""
            cache_variant = (
                self.cache.current_command_signature() if self.cache else ""
            )

            async def send_selected(
                target_event: AstrMessageEvent,
                selected: dict[str, Any],
            ):
                selected_variant = cache_variant
                if self.cache:
                    selected_variant = (
                        f"{cache_variant}:{self.cache.value_signature(selected)}"
                    )
                await send_result(
                    target_event,
                    lambda: action2(selected),
                    cache_name=cache_name,
                    cache_variant=selected_variant,
                )

            @session_waiter(timeout=timeout)
            async def option_select_waiter(
                controller: SessionController,
                new_event: AstrMessageEvent,
            ):
                if new_event.get_sender_id() != user_id:
                    return

                msg = new_event.get_message_str().strip()
                if not msg.isdigit():
                    await new_event.send(MessageChain().message("输入异常，结束会话"))
                    controller.stop()
                    return

                choice = int(msg)
                choice_key = str(choice)
                if choice_key not in options:
                    await new_event.send(MessageChain().message("无效序号，结束会话"))
                    controller.stop()
                    return

                # 已取得有效选择，先结束等待计时，再执行可能超过 timeout 的
                # API 请求与图片渲染，避免超时分支重复发送默认第一项。
                controller.stop()
                try:
                    await send_selected(
                        new_event,
                        {choice_key: options[choice_key]},
                    )
                except Exception as e:
                    logger.error(f"二轮查询执行错误: {e}")
                    await new_event.send(MessageChain().message("猪脑过载，请稍后再试"))

            try:
                await option_select_waiter(event)
            except TimeoutError:
                first_key = next(iter(options))
                await send_selected(
                    event,
                    {first_key: options[first_key]},
                )
            except Exception:
                logger.error("二轮选择发生异常", exc_info=True)

        except (ValueError, RuntimeError) as e:
            await event.send(event.plain_result(str(e) or "未搜索到可选内容"))
        except Exception as e:
            logger.error(f"二轮会话执行错误: {e}")
            await event.send(event.plain_result("猪脑过载，请稍后再试"))


    async def helps(self, event: AstrMessageEvent):
        """功能"""
        import json, shutil
        from astrbot.api.star import StarTools
        template=await load_template('helps.html');options=self._build_render_options({'width':1440})
        signature=hashlib.sha256((template+json.dumps(options,sort_keys=True)).encode()).hexdigest()
        directory=StarTools.get_data_dir('astrbot_plugin_jx3_miemie')/'static-help';directory.mkdir(parents=True,exist_ok=True)
        target=directory/(signature+'.'+('png' if options.get('type')=='png' else 'jpg'))
        if not hasattr(self,'_help_lock'):self._help_lock=asyncio.Lock()
        async with self._help_lock:
            if not target.is_file():
                rendered=await self._render_image_file(template,{},render_options={'width':1440},disable_cache=True)
                temporary=target.with_suffix('.tmp');await asyncio.to_thread(shutil.copyfile,rendered,temporary);temporary.replace(target)
                older=sorted((x for x in directory.glob('*') if x.suffix in ('.jpg','.png') and x!=target),key=lambda x:x.stat().st_mtime,reverse=True)
                for path in older[2:]:path.unlink(missing_ok=True)
        await event.send(event.image_result(str(target)))

    async def  richang(self, event: AstrMessageEvent, num: int = 0):
        """ 日常 天数"""
        return await self.plain_msg(event, lambda: self.jx3api.richang("day",num))


    async def  richangyuche(self, event: AstrMessageEvent):
        """ 日常预测"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.richang("list",15))


    async def  qiongyewei(self, event: AstrMessageEvent):
        """ 穹野卫"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.xingxiashijian("穹野卫"))
    
    async def  pifenghui(self, event: AstrMessageEvent):
        """ 披风会"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.xingxiashijian("披风会"))

    async def  yunchongshe(self, event: AstrMessageEvent):
        """ 云从社"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.xingxiashijian("云从社"))

    async def  chutianshe(self, event: AstrMessageEvent):
        """ 楚天社 """
        return await self.T2I_image_msg(event, lambda: self.jx3api.xingxiashijian("楚天社"))

    async def  guanaishouling(self, event: AstrMessageEvent):
        """ 关隘首领"""
        return await self.T2I_image_msg(event, self.jx3api.guanaishouling)

    async def  benrichitu(self, event: AstrMessageEvent):
        """ 本日赤兔"""
        return await self.plain_msg(event, self.jx3api.benrichitu)

    async def  benzhouchitu(self, event: AstrMessageEvent):
        """ 本周赤兔"""
        return await self.plain_msg(event, self.jx3api.benzhouchitu)

    async def  zhenyingevent(self, event: AstrMessageEvent, name:str = ""):
        """ 阵营事件 阵营"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.zhenyingevent(name,50))

    async def yanhuachaxun(self, event: AstrMessageEvent, server: str, name: str = "", limit: int = 50):
        """ 烟花 服务器 角色 条数"""
        limit = limit if limit > 0 else 50
        return await self.T2I_image_msg(
            event, lambda: self.jx3api.yanhuachaxun(server, name, limit)
        )

    async def  shuma(self, event: AstrMessageEvent,server: str ): 
        """ 刷马 服务器"""
        return await self.plain_msg(event, lambda: self.jx3api.shuma(server))

    async def  machang(self, event: AstrMessageEvent,server: str ): 
        """ 马场 服务器"""
        return await self.plain_msg(event, lambda: self.jx3api.machang(server,1))

    async def  role_state(self, event: AstrMessageEvent, server: str = "", name: str = ""):
        """ 角色状态 服务器 角色名 """
        if not str(name or "").strip():
            await event.send(event.plain_result(
                "用法：角色状态 服务器 角色名"))
            return
        return await self.T2I_image_msg(event, lambda: self.jx3api.role_state(name, server))


    # ==================== 角色绑定 ✅ ====================
    def _binding_service(self):
        if not hasattr(self, "_rb_svc"):
            from .role_binding import get_binding_service
            self._rb_svc = get_binding_service()
        return self._rb_svc

    @staticmethod
    def _uid(event) -> str:
        try:
            return str(event.get_sender_id() or "")
        except Exception:
            return ""

    async def  role_bind(self, event: AstrMessageEvent, server: str = "", name: str = ""):
        """ 绑定角色 服务器 角色名 """
        uid = self._uid(event)
        if not server or not name:
            await event.send(event.plain_result(
                "用法：绑定角色 服务器 角色名\n例：绑定角色 唯满侠 清唳"))
            return
        # ① 验证角色存在 ✅
        detail, _ = await self.jx3api._cached_request(
            "/role/detail",
            {"server": server, "name": name, "token": self.jx3api.token,
             "ticket": self.jx3api.ticket},
        )
        if not isinstance(detail, dict) or not detail.get("roleName"):
            await event.send(event.plain_result(
                f"❌ 没找到角色「{name}」@「{server}」\n请检查区服和角色名是否正确 ✅"))
            return

        info = {
            "server": detail.get("serverName") or server,
            "name": detail.get("roleName") or name,
            "zone": detail.get("zoneName", ""),
            "roleId": detail.get("roleId", ""),
            "force": detail.get("forceName", ""),
            "camp": detail.get("campName") or detail.get("camp", ""),
        }
        # ② 保存 ✅
        status, entry = self._binding_service().bind(uid, info)
        count = len(self._binding_service().list_of(uid))
        if status == "exists":
            await event.send(event.plain_result(
                f"ℹ️ 「{entry['name']}·{entry['server']}」已经绑定过了 ✅\n"
                f"当前共绑定 {count} 个角色"))
            return
        if status == "limit":
            await event.send(event.plain_result("⚠️ 最多绑定 20 个角色 ✅"))
            return

        extra = ""
        if info.get("force"):
            extra = f"（{info['force']}"
            if info.get("camp"):
                extra += f" · {info['camp']}"
            extra += "）"
        await event.send(event.plain_result(
            "✅ 绑定成功\n"
            f"　角色：{info['name']} · {info['server']}{extra}\n"
            f"　当前共绑定 {count} 个角色 ✅\n"
            "　可用「角色列表」查看，「在线统计」查在线状态"))

    async def  role_unbind(self, event: AstrMessageEvent, server: str = "", name: str = ""):
        """ 解绑角色 服务器 角色名 """
        uid = self._uid(event)
        if not server or not name:
            await event.send(event.plain_result(
                "用法：解绑角色 服务器 角色名\n例：解绑角色 唯满侠 清唳"))
            return
        removed = self._binding_service().unbind(uid, server, name)
        if removed is None:
            # 只按名字再试一次 ✅
            removed = self._binding_service().unbind(uid, "", name)
        if removed is None:
            await event.send(event.plain_result(
                f"❌ 你没有绑定「{name}」@「{server}」\n用「角色列表」看看都绑了哪些 ✅"))
            return
        count = len(self._binding_service().list_of(uid))
        await event.send(event.plain_result(
            f"✅ 已解绑\n　角色：{removed.get('name')} · {removed.get('server')}\n"
            f"　剩余 {count} 个角色 ✅"))

    async def  role_list(self, event: AstrMessageEvent):
        """ 角色列表 """
        uid = self._uid(event)
        rows = self._binding_service().list_of(uid)
        if not rows:
            await event.send(event.plain_result(
                "你还没有绑定任何角色 ✅\n"
                "可用「绑定角色 服务器 角色名」绑定\n例：绑定角色 唯满侠 清唳"))
            return
        lines = [f"📋 我的角色列表（共 {len(rows)} 个）", "━━━━━━━━━━━━━━"]
        for i, r in enumerate(rows, 1):
            extra = ""
            if r.get("force"):
                extra = f"  {r['force']}"
            if r.get("camp"):
                extra += f" · {r['camp']}"
            lines.append(f"{i}. {r.get('name')} · {r.get('server')}{extra}")
        lines.append("━━━━━━━━━━━━━━")
        lines.append("💡 解绑角色 服务器 角色名")
        lines.append("💡 在线统计 查看全部在线状态")
        await event.send(event.plain_result("\n".join(lines)))

    async def  role_online_stats(self, event: AstrMessageEvent):
        """ 在线统计（图片版 ✅）"""
        uid = self._uid(event)
        rows = self._binding_service().list_of(uid)
        if not rows:
            await event.send(event.plain_result(
                "你还没有绑定任何角色 ✅\n先用「绑定角色 服务器 角色名」绑定"))
            return

        await event.send(event.plain_result(
            f"🔍 正在查询 {len(rows)} 个角色的在线状态，请稍候…"))

        items = await self.jx3api.online_rows(rows)
        online_rows = [x for x in items if x.get("status") == 2]
        app_rows = [x for x in items if x.get("status") == 1]
        off_rows = [x for x in items if x.get("status") in (0, -1)]

        async def action():
            result = self.jx3api._init_return_data()
            try:
                result["temp"] = await self.jx3api._template_repo().get("online_stats.html")
            except Exception as exc:
                logger.error(f"加载在线统计模板失败: {exc}")
                result["msg"] = "模板缺失"
                return result
            result["code"] = 200
            result["data"] = {
                "total": len(items),
                "online_rows": online_rows,
                "app_rows": app_rows,
                "off_rows": off_rows,
            }
            return result

        await self.T2I_image_msg(
            event, action,
            cache_name="online_stats",
            cache_variant=f"{uid}:{len(items)}",
        )

    async def  role_online(self, event: AstrMessageEvent, server: str = "", name: str = ""):
        """ 在线 服务器 角色名 """
        if not str(server or "").strip():
            await event.send(event.plain_result(
                "用法：在线 服务器 角色名\n例：在线 唯我独尊 清唳"))
            return
        if not str(name or "").strip():
            # 只给了服务器 → 提示补名字 ✅
            await event.send(event.plain_result(
                f"用法：在线 服务器 角色名\n例：在线 {server} 清唳"))
            return
        result = await self.jx3api.role_online(name, server)
        if not isinstance(result, dict) or result.get("code") != 200:
            await event.send(event.plain_result(
                result.get("msg", "查询失败，请稍后再试") if isinstance(result, dict) else "查询失败"))
            return
        # ★ 一条消息：文字 + 图标嵌在文字里 ✅
        await event.send(event.plain_result(result.get("data", "")))

    async def  zhanji(self, event: AstrMessageEvent, server: str ,name: str , mode:str = "33"):
        """ 战绩 服务器 角色名 [模式]"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.zhanji(name, server,mode))

    async def  mingjianpaihang(self, event: AstrMessageEvent, mode:str = "33",limit: int = 50):
        """ 名剑排行 模式 数量 """
        return await self.T2I_image_msg(event, lambda: self.jx3api.mingjianpaihang(limit,mode))

    async def  mingjiantongji(self, event: AstrMessageEvent,mode: str = "33"):
        """ 名剑统计 模式"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.mingjiantongji(mode))

    async def kuafumingjian(self, event: AstrMessageEvent, server: str, mode: int = 33):
        """跨服名剑 服务器 [模式]。"""
        api_mode = {22: 0, 33: 1, 55: 2}.get(mode, 1)
        return await self.T2I_image_msg(
            event,
            lambda: self.jx3api.kuafumingjian(server, api_mode),
        )

    async def wulinzhengba(self,event: AstrMessageEvent,server: str,camp: str = "浩气盟",):
        """武林争霸 服务器 [阵营]。"""
        api_camp = {"浩气盟": 1, "恶人谷": 2}.get(camp, 1)
        return await self.T2I_image_msg(
            event,
            lambda: self.jx3api.wulinzhengba(server, api_camp),
        )

    async def bukairongyu(self, event: AstrMessageEvent, server: str):
        """捕快荣誉 服务器。"""
        return await self.T2I_image_msg(
            event,
            lambda: self.jx3api.bukairongyu(server),
        )

    async def jianghulangke(self, event: AstrMessageEvent, server: str):
        """江湖浪客 服务器。"""
        return await self.T2I_image_msg(
            event,
            lambda: self.jx3api.jianghulangke(server),
        )

    async def juedoutiaozhan(self,event: AstrMessageEvent,server: str,mode: int = "公开",):
        """决斗挑战 服务器 [模式]。"""
        api_mode = {"公开": 1, "私密": 2}.get(mode, 1)
        return await self.T2I_image_msg(
            event,
            lambda: self.jx3api.juedoutiaozhan(server, api_mode),
        )

    async def banghuipaihang(self, event: AstrMessageEvent, server: str):
        """帮会排行 服务器。"""
        return await self.handler_plain_image_msg(
            event,
            lambda: self.jx3api.banghui_rank_menu(),
            lambda selected: self.jx3api.rank_statistical_select(server,selected,),
        )

    async def zhenyingpaihang(self, event: AstrMessageEvent, server: str):
        """阵营排行 服务器。"""
        return await self.handler_plain_image_msg(
            event,
            lambda: self.jx3api.zhenying_rank_menu(),
            lambda selected: self.jx3api.rank_statistical_select(server,selected,),
        )

    async def qitapaihang(self, event: AstrMessageEvent, server: str):
        """其他排行 服务器。"""
        return await self.handler_plain_image_msg(
            event,
            lambda: self.jx3api.qita_rank_menu(),
            lambda selected: self.jx3api.rank_statistical_select(server,selected,),
        )

    async def  shilianpaixing(self, event: AstrMessageEvent, server: str , kungfu: str):
        """ 试炼排行 服务器 心法 """
        return await self.T2I_image_msg(event, lambda: self.jx3api.shilianpaixing(kungfu, server))

    async def  zhengyingpaimai(self, event: AstrMessageEvent,server: str , name: str = "", limit: int = 50 ):
        """ 阵营拍卖 物品名称 服务器"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.zhengyingpaimai(server, name, limit))

    async def  dilujilu(self, event: AstrMessageEvent,server: str ):
        """ 的卢 服务器"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.dilujilu(server))

    async def  jinjia(self, event: AstrMessageEvent,server: str , limit:str = "15"):
        """ 金价 服务器"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.jinjia( server,limit))

    async def wujia(
        self,
        event: AstrMessageEvent,
        Name: str = "",
        server: str = "",
        *extra: str,
    ):
        """ 物价 外观名称... [服务器]

        支持多词，例如「物价 浮光归云 白色」。
        完整名称直接出图；多款先列选项并生成默认款，可在两分钟内换款。
        """
        binding = getattr(self, "server_binding", None)
        raw_tokens = [str(t).strip() for t in (Name, server, *extra) if str(t).strip()]
        if not raw_tokens:
            await event.send(
                event.plain_result(
                    "用法：物价 外观名称 [服务器]"
                )
            )
            return

        # 把「像区服的词」挑出来当区服，其余全部并入关键词
        words = []
        server_token = ""
        for token in raw_tokens:
            is_server = False
            if binding is not None:
                try:
                    is_server = bool(
                        binding.is_known_server(token)
                        or binding.is_all_servers_query(token)
                    )
                except Exception:
                    is_server = False
            if is_server and not server_token:
                server_token = token
                continue
            words.append(token)

        if not server_token and binding is not None:
            try:
                server_token = (
                    await binding.get_binding(event.unified_msg_origin) or ""
                )
            except Exception:
                server_token = ""

        if not words:
            words = raw_tokens
        words = [normalize_query(word) for word in words]
        query = " ".join(words)
        choices = self.market_choices()
        pending = choices.start(event, {}, server_token)
        pending_event = ChoiceEvent(event, lambda: choices.current(event, pending, 0)) if pending else event
        # Searching the lightweight name index first prevents a provider's
        # successful default resolution from hiding other colours/editions.
        menu = await self._market_wait_notice(
            pending_event, self.jx3api.wujia_menu(words[0], words[1:]),
            "正在查找外观候选…",
        )
        if pending and not choices.current(event, pending, 0):
            return
        if menu:
            state = choices.start(event, menu, server_token)
            if len(menu) > 1:
                return await self._market_menu_and_image(event, state, menu, server_token)
            return await self._send_market_choice(event, state, menu[next(iter(menu))], server_token,
                                                   notified=False)

        # A name-search outage or an alias missing from its index can still use
        # the direct endpoint. Extra filters are never silently discarded.
        if len(words) == 1:
            direct = await self._market_wait_notice(pending_event, self.jx3api.wujia(query, server_token),
                                                     "正在查询外观物价…")
            data = direct.get('data') if isinstance(direct, dict) else None
            if isinstance(data, dict) and direct.get('code') == 200:
                name = (data.get('product') or {}).get('name') or data.get('name')
                if name:
                    if pending and not choices.current(event, pending, 0):
                        return
                    state = choices.start(event, {'1': name}, server_token)
                    target = ChoiceEvent(event, lambda: choices.current(event, state, 0)) if state else event
                    if name != query:
                        await target.send(event.plain_result(f"已匹配：{name}。正在生成物价图…"))
                    async def hit():
                        return direct
                    return await self.T2I_image_msg(target, hit, cache_name='物价',
                                                     cache_variant=self._market_variant(name, server_token))
        await pending_event.send(event.plain_result(f"没找到与「{query}」匹配的外观。可调整颜色、版本或外观名称重试。"))
        if pending and choices.current(event, pending, 0):
            choices.clear(event)

    def market_choices(self):
        if not hasattr(self, '_market_choice_store'):
            self._market_choice_store = MarketChoices()
        return self._market_choice_store

    async def _market_menu_and_image(self, event, state, menu, server):
        first = next(iter(menu))
        name = canonical_name(menu[first])
        store = self.market_choices()
        target = ChoiceEvent(event, lambda: store.current(event, state, 0)) if state else event
        # Give useful facts a small budget; a slow provider never holds the
        # choices hostage. The image reuses this same primary request.
        primary = asyncio.create_task(self.jx3api.wujia_snapshot(name, server))
        detail = asyncio.create_task(self.jx3api.wujia_brief_detail(name))
        tasks = {primary, detail}
        try:
            await asyncio.wait(tasks, timeout=0.7)
            if state and not store.current(event, state, 0):
                return
            def ready(task, default):
                return task.result() if task.done() and not task.cancelled() and task.exception() is None else default
            data, _ = ready(primary, ({}, {}))
            text = market_card(menu, data, ready(detail, {}), self._market_command_prefix(), bool(state))
            if not hasattr(self, '_market_buttons'):
                self._market_buttons = MarketButtons()
            sent = await self._market_buttons.send(
                target, text, menu, server, self._market_command_prefix(),
                enabled=bool(getattr(self.jx3api, '_config', {}).get('market_custom_buttons', False)),
            )
            if not sent:
                await target.send(event.plain_result(text))
            if state and not store.current(event, state, 0):
                return
            async def shared_query():
                snapshot = await primary
                return await self.jx3api.wujia(name, server, snapshot=snapshot)
            return await self._send_market_choice(event, state, menu[first], server,
                                                   notified=True, action=shared_query)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _market_command_prefix(self):
        config = getattr(self.jx3api, '_config', {})
        prefix = config.get('prefix', {})
        return str(prefix.get('text') or '').strip() if prefix.get('enable') else ''

    def _market_variant(self, name, server):
        if self.cache:
            return self.cache.value_signature(['market-choice-v2', name, server])
        return name + ':' + server

    @staticmethod
    async def _market_wait_notice(event, work, text, delay=1.2):
        task = asyncio.create_task(work)
        try:
            done, _ = await asyncio.wait({task}, timeout=delay)
            if not done:
                await event.send(event.plain_result(text))
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _send_market_choice(self, event, state, label, server, notified=False, action=None):
        name = canonical_name(label)
        target = event
        if state:
            revision = state.revision
            target = ChoiceEvent(event, lambda: self.market_choices().current(event, state, revision))
        work = self.T2I_image_msg(target, action or (lambda: self.jx3api.wujia(name, server)),
                                  cache_name='物价', cache_variant=self._market_variant(name, server))
        if notified:
            return await work
        return await self._market_wait_notice(target, work, f"正在查询「{name}」并生成物价图…")

    async def wujia_choice(self, event: AstrMessageEvent, choice: str = ''):
        """物价选 序号（也可填“默认”或“取消”）"""
        store = self.market_choices()
        state = store.active(event)
        if not state:
            await event.send(event.plain_result("当前没有有效的外观选项。请重新发送：物价 外观名称"))
            return
        if choice == '取消':
            store.clear(event)
            await event.send(event.plain_result("已取消这次物价查询。"))
            return
        if not state.options:
            await event.send(event.plain_result("外观候选正在查询，请稍后再选。"))
            return
        key = next(iter(state.options)) if choice == '默认' else str(choice).strip()
        if key not in state.options:
            await event.send(event.plain_result(f"请选择 1–{len(state.options)}，例如：{self._market_command_prefix()}物价选2"))
            return
        state.selected = key
        state.revision += 1
        label = state.options[key]
        await event.send(event.plain_result(f"已选择：{canonical_name(label)}。正在生成物价图…"))
        return await self._send_market_choice(event, state, label, state.server, notified=True)

    async def  chengbeng(self, event: AstrMessageEvent, server: str ,Name: str ,source : int = 0):
        """ 成本 服务器 物品名称 """    
        return await self.T2I_image_msg(event, lambda: self.jx3api.chengbeng(Name, server,source)) 

    async def  kanhao(self, event: AstrMessageEvent,id: str):
        """ 看号 万宝楼编号 """    
        return await self.plain_msg(event, lambda: self.jx3api.bianhao(id)) 

    async def  bangzhanjilu(self, event: AstrMessageEvent, server: str):
        """ 帮战 服务器"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.bangzhanjilu(server))

    async def  shapan(self, event: AstrMessageEvent,server: str):
        """ 沙盘 服务器"""
        return await self.T2I_image_msg(
            event,
            lambda: self.jx3api.shapan(server),
            render_options={"omit_background": True, "type": "png"},
        )

    async def  zhueevent(self, event: AstrMessageEvent, server: str):
        """ 诛恶事件 服务器"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.zhueevent(server,20))

    async def  jueshemingpian(self, event: AstrMessageEvent, server: str , name: str , ):
        """ 名片 服务器 角色 """
        return await self.plain_chain(event, lambda: self.jx3api.jueshemingpian(server, name)) 

    async def refresh_mingpian(self, event: AstrMessageEvent, server: str, name: str):
        """刷新名片 服务器 角色名"""
        return await self.plain_chain(
            event, lambda: self.jx3api.jueshemingpian(server, name, force_refresh=True)
        )

    async def  shuoyoumingpian(self, event: AstrMessageEvent, server: str, name: str, ):
        """ 全名片 服务器 角色 """
        return await self.plain_chain(event, lambda: self.jx3api.shuoyoumingpian(server,name)) 

    async def  shuijimingpian(self, event: AstrMessageEvent,server: str, force: str = "", body: str = "", ):
        """ 随机秀 服务器 门派 体型 """
        return await self.plain_chain(event, lambda: self.jx3api.shuijimingpian(server,force,body))

    async def  qiyuhuizong(self, event: AstrMessageEvent,server: str, num: str = "7" ):
        """ 汇总 服务器 天数 """
        return await self.T2I_image_msg(event, lambda: self.jx3api.qiyuhuizong(server, num))

    async def  weizuoqiyu(self, event: AstrMessageEvent,server: str, name: str, ):
        """ 未出 服务器 角色 """
        return await self.T2I_image_msg(event, lambda: self.jx3api.weizuoqiyu(server,name))

    async def  jinqiqiyu(self, event: AstrMessageEvent,server: str, limit: int = 20):
        """ 近期 服务器 数量"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.jinqiqiyu(server,limit))

    async def  juesheqiyu(self, event: AstrMessageEvent, server: str, name: str):
        """ 奇遇 服务器 角色 """
        return await self.T2I_image_msg(event, lambda: self.jx3api.juesheqiyu(server,name, 0))

    async def  qiyutongji(self, event: AstrMessageEvent,adventureName: str, server: str = "",limit: int = 20):
        """ 统计 奇遇 服务器 数量"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.qiyutongji(adventureName,server,limit))

    async def  qiyugonglue(self, event: AstrMessageEvent,name: str):
        """ 攻略 奇遇"""
        return await self.T2I_image_msg(event, lambda: self.jx3box.qiyugonglue(name))

    async def tarot_single(self, event: AstrMessageEvent, *question: str):
        """塔罗 问题（可不填）"""
        return await self.T2I_image_msg(
            event, lambda: tarot_reading(' '.join(question), 1),
            render_options={'width': 960}, disable_cache=True,
        )

    async def tarot_three(self, event: AstrMessageEvent, *question: str):
        """塔罗三张 问题（可不填）"""
        return await self.T2I_image_msg(
            event, lambda: tarot_reading(' '.join(question), 3),
            render_options={'width': 960}, disable_cache=True,
        )

    async def fortune_lot(self, event: AstrMessageEvent, *arguments: str):
        """生辰八字 公历出生日期 出生时间 性别（性别可选；留空抽签）"""
        return await self.T2I_image_msg(
            event, lambda: fortune_reading(' '.join(arguments)),
            render_options={'width': 960}, disable_cache=True,
        )

    async def _daily_draw(self, event):
        from astrbot.api.star import StarTools
        if not hasattr(self,'_daily_secret'):
            self._daily_secret=await asyncio.to_thread(load_secret,StarTools.get_data_dir('astrbot_plugin_jx3_miemie'))
        platform=str(event.get_platform_name()) if hasattr(event,'get_platform_name') else str(getattr(event,'unified_msg_origin','')).split(':',1)[0]
        sender=str(event.get_sender_id() or '')
        if not sender:raise ValueError('每日牌需要有效的用户身份')
        return await asyncio.to_thread(daily_card,platform+'\0'+sender,self._daily_secret)

    async def horoscope(self, event: AstrMessageEvent, *arguments: str):
        """今日运势 星座（可不填）"""
        if not hasattr(self,'_horoscope'):self._horoscope=HoroscopeService(self.jx3api._api.get_session)
        query=' '.join(arguments);draw=await self._daily_draw(event)
        async def action():
            result=await self._horoscope.reading(query)
            if result['code']==200:result['data']={**result['data'],'daily_card':draw['card']}
            return result
        bucket=int(time.time()//1800)
        return await self.T2I_image_msg(event,action,render_options={'width':1200},cache_name='今日运势',cache_variant=f"dated-v2:{draw['signature']}:{parse_sign(query) or query}:{bucket}")

    async def tarot_daily(self, event: AstrMessageEvent):
        """今日塔罗"""
        draw=await self._daily_draw(event)
        async def action():
            return {'code':200,'msg':'ok','temp':await load_template('tarot.html'),'data':{'cards':[draw['card']],'date':draw['date'],'question':'','daily':True}}
        return await self.T2I_image_msg(event,action,render_options={'width':960},cache_name='今日塔罗',cache_variant='daily-v1:'+draw['signature'])

    async def ziwei(self, event: AstrMessageEvent, *arguments: str):
        """紫微斗数 公历出生日期 出生时间 性别"""
        return await self.T2I_image_msg(
            event, lambda: ziwei_reading(' '.join(arguments)),
            render_options={'width':1200}, disable_cache=True,
        )

    async def love_book(self, event: AstrMessageEvent, *theme: str):
        """恋爱书 随机翻页（先在心里默念问题）"""
        if not hasattr(self, '_love_book'):
            self._love_book = LoveBook(library_name='love_answers')
        sender = str(event.get_sender_id() or '')
        session = str(getattr(event, 'unified_msg_origin', '') or '')
        owner = hashlib.sha256((session + '\0' + sender).encode()).hexdigest() if sender else ''
        return await self.T2I_image_msg(
            event, lambda: love_reading(self._love_book, ' '.join(theme), owner),
            render_options={'width': 900}, disable_cache=True,
        )

    async def  jingnai(self, event: AstrMessageEvent, server: str, name: str):
        """ 精耐 服务器 角色 """
        return await self.T2I_image_msg(event, lambda: self.jx3api.jingnai(name, server))
    
    async def  baizhan(self, event: AstrMessageEvent):
        """ 百战"""
        return await self.T2I_image_msg(event, self.jx3api.baizhan)
    
    async def  chengjiu(self, event: AstrMessageEvent, server:str, role:str, name:str):
        """ 成就 服务器 角色 成就"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.chengjiuchaxun(server,role,name))

    async def  jueshe(self, event: AstrMessageEvent,server: str, name: str):
        """ 角色 服务器 名称 """
        return await self.plain_msg(event, lambda: self.jx3api.jueshe(server, name, 1))

    async def  zhenyan(self, event: AstrMessageEvent, kungfu: str):
        """ 阵眼 心法"""
        return await self.plain_msg(event, lambda: self.jx3api.zhenyan(kungfu))

    async def  peizhuang(self, event: AstrMessageEvent, kungfu: str, tags: str = ""):
        """ 配装 心法 类型"""
        return await self.plain_msg(event, lambda: self.jx3box.peizhuang(kungfu,tags))

    async def  zilipaixing(self, event: AstrMessageEvent, server: str = "", school: str = ""):
        """ 资历排行 服务器 门派 """
        return await self.T2I_image_msg(event, lambda: self.jx3api.zilipaixing(server, school))

    async def  jineng(self, event: AstrMessageEvent, kungfu: str):
        """ 技能 心法"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.jineng(kungfu,0))

    async def  qixue(self, event: AstrMessageEvent, kungfu: str):
        """ 奇穴 心法"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.qixue(kungfu,0))

    # ==================== 豆包 AI 生图 ====================

    # ==================== 名片 → AI 生图 ====================

    # 「门派」模板：用角色查询到的门派/体型填充
    _CREST_PROMPT = (
        "根据【剑网三·{force}(门派){body}(体型)】生成一张收藏版史诗叙事海报："
        "巨大优雅的人物侧脸剪影作为画面外轮廓；"
        "剪影内部自然生长出最契合该门派的世界观：门派标志性建筑群（山门、殿宇、飞檐、石阶）、"
        "门派阵法与图腾符号、云海与仙山、松树与仙鹤、该门派的代表道具（剑/刀/枪/扇等），"
        "门派同门群像剪影（含并肩而立的人物、可手持门派武器）作为叙事点缀，"
        "以及该门派的【家徽纹章】作为装饰符号；"
        "整体是高级的剪影轮廓填充式双重曝光合成，元素从人物轮廓里自然生长，不是拼贴；"
        "画面左侧用毛笔书法竖排书写角色名「{name}」作为主标题，"
        "旁边配一行门派名「{force}」的小字，并盖一枚红色篆刻印章，印章文字为「剑网3」；"
        "色调、场景、材质全部按该门派自动适配："
        "纯阳＝冰蓝月白、藏剑＝金棕暖金、刀宗＝墨青、天策＝玄青暗金、"
        "七秀＝粉紫、五毒＝紫绿、明教＝赤金、万花＝青碧等，低饱和高级灰；"
        "柔和空气透视，轻雾化过渡，纸张颗粒质感，边缘飞白与毛笔刷痕，大面积留白；"
        "版式克制高级，安静、宏大、神圣、怀旧、诗意、传说感强；"
        "不要杂乱，不要硬拼贴，不要模板化背景，不要廉价奇幻素材。"
    )

    # 菜单（第 1 项 = 门派，按需求置顶）
    _CARD_MENU = [
        ("门派", "门派史诗海报"),
        ("眼睛条", "眼睛条横幅"),
        ("水墨", "水墨"),
        ("工笔", "工笔重彩"),
        ("敦煌", "敦煌壁画"),
        ("动漫", "日系动漫"),
        ("新海诚", "新海诚"),
        ("吉卜力", "吉卜力"),
        ("q版", "Q版"),
        ("手办", "树脂手办"),
        ("油画", "古典油画"),
        ("水彩", "清透水彩"),
        ("胶片", "复古胶片"),
        ("塔罗", "塔罗牌"),
        ("赛博", "赛博朋克"),
        ("像素", "像素艺术"),
    ]

    _CARD_STYLES = {
        "眼睛条": (
            "横向宽幅「眼睛条」海报，电影级构图："
            "背景为深色（黑或深色调），带水平扫描线纹理与霓虹光带，氛围梦幻高级；"
            "画面正中是一个发光的圆角矩形画框，占画面绝大部分；"
            "画框内是参考图角色【双眼的正面大特写】——高还原度保留原图眼部妆造、"
            "睫毛、眼影、卧蚕、眼镜（若有）、发丝等全部元素，原图有的就保留，"
            "不改变眼距与眼型；加强眼妆闪粉与瞳孔光泽，让眼睛通透有神、闪闪发亮；"
            "前景是参考图中【无背景的半身人物形象】，位于画面正中、恰好处于双眼之间，"
            "不改变人物任何动作与姿态、不改变角度与方位，人物高度与背景画框适配，"
            "保持前后主体一致性（同一角色、同一配色、同一服饰）；"
            "整体高饱和、闪亮、精致，奢侈品广告海报质感，不要杂乱，不要廉价素材。"
        ),
        "水墨": (
            "根据【剑网三·{force}(门派){body}(体型)】绘制水墨国风插画："
            "杰作，最高画质，8k，细节丰富，古风人物，水墨国风，泼墨笔触，金箔泼洒质感，"
            "宣纸背景，水墨山水，梅花竹子，远山古亭，飘逸华丽古风服饰，半透纱质衣袂，"
            "长发随风飞扬，精致发饰，额间朱砂花钿点缀，"
            "【面部干净无瑕，皮肤细腻通透，五官精致完整，无任何斑点与污渍】，"
            "笔触纹理，"
            "左侧用毛笔书法竖排书写角色名「{name}」作为题字，字迹飘逸有飞白；"
            "【不要任何印章，不要落款，不要其他文字】；"
            "喷溅墨点，金漆碎屑，氛围感，剑网3游戏插画"
        ),
        "油画": "古典油画风格，厚重笔触与伦勃朗光影，保留人物特征",
        "水彩": "清透水彩画风格，湿润晕染，留白呼吸感，保留人物特征",
        "胶片": "复古胶片摄影风格，颗粒质感与暖调，怀旧氛围，保留人物特征",
        "塔罗": "神秘塔罗牌卡面风格，金线繁复纹饰与星辰符号，对称构图，保留人物特征",
        "赛博": "赛博朋克风格，霓虹光效与全息投影，未来都市感，保留角色配色特征",
        "像素": "复古像素艺术风格，16bit 游戏画面感，保留角色特征",
    }

    # 需要后处理文字层的模板（V-A 方案：让模型自己写字，故留空 ✅）
    #  想改回"弟子绘制"就写： ("水墨", "门派")
    _OVERLAY_TEMPLATES: tuple[str, ...] = ()

    # 各模板的反向提示词（豆包支持 negative_prompt ✅）
    _CARD_NEGATIVE = {
        "水墨": ("低画质，模糊，畸形，五官崩坏，多余肢体，水印，签名，多余文字，"
                 "现代服装，照片写实，3D渲染，平涂，"
                 "【面部瑕疵，斑点，痘印，污渍，脏点，皮肤噪点，皮肤粗糙，"
                 "脸上墨点，面部脏污，妆容脱落，泪痕】"),
    }

    # 特殊尺寸模板：(生成尺寸, 最终尺寸)
    #   豆包要求画面 ≥ 921600 像素，所以小尺寸横幅先生成大图再压缩
    _CARD_EXTRA = {
        "眼睛条": ("1536x608", (860, 340)),
    }

    async def _role_crest(self, server: str, name: str) -> tuple[str, str]:
        """取角色门派与体型（供「门派」模板使用）。"""
        try:
            data, _meta = await self.jx3api._cached_request(
                "/role/detail",
                {"server": server, "name": name,
                 "token": self.jx3api.token, "ticket": self.jx3api.ticket},
            )
            if isinstance(data, dict):
                return str(data.get("forceName") or ""), str(data.get("bodyName") or "")
        except Exception:
            logger.debug("门派模板：取角色门派/体型失败", exc_info=True)
        return "", ""

    @staticmethod
    def _card_menu_text(name: str) -> str:
        lines = [f"🎨 请选择「{name}」的生图模板（回复序号）：", ""]
        for i, (_k, label) in enumerate(MessageBuilder._CARD_MENU, start=1):
            lines.append(f"  {i:>2}. {label}")
            if i % 4 == 0:
                lines.append("")
        lines.append("")
        lines.append("⏱ 120 秒内回复序号；回复「退出」取消")
        return "\n".join(lines)

    async def _card_generate(self, target_event, server, name, ref, style_key, style_text):
        gen = getattr(self, "image_gen", None)
        if gen is None or not gen.enabled:
            await target_event.send(target_event.plain_result("⚠️ 还没配置豆包 API Key"))
            return
        key = str(style_key or "").strip().lower()
        prompt = self._CARD_STYLES.get(key) or self._CARD_STYLES.get(str(style_key).strip())
        if not prompt:
            prompt = (f"在保留角色五官、发型与服饰特征的前提下，改成「{style_text}」风格，"
                      f"构图优雅，背景留白，画面高级")

        if "{force}" in prompt:
            force, body = await self._role_crest(server, name)
            if not force:
                await target_event.send(target_event.plain_result(
                    "⚠️ 没查到该角色的门派信息，换个模板试试"))
                return
            prompt = (str(prompt).replace("{force}", force)
                                  .replace("{body}", body or "未知")
                                  .replace("{name}", str(name)))
            await target_event.send(target_event.plain_result(
                f"🎨 已识别：{force} · {body or '未知体型'}，正在生成「{style_text}」…"))
        else:
            await target_event.send(target_event.plain_result(
                f"🎨 正在生成「{style_text}」风格，约 10~30 秒…"))

        # 特殊尺寸模板（如眼睛条 860x340）
        extra = (self._CARD_EXTRA.get(key)
                 or self._CARD_EXTRA.get(str(style_key).strip())
                 or self._CARD_EXTRA.get(str(style_text).strip()))
        gen_size, final_size = extra if extra else ("1024x1536", None)

        negative = (self._CARD_NEGATIVE.get(key)
                    or self._CARD_NEGATIVE.get(str(style_key).strip())
                    or self._CARD_NEGATIVE.get(str(style_text).strip()))
        try:
            out = await gen.generate(prompt, reference_image=ref, size=gen_size,
                                     negative_prompt=negative)
        except Exception as e:
            logger.error(f"名片生图失败: {e}")
            await target_event.send(target_event.plain_result(f"❌ 生图失败：{e}"))
            return

        if final_size:
            try:
                from PIL import Image as _Image
                im = _Image.open(out).convert("RGB")
                im = im.resize(final_size, _Image.LANCZOS)
                final_path = pathlib.Path(str(out)).with_name(
                    pathlib.Path(str(out)).stem + "_final.jpg")
                im.save(final_path, "JPEG", quality=94)
                out = str(final_path)
                logger.info(f"已压缩到 {final_size}: {final_path}")
            except Exception as e:
                logger.warning(f"压缩到 {final_size} 失败，用原图: {e}")

        # 后处理文字层：角色名 + 印章（100% 准确，避免 AI 写错生僻字）
        # V-A 方案：让模型自己写毛笔字，不叠加文字层
        if key in self._OVERLAY_TEMPLATES:
            try:
                overlay = getattr(self, "text_overlay", None)
                if overlay is None:
                    from .text_overlay import TextOverlay
                    overlay = TextOverlay(pathlib.Path(__file__).resolve().parent.parent)
                    self.text_overlay = overlay
                out = await overlay.draw_async(str(out), name=str(name), seal_text="剑网3")
            except Exception:
                logger.warning("文字层绘制失败，用原图", exc_info=True)

        await target_event.send(target_event.image_result(str(out)))

    def _resolve_server_alias(self, server: str) -> str:
        """把区服别名解析成正式名（唯满侠 → 唯我独尊）。"""
        raw = str(server or "").strip()
        if not raw:
            return raw
        try:
            binding = getattr(self, "server_binding", None)
            if binding is not None:
                resolved = binding.resolve_server(raw)
                if resolved and resolved != raw:
                    logger.info(f"区服别名解析：{raw} → {resolved}")
                return resolved or raw
        except Exception:
            logger.debug("区服别名解析失败，按原值使用", exc_info=True)
        return raw

    async def  mingpian_aigen(self, event: AstrMessageEvent, server: str = "", name: str = "", style: str = ""):
        """ 名片生图 服务器 角色 [模板] """
        server = self._resolve_server_alias(server)
        if not (server or "").strip() or not (name or "").strip():
            await event.send(event.plain_result(
                "用法：名片生图 服务器 角色 [模板]\n"
                "例：名片生图 唯我独尊 清唳\n"
                "（不填模板会弹出菜单让你选）"))
            return
        gen = getattr(self, "image_gen", None)
        if gen is None or not gen.enabled:
            await event.send(event.plain_result("⚠️ 还没配置豆包 API Key（插件配置里填一下）"))
            return

        # ① 查名片
        await event.send(event.plain_result(f"🔍 正在查询「{server}-{name}」的名片…"))
        try:
            card = await self.jx3api.jueshemingpian(server, name)
        except Exception as e:
            await event.send(event.plain_result(f"❌ 查询名片失败：{e}"))
            return
        if card.get("code") != 200:
            await event.send(event.plain_result(f"❌ {card.get('msg') or '没查到名片'}"))
            return

        img_url = ""
        for comp in (card.get("data") or []):
            u = getattr(comp, "url", None) or getattr(comp, "file", None)
            if u and str(u).startswith("http"):
                img_url = str(u)
                break
        if not img_url:
            await event.send(event.plain_result("❌ 没拿到名片图片地址"))
            return

        # ② 先发原名片图
        await event.send(event.image_result(img_url))

        # ③ 下载备用
        import aiohttp
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                async with session.get(img_url) as resp:
                    ref = await resp.read()
                    if resp.status != 200 or len(ref) < 1000:
                        raise RuntimeError(f"HTTP {resp.status}")
        except Exception as e:
            await event.send(event.plain_result(f"❌ 下载名片图失败：{e}"))
            return

        # ④ 指定模板 → 直接生成
        direct = str(style or "").strip()
        if direct:
            await self._card_generate(event, server, name, ref, direct, direct)
            return

        # ⑤ 弹菜单等选择
        await event.send(event.plain_result(self._card_menu_text(name)))
        owner = str(event.get_sender_id())

        @session_waiter(timeout=120)
        async def card_menu_waiter(controller: SessionController, new_event: AstrMessageEvent):
            if str(new_event.get_sender_id()) != owner:
                return
            raw = new_event.get_message_str().strip()
            if raw.lower() in ("退出", "取消", "q", "cancel", "exit", "算了"):
                await new_event.send(new_event.plain_result("已取消 ✅"))
                controller.stop()
                return
            if not re.fullmatch(r"[0-9]+", raw) or not (1 <= int(raw) <= len(self._CARD_MENU)):
                await new_event.send(new_event.plain_result(
                    f"请输入 1~{len(self._CARD_MENU)} 的序号，或回复「退出」"))
                return
            key, label = self._CARD_MENU[int(raw) - 1]
            controller.stop()
            try:
                await self._card_generate(new_event, server, name, ref, key, label)
            except Exception as e:
                logger.error(f"名片生图（菜单）失败: {e}")
                await new_event.send(new_event.plain_result(f"❌ 生图失败：{e}"))

        try:
            await card_menu_waiter(event)
        except TimeoutError:
            await event.send(event.plain_result("⏱ 选择超时，已取消（可重新发送指令）"))
        except Exception:
            logger.error("名片生图菜单会话异常", exc_info=True)

    async def  shengtu(self, event: AstrMessageEvent, prompt: str = "", *extra: str):
        """ 生图 描述词 """
        text = " ".join(str(p) for p in (prompt, *extra) if str(p).strip()).strip()
        if not text:
            await event.send(event.plain_result(
                "用法：生图 描述词\n例：生图 一只在华山雪顶睡觉的白羊，水墨风"))
            return

        gen = getattr(self, "image_gen", None)
        if gen is None or not gen.enabled:
            await event.send(event.plain_result(
                "⚠️ 还没配置豆包 API Key\n"
                "请到插件配置里填「豆包（火山方舟）API Key」，填完重载插件即可"))
            return

        await event.send(event.plain_result("🎨 正在生成，大概 10~30 秒…"))
        try:
            path = await gen.generate(text)
        except Exception as e:
            logger.error(f"生图失败: {e}")
            await event.send(event.plain_result(f"❌ 生图失败：{e}"))
            return
        await event.send(event.image_result(str(path)))

    async def  tushengtu(self, event: AstrMessageEvent, prompt: str = "", *extra: str):
        """ 图生图 描述词（需同时带一张图）"""
        text = " ".join(str(p) for p in (prompt, *extra) if str(p).strip()).strip()

        # 从消息里取参考图
        ref = None
        try:
            from astrbot.api.message_components import Image as _CompImage
            for comp in event.get_messages():
                if isinstance(comp, _CompImage):
                    fp = await comp.convert_to_file_path()
                    ref = pathlib.Path(fp).read_bytes()
                    break
        except Exception:
            logger.debug("图生图：读取参考图失败", exc_info=True)

        if ref is None:
            await event.send(event.plain_result(
                "用法：**同时发一张图 + 文字**「图生图 描述词」\n"
                "例：（附一张图）图生图 改成水墨画风格"))
            return
        if not text:
            await event.send(event.plain_result("请补上描述词，例如：图生图 改成水墨画风格"))
            return

        gen = getattr(self, "image_gen", None)
        if gen is None or not gen.enabled:
            await event.send(event.plain_result(
                "⚠️ 还没配置豆包 API Key（插件配置里填）"))
            return

        await event.send(event.plain_result(
            f"🎨 收到参考图（{len(ref)/1024:.0f} KB），正在生成…"))
        try:
            path = await gen.generate(text, reference_image=ref)
        except Exception as e:
            logger.error(f"图生图失败: {e}")
            await event.send(event.plain_result(f"❌ 图生图失败：{e}"))
            return
        await event.send(event.image_result(str(path)))


    async def  liaotian(self, event: AstrMessageEvent, server:str, name: str, limit:int = 20, page:int = 1):
        """ 发言 服务器 角色 条数 页数（出图后支持回复翻页）"""
        total = await self._send_liaotian_page(event, server, name, limit, page)
        # 有下一页才开启翻页会话，避免无意义地占用会话
        if total and total > limit:
            await self._liaotian_paging(event, server, name, limit, page, total)
        return None

    async def _send_liaotian_page(
        self,
        event: AstrMessageEvent,
        server: str,
        name: str,
        limit: int,
        page: int,
    ) -> int:
        """渲染并发送「发言」第 page 页，返回总条数。"""
        # 先取一次数据（进缓存），拿到 total 用于算总页数
        total = 0
        try:
            probe = await self.jx3api.juesheliaotian(server, name, limit, page)
            payload = probe.get("data") if isinstance(probe, dict) else None
            if isinstance(payload, dict):
                total = int(payload.get("total") or 0)
        except Exception:
            logger.debug("发言：预取总数失败，继续出图", exc_info=True)

        await self.T2I_image_msg(
            event,
            lambda: self.jx3api.juesheliaotian(server, name, limit, page),
        )

        if total:
            pages = max(1, math.ceil(total / max(limit, 1)))
            tip = (
                f"📄 第 {page}/{pages} 页 · 共 {total} 条（每页 {limit} 条）\n"
                f"回复「下一页」「上一页」「页码」翻页，回复「退出」结束"
            )
        else:
            tip = "📄 回复「下一页」「页码」翻页，回复「退出」结束"
        try:
            await event.send(MessageChain().message(tip))
        except Exception:
            logger.debug("发言：发送翻页提示失败", exc_info=True)
        return total

    async def _liaotian_paging(
        self,
        event: AstrMessageEvent,
        server: str,
        name: str,
        limit: int,
        start_page: int,
        total: int,
    ) -> None:
        """回复式翻页会话（只有同一人可以翻页）。"""
        owner = str(event.get_sender_id())
        pages = max(1, math.ceil(total / max(limit, 1)))
        state = {"page": start_page}

        @session_waiter(timeout=180)
        async def paging_waiter(
            controller: SessionController,
            new_event: AstrMessageEvent,
        ):
            if str(new_event.get_sender_id()) != owner:
                return

            msg = new_event.get_message_str().strip().lower()
            if msg in ("退出", "结束", "q", "exit", "cancel", "算了"):
                await new_event.send(MessageChain().message("已退出翻页 ✅"))
                controller.stop()
                return

            cur = state["page"]
            if msg in ("下一页", "下页", "n", "next", ">"):
                target = cur + 1
            elif msg in ("上一页", "上页", "p", "prev", "<"):
                target = cur - 1
            elif msg.isdigit():
                target = int(msg)
            else:
                await new_event.send(MessageChain().message(
                    "翻页用法：回复「下一页」/「上一页」/ 页码 /「退出」"))
                return

            if target < 1 or target > pages:
                await new_event.send(MessageChain().message(
                    f"页码超出范围（共 {pages} 页）"))
                return

            state["page"] = target
            try:
                await self._send_liaotian_page(new_event, server, name, limit, target)
            except Exception as e:
                logger.error(f"发言翻页渲染失败: {e}")
                await new_event.send(MessageChain().message("猪脑过载，请稍后再试"))
                controller.stop()

        try:
            await paging_waiter(event)
        except TimeoutError:
            pass
        except Exception:
            logger.error("发言翻页会话异常", exc_info=True)

    async def  tongzhanyy(self, event: AstrMessageEvent, server: str = ""):
        """ 统战 服务器"""
        return await self.plain_msg(event, lambda: self.jx3api.tongzhanyy(server))

    async def  xiaoyao(self, event: AstrMessageEvent, kungfu:str = ""):
        """ 小药 心法"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.xiaoyao(kungfu))

    async def  pianzhi(self, event: AstrMessageEvent, uid: str, server:str = ""):
        """ 骗子 uid 服务器"""
        return await self.plain_msg(event, lambda: self.jx3api.pianzhi(server,uid))

    async def  huajia(self, event: AstrMessageEvent,  server: str, name: str= "" , map: str= ""):
        """ 花价 服务器 名称 地图"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.huajia(server,name,map))

    async def  zhuangshi(self, event: AstrMessageEvent,  name: str):
        """ 装饰 名称"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.zhuangshi(name))

    async def  qiwu(self, event: AstrMessageEvent,  name: str):
        """ 器物 地图名称"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.qiwu(name))

    async def  baishi(self, event: AstrMessageEvent, server: str, keyword: str = ""):
        """ 拜师 服务器 关键词 """
        return await self.T2I_image_msg(event, lambda: self.jx3api.shitu(2, server, keyword, 50))

    async def  shoutu(self, event: AstrMessageEvent, server: str, keyword: str = ""):
        """ 收徒 服务器 关键词 """
        return await self.T2I_image_msg(event, lambda: self.jx3api.shitu(1, server, keyword, 50))

    async def  weihu(self, event: AstrMessageEvent,limit:int = 5):
        """ 维护 数量"""
        return await self.plain_msg(event, lambda: self.jx3api.weihu(limit))

    async def  xinwen(self, event: AstrMessageEvent,limit:int = 5):
        """ 新闻 数量"""
        return await self.plain_msg(event, lambda: self.jx3api.xinwen(limit))

    async def  tuanduizhaomu(self, event: AstrMessageEvent,server: str, keyword: str = ""):
        """ 招募 服务器 副本"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.tuanduizhaomu( server,1,keyword,50))

    async def  tuanzhang(self, event: AstrMessageEvent,server: str, keyword: str = ""):
        """ 团长 服务器 名字"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.tuanduizhaomu( server,2,keyword,50))

    async def  tuanpai(self, event: AstrMessageEvent,server: str, keyword: str = ""):
        """ 团牌 服务器 内容"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.tuanduizhaomu( server,3,keyword,50))

    async def  daanzhishu(self, event: AstrMessageEvent):
        """ 答案之书"""
        return await self.plain_msg(event, self.jx3api.daanzhishu)

    async def  tiangou(self, event: AstrMessageEvent):
        """ 舔狗语录"""
        return await self.plain_msg(event, self.jx3api.tiangou)

    async def  fkxq4(self, event: AstrMessageEvent):
        """ 疯狂星期四"""
        return await self.plain_msg(event, lambda: self.jx3api.fengleiyulu("疯狂星期四"))

    async def  caihongpi(self, event: AstrMessageEvent):
        """ 彩虹屁"""
        return await self.plain_msg(event, lambda: self.jx3api.fengleiyulu("彩虹屁"))

    async def  dujitang(self, event: AstrMessageEvent):
        """ 毒鸡汤"""
        return await self.plain_msg(event, lambda: self.jx3api.fengleiyulu("毒鸡汤"))

    async def  pengyouquan(self, event: AstrMessageEvent):
        """ 朋友圈"""
        return await self.plain_msg(event, lambda: self.jx3api.fengleiyulu("朋友圈"))

    async def  heshengme(self, event: AstrMessageEvent,):
        """ 喝什么"""
        return await self.plain_msg(event, self.jx3api.heshengme)

    async def  chishengme(self, event: AstrMessageEvent,):
        """ 吃什么"""
        return await self.plain_msg(event, self.jx3api.chishengme)

    async def  shaohua(self, event: AstrMessageEvent,):
        """ 骚话"""
        return await self.plain_msg(event, self.jx3api.shaohua)

    async def  zhananyulu(self, event: AstrMessageEvent,):
        """ 渣男语录"""
        return await self.plain_msg(event, self.jx3api.zhananyulu)

    async def  tiebawujia(self, event: AstrMessageEvent, name: str, server: str = "", limit: str = "5", ):
        """ 贴吧物价 名称 服务器 数量 """
        return await self.plain_msg(event, lambda: self.jx3api.tiebawujia(name, server, limit))

    async def  bagua(self, event: AstrMessageEvent, server:str = "", limit:str = 10):
        """ 818 """
        return await self.plain_msg(event, lambda: self.jx3api.bagua(818,server,limit))

    async def  keju(self, event: AstrMessageEvent,subject: str, limit: int = 5):
        """ 科举 题目 条数"""
        return await self.plain_msg(event, lambda: self.jx3api.keju(subject,limit))

    async def  zhuangtai(self, event: AstrMessageEvent):
        """ 区服"""
        return await self.T2I_image_msg(event, lambda: self.jx3api.zhuangtai(""))

    async def  kaifu(self, event: AstrMessageEvent,server: str):
        """ 开服 服务器"""
        return await self.plain_msg(event, lambda: self.jx3api.kaifu(server))

    async def  jigai(self, event: AstrMessageEvent,):
        """ 技改"""
        return await self.plain_msg(event, self.jx3api.jigai)

    async def  jiemi(self, event: AstrMessageEvent):
        """ 解密"""
        return await self.plain_msg(event, self.jx3api.jiemi)

    async def  fubeng(self, event: AstrMessageEvent, server:str, name:str):
        """副本 服务器 角色名"""
        # Check fresh data before any image lookup, so an old screenshot cannot
        # hide new progress or an expired credential behind the 10-minute TTL.
        data = await self.jx3api.fubengjilu(server, name)
        if data["code"] != 200:
            await event.send(event.plain_result(data["msg"]))
            return
        variant = ""
        if self.cache:
            variant = self.cache.value_signature({
                "data": data["data"], "created_at": data.get("_cache", {}).get("created_at"),
            })
        async def ready():
            return data
        return await self.T2I_image_msg(event, ready, cache_name="副本CD", cache_variant=variant)

    async def  diaoluo(self, event: AstrMessageEvent, name: str,  server: str = "", limit: str = "20",):
        """ 掉落 物品 服务器 数量 """
        return await self.T2I_image_msg(event, lambda: self.jx3api.diaoluo(name, server, limit))


    async def  hong(self, event: AstrMessageEvent, kungfu: str):
        """ 宏 心法"""
        return await self.handler_plain_image_msg(
            event,
            lambda: self.jx3box.hong1(kungfu),
            lambda selected: self.jx3box.hong_select(kungfu, selected),
            result_handler=self.plain_image_msg,
        )

    async def  zili(self, event: AstrMessageEvent, server: str, name: str):
        """ 资历 角色名称 服务器"""
        return await self.handler_plain_image_msg(
            event,
            lambda: self.jx3api.zili_menu(),
            lambda selected: self.jx3api.zili(server, name, selected),
        )

    async def  jiaoyihang(self, event: AstrMessageEvent,server: str, Name: str):
        """ 交易行 服务器 物品名称"""     
        return await self.T2I_image_msg(event, lambda: self.jx3box.jiaoyihang(Name,server))


    async def bilei_add(self, event: AstrMessageEvent,name: str, text: str):
        """避雷添加 名称 备注"""
        return await self.plain_msg(
            event,
            lambda: self.bilei.add(
                event.unified_msg_origin,
                name,
                text,
                event.get_sender_name(),
            ),
        )
    
    async def bilei_all(self, event: AstrMessageEvent):
        """避雷查看"""
        return await self.T2I_image_msg(
            event,
            lambda: self.bilei.all(event.unified_msg_origin),
        )
    
    async def bilei_select(self, event: AstrMessageEvent, name:str):
        """避雷查询"""
        return await self.T2I_image_msg(
            event,
            lambda: self.bilei.select(event.unified_msg_origin, name),
        )

    async def bilei_update(self, event: AstrMessageEvent, id:int, name: str, text: str):
        """避雷修改 ID 名称 备注"""
        return await self.plain_msg(
            event,
            lambda: self.bilei.update(
                event.unified_msg_origin,
                id,
                name,
                text,
                event.get_sender_name(),
            ),
        )

    async def bilei_delete(self, event: AstrMessageEvent, id:int):
        """避雷删除 ID"""
        return await self.plain_msg(
            event,
            lambda: self.bilei.delete(event.unified_msg_origin, id),
        )

    async def team_create(
        self,
        event: AstrMessageEvent,
        team_name: str = "",
        capacity: str = "",
        *announcement_parts: str,
    ):
        """开团 团名 人数 [公告]"""
        return await self.plain_msg(
            event,
            lambda: self.team_manager.create(
                event,
                team_name,
                capacity,
                *announcement_parts,
            ),
        )

    async def team_open_registration(
        self,
        event: AstrMessageEvent,
        team_id: int = 0,
    ):
        """打开报名 团编号"""
        return await self.plain_msg(
            event,
            lambda: self.team_manager.open_registration(event, team_id),
        )

    async def team_close_registration(
        self,
        event: AstrMessageEvent,
        team_id: int = 0,
    ):
        """关闭报名 团编号"""
        return await self.plain_msg(
            event,
            lambda: self.team_manager.close_registration(event, team_id),
        )

    async def team_end(self, event: AstrMessageEvent, team_id: int = 0):
        """结束团队 团编号"""
        return await self.plain_msg(
            event,
            lambda: self.team_manager.end(event, team_id),
        )

    async def team_end_all(self, event: AstrMessageEvent):
        """结束全部团队"""
        return await self.plain_msg(
            event,
            lambda: self.team_manager.end_all(event),
        )

    async def team_clear_members(
        self,
        event: AstrMessageEvent,
        team_id: int = 0,
    ):
        """清空报名 团编号"""
        return await self.plain_msg(
            event,
            lambda: self.team_manager.clear_members(event, team_id),
        )

    async def team_signup(
        self,
        event: AstrMessageEvent,
        team_id: int = 0,
        kungfu: str = "",
        role_name: str = "",
        boss_mark: str = "",
    ):
        """报名 团编号 心法 角色名 [老板]"""
        return await self.plain_msg(
            event,
            lambda: self.team_manager.signup(
                event,
                team_id,
                kungfu,
                role_name,
                boss_mark,
            ),
        )

    async def team_cancel_signup(
        self,
        event: AstrMessageEvent,
        team_id: int = 0,
        role_name: str = "",
    ):
        """取消报名 团编号 角色名"""
        return await self.plain_msg(
            event,
            lambda: self.team_manager.cancel_signup(event, team_id, role_name),
        )

    async def team_view(
        self,
        event: AstrMessageEvent,
        team_id: int = 0,
    ):
        """查看团队 [团编号]"""
        return await self.adaptive_msg(
            event,
            lambda: self.team_manager.view(event, team_id),
        )

    async def team_blacklist(
        self,
        event: AstrMessageEvent,
        team_id: int = 0,
    ):
        """团队黑本 团编号"""
        return await self.plain_msg(
            event,
            lambda: self.team_manager.blacklist(event, team_id),
        )

    async def team_update_member(
        self,
        event: AstrMessageEvent,
        team_id: int = 0,
        role_name: str = "",
        new_role_name: str = "",
        kungfu: str = "",
        boss_mark: str = "",
    ):
        """修改报名 团编号 原角色名 新角色名 心法 [老板]"""
        return await self.plain_msg(
            event,
            lambda: self.team_manager.update_member(
                event,
                team_id,
                role_name,
                new_role_name,
                kungfu,
                boss_mark,
            ),
        )

    async def team_swap_slots(
        self,
        event: AstrMessageEvent,
        team_id: int = 0,
        first_slot: int = 0,
        second_slot: int = 0,
    ):
        """交换位置 团编号 位置1 位置2"""
        return await self.plain_msg(
            event,
            lambda: self.team_manager.swap_slots(
                event,
                team_id,
                first_slot,
                second_slot,
            ),
        )

    async def team_view_rules(
        self,
        event: AstrMessageEvent,
        scope: str = "",
    ):
        """查看限制 默认10|默认25|团编号"""
        return await self.plain_msg(
            event,
            lambda: self.team_manager.view_rules(event, scope),
        )

    async def team_add_rule(
        self,
        event: AstrMessageEvent,
        scope: str = "",
        rule_type: str = "",
        target: str = "",
        min_count: int = -1,
        max_count: int = -1,
    ):
        """添加限制 范围 职责|心法 目标 最小数量 最大数量"""
        return await self.plain_msg(
            event,
            lambda: self.team_manager.save_rule(
                event,
                scope,
                rule_type,
                target,
                min_count,
                max_count,
            ),
        )

    async def team_update_rule(
        self,
        event: AstrMessageEvent,
        scope: str = "",
        rule_id: int = 0,
        rule_type: str = "",
        target: str = "",
        min_count: int = -1,
        max_count: int = -1,
    ):
        """修改限制 范围 规则编号 职责|心法 目标 最小数量 最大数量"""
        return await self.plain_msg(
            event,
            lambda: self.team_manager.save_rule(
                event,
                scope,
                rule_type,
                target,
                min_count,
                max_count,
                rule_id,
            ),
        )

    async def team_delete_rule(
        self,
        event: AstrMessageEvent,
        scope: str = "",
        rule_id: int = 0,
    ):
        """删除限制 范围 规则编号"""
        return await self.plain_msg(
            event,
            lambda: self.team_manager.delete_rule(event, scope, rule_id),
        )

    async def team_reset_rules(
        self,
        event: AstrMessageEvent,
        scope: str = "",
    ):
        """恢复默认限制 团编号"""
        return await self.plain_msg(
            event,
            lambda: self.team_manager.reset_rules(event, scope),
        )


    async def shijian_tuisong(
        self,
        event: AstrMessageEvent,
        first: str = "",
        second: str = "",
    ):
        """管理当前会话的实时事件订阅。"""
        return_msg = await self.event_push.configure(
            event.unified_msg_origin,
            first,
            second,
        )
        await event.send(event.plain_result(return_msg))


    # ==================== 补全接口（JX3API 未覆盖部分） ====================
