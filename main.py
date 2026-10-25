import inspect
from pathlib import Path
import asyncio
import os
import re
import sys
import time
from sys import maxsize
from typing import cast

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .core.bilei_data import BiLeidata
from .core.cache import CacheService
from .core.command_hints import command_hint
from .core.event_push import EventPushService
from .core.fun_basic import load_as_base64
from .core.jx3api_data import JX3APIService
from .core.local_t2i import LocalT2I
from .core.image_gen import DoubaoImageGen
from .core.jx3box_data import JX3BOXService
from .core.kungfu_alias import KungfuAliasService
from .core.message import MessageBuilder
from .core.privacy_logging import PrivacyLogging
from .core.server_binding import ServerBindingService
from .core.session_control import SessionControlService
from .core.sqlite import AsyncSQLiteDB
from .core.team import TeamService
from .core.team_blacklist import TeamBlacklistService
from .core.team_manager import TeamManager
from .core.team_rules import TeamRuleService
from .core.webui import WebUIService

PLUGIN_NAME = "astrbot_plugin_jx3_miemie"

@register(
    "astrbot_plugin_jx3_miemie",
    "chhunshenya",
    "聚合剑网三游戏数据，提供查询、团队管理、图片渲染、本地避雷和实时事件推送。",
    "1.1.0",
    "https://github.com/chunshenya/JX3MIEMIE.git",
)
class Jx3ApiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # 获取插件配置
        self.conf = config
        self.privacy_logging = None
        if self.conf.get('privacy_logging', True):
            self.privacy_logging = PrivacyLogging()
            self.privacy_logging.install()
            logger.info('日志隐私过滤已启用：聊天正文与 Trace 不进入运行日志')

        # 指令前缀
        self.prefix = self.conf.get("prefix", {})
        prefix_text = str(self.prefix.get("text") or "").strip()
        if self.prefix.get("enable") and prefix_text:
            logger.info(f"已启用指令前缀功能，前缀为：{prefix_text}")
        elif self.prefix.get("enable"):
            logger.warning("指令前缀已开启但内容为空，将按未开启前缀处理")
        else:
            logger.info("未启用指令前缀功能。")

        # 获取数据文件路径
        self.get_data_path()
        # 加载图片base64编码
        self.load_local_base64()
        # 构造所有类
        self.create_all()
        # 注册插件管理页接口
        self.webui.register(context, PLUGIN_NAME)

        # 声明指令集
        self.command_map = {}

        logger.info("jx3api插件初始化完成")

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        # 自建渲染服务：一键自检 → 缺啥装啥 → 自动启动 → 接管渲染端点
        if self.conf.get("local_render_service", True):
            try:
                self.local_t2i = LocalT2I(
                    workdir=Path(StarTools.get_data_dir(PLUGIN_NAME)) / "t2i",
                    server_template=Path(__file__).parent / "data" / "t2i_server.py",
                    conf=self.conf,
                )
                # ★ 必须用后台方式：首次可能要装依赖，同步会卡住插件加载
                self.jx3cmd.local_t2i = self.local_t2i
                await self.local_t2i.bootstrap_background()
            except Exception:
                logger.exception("本地渲染服务初始化失败，沿用默认渲染")

        # 豆包 AI 生图（配了 doubao_api_key 才真正启用）
        try:
            self.image_gen = DoubaoImageGen(
                self.conf, Path(StarTools.get_data_dir(PLUGIN_NAME)) / "aigen"
            )
            self.jx3cmd.image_gen = self.image_gen
            logger.info(
                "豆包生图：%s", "已启用" if self.image_gen.enabled else "未配置 API Key"
            )
        except Exception:
            logger.exception("豆包生图初始化失败")
        try:
            # 数据库初始化
            await self.local_sql_db.connect()
            await self.cache.initialize()
            await self.bilei.initialize()
            await self.init_trade_item_cache_data()
            await self.kungfu_alias.initialize()
            await self.server_binding.initialize()
            await self.session_control.initialize()
            await self.team.initialize()
            await self.team_rules.initialize()

            # 获取区服目录，用于识别完整参数与区服别名。
            await self.server_binding.update_server_catalog(
                await self.jx3api.server_list()
            )

            # 开启实时事件通道
            await self.event_push.initialize()

        except Exception:
            if getattr(self, "local_t2i", None) is not None:
                await self.local_t2i.stop()
            if self.event_push is not None:
                await self.event_push.stop()
            await self.cache.stop()
            logger.exception("功能模块初始化失败")
            raise

        # 指令集
        self.ini_command_map()

        logger.info("jx3api 异步插件初始化完成")

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""

        if getattr(self, "local_t2i", None) is not None:
            await self.local_t2i.stop()

        if self.event_push:
            await self.event_push.stop()

        if self.cache:
            await self.cache.stop()

        if self.jx3api:
            await self.jx3api.close()

        if self.jx3box:
            await self.jx3box.close()

        if self.local_sql_db:
            await self.local_sql_db.close()

        logger.info("jx3api插件已卸载/停用")
        if getattr(self, 'jx3cmd', None):
            self.jx3cmd.market_choices().close()
        if getattr(self, 'privacy_logging', None):
            self.privacy_logging.close()

    def get_data_path(self):
        """获取数据文件路径"""
        # 本地数据存储路径
        self.local_data_dir = StarTools.get_data_dir("astrbot_plugin_jx3_miemie")
        # 插件数据存储路径
        self.plugin_data_dir = Path(__file__).parent / "data"
        self.plugin_temp_dir = Path(__file__).parent / "templates"

        # SQLite本地路径
        self.local_data_path = self.local_data_dir / "local_data.db"
        self.cache_image_dir = self.local_data_dir / "cache" / "images"
        self.kungfu_seed_path = self.plugin_data_dir / "kungfu.json"
        self.server_alias_seed_path = self.plugin_data_dir / "server_aliases.json"

        # 图片文件路径
        self.plugin_temp_img = self.plugin_temp_dir / "img"
        self.plugin_temp_sand = self.plugin_temp_img / "sand"
        self.plugin_temp_sect = self.plugin_temp_dir / "sect"
        self.plugin_temp_serendipity = self.plugin_temp_dir / "serendipity"

        # 数据路径打印
        logger.debug(f"本地数据路径: {self.local_data_path}")
        logger.debug(f"心法种子数据路径: {self.kungfu_seed_path}")
        logger.debug(f"区服别名种子数据路径: {self.server_alias_seed_path}")
        logger.debug(f"图片文件路径: {self.plugin_temp_img}")
        logger.debug(f"沙盘图片文件路径: {self.plugin_temp_sand}")
        logger.debug(f"图片文件路径: {self.plugin_temp_sect}")
        logger.debug(f"图片文件路径: {self.plugin_temp_serendipity}")

    def load_local_base64(self):
        """加载图片文件的base64编码"""
        img = load_as_base64(str(self.plugin_temp_img))
        sand = load_as_base64(str(self.plugin_temp_sand))
        sect = load_as_base64(str(self.plugin_temp_sect))
        serendipity = load_as_base64(str(self.plugin_temp_serendipity))
        self.icons = {
            "img": img,
            "sand": sand,
            "sect": sect,
            "serendipity": serendipity,
        }
        logger.debug("图片base64编码加载完成")

    def create_all(self):
        """构造所有类"""
        # 数据库实例化
        self.local_sql_db = AsyncSQLiteDB(str(self.local_data_path))
        self.cache = CacheService(
            self.local_sql_db,
            self.cache_image_dir,
            (self.plugin_temp_dir,),
        )
        # 剑网三功能实例化
        self.bilei = BiLeidata(self.local_sql_db)
        self.jx3api = JX3APIService(self.conf, self.local_sql_db, self.cache)
        self.jx3box = JX3BOXService(self.conf, self.local_sql_db, self.local_sql_db)
        self.kungfu_alias = KungfuAliasService(
            self.local_sql_db,
            self.kungfu_seed_path,
        )
        self.server_binding = ServerBindingService(
            self.local_sql_db,
            self.server_alias_seed_path,
        )
        self.session_control = SessionControlService(self.local_sql_db)
        self.team_rules = TeamRuleService(self.local_sql_db, self.kungfu_alias)
        self.team = TeamService(
            self.local_sql_db,
            self.kungfu_alias,
            self.team_rules,
        )
        team_config = self.conf.get("team_management", {})
        self.team_blacklist = TeamBlacklistService(
            cast(Context, self.context),
            team_config,
        )
        self.team_manager = TeamManager(
            self.team,
            self.team_rules,
            self.team_blacklist,
            team_config,
        )
        self.event_push = EventPushService(
            cast(Context, self.context),
            self.conf,
            self.local_sql_db,
            self.server_binding,
            self.session_control,
        )
        self.webui = WebUIService(
            self.jx3api,
            self.event_push,
            self.server_binding,
            self.kungfu_alias,
            self.session_control,
            self.bilei,
            self.cache,
            self.team,
            self.team_rules,
            self.icons["sect"],
        )
        self.jx3cmd = MessageBuilder(
            self.jx3api,
            self.jx3box,
            self.bilei,
            self.event_push,
            self.team_manager,
            self.icons,
            self.conf.get("image_render_quality", {}),
            self.cache,
            self.server_binding,
        )

    async def init_trade_item_cache_data(self):
        """初始化交易行物品缓存，并清理已停用的资历缓存表。"""
        await self.local_sql_db.execute("""
        CREATE TABLE IF NOT EXISTS trade_item_cache(
            key TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """)

        legacy_cache = await self.local_sql_db.fetch_one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("achievement_cache",),
        )
        if legacy_cache:
            # 旧表曾与交易行共用；仅迁移仍有效的交易行缓存后移除资历缓存。
            await self.local_sql_db.execute(
                """
                INSERT OR IGNORE INTO trade_item_cache (key, content, updated_at)
                SELECT key, content, updated_at
                FROM achievement_cache
                WHERE key = ?
                """,
                ("trade_item_groups",),
            )
            await self.local_sql_db.execute("DROP TABLE achievement_cache")

    def ini_command_map(self):
        """初始化指令集"""
        self.command_map = {
            "功能": self.jx3cmd.helps,
            "隐私说明": self.privacy_notice,
            "日常": self.jx3cmd.richang,
            "日常预测": self.jx3cmd.richangyuche,
            "穹野卫": self.jx3cmd.qiongyewei,
            "披风会": self.jx3cmd.pifenghui,
            "云从社": self.jx3cmd.yunchongshe,
            "楚天社": self.jx3cmd.chutianshe,
            "关隘": self.jx3cmd.guanaishouling,
            "赤兔": self.jx3cmd.benrichitu,
            "本周赤兔": self.jx3cmd.benzhouchitu,
            "阵营奉献": self.jx3cmd.zhenyingevent,
            "烟花": self.jx3cmd.yanhuachaxun,
            "刷马": self.jx3cmd.shuma,
            "马场": self.jx3cmd.machang,
            "战绩": self.jx3cmd.zhanji,
            "名剑排行": self.jx3cmd.mingjianpaihang,
            "名剑统计": self.jx3cmd.mingjiantongji,
            "跨服名剑": self.jx3cmd.kuafumingjian,
            "武林争霸": self.jx3cmd.wulinzhengba,
            "捕快荣誉": self.jx3cmd.bukairongyu,
            "江湖浪客": self.jx3cmd.jianghulangke,
            "决斗挑战": self.jx3cmd.juedoutiaozhan,
            "帮会排行": self.jx3cmd.banghuipaihang,
            "阵营排行": self.jx3cmd.zhenyingpaihang,
            "其他排行": self.jx3cmd.qitapaihang,
            "试炼排行": self.jx3cmd.shilianpaixing,
            "资历": self.jx3cmd.zili,
            "阵营拍卖": self.jx3cmd.zhengyingpaimai,
            "的卢": self.jx3cmd.dilujilu,
            "金价": self.jx3cmd.jinjia,
            "物价": self.jx3cmd.wujia,
            "物价选": self.jx3cmd.wujia_choice,
            "成本": self.jx3cmd.chengbeng,
            "看号": self.jx3cmd.kanhao,
            "帮战": self.jx3cmd.bangzhanjilu,
            "沙盘": self.jx3cmd.shapan,
            "诛恶": self.jx3cmd.zhueevent,
            "名片": self.jx3cmd.jueshemingpian,
            "刷新名片": self.jx3cmd.refresh_mingpian,
            "全名片": self.jx3cmd.shuoyoumingpian,
            "随机秀": self.jx3cmd.shuijimingpian,
            "奇遇": self.jx3cmd.juesheqiyu,
            "查询": self.jx3cmd.juesheqiyu,
            "未出": self.jx3cmd.weizuoqiyu,
            "汇总": self.jx3cmd.qiyuhuizong,
            "近期": self.jx3cmd.jinqiqiyu,
            "统计": self.jx3cmd.qiyutongji,
            "攻略": self.jx3cmd.qiyugonglue,
            "奇遇攻略": self.jx3cmd.qiyugonglue,
            "塔罗": self.jx3cmd.tarot_single,
            "塔罗牌": self.jx3cmd.tarot_single,
            "塔罗三张": self.jx3cmd.tarot_three,
            "今日运势": self.jx3cmd.horoscope,
            "今日塔罗": self.jx3cmd.tarot_daily,
            "星座": self.jx3cmd.horoscope,
            "星座运势": self.jx3cmd.horoscope,
            "紫微": self.jx3cmd.ziwei,
            "紫微斗数": self.jx3cmd.ziwei,
            "八字": self.jx3cmd.fortune_lot,
            "生辰八字": self.jx3cmd.fortune_lot,
            "命格签": self.jx3cmd.fortune_lot,
            "恋爱书": self.jx3cmd.love_book,
            "恋爱之书": self.jx3cmd.love_book,
            "精耐": self.jx3cmd.jingnai,
            "百战": self.jx3cmd.baizhan,
            "成就": self.jx3cmd.chengjiu,
            "角色": self.jx3cmd.jueshe,
            "角色状态": self.jx3cmd.role_state,
            "生图": self.jx3cmd.shengtu,
            "名片生图": self.jx3cmd.mingpian_aigen,
            "图生图": self.jx3cmd.tushengtu,
            "在线": self.jx3cmd.role_online,
            "绑定角色": self.jx3cmd.role_bind,
            "绑定": self.jx3cmd.role_bind,
            "解绑角色": self.jx3cmd.role_unbind,
            "解绑": self.jx3cmd.role_unbind,
            "角色列表": self.jx3cmd.role_list,
            "我的角色": self.jx3cmd.role_list,
            "在线统计": self.jx3cmd.role_online_stats,
            "角色在线": self.jx3cmd.role_online,
            "副本": self.jx3cmd.fubeng,
            "副本CD": self.jx3cmd.fubeng,
            "副本cd": self.jx3cmd.fubeng,
            "团本CD": self.jx3cmd.fubeng,
            "团本cd": self.jx3cmd.fubeng,
            "阵眼": self.jx3cmd.zhenyan,
            "配装": self.jx3cmd.peizhuang,
            "资历排行": self.jx3cmd.zilipaixing,
            "技能": self.jx3cmd.jineng,
            "奇穴": self.jx3cmd.qixue,
            "发言": self.jx3cmd.liaotian,
            "统战": self.jx3cmd.tongzhanyy,
            "小药": self.jx3cmd.xiaoyao,
            "骗子": self.jx3cmd.pianzhi,
            "花价": self.jx3cmd.huajia,
            "装饰": self.jx3cmd.zhuangshi,
            "器物": self.jx3cmd.qiwu,
            "拜师": self.jx3cmd.baishi,
            "收徒": self.jx3cmd.shoutu,
            "维护": self.jx3cmd.weihu,
            "新闻": self.jx3cmd.xinwen,
            "招募": self.jx3cmd.tuanduizhaomu,
            "团长": self.jx3cmd.tuanzhang,
            "团牌": self.jx3cmd.tuanpai,
            "答案之书": self.jx3cmd.daanzhishu,
            "舔狗语录": self.jx3cmd.tiangou,
            "疯狂星期四": self.jx3cmd.fkxq4,
            "彩虹屁": self.jx3cmd.caihongpi,
            "毒鸡汤": self.jx3cmd.dujitang,
            "朋友圈": self.jx3cmd.pengyouquan,
            "喝什么": self.jx3cmd.heshengme,
            "吃什么": self.jx3cmd.chishengme,
            "骚话": self.jx3cmd.shaohua,
            "渣男语录": self.jx3cmd.zhananyulu,
            "贴吧物价": self.jx3cmd.tiebawujia,
            "818": self.jx3cmd.bagua,
            "科举": self.jx3cmd.keju,
            "区服": self.jx3cmd.zhuangtai,
            "开服": self.jx3cmd.kaifu,
            "技改": self.jx3cmd.jigai,
            "解密": self.jx3cmd.jiemi,
            "掉落": self.jx3cmd.diaoluo,
            "宏": self.jx3cmd.hong,
            "交易行": self.jx3cmd.jiaoyihang,
            "绑定区服": self.bind_server,
            "解绑区服": self.unbind_server,
            "事件推送": self.jx3cmd.shijian_tuisong,
            "避雷添加": self.jx3cmd.bilei_add,
            "避雷查看": self.jx3cmd.bilei_all,
            "避雷查询": self.jx3cmd.bilei_select,
            "避雷修改": self.jx3cmd.bilei_update,
            "避雷删除": self.jx3cmd.bilei_delete,
            "开团": self.jx3cmd.team_create,
            "打开报名": self.jx3cmd.team_open_registration,
            "关闭报名": self.jx3cmd.team_close_registration,
            "关闭报警": self.jx3cmd.team_close_registration,
            "结束团队": self.jx3cmd.team_end,
            "结束全部团队": self.jx3cmd.team_end_all,
            "清空报名": self.jx3cmd.team_clear_members,
            "报名": self.jx3cmd.team_signup,
            "取消报名": self.jx3cmd.team_cancel_signup,
            "查看团队": self.jx3cmd.team_view,
            "团队黑本": self.jx3cmd.team_blacklist,
            "修改报名": self.jx3cmd.team_update_member,
            "交换位置": self.jx3cmd.team_swap_slots,
            "查看限制": self.jx3cmd.team_view_rules,
            "添加限制": self.jx3cmd.team_add_rule,
            "修改限制": self.jx3cmd.team_update_rule,
            "删除限制": self.jx3cmd.team_delete_rule,
            "恢复默认限制": self.jx3cmd.team_reset_rules,
        }
        self.cache.register_image_names(
            command_name
            for command_name, handler in self.command_map.items()
            if handler.__name__ in MessageBuilder.IMAGE_RENDER_HANDLERS
        )

    def parse_message(self, text: str) -> list[str] | None:
        """消息解析"""
        text = text.strip()
        if not text:
            return None

        # 前缀模式
        if self.prefix.get("enable"):
            prefix = str(self.prefix.get("text") or "").strip()
            if prefix and text.startswith(prefix):
                text = text[len(prefix) :].strip()
            elif prefix:
                # 非前缀消息，直接忽略
                return None

        return text.split()

    async def privacy_notice(self, event: AstrMessageEvent):
        """隐私说明"""
        if self.privacy_logging and self.privacy_logging.targets:
            status = "已开启日志隐私保护：聊天收发正文和 Trace 不进入 AstrBot 控制台、后台日志面板及日志文件。运行日志保留指令名、耗时、异常类型和代码位置。"
        else:
            status = "当前未开启日志隐私保护，聊天内容可能出现在 AstrBot 运行日志中。"
        await event.send(event.plain_result(
            status + "\n\n外观选项只保存在内存中，2 分钟后清除。区服绑定、团队报名和查询缓存按正常功能保存。"
            "\n\n此设置从开启时生效。已有历史日志、消息平台和其他插件的数据仍由各自规则管理。"
        ))

    def resolve_command(self, event: AstrMessageEvent):
        """从平台处理后文本和平台原始文本中识别本插件指令。"""
        processed_text = event.message_str
        original_text = getattr(event.message_obj, "message_str", "")
        seen = set()

        for text in (processed_text, original_text):
            if not isinstance(text, str):
                continue

            text = text.strip()
            if not text or text in seen:
                continue
            seen.add(text)

            builder = getattr(self, 'jx3cmd', None)
            if builder and builder.market_choices().numeric_reply(event, text):
                return '物价选', [text], self.command_map['物价选']

            parts = self.parse_message(text)
            if not parts:
                continue

            cmd, *args = parts
            selection = re.fullmatch(r'物价选\s*(\d+|默认|取消)', ' '.join(parts))
            if selection:
                return '物价选', [selection.group(1)], self.command_map['物价选']
            handler = self.command_map.get(cmd)
            if handler:
                return cmd, args, handler

        return None

    def suggest_command(self, event: AstrMessageEvent) -> str:
        for text in (event.message_str, getattr(event.message_obj, "message_str", "")):
            if not isinstance(text, str):
                continue
            hint = command_hint(self.parse_message(text) or [], self.command_map)
            if hint:
                return hint
        return ""

    async def _prepare_server_args(
        self,
        handler,
        event: AstrMessageEvent,
        args: list[str],
    ) -> list[str]:
        """补齐会话绑定、解析区服别名，并支持用“全区”显式传空区服。"""
        params = [
            parameter
            for parameter in inspect.signature(handler).parameters.values()
            if parameter.name not in {"self", "event"}
        ]
        server_index = next(
            (
                index
                for index, parameter in enumerate(params)
                if parameter.name == "server"
            ),
            None,
        )
        if server_index is None:
            return args

        prepared = list(args)
        bound_server = await self.server_binding.get_binding(event.unified_msg_origin)
        has_server_arg = server_index < len(prepared)

        if bound_server:
            explicit_server = has_server_arg and (
                self.server_binding.is_all_servers_query(prepared[server_index])
                or self.server_binding.is_known_server(prepared[server_index])
            )
            if explicit_server:
                prepared[server_index] = self.server_binding.resolve_query_server(
                    prepared[server_index]
                )
            else:
                prepared.insert(server_index, bound_server)
        elif has_server_arg:
            prepared[server_index] = self.server_binding.resolve_query_server(
                prepared[server_index]
            )

        return prepared

    def _prepare_kungfu_args(self, handler, args: list[str]) -> list[str]:
        """Resolve the kungfu parameter through the global alias catalog.

        Args:
            handler: Command handler whose signature defines argument positions.
            args: Positional command arguments after server preparation.

        Returns:
            Arguments with a recognized kungfu alias replaced by its canonical name.
        """
        params = [
            parameter
            for parameter in inspect.signature(handler).parameters.values()
            if parameter.name not in {"self", "event"}
        ]
        kungfu_index = next(
            (
                index
                for index, parameter in enumerate(params)
                if parameter.name == "kungfu"
            ),
            None,
        )
        if kungfu_index is None or kungfu_index >= len(args):
            return args

        prepared = list(args)
        prepared[kungfu_index] = self.kungfu_alias.resolve_kungfu(
            prepared[kungfu_index]
        )
        return prepared

    async def _call_with_auto_args(
        self, handler, event: AstrMessageEvent, args: list[str]
    ):
        """指令执行函数"""
        sig = inspect.signature(handler)
        params = list(sig.parameters.values())

        call_args = []
        arg_index = 0

        for p in params:
            if p.name == "self":
                continue

            if p.name == "event":
                call_args.append(event)
                continue

            if p.kind is inspect.Parameter.VAR_POSITIONAL:
                call_args.extend(args[arg_index:])
                arg_index = len(args)
                continue

            if arg_index < len(args):
                raw = args[arg_index]
                arg_index += 1
                try:
                    if p.annotation is int:
                        call_args.append(int(raw))
                    elif p.annotation is float:
                        call_args.append(float(raw))
                    else:
                        call_args.append(raw)
                except Exception:
                    call_args.append(p.default)
            else:
                if p.default is not inspect._empty:
                    call_args.append(p.default)
                else:
                    raise ValueError(f"缺少参数: {p.name}")

        # 只允许 coroutine
        return await handler(*call_args)


    @staticmethod
    def _explain_failure(cmd, handler, error) -> str:
        """把兜底报错换成人能看懂、并且能自己解决的提示。"""
        usage = ""
        doc = (getattr(handler, "__doc__", "") or "").strip().splitlines()
        if doc and doc[0].strip():
            usage = f"\n用法：{doc[0].strip()}"

        text = str(error or "")
        missing = re.findall(r"缺少参数[:：]\s*([A-Za-z_][\w]*)", text)
        if missing:
            labels = {"server": "服务器", "name": "角色名" if "角色" in usage else "名称",
                      "Name": "物品名称", "mode": "模式", "limit": "数量", "id": "编号",
                      "kungfu": "心法"}
            names = "、".join(labels.get(name, name) for name in dict.fromkeys(missing))
            tip = f"「{cmd}」缺少参数：{names}{usage}"
            if "server" in missing:
                tip += (
                    "\n提示：先发「绑定区服 区服名」绑定默认区服，"
                    "之后就不用每次填区服了。"
                )
            return tip

        return f"「{cmd}」执行失败：{text[:120]}{usage}\n（详细原因已记入日志）"

    @filter.event_message_type(
        filter.EventMessageType.ALL,
        priority=maxsize - 10,
    )
    async def on_all_message(self, event: AstrMessageEvent):
        """解析所有消息"""
        if not self.command_map:
            logger.debug("插件尚未初始化完成，忽略消息")
            return

        command = self.resolve_command(event)
        if not command:
            hint = self.suggest_command(event)
            if hint and self.session_control.is_allowed(event.unified_msg_origin):
                event.stop_event()
                event.should_call_llm(True)
                yield event.plain_result(hint)
                return
            logger.debug("未触发指令，忽略消息")
            return

        cmd, args, handler = command

        # 一旦确认是本插件指令，立即阻止后续插件和默认 LLM 继续处理。
        event.stop_event()
        event.should_call_llm(True)

        if not self.session_control.is_allowed(event.unified_msg_origin):
            logger.info(
                f"会话控制已拦截插件指令：command={cmd}, "
                f"session={event.unified_msg_origin}"
            )
            return

        command_token = None
        started = time.perf_counter()
        try:
            args = await self._prepare_server_args(handler, event, args)
            args = self._prepare_kungfu_args(handler, args)
            command_token = self.cache.enter_command(cmd, args)
            ret = await self._call_with_auto_args(handler, event, args)
            if ret is not None:
                yield ret
        except Exception as e:
            logger.exception(f"指令执行失败: {cmd}, error={e}")
            yield event.plain_result(self._explain_failure(cmd, handler, e))
        finally:
            if command_token is not None:
                self.cache.leave_command(command_token)
            logger.info("指令耗时 command=%s total_ms=%.0f", cmd, (time.perf_counter() - started) * 1000)

    async def bind_server(
        self,
        event: AstrMessageEvent,
        server_name: str = "",
    ):
        """查看或设置当前会话绑定区服。"""
        session_id = event.unified_msg_origin
        if not server_name.strip():
            bound_server = await self.server_binding.get_binding(session_id)
            text = (
                f"当前会话绑定区服：{bound_server}"
                if bound_server
                else "当前会话尚未绑定区服。\n用法：绑定区服 区服名"
            )
        else:
            server = self.server_binding.resolve_server(server_name)
            await self.server_binding.set_binding(session_id, server)
            text = f"当前会话已绑定区服：{server}"
        await event.send(event.plain_result(text))

    async def unbind_server(self, event: AstrMessageEvent):
        """解除当前会话的区服绑定。"""
        await self.server_binding.delete_binding(event.unified_msg_origin)
        await event.send(event.plain_result("当前会话已解除区服绑定。"))
