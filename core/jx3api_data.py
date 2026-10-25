import json
import pathlib
import math
import html
import re
import hashlib
import asyncio
import time
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit, urlencode
from typing import TYPE_CHECKING, Dict, Any, Optional, List, Union
from inspect import isawaitable
from typing import Any, Awaitable, Callable, Dict, Optional

from astrbot.api import logger
from astrbot.api import AstrBotConfig
import astrbot.api.message_components as Comp

from .market_design import market_page, make_fused_chart
from .market_sources import AiJX3MarketSource, enrich_product
from .market_choices import build_menu, normalize_query
from .request import APIClient, APIErrorResponse
from .role_status import parse_game_online
from .tuilan_cd import CD_CACHE_ENDPOINT, TuilanCDClient, TuilanCDError, build_cd_page
from .sqlite import AsyncSQLiteDB
from .fun_basic import load_template,gold_to_parts,week_to_num,compare_date_str,format_time,format_remaining

if TYPE_CHECKING:
    from .cache import CacheService


ROLE_RANK_NAMES = {
    "名士五十强",
    "老江湖五十强",
    "兵甲藏家五十强",
    "名师五十强",
    "阵营英雄五十强",
    "薪火相传五十强",
    "庐园广记一百强",
}
TONG_RANK_NAMES0 = {
    "赛季恶人五十强",
    "赛季浩气五十强",
    "本周恶人五十强",
    "本周浩气五十强",
}
TONG_RANK_NAMES1 = {
    "浩气神兵宝甲五十强",
    "恶人神兵宝甲五十强",
    "浩气爱心帮会五十强",
    "恶人爱心帮会五十强",
}
TONG_RANK_NAMES2 = {
    "上周恶人五十强",
    "上周浩气五十强",
}

GUILD_RANK_OPTIONS = {
    "1": "浩气神兵宝甲五十强",
    "2": "恶人神兵宝甲五十强",
    "3": "浩气爱心帮会五十强",
    "4": "恶人爱心帮会五十强",
}
CAMP_RANK_OPTIONS = {
    "1": "赛季恶人五十强",
    "2": "赛季浩气五十强",
    "3": "上周恶人五十强",
    "4": "上周浩气五十强",
    "5": "本周恶人五十强",
    "6": "本周浩气五十强",
}
OTHER_RANK_OPTIONS = {
    "1": "名士五十强",
    "2": "老江湖五十强",
    "3": "兵甲藏家五十强",
    "4": "名师五十强",
    "5": "阵营英雄五十强",
    "6": "薪火相传五十强",
    "7": "庐园广记一百强",
}

RANK_NAMES = frozenset().union(
    ROLE_RANK_NAMES,
    TONG_RANK_NAMES0,
    TONG_RANK_NAMES1,
    TONG_RANK_NAMES2,
)


class JX3APIService:
    def __init__(
        self,
        config: AstrBotConfig,
        sqlite: AsyncSQLiteDB,
        cache: Optional["CacheService"] = None,
    ):
        # 引用插件配置文件
        self._config = config
        # 仅显式配置为 false 时关闭证书验证，旧配置默认安全开启。
        self._api: APIClient = APIClient(
            ssl_verify=self._config.get("tls_verify", True) is not False
        )
        # 引用sqlite
        self._sql_db = sqlite
        self._cache = cache
        self._market_source = AiJX3MarketSource(
            cache, ssl_verify=self._config.get("tls_verify", True) is not False
        )
        self._tuilan_cd = TuilanCDClient()
        self._token_stats_cache: tuple[float, Dict[str, Any]] | None = None
        self._token_stats_lock = asyncio.Lock()
        # 获取配置中的 Token
        self.token = self._config.get("jx3api_token", "")
        if  self.token == "":
            logger.warning("获取配置token失败，请正确填写token,否则部分功能无法正常使用")
        else:
            logger.debug(f"获取配置token成功。")
        # 获取配置中的 ticket
        self.ticket = self._config.get("jx3api_ticket", "")
        if  self.ticket == "":
            logger.warning("获取配置ticket失败，请正确填写ticket,否则部分功能无法正常使用")
        else:
            logger.debug(f"获取配置ticket成功。")
        

    async def close(self):
        """释放底层 APIClient 资源"""
        if self._api:
            await self._api.close()
        await self._market_source.close()
        await self._tuilan_cd.close()

    async def server_list(self, force_refresh: bool = False) -> list[str]:
        """获取当前有效区服名称，供会话绑定和参数消歧使用。"""
        data, _ = await self._cached_request(
            "/server/status/check",
            {"server": "", "type": "其他"},
            force_refresh=force_refresh,
        )
        if not isinstance(data, list):
            return []
        return sorted(
            {
                str(item.get("server") or "").strip()
                for item in data
                if isinstance(item, dict) and item.get("server")
            }
        )

    async def token_stats(self) -> Optional[Dict[str, Any]]:
        """读取令牌统计；短时内存复用，避免 WebUI 保存配置时重复请求。"""
        if not str(self.token or "").strip():
            return None
        now = time.monotonic()
        if self._token_stats_cache and self._token_stats_cache[0] > now:
            return dict(self._token_stats_cache[1])

        async with self._token_stats_lock:
            now = time.monotonic()
            if self._token_stats_cache and self._token_stats_cache[0] > now:
                return dict(self._token_stats_cache[1])
            result = await self._fetch_token_stats()
            if result is not None:
                self._token_stats_cache = (now + 30, dict(result))
            return result

    async def _fetch_token_stats(self) -> Optional[Dict[str, Any]]:
        """查询当前配置 JX3API Token 的等级、用量及有效状态。"""
        if not str(self.token or "").strip():
            return None

        async def requester():
            return await self._api.post(
                "https://www.jx3api.com/token/stats",
                data={"token": self.token},
                out_key="data",
                success_codes=(200, "200"),
                return_error=True,
            )

        try:
            data = await requester()
        except Exception as exc:
            logger.warning(f"查询 JX3API Token 统计失败: {exc}")
            return None

        if isinstance(data, APIErrorResponse):
            logger.warning(
                f"JX3API Token 统计返回错误: "
                f"code={data.code}, msg={data.message or '未知错误'}"
            )
            return None
        if not isinstance(data, dict):
            return None

        def nonnegative_int(value: Any) -> Optional[int]:
            if isinstance(value, bool):
                return None
            try:
                number = int(value)
            except (TypeError, ValueError):
                return None
            return number if number >= 0 else None

        valid = data.get("valid")
        return {
            "level": nonnegative_int(data.get("level")),
            "used": nonnegative_int(data.get("used")),
            "remaining": nonnegative_int(data.get("remaining")),
            "valid": valid if isinstance(valid, bool) else None,
        }


    def _init_return_data(self) -> Dict[str, Any]:
            """初始化标准的返回数据结构"""
            return {
                "code": 0,
                "msg": "功能函数未执行",
                "data": {},
                "temp": "",
                "icons": {}
            }

    
    async def _base_request(
        self, 
        api_path: str, 
        params: Optional[Dict[str, Any]] = None, 
        out: Optional[str] = "data"
    ) -> Optional[Any]:
        """
        基础请求封装，处理配置获取和API调用。
        """
        try:
            if not self._api:
                logger.error("API client is not initialized")
                return None

            base_url = "https://www.jx3api.com"
            api_url = base_url + api_path
            data = await self._api.get(
                api_url,
                params=params,
                out_key=out,
                success_codes=(200, "200"),
                return_error=True,
            )
            
            if not data:
                logger.warning(f"获取接口信息失败或返回空数据: {api_url}")
            
            return data
            
        except Exception as e:
            logger.error(f"基础请求调用出错 ({api_path}): {e}")
            return None

    @staticmethod
    def _is_cacheable_response(data: Any) -> bool:
        return data is not None and not isinstance(data, APIErrorResponse)

    async def _cached_request(
        self,
        api_path: str,
        params: Optional[Dict[str, Any]] = None,
        out: Optional[str] = "data",
        force_refresh: bool = False,
    ) -> tuple[Any, dict[str, Any]]:
        request_params = params or {}
        if not self._cache:
            return await self._base_request(api_path, request_params, out), {
                "endpoint": api_path,
                "hit": False,
                "stale": False,
                "ttl_seconds": 0,
            }

        cache_params = dict(request_params)
        credential_values = [
            str(value)
            for key, value in request_params.items()
            if str(key).lower() in {"token", "ticket"} and value
        ]
        if credential_values:
            cache_params["__credential_scope"] = hashlib.sha256(
                "|".join(credential_values).encode("utf-8")
            ).hexdigest()

        try:
            return await self._cache.request_api(
                api_path,
                cache_params,
                lambda: self._base_request(api_path, request_params, out),
                self._is_cacheable_response,
                force_refresh=force_refresh,
            )
        except Exception as exc:
            logger.warning(f"接口缓存不可用，直接请求 JX3API endpoint={api_path}: {exc}")
            return await self._base_request(api_path, request_params, out), {
                "endpoint": api_path,
                "hit": False,
                "stale": False,
                "ttl_seconds": 0,
            }


    async def _request_api(
        self,
        path: str,
        params: Dict[str, Any],
        processor: Optional[
            Callable[[Any, Dict[str, Any]], Any | Awaitable[Any]]
        ] = None,
        template: Optional[str] = None,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """通用接口请求与模板处理。"""
        return_data = self._init_return_data()

        data, cache_metadata = await self._cached_request(
            path, params, force_refresh=force_refresh
        )
        return_data["_cache"] = cache_metadata
        if isinstance(data, APIErrorResponse):
            return_data["msg"] = data.message or "获取接口信息失败"
            return return_data
        if data is None:
            return_data["msg"] = "获取接口信息失败"
            return return_data

        try:
            await processor(data, return_data)
        except Exception as e:
            logger.exception(f"数据处理时出错: {e}")
            return_data["msg"] = "处理接口返回信息时出错"
            return return_data

        # template 为空时不加载模板
        if template:
            try:
                return_data["temp"] = await load_template(template)
            except FileNotFoundError as e:
                logger.error(f"加载模板失败: {e}")
                return_data["msg"] = "系统错误：模板文件不存在"
                return return_data

        return_data["code"] = 200
        return return_data


    # --- 业务功能函数 ---
    async def helps(self) -> Dict[str, Any]:
        """帮助"""
        return_data = self._init_return_data()
        
        # 加载模板
        try:
            return_data["temp"] = await load_template("helps.html")
        except FileNotFoundError as e:
            logger.error(f"加载模板失败: {e}")
            return_data["msg"] = "系统错误：模板文件不存在"
            return return_data
            
        return_data["code"] = 200
   
        return return_data


    async def richang(self, mode: str, num: int) -> Dict[str, Any]:
        """活动日历"""
        # 数据处理
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            if mode == "list":
                today = data.get("today") or {}
                total = data.get("total") or []

                # ★ 修正：用「列表第一天的星期」补空格。
                #   原实现用 today 的星期，会导致整张日历右移一格
                #   （例如 09-07 是周一，却被排到周二列）。
                first_date = ""
                for _item in total:
                    if isinstance(_item, dict) and _item.get("date"):
                        first_date = _item["date"]
                        break
                try:
                    pad = (
                        datetime.strptime(first_date, "%Y-%m-%d").weekday()
                        if first_date
                        else 0
                    )
                except (TypeError, ValueError):
                    pad = week_to_num(today.get("week", "")) or 0

                items = [
                    {
                        "en": False,
                        "compare": "",
                        "date": "",
                        "war": "",
                        "battle": "",
                    }
                    for _ in range(pad)
                ]

                items.extend(
                    {
                        "en": True,
                        "compare": compare_date_str(item.get("date", "")),
                        "date": item.get("date", ""),
                        "war": item.get("war", ""),
                        "battle": item.get("battle", ""),
                    }
                    for item in total
                    if isinstance(item, dict)
                )

                return_data["data"]["items"] = items
                return_data["data"]["today"] = today
                return

            weekly = data.get("weekly") or {}

            return_data["data"] = (
                f"{data.get('date', '')} 星期{data.get('week', '')}\n"
                f"大战：{data.get('war', '')}\n"
                f"战场：{data.get('battle', '')}\n"
                f"阵营：{data.get('orecar', '')}\n"
                f"宗门：{data.get('school', '')}\n"
                f"驰援：{data.get('rescue', '')}\n"
                f"【宠物福缘】\n{', '.join(data.get('lucky') or [])}\n"
                f"【家园声望·加倍道具】\n{', '.join(data.get('card') or [])}\n"
                f"【武林通鉴·公共任务】\n{', '.join(weekly.get('conn') or [])}\n"
                f"【武林通鉴·团队秘境】\n{', '.join(weekly.get('raid') or [])}\n"
            )

        return await self._request_api(
            path="/active/calendar",
            params={"mode": mode, "num": num},
            processor=processor,
            template="richangyuche.html" if mode == "list" else None,
        )

    
    async def xingxiashijian(self,name: str) -> Dict[str, Any]:
        """地图活动"""
        # 数据处理
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            return_data["data"]["items"] = data
            return_data["data"]["name"] = name

        return await self._request_api(
            path="/active/celebs",
            params={ "name": name},
            processor=processor,
            template="xingxiashijian.html"
        )


    async def guanaishouling(self) -> Dict[str, Any]:
        """关隘首领"""
        # 数据处理
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            groups = [
                {
                    "server": group.get("server", ""),
                    "records": [
                        {
                            "camp_name": item.get("campName", ""),
                            "castle": item.get("castle", ""),
                            "str_status": item.get("statusText", ""),
                            "start_time": format_time(item.get("startTime")),
                            "end_time": format_time(item.get("endTime")),
                            "remaining_time": format_remaining(item.get("endTime")),
                        }
                        for item in group.get("data", [])
                        if isinstance(item, dict)
                    ],
                }
                for group in data
                if isinstance(group, dict) and group.get("data")
            ]

            groups = [group for group in groups if group["records"]]

            if not groups:
                return_data["msg"] = "未查询到关隘首领信息"
                return return_data

            return_data["data"] = {
                "groups": groups,
                "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

        return await self._request_api(
            path="/castle/status",
            params={"token": self.token},
            processor=processor,
            template="guanaishouling.html"
        )


    async def benrichitu(self) -> Dict[str, Any]:
        """本日赤兔"""
        # 数据处理
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            if not data or not isinstance(data, list):
                return_data["data"] = "今日赤兔 暂无数据"
                return 
            
            result_lines = ["今日赤兔"]
            for item in data:
                result_lines.extend([
                    f"时间：{item['date']}",
                    f"区服：{item['server']}",
                    f"地图：{item['mapName']}",
                ])

            return_data["data"] = "\n".join(result_lines).rstrip()
            
        return await self._request_api(
            path="/chitu/records",
            params={"token": self.token},
            processor=processor,
            template=""
        )        


    async def benzhouchitu(self) -> Dict[str, Any]:
        """本周赤兔"""
        # 数据处理
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            if not data or not isinstance(data, list):
                return_data["data"] = "本周赤兔 暂无数据"
                return 
            
            result_lines = ["本周赤兔"]
            for item in data:
                result_lines.extend([
                    f"时间：{item['date']}",
                    f"区服：{item['server']}",
                    f"地图：{item['mapName']}",
                ])

            return_data["data"] = "\n".join(result_lines).rstrip()
            
        return await self._request_api(
            path="/chitu/week/records",
            params={"token": self.token},
            processor=processor,
            template=""
        )  


    async def zhenyingevent(self,name: str,limit: str) -> Dict[str, Any]:
        """阵营事件"""
        # 数据处理
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            for item in data:
                item["seizeTime"] = format_time(item.get("seizeTime"))

            return_data["data"] = {
                "items": data,
                "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
        return await self._request_api(
            path="/fenxian/records",
            params={"token": self.token,"name": name, "limit": limit},
            processor=processor,
            template="zhenyingevent.html"
        )  
            

    async def yanhuachaxun(self, server: str, name: str, limit: int) -> Dict[str, Any]:
        """烟花记录"""
        # 数据处理
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            data = data[:limit]
            for item in data:
                item["time"] = format_time(item.get("time"))

            return_data["data"]["list"] = data
            
        return await self._request_api(
            path="/firework/records",
            params={"token": self.token,"name": name, "server": server},
            processor=processor,
            template="yanhuan.html"
        )  


    async def shuma(self,server:str) -> Dict[str, Any]:
        """刷马"""
        # 数据处理
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:    
            return_data["data"] = (
                f"【阴山大草原】\n{', '.join(data.get('阴山大草原') or [])}\n"
                f"【鲲鹏岛】\n{', '.join(data.get('鲲鹏岛') or [])}\n"
                f"【黑戈壁】\n{', '.join(data.get('黑戈壁') or [])}\n"
            )
            
        return await self._request_api(
            path="/ranch/chat",
            params={"token": self.token, "server": server},
            processor=processor,
            template=""
        )  


    async def machang(self, server: str, expired: int) -> Dict[str, Any]:
        """马场"""
        # 数据处理
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            data0 = data.get("data") 
            return_data["data"] = (
                f"【区服】：{data.get('server')}\n"
                f"【阴山大草原】\n{', '.join(data0.get('阴山大草原') or [])}\n"
                f"【鲲鹏岛】\n{', '.join(data0.get('鲲鹏岛') or [])}\n"
                f"【黑戈壁】\n{', '.join(data0.get('黑戈壁') or [])}\n"
                f"【龙泉府 / 进图（21:10）】\n{', '.join(data0.get('龙泉府 / 进图（21:10）') or [])}\n"
                f"\n{data.get('note')}\n"
            )
            
        return await self._request_api(
            path="/ranch/records",
            params={"token": self.token, "server": server, "expired": expired},
            processor=processor,
            template=""
        )  


    async def zhanji(self, name: str, server:str, mode:str) -> Dict[str, Any]:
        """战绩"""
        # 数据处理
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            performance_key = {
                "22": "2v2",
                "33": "3v3",
                "55": "5v5",
            }.get(str(mode), "3v3")
            performance = data.get("performance") or {}
            current = performance.get(performance_key) if isinstance(performance, dict) else None
            return_data["data"] = dict(
                data, currentPerformance=current if isinstance(current, dict) else {},
                mode_name=performance_key, history=data.get("history") or [],
            )
            
        return await self._request_api(
            path="/arena/recent",
            params={"server": server, "name":name, "mode":mode, "token": self.token, "ticket": self.ticket},
            processor=processor,
            template="zhanji.html"
        )  


    async def mingjianpaihang(self, limit: str, mode:str) -> Dict[str, Any]:
        """名剑排行"""
        # 数据处理
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            return_data["data"]["lists"] = data
            return_data["data"]["mode"] = mode
            
        return await self._request_api(
            path="/arena/awesome",
            params={"limit": limit, "mode":mode, "token": self.token, "ticket": self.ticket},
            processor=processor,
            template="mingjianpaihang.html"
        )          


    async def mingjiantongji(self, mode: str) -> Dict[str, Any]:
        """名剑统计"""
        # 数据处理
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            return_data["data"] = {
                "items": data,
                "mode": mode,
                "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
        return await self._request_api(
            path="/arena/schools",
            params={"mode": mode, "token": self.token, "ticket": self.ticket},
            processor=processor,
            template="mingjiantongji.html"
        )         

    async def kuafumingjian(self, server: str, mode: int = 1) -> Dict[str, Any]:
        """跨服名剑榜。"""
        mode_names = {0: "2v2", 1: "3v3", 2: "5v5"}

        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            items = data.get("data") or []
            if not isinstance(items, list):
                items = []

            return_data["data"] = {
                "items": [item for item in items if isinstance(item, dict)],
                "name": data.get("name") or "跨服名剑榜",
                "server": data.get("server") or server or "全区",
                "mode_name": mode_names[mode],
                "update_time": format_time(data.get("time")),
            }

        return await self._request_api(
            path="/rank/arena",
            params={"server": server, "mode": mode, "token": self.token},
            processor=processor,
            template="kuafumingjian.html",
        )

    async def wulinzhengba(self, server: str, camp: int = 1) -> Dict[str, Any]:
        """武林争霸赛帮会榜。"""
        camp_names = {1: "浩气盟", 2: "恶人谷"}

        def format_match_time(value: Any) -> str:
            try:
                total_seconds = int(value)
            except (TypeError, ValueError):
                return ""
            if total_seconds < 0:
                return ""

            hours, remainder = divmod(total_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            if hours:
                return f"{hours}时{minutes:02d}分{seconds:02d}秒"
            return f"{minutes}分{seconds:02d}秒"

        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            items = data.get("data") or []
            if not isinstance(items, list):
                items = []

            rank_items = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                rank_item = item.copy()
                rank_item["lastMatchTimeText"] = format_match_time(
                    item.get("lastMatchTime")
                )
                rank_items.append(rank_item)

            return_data["data"] = {
                "items": rank_items,
                "name": data.get("name") or "武林争霸赛",
                "server": data.get("server") or server or "全区",
                "camp_name": camp_names[camp],
                "update_time": format_time(data.get("time")),
            }

        return await self._request_api(
            path="/rank/championship",
            params={"server": server, "camp": camp, "token": self.token},
            processor=processor,
            template="wulinzhengba.html",
        )

    async def _bounty_rank(self,server: str,path: str,fallback_name: str,show_hostile_count: bool,) -> Dict[str, Any]:
        """处理捕快荣誉和江湖浪客共用的榜单结构。"""

        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            items = data.get("data") or []
            if not isinstance(items, list):
                items = []

            return_data["data"] = {
                "items": [item for item in items if isinstance(item, dict)],
                "name": data.get("name") or fallback_name,
                "server": data.get("server") or server or "全区",
                "show_hostile_count": show_hostile_count,
                "update_time": format_time(data.get("time")),
            }

        return await self._request_api(
            path=path,
            params={"server": server, "token": self.token},
            processor=processor,
            template="bounty_rank.html",
        )

    async def bukairongyu(self, server: str) -> Dict[str, Any]:
        """捕快荣誉榜。"""
        return await self._bounty_rank(
            server=server,
            path="/rank/constable",
            fallback_name="捕快荣誉榜",
            show_hostile_count=False,
        )

    async def jianghulangke(self, server: str) -> Dict[str, Any]:
        """江湖浪客榜。"""
        return await self._bounty_rank(
            server=server,
            path="/rank/outlaw",
            fallback_name="江湖浪客榜",
            show_hostile_count=True,
        )

    async def juedoutiaozhan(self,server: str,mode: int = 1,) -> Dict[str, Any]:
        """决斗挑战榜。"""
        mode_names = {1: "公开", 2: "私密"}

        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            items = data.get("data") or []
            if not isinstance(items, list):
                items = []

            now_timestamp = int(datetime.now().timestamp())
            rank_items = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                rank_item = item.copy()
                try:
                    timeout = int(item.get("timeOut"))
                except (TypeError, ValueError):
                    remaining_time = ""
                else:
                    remaining_time = (
                        format_remaining(timeout)
                        if timeout > now_timestamp
                        else "已结束"
                    )
                rank_item["remainingTime"] = remaining_time
                rank_items.append(rank_item)

            return_data["data"] = {
                "items": rank_items,
                "name": data.get("name") or "决斗挑战榜",
                "server": data.get("server") or server or "全区",
                "mode_name": mode_names[mode],
                "update_time": format_time(data.get("time")),
            }

        return await self._request_api(
            path="/rank/wanted",
            params={"server": server, "mode": mode, "token": self.token},
            processor=processor,
            template="juedoutiaozhan.html",
        )


    async def banghui_rank_menu(self) -> Dict[str, str]:
        """帮会排行榜选择内容。"""
        return GUILD_RANK_OPTIONS.copy()

    async def zhenying_rank_menu(self) -> Dict[str, str]:
        """阵营排行榜选择内容。"""
        return CAMP_RANK_OPTIONS.copy()

    async def qita_rank_menu(self) -> Dict[str, str]:
        """其他排行榜选择内容。"""
        return OTHER_RANK_OPTIONS.copy()

    async def rank_statistical_select(self, server: str, selected: Dict[str, Any],) -> Dict[str, Any]:
        """排行榜次轮：根据单项选择数据查询对应榜单。"""
        if not isinstance(selected, dict) or len(selected) != 1:
            return_data = self._init_return_data()
            return_data["msg"] = "排行榜选项数据格式异常"
            return return_data

        rank_name = str(next(iter(selected.values())) or "").strip()
        if rank_name not in RANK_NAMES:
            return_data = self._init_return_data()
            return_data["msg"] = "无效排行榜选项"
            return return_data

        return await self.rank_statistical(rank_name, server)

    async def rank_statistical(self, name: str, server: str) -> Dict[str, Any]:
        """排行榜单"""

        if name in ROLE_RANK_NAMES:
            template_name = "rank_role.html"
        elif name in TONG_RANK_NAMES0:
            template_name = "rank_tong0.html"
        elif name in TONG_RANK_NAMES1:
            template_name = "rank_tong1.html"
        elif name in TONG_RANK_NAMES2:
            template_name = "rank_tong2.html"

        # 数据处理
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            items = data.get("data") or []
            if isinstance(items, list):
                items = items[:50]
            else:
                items = []

            return_data["data"] = {
                "items": items,
                "server": data.get("server", server),
                "rank_name": data.get("name", name),
                "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
        return await self._request_api(
            path="/rank/statistics",
            params={"server": server, "name": name, "token": self.token},
            processor=processor,
            template=template_name
        )   


    async def shilianpaixing(self, name: str, server: str) -> Dict[str, Any]:
        """试炼排行"""
        # 数据处理
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            items = data.get("data", [])

            return_data["data"] = {
                "items": items,
                "name": data.get("name", name),
                "server": data.get("server", server),
                "update_time": format_time(data.get("time"))
            }
            
        return await self._request_api(
            path="/rank/trials",
            params={"server": server,"name": name,"token": self.token,},
            processor=processor,
            template="shilianpaixing.html"
        )   


    async def zhengyingpaimai(self, server: str, name: str, limit: int) -> Dict[str, Any]:
        """阵营拍卖"""
        # 数据处理
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            for item in data:
                item["time"] =  format_time(item["time"])
            return_data["data"]["list"] = data

            return_data["data"]["trend_svg"] = self._trend_svg(
                [
                    (
                        str(item.get("time") or "")[:10],
                        self._parse_gold_amount(item.get("itemAmount")),
                    )
                    for item in data or []
                    if isinstance(item, dict)
                ],
                title="拍卖成交价走势",
                unit="金",
                limit=14,
                fmt=lambda value: f"{value:,.0f}",
            )
            
        return await self._request_api(
            path="/auction/records",
            params={"server": server, "name": name, "limit": limit, "token": self.token},
            processor=processor,
            template="zhengyingpaimai.html"
        )           


    async def dilujilu(self, server: str) -> Dict[str, Any]:
        """的卢拍卖"""
        # 数据处理
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            for item in data:
                item["refreshTime"] = format_time(item["refreshTime"]) 
                item["captureTime"] = format_time(item["captureTime"])
                item["auctionTime"] = format_time(item["auctionTime"])
            return_data["data"]["list"] = data

            return_data["data"]["trend_svg"] = self._trend_svg(
                [
                    (
                        str(item.get("auctionTime") or "")[:10],
                        self._parse_gold_amount(item.get("auctionAmount")),
                    )
                    for item in data or []
                    if isinstance(item, dict)
                ],
                title="的卢拍卖价走势",
                unit="金",
                limit=14,
                fmt=lambda value: f"{value:,.0f}",
            )
            
        return await self._request_api(
            path="/steed/records",
            params={"server": server, "token": self.token},
            processor=processor,
            template="dilujilu.html"
        ) 


    async def jinjia(self, server: str, limit: str) -> Dict[str, Any]:
        """金价行情

        只保留万宝楼：JX3API 的 tieba / dd373 / 5173 等字段长期是恒定值
        （所有服务器都是 800.00），属于上游失效数据，展示出来会误导人。
        """

        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            rows = []
            for item in data or []:
                if not isinstance(item, dict):
                    continue
                if not self._to_float(item.get("wanbaolou")):
                    continue
                rows.append(
                    {
                        "date": str(item.get("date") or ""),
                        "price": item.get("wanbaolou"),
                        "server": str(item.get("server") or server),
                        "zone": str(item.get("zone") or ""),
                    }
                )
            rows.sort(key=lambda row: row["date"], reverse=True)

            prices = [p for p in (self._to_float(r["price"]) for r in rows) if p]
            return_data["data"] = {
                "items": rows,
                "server": server,
                "days": len(rows),
                "latest_price": rows[0]["price"] if rows else "",
                "latest_date": rows[0]["date"] if rows else "",
                "highest": f"{max(prices):,.0f}" if prices else "",
                "lowest": f"{min(prices):,.0f}" if prices else "",
                "trend_svg": self._trend_svg(
                    [(row["date"], row["price"]) for row in rows],
                    title="金价走势 · 万宝楼",
                    unit="元 / 万金",
                    limit=20,
                    fmt=lambda value: f"{value:,.0f}",
                    foot="数据来源：JX3API · 万宝楼，仅供参考",
                ),
            }

        return await self._request_api(
            path="/trade/demon",
            params={"server": server, "limit": limit, "token": self.token},
            processor=processor,
            template="jinjia.html",
        )


    async def wujia_snapshot(self, Name: str, server: str):
        """Share the primary request between the text card and the full image."""
        params: Dict[str, Any] = {"name": Name, "token": self.token}
        if str(server or "").strip():
            params["server"] = server
        return await self._cached_request("/trade/records", params)

    async def wujia_brief_detail(self, name: str):
        if not self._config.get("market_history_enabled", True):
            return {}
        return await self._market_source.detail(name)

    async def wujia(self, Name: str, server: str, snapshot=None) -> Dict[str, Any]:
        """物价查询（与「行情」共用同一套价格页设计）"""
        data, metadata = snapshot if snapshot is not None else await self.wujia_snapshot(Name, server)
        result = self._init_return_data()
        result["_cache"] = metadata
        if isinstance(data, APIErrorResponse):
            result["msg"] = data.message or "物价接口暂时不可用"
            return result
        if not isinstance(data, dict) or not data.get("name"):
            result["msg"] = "未找到该外观"
            return result
        result["data"] = await self._market_data_with_history(data)
        result["temp"] = await load_template("hangqing.html")
        result["code"] = 200
        return result

    async def _market_data_with_history(self, data, tieba_source=None):
        extra = {}
        if self._config.get("market_history_enabled", True):
            extra = await self._market_source.get(data["name"])
        product = enrich_product(data, extra.get("product") or {})
        rendered = self._market_render_data(product, tieba_source=tieba_source)
        series = extra.get("series") or []
        rendered["trend"] = make_fused_chart([*series, *rendered.pop("_history_series", [])])
        return rendered


    @staticmethod
    def _match_word(text: str, word: str) -> bool:
        """口语词与库内命名的模糊匹配（库里的叫法常和人说的不一样）。"""
        word = str(word or "").strip()
        if not word:
            return True
        if word in text:
            return True
        if len(word) >= 2 and all(char in text for char in word):
            return True
        # 退一步：首字命中即可，宁松勿漏（后面还有候选列表兜着）
        return len(word) >= 2 and word[0] in text

    async def wujia_menu(
        self, keyword: str, filters: Any = ()
    ) -> Dict[str, str]:
        """物价关键词模糊搜索，返回 {序号: 名称｜别名} 供二轮选择。

        filters 为额外关键词，会对候选做二次过滤。
        """
        if not str(keyword or "").strip():
            return {}
        keyword = normalize_query(keyword)
        data, _ = await self._cached_request(
            "/trade/item/search", {"name": keyword, "token": self.token}
        )
        if not isinstance(data, list):
            return {}

        words = [normalize_query(f) for f in (filters or []) if str(f).strip()]
        return build_menu(data, ' '.join([keyword, *words]), words)

    async def wujia_selected(
        self, selected: Any, server: str = ""
    ) -> Dict[str, Any]:
        """二轮选择之后渲染价格页。"""
        value = ""
        if isinstance(selected, dict):
            value = str(next(iter(selected.values()), ""))
        elif selected:
            value = str(selected)
        name = value.split("｜")[0].strip()
        if not name:
            return self._hint("没取到物品名称，请重新查询。")
        return await self.wujia(name, server)


    async def chengbeng(self, Name: str, server:str, source: int) -> Dict[str, Any]:
        """成本计算"""
        # 数据处理
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            return_data["data"] = data
            
        return await self._request_api(
            path="/trade/manufacture",
            params={"name": Name,"token": self.token, "server": server, "source": source},
            processor=processor,
            template="chengbeng.html"
        ) 

    async def bianhao(self, id: str) -> Dict[str, Any]:
        """编号搜索"""
        # 数据处理
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            if not isinstance(data, dict):
                return_data["data"] = "账号角色数据格式错误"
                return

            # 同时兼容完整接口数据和直接传入 data 字段
            data = data.get("data", data)

            if not isinstance(data, dict):
                return_data["data"] = "账号角色数据为空"
                return

            # 将 replyContent 中的 HTML 转换为纯文本
            detail = str(data.get("replyContent") or "")
            detail = re.sub(r"<br\s*/?>", "\n", detail, flags=re.IGNORECASE)
            detail = re.sub(r"<[^>]+>", "", detail)
            detail = html.unescape(detail).strip() or "暂无账号详细信息"

            # 交易状态
            trade_status = {
                1: "公示中",
                2: "出售中",
                3: "出售中",
                4: "已售出",
                5: "已下架",
            }.get(data.get("tradeStatus"), f"状态码 {data.get('tradeStatus', '未知')}")

            # 调价记录，接口中的时间为毫秒时间戳
            update_prices = data.get("updatePrices") or []
            update_price_text = "\n".join(
                f"{index}. {format_time(int(item.get('updateTime') or 0) // 1000)}："
                f"{item.get('updatePrice', 0)} 元"
                for index, item in enumerate(update_prices, start=1)
                if isinstance(item, dict)
            ) or "暂无调价记录"

            return_data["data"] = (
                f"【万宝楼账号】\n"
                f"{data.get('replyTitle') or '暂无标题'}\n\n"

                f"【角色信息】\n"
                f"区服：{data.get('serverName') or '未知'}\n"
                f"角色：{data.get('roleName') or '未知'}\n"
                f"等级：{data.get('roleLevel') or 0}\n"
                f"门派：{data.get('forceName') or '未知'}\n"
                f"体型：{data.get('bodyName') or '未知'}\n"
                f"阵营：{data.get('campName') or '未知'}\n\n"

                f"【账号数据】\n"
                f"装备分数：{data.get('equipScore') or 0}\n"
                f"江湖资历：{data.get('seniorityNum') or 0}\n"
                f"约见次数：{data.get('meetingNum') or 0}\n"
                f"关注人数：{data.get('followNum') or 0}\n\n"

                f"【交易信息】\n"
                f"挂牌价格：{data.get('priceNum') or 0} 元\n"
                f"交易状态：{trade_status}\n"
                f"商品编号：{data.get('id') or '未知'}\n"
                f"发布时间：{format_time(data.get('replyTime') or 0)}\n\n"

                f"【调价记录】\n"
                f"{update_price_text}\n\n"

                f"【账号详情】\n"
                f"{detail}"
            )
            
        return await self._request_api(
            path="/trade/wanbaolou",
            params={"id": id,"token": self.token},
            processor=processor,
            template=""
        ) 


    async def bangzhanjilu(self, server: str) -> Dict[str, Any]:
        """帮战记录"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            for item in data:
                item["startTime"] = format_time(item["startTime"])
                item["durationSeconds"] = format_remaining(item["durationSeconds"])
                item["endTime"] = format_time(item["endTime"])

            return_data["data"] = {
                "items": data,
                "server": server,
                "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            
        return await self._request_api(
            path="/battle/records",
            params={"server": server},
            processor=processor,
            template="bangzhanjilu.html"
        ) 


    async def shapan(self, server: str) -> Dict[str, Any]:
        """阵营沙盘。"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            if not isinstance(data, dict):
                raise ValueError("沙盘接口返回的数据结构无效")

            records = data.get("data")
            if not isinstance(records, list) or not records:
                raise ValueError("沙盘接口未返回据点数据")

            items = []
            for record in records:
                if not isinstance(record, dict):
                    continue

                camp_id = record.get("campId")
                if camp_id not in (1, 2):
                    continue

                castle_name = str(record.get("castleName") or "").strip()
                if not castle_name:
                    continue

                items.append(
                    {
                        "castle_name": castle_name,
                        "camp_key": "浩" if camp_id == 1 else "恶",
                        "camp_name": str(record.get("campName") or ""),
                        "tong_name": str(record.get("tongName") or ""),
                        "master_name": str(record.get("masterName") or ""),
                        "ride_piece": int(record.get("ridePiece") or 0),
                        "defend_count": int(record.get("defendCount") or 0),
                    }
                )

            if not items:
                raise ValueError("沙盘接口未返回有效据点")

            update_time = format_time(data.get("update"))
            return_data["data"] = {
                "zone": str(data.get("zone") or ""),
                "server": str(data.get("server") or server),
                "reset": int(data.get("reset") or 0),
                "update_time": update_time,
                "items": items,
            }

        return await self._request_api(
            path="/sand/records",
            params={"server": server, "token": self.token},
            processor=processor,
            template="shapan.html",
        )


    async def zhueevent(self,server: str,limit: str) -> Dict[str, Any]:
        """诛恶事件"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            for item in data:
                item["time"] = format_time(item["time"])

            return_data["data"] = {
                "items": data,
                "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
        return await self._request_api(
            path="/wicked/records",
            params={"token": self.token, "server": server, "limit": limit},
            processor=processor,
            template="zhueevent.html"
        ) 


    async def jueshemingpian(
        self, server: str, name: str, force_refresh: bool = False
    ) -> Dict[str, Any]:
        """查询角色名片。"""
        return await self.card_remote(server, name, force_refresh=force_refresh)


    async def shuijimingpian(self, server:str, force: str, body:str,) -> Dict[str, Any]:
        """随机名片"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            url = data.get("showAvatar")
            if not url:
                return_data["msg"] = "未获取到名片图片"
                return return_data

            server_name = data.get("serverName")
            role_name = data.get("roleName")
            msg = f"{server_name}-{role_name}"

            return_data["data"] = [
                Comp.Plain(msg),
                Comp.Image.fromURL(url),
            ]
            
        return await self._request_api(
            path="/card/random",
            params={"server": server, "body": body, "force":force, "token": self.token},
            processor=processor,
            template=""
        ) 


    async def shuoyoumingpian(self, server: str, name: str) -> Dict[str, Any]:
        """名片历史"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            chain = []
            for m in data:
                status = "当前展示" if m.get("showActive") else "未展示"
                msg = f"第{m.get('showIndex')}张 {status}"
                url = m.get("showAvatar")

                if not url:
                    logger.warning(f"第{m.get('showIndex')}张名片缺少图片URL，已跳过")
                    continue

                chain.extend([
                    Comp.Plain(msg),
                    Comp.Image.fromURL(url),
                ])

            if not chain:
                return_data["msg"] = "未获取到有效的名片数据"
                return return_data

            return_data["data"] = chain

        return await self._request_api(
            path="/card/records",
            params={"server": server, "name": name, "token": self.token},
            processor=processor,
            template=""
        ) 


    async def qiyuhuizong(self, server: str, num: int) -> Dict[str, Any]:
        """奇遇汇总"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            for item in data:
                latest = item.get("last") or {}
                item["latest_name"] = latest.get("name") or "—"
                item["latest_time"] = format_time(latest.get("time")) if latest.get("time") else ""

            return_data["data"] = {
                "items": data,
                "server": server,
                "num": num,
                "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
        return await self._request_api(
            path="/event/collect",
            params={"server": server, "num": num, "token": self.token},
            processor=processor,
            template="qiyuhuizong.html"
        ) 


    async def weizuoqiyu(self, server: str, name: str, ) -> Dict[str, Any]:
        """未出奇遇"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            return_data["data"]["ptqy"] = []
            return_data["data"]["jsqy"] = []
            return_data["data"]["name"] = name
            return_data["data"]["server"] = server

            for item in data:
                try:
                    level = int(item.get("level", 0))
                except (TypeError, ValueError):
                    continue

                event_item = {
                    "event": item.get("name"),
                    "time": "未触发"
                }

                if level == 1:
                    return_data["data"]["ptqy"].append(event_item)
                elif level == 2:
                    return_data["data"]["jsqy"].append(event_item)

            if not return_data["data"]["ptqy"] and not return_data["data"]["jsqy"]:
                return_data["msg"] = "未查询到未做普通或绝世奇遇"
                return return_data
            
        return await self._request_api(
            path="/event/missing",
            params={"server": server, "name": name, "token": self.token},
            processor=processor,
            template="weizuoqiyu.html"
        ) 


    async def jinqiqiyu(self, server: str, limit: int) -> Dict[str, Any]:
        """近期奇遇"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            for item in data:
                item["time"] = format_time(item.get("time"))

            return_data["data"] = {
                "items": data,
                "server": server,
                "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

        return await self._request_api(
            path="/event/recent",
            params={"server": server, "limit": limit, "token": self.token},
            processor=processor,
            template="jinqiqiyu.html"
        ) 

    
    async def juesheqiyu(self, server: str, name: str, full: int) -> Dict[str, Any]:
        """角色奇遇"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            return_data["data"]["name"] = name
            return_data["data"]["server"] = server
            return_data["data"]["ptqy"] = []
            return_data["data"]["jsqy"] = []
            return_data["data"]["cwqy"] = []

            for item in data:
                item["time"] = datetime.fromtimestamp(item["time"]).strftime("%Y-%m-%d %H:%M:%S")
                if item["level"] == 1:
                    return_data["data"]["ptqy"].append(item)
                if item["level"] == 2:
                    return_data["data"]["jsqy"].append(item)
                if item["level"] == 3:
                    return_data["data"]["cwqy"].append(item)
            
        return await self._request_api(
            path="/event/records",
            params= {"server": server, "name": name, "full": full, "token": self.token},
            processor=processor,
            template="juesheqiyu.html"
        ) 


    async def qiyutongji(self, name: str, server: str, limit: int) -> Dict[str, Any]:
        """奇遇统计"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            for item in data:
                item["time"] = format_time(item.get("time"))

            return_data["data"] = {
                "items": data,
                "server": server,
                "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "qiyuname": name
            }

        return await self._request_api(
            path="/event/statistics",
            params={"name": name, "server": server, "limit": limit, "token": self.token},
            processor=processor,
            template="qiyuliebiao.html"
        ) 


    async def jingnai(self, name: str, server: str) -> Dict[str, Any]:
        """角色百战"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            items = data.get("skillList", [])
            if not isinstance(items, list) or not items:
                return_data["msg"] = "未查询到技能信息"
                return return_data

            color_map = {
                7: "黑色",
                6: "紫色",
                5: "红色",
                4: "绿色",
                3: "蓝色",
                2: "黄色",
                0: "无",
            }

            for item in items:
                if not isinstance(item, dict):
                    continue

                color = item.get("nColor")
                try:
                    color_key = int(color)
                except (TypeError, ValueError):
                    color_key = None
                item["skill_color_text"] = color_map.get(color_key, str(color) if color is not None else "")

            return_data["data"] = {
                "server": data.get("server", server),
                "role_name": data.get("roleName", name),
                "skill_energy": data.get("skillEnergy", ""),
                "skill_stamina": data.get("skillStamina", ""),
                "skill_count": data.get("skillCount", len(items)),
                "items": items,
                "update_time": format_time( data.get("updateTime"))
            }

        return await self._request_api(
            path="/monster/records",
            params={"server": server, "name": name, "token": self.token},
            processor=processor,
            template="jingnai.html"
        ) 


    async def baizhan(self) -> Dict[str, Any]:
        """百战首领"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            return_data["data"] = data
            return_data["data"]["start"]  = format_time(data["start"])   
            return_data["data"]["end"]  = format_time(data["end"])   

        return await self._request_api(
            path="/monster/weekly",
            params= { "token": self.token},
            processor=processor,
            template="baizhan.html"
        ) 


    async def chengjiuchaxun(self, server:str, role:str, name:str) -> Dict[str, Any]:
        """成就查询"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            return_data["data"] = data

        return await self._request_api(
            path="/role/achievement",
            params= {"server": server, "role": role, "name": name, "token": self.token},
            processor=processor,
            template="chengjiu.html"
        ) 


    async def zili_menu(self) -> Dict[str, str]:
        """资历选择内容。"""
        return {
            "1": "总览",
            "2": "杂闻",
            "3": "武学",
            "4": "修为",
            "5": "装备",
            "6": "技艺",
            "7": "阅读",
            "8": "任务",
            "9": "足迹",
            "10": "战斗",
            "11": "声望",
            "12": "秘境",
            "13": "帮会",
            "14": "阵营",
            "15": "节日",
            "16": "活动",
            "17": "风雨江湖路",
            "18": "家园",
            "19": "剑侠录"
        }


    async def zili(self,server: str,name: str,selected: Dict[str, Any],) -> Dict[str, Any]:
        """资历分布"""
        if not isinstance(selected, dict) or len(selected) != 1:
            return_data = self._init_return_data()
            return_data["msg"] = "资历选项数据格式异常"
            return return_data

        selected_name = str(next(iter(selected.values())) or "").strip()
        selected_subclass = "" if selected_name == "总览" else selected_name

        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            statistics = data.get("data") or {}
            categories = statistics.get("total") or {}
            if not isinstance(categories, dict) or not categories:
                raise ValueError("接口未返回资历统计")

            # 查询总览时 total 为“大类 -> 小类 -> 统计”；指定 subclass 后，
            # JX3API 会去掉大类这一层，total 直接变成“小类 -> 统计”。
            data["subclass"] = selected_subclass
            return_data["data"] = data

        return await self._request_api(
            path="/tuilan/achievement",
            params={"server": server,"name": name,"class": 1,"subclass": selected_subclass,"ticket": self.ticket,"token": self.token,},
            processor=processor,
            template="zili.html",
        )


    async def jueshe(self,server: str, name: str, history:int) -> Dict[str, Any]:
        """角色详情"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            role_history = data.get("roleHistory") or {}
            role_names = role_history.get("roleNames") or []
            tong_names = role_history.get("TongNames") or []

            # 角色名称历史
            role_history_lines = []

            for item in role_names:
                if not isinstance(item, dict):
                    continue

                server = item.get("server") or "未知服务器"
                name = item.get("name") or "未知角色名"
                time_text = format_time(item.get("time", 0))

                role_history_lines.append(
                    f"{time_text}　{server}·{name}"
                )

            if role_history_lines:
                role_history_text = "\n".join(role_history_lines)
            else:
                role_history_text = "暂无角色历史记录"

            # 帮会历史
            tong_history_lines = []

            for item in tong_names:
                if not isinstance(item, dict):
                    continue

                server = item.get("server") or "未知服务器"
                tong_name = item.get("name") or "无帮会"
                time_text = format_time(item.get("time", 0))

                tong_history_lines.append(
                    f"{time_text}　{server}·{tong_name}"
                )

            if tong_history_lines:
                tong_history_text = "\n".join(tong_history_lines)
            else:
                tong_history_text = "暂无帮会历史记录"

            return_data["data"] = (
                f"服务器：{data.get('zoneName') or '未知'}·"
                f"{data.get('serverName') or '未知'}\n"
                f"名称：{data.get('roleName') or '未知'}\n"
                f"角色ID：{data.get('roleId') or '未知'}\n"
                f"推栏ID：{data.get('globalId') or '未知'}\n"
                f"职业：{data.get('forceName') or '未知'}·"
                f"{data.get('bodyName') or '未知'}\n"
                f"帮会：{data.get('tongName') or '无帮会'}\n"
                f"阵营：{data.get('campName') or '未知'}\n"
                f"\n"
                f"【角色历史】\n"
                f"{role_history_text}\n"
                f"\n"
                f"【帮会历史】\n"
                f"{tong_history_text}"
            )

        return await self._request_api(
            path="/role/detail",
            params= {"server": server, "name": name, "history": history, "token": self.token},
            processor=processor,
            template=""
        ) 


    async def zhenyan(self, name: str) -> Dict[str, Any]:
        """阵眼"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            items = data.get("data", [])
            if not isinstance(items, list) or not items:
                return_data["msg"] = "未查询到该心法阵眼信息"
                return return_data

            result_msg = f"{data.get('name', name)}-{data.get('skillName', '')}\n"
            for item in items:
                if not isinstance(item, dict):
                    continue
                result_msg += f"{item.get('name', '')}：{item.get('desc', '')}\n"

            return_data["data"] = result_msg.rstrip()

        return await self._request_api(
            path="/school/matrix",
            params= {"name": name, "ticket": self.ticket, "token": self.token},
            processor=processor,
            template=""
        ) 


    async def zilipaixing(self,server: str, school: str) -> Dict[str, Any]:
        """资历排行"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            return_data["data"] = {
                "items": data,
                "school": school,
                "server": server,
                "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

        return await self._request_api(
            path="/school/seniority",
            params= {"server": server,"school": school,"ticket": self.ticket,"token": self.token,},
            processor=processor,
            template="zilipaixing.html"
        ) 


    async def jineng(self, name: str,update:int) -> Dict[str, Any]:
        """技能"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            groups = []
            for group in data:
                if not isinstance(group, dict):
                    continue

                skills = group.get("data", [])
                if not isinstance(skills, list) or not skills:
                    continue

                parsed_skills = []
                for skill in skills:
                    if not isinstance(skill, dict):
                        continue

                    parsed_skills.append({
                        "name": skill.get("name", ""),
                        "icon": skill.get("icon", ""),
                        "desc": skill.get("desc", ""),
                        "interval": skill.get("interval", ""),
                        "distance": skill.get("distance", ""),
                        "release_type": skill.get("releaseType", ""),
                        "weapon": skill.get("weapon", ""),
                    })

                if parsed_skills:
                    groups.append({
                        "title": group.get("class", "其他技能"),
                        "skills": parsed_skills,
                    })

            if not groups:
                return_data["msg"] = "未查询到该心法技能信息"
                return return_data

            return_data["data"] = {
                "name": name,
                "groups": groups,
                "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

        return await self._request_api(
            path="/school/skills",
            params= {"name": name,"update": update, "ticket": self.ticket, "token": self.token},
            processor=processor,
            template="jineng.html"
        ) 


    async def qixue(self, name: str, update:int) -> Dict[str, Any]:
        """奇穴"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            level_titles = {
                1: "主奇穴",
                2: "第一重",
                3: "第二重",
                4: "第三重",
                5: "第四重",
                6: "第五重",
                7: "第六重",
                8: "混池",
            }

            groups = []
            for group in data:
                if not isinstance(group, dict):
                    continue

                level = group.get("level")
                try:
                    level_key = int(level)
                except (TypeError, ValueError):
                    level_key = 0

                items = group.get("data", [])
                if not isinstance(items, list) or not items:
                    continue

                parsed_items = []
                for item in items:
                    if not isinstance(item, dict):
                        continue

                    class_value = item.get("class")
                    is_active = class_value == 1 or str(class_value) == "1"
                    parsed_items.append({
                        "name": item.get("name", ""),
                        "icon": item.get("icon", ""),
                        "desc": item.get("desc", ""),
                        "class_text": "主动" if is_active else "被动",
                        "interval": item.get("interval", "") if is_active else "",
                    })

                if parsed_items:
                    groups.append({
                        "level": level_key,
                        "title": level_titles.get(level_key, f"第{level_key}组"),
                        "talents": parsed_items,
                    })

            if not groups:
                return_data["msg"] = "未查询到该心法奇穴信息"
                return return_data

            groups.sort(key=lambda item: item["level"])
            return_data["data"] = {
                "name": name,
                "groups": groups,
                "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

        return await self._request_api(
            path="/school/talent",
            params= {"name": name,"update": update, "ticket": self.ticket, "token": self.token},
            processor=processor,
            template="qixue.html"
        ) 


    async def juesheliaotian(self, server:str, name: str, limit:int, page:int) -> Dict[str, Any]:
        """角色聊天"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            chat_list = data.get("list", [])

            for item in chat_list:
                item["time"] = format_time(item.get("time", 0))

            return_data["data"] = data

        return await self._request_api(
            path="/chat/records",
            params= {"server": server,"name": name, "limit": limit, "page": page, "token": self.token},
            processor=processor,
            template="juesheliaotian.html"
        ) 


    async def tongzhanyy(self, server: str) -> Dict[str, Any]:
        """统战歪歪"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            lines = ["【统战歪歪】"]
            for group in data:
                group_server = group.get("server", "")
                channels = group.get("data", [])

                if len(lines) > 1:
                    lines.append("")
                lines.append(f"服务器：{group_server}")

                for item in channels:
                    if not isinstance(item, dict):
                        continue

                    short_id = item.get("esid") or item.get("asid", "")
                    lines.extend([
                        f"阵营：{item.get('campName', '')}",
                        f"频道ID：{item.get('sid', '')}",
                        f"短位ID：{short_id}",
                        f"在线人数：{item.get('users', '')}",
                        f"频道名：{item.get('snick', '')}",
                        "",
                    ])

                    return_data["data"] = "\n".join(lines)

        return await self._request_api(
            path="/duowan/statistics",
            params= {"server": server},
            processor=processor,
            template=""
        ) 


    async def xiaoyao(self, name:str) -> Dict[str, Any]:
        """小吃小药"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            result = {}

            for item in data:
                k = item["kungfu"]
                color = item["color"]
                cls = item["class"]
                name = item["name"]

                if k not in result:
                    result[k] = {
                        "kungfu": k,
                        "purple": {},
                        "blue": {}
                    }

                if color == "紫":
                    result[k]["purple"][cls] = name
                else:
                    result[k]["blue"][cls] = name

            return_data["data"]["items"] = list(result.values())

        return await self._request_api(
            path="/food/list",
            params= {"name": name},
            processor=processor,
            template="xiaoyao.html"
        ) 


    async def pianzhi(self, server:str, uid: str) -> Dict[str, Any]:
        """骗子查询"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            records = data["records"]

            if not records:
                result_msg = "未找到该用户行骗记录，很棒！继续保持！"
            else:
                result_msg = ""

                for record in records:
                    result_msg += f"区服：{record['server']}  标签：{record['tieba']}\n\n"

                    for item in record["data"]:
                        result_msg += f"标题：{item['title']}\n"
                        result_msg += f"地址：{item['url']}\n"
                        result_msg += f"ID：{item['tid']}\n"
                        result_msg += f"内容：{item['text']}\n"
                        result_msg += (
                            f"时间：{datetime.fromtimestamp(item['time']).strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                        )

                    result_msg += "\n\n"
            return_data["data"] = result_msg

        return await self._request_api(
            path="/fraud/detail",
            params= {"server": server, "uid": uid, "token": self.token},
            processor=processor,
            template=""
        ) 


    async def huajia(self,server: str, name: str, map: str) -> Dict[str, Any]:
        """家园鲜花"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            return_data["data"]["data"] = data
            return_data["data"]["server"] = server

            rows = data.get("list") if isinstance(data, dict) else data
            return_data["data"]["trend_svg"] = self._trend_svg(
                self._series_from_rows(rows),
                title="鲜花价格走势",
                unit="金",
            )

        return await self._request_api(
            path="/home/flower",
            params= {"server": server, "name": name,  "map": map},
            processor=processor,
            template="huajia.html"
        ) 


    async def zhuangshi(self,name: str) -> Dict[str, Any]:
        """家园装饰"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            return_data["data"]["data"] = data

        return await self._request_api(
            path="/home/furniture",
            params= { "name": name},
            processor=processor,
            template="zhuangshi.html"
        ) 


    async def qiwu(self,name: str) -> Dict[str, Any]:
        """器物图谱"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            return_data["data"]["data"] = data
            return_data["data"]["name"] = name

        return await self._request_api(
            path="/home/travel",
            params= { "name": name},
            processor=processor,
            template="qiwu.html"
        ) 
 

    async def shitu(self, label: int, server: str, keyword: str, limit:int) -> Dict[str, Any]:
        """师徒招募"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            items = data
            if not items:
                return_data["msg"] = "未查询到师徒招募信息"
                return return_data

            title = "收徒信息" if label == 1 else "拜师信息"
            return_data["data"] = {
                "items": items,
                "server": server,
                "keyword": keyword,
                "type_value": label,
                "title": title,
                "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

        return await self._request_api(
            path="/mentor/search",
            params= {"label": label, "server": server, "keyword": keyword, "limit": limit, "token": self.token},
            processor=processor,
            template="shitu.html"
        ) 


    async def weihu(self, limit:int) -> Dict[str, Any]:
        """维护公告"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            result = data[0]
            result_msg = "维护公告\n"
            # 仅展示前1条，避免消息过长
            for i, item in enumerate(data[:limit], 1): 
                result_msg += f"{i}. 【{item.get('type', '无类型')}】\n"
                result_msg += f"标题：{item.get('title', '未知时间')}\n"
                result_msg += f"时间：{item.get('date', '未知时间')}\n"
                result_msg += f"链接：{item.get('url', '无链接')}\n"
                
            return_data["data"] = result_msg

        return await self._request_api(
            path="/news/announce",
            params= {"limit": limit},
            processor=processor,
            template=""
        ) 


    async def xinwen(self, limit:int) -> Dict[str, Any]:
        """新闻资讯"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            result = data[0]
            result_msg = "新闻资讯\n"
            # 仅展示前1条，避免消息过长
            for i, item in enumerate(data[:limit], 1): 
                result_msg += f"{i}. 【{item.get('type', '无类型')}】\n"
                result_msg += f"标题：{item.get('title', '未知时间')}\n"
                result_msg += f"时间：{item.get('date', '未知时间')}\n"
                result_msg += f"链接：{item.get('url', '无链接')}\n"
                
            return_data["data"] = result_msg

        return await self._request_api(
            path="/news/records",
            params= {"limit": limit},
            processor=processor,
            template=""
        ) 


    async def tuanduizhaomu(self, server: str, label:int, keyword: str, limit:int) -> Dict[str, Any]:
        """团队招募"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            for item in data:
                item["createTime"] = format_time(item["createTime"])   
                item["maxMemberCount"] = f"{item['currentMemberCount']}/{item['maxMemberCount']}"
                return_data["data"]["list"] = data

        return await self._request_api(
            path="/recruit/search",
            params= {"server": server, "label": label, "keyword": keyword, "limit": limit, "token": self.token},
            processor=processor,
            template="tuanduizhaomu.html"
        ) 


    async def daanzhishu(self) -> Dict[str, Any]:
        """答案之书"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            return_data["data"] = (
                f"答案：{data.get('answer', '')}\n"
                f"鼓励：{data.get('hearten', '')}\n"
            )

        return await self._request_api(
            path="/saohua/answer",
            params= {},
            processor=processor,
            template=""
        ) 


    async def tiangou(self) -> Dict[str, Any]:
        """舔狗日志"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            return_data["data"] = data.get('text')

        return await self._request_api(
            path="/saohua/content",
            params= {},
            processor=processor,
            template=""
        ) 


    async def fengleiyulu(self, name:str) -> Dict[str, Any]:
        """分类语录"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            return_data["data"] = data.get('content')

        return await self._request_api(
            path="/saohua/context",
            params= { "name": name, "token": self.token},
            processor=processor,
            template=""
        ) 


    async def heshengme(self) -> Dict[str, Any]:
        """喝什么"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            return_data["data"] = (
                f"{', '.join(data or [])}\n"
            )

        return await self._request_api(
            path="/saohua/drink",
            params= {},
            processor=processor,
            template=""
        ) 


    async def chishengme(self) -> Dict[str, Any]:
        """吃什么"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            return_data["data"] = (
                f"{', '.join(data or [])}\n"
            )

        return await self._request_api(
            path="/saohua/eat",
            params= {},
            processor=processor,
            template=""
        ) 


    async def shaohua(self) -> Dict[str, Any]:
        """随机骚话"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            return_data["data"] = data.get('text')

        return await self._request_api(
            path="/saohua/random",
            params= {},
            processor=processor,
            template=""
        ) 


    async def zhananyulu(self) -> Dict[str, Any]:
        """渣男语录"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            return_data["data"] = data.get('text')

        return await self._request_api(
            path="/saohua/zhanan",
            params= {},
            processor=processor,
            template=""
        ) 


    async def keju(self,subject: str, limit: int) -> Dict[str, Any]:
        """科举"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            result_msg = ""
            for m in data:
                result_msg += f"{m['id']}.{m['question']}\n"
                result_msg += f"答案：{m['answer']}\n\n"

            return_data["data"] = result_msg

        return await self._request_api(
            path="/exam/search",
            params= {"subject": subject, "limit": limit},
            processor=processor,
            template=""
        ) 


    async def zhuangtai(self,server:str) -> Dict[str, Any]:
        """区服状态"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            server_wj = []
            server_dx = []
            server_sx = []

            for itme in data:
                if itme['zone'] == "无界区":
                    server_wj.append(itme)
                elif itme['zone'] == "电信区":
                    server_dx.append(itme)
                elif itme['zone'] == "双线区":
                    server_sx.append(itme)

            return_data["data"]["server_wj"] = server_wj
            return_data["data"]["server_dx"] = server_dx
            return_data["data"]["server_sx"] = server_sx

        return await self._request_api(
            path="/server/status/check",
            params= {"server": server, "type": "其他"},
            processor=processor,
            template="qufuzhuangtai.html"
        ) 


    async def kaifu(self, server: str) -> Dict[str, Any]:
        """开服状态查询"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            list_data = data[0]
            status = list_data.get("status")
            
            if status == 1:
                status_str = f"{server}服务器已开服，快冲，快冲！"
                status_bool = True
            else:
                status_str = f"{server}服务器当前维护中，等会再来吧！"
                status_bool = False

            return_data["status"] = status_bool
            return_data["data"] = status_str

        return await self._request_api(
            path="/server/status/check",
            params= {"server": server, "type": 1},
            processor=processor,
            template="qufuzhuangtai.html"
        ) 


    async def jigai(self) -> Dict[str, Any]:
        """技改记录"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            result_msg = "最近技改\n"
            
            for i, item in enumerate(data[:3], 1): 
                result_msg += f"{i}. {item.get('title', '无标题')}\n"
                result_msg += f"时间：{item.get('time', '未知时间')}\n"
                result_msg += f"链接：{item.get('url', '无链接')}\n\n"
                
            return_data["data"] = result_msg

        return await self._request_api(
            path="/skill/rework",
            params= {},
            processor=processor,
            template=""
        ) 


    async def jiemi(self) -> Dict[str, Any]:
        """解密"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            return_data["data"] = "\n".join([
                "解密",
                f"当前时间：{data.get('nowTime', '')}",
                f"当前节点：{data.get('nowNode', '')}",
                f"当前结果：{data.get('nowResult', '')}",
                f"下轮节点：{data.get('nextNode', '')}",
                f"下轮结果：{data.get('nextResult', '')}",
                f"剩余时间：{data.get('intervalTime', '')}",
            ])

        return await self._request_api(
            path="/mech/decrypt",
            params= {"token": self.token},
            processor=processor,
            template=""
        ) 


    async def diaoluo(self, name: str, server: str, limit: int ) -> Dict[str, Any]:
        """物品掉落记录"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            for item in data:
                item["time"] = format_time(item.get("time"))

            return_data["data"] = {
                "items": data,
                "name": name,
                "limit": limit,
                "server": server,
                "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }

        return await self._request_api(
            path="/reward/statistics",
            params= {"server": server, "name": name, "limit": limit, "token": self.token},
            processor=processor,
            template="diaoluo.html"
        ) 




    async def tiebawujia(self, name: str, server: str, limit: int ) -> Dict[str, Any]:
        """贴吧物价"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            lines = [
                f"贴吧物价：{name}",
                f"服务器：{server}",
                f"记录数：{len(data)}",
                "",
            ]

            for index, item in enumerate(data, start=1):
                if not isinstance(item, dict):
                    continue

                item_time = item.get("time", "")
                if item_time:
                    try:
                        item_time = datetime.fromtimestamp(int(item_time)).strftime("%Y-%m-%d %H:%M:%S")
                    except (TypeError, ValueError, OSError):
                        item_time = str(item_time)

                lines.extend([
                    f"{index}. {item.get('name', '')}",
                    f"区服：{item.get('zone', '')}  服务器：{item.get('server', '')}",
                    f"内容：{item.get('context', '')}",
                    f"回复：{item.get('reply', '')}  楼层：{item.get('floor', '')}",
                    f"时间：{item_time}",
                    f"链接：https://tieba.baidu.com/p/{item.get('url', '')}",
                    "",
                ])

            if len(lines) <= 4:
                return_data["msg"] = "未查询到贴吧物价记录"
                return return_data

            return_data["data"] = "\n".join(lines).rstrip()

        return await self._request_api(
            path="/tieba/item/records",
            params= {"server": server,"name": name,"limit": limit,"token": self.token,},
            processor=processor,
            template=""
        ) 


    async def bagua(self, tags: str, server:str, limit:str ) -> Dict[str, Any]:
        """八卦"""
        async def processor(data: Any, return_data: Dict[str, Any]) -> None:   
            if not data:
                result_msg = f"未找到相关 {tags} 记录。\n"
                result_msg += f"可选范围：818 616 鬼网三 鬼网3 树洞 记录 教程 街拍 故事 避雷 吐槽 提问"
            else:
                result_msg = f"类型：【{tags}】\n\n"

                for item in data:
                    result_msg += f"{item['title']}\n"
                    result_msg += f"服务器：{item['server']}\n"
                    result_msg += f"所属吧：{item['name']}\n"
                    result_msg += f"链接：https://tieba.baidu.com/p/{item['url']}\n"
                    result_msg += f"日期：{item['date']}\n\n"
            return_data["data"] = result_msg

        return await self._request_api(
            path="/tieba/random",
            params= {"server": server,"tags": tags,"limit": limit,"token": self.token,},
            processor=processor,
            template=""
        ) 


    async def fubengjilu(self, server:str, name: str ) -> Dict[str, Any]:
        """Resolve the requested role, then read its current CD from Tuilan."""
        result = self._init_return_data()
        ticket = str(self._config.get("jx3api_ticket") or "").strip()
        if not ticket:
            result["msg"] = "副本 CD 需要推栏登录凭据，请管理员配置推栏标识。"
            return result
        if not str(self.token or "").strip():
            result["msg"] = "查询角色需要 JX3API Token，请管理员完成配置。"
            return result

        async def weekly_names():
            # Optional calendar lookup is capped separately; a slow calendar
            # must not hold up the real progress query.
            try:
                data, _ = await asyncio.wait_for(self._cached_request(
                    "/active/calendar", {"server": server}), timeout=2)
                if isinstance(data, dict):
                    weekly = data.get("weekly")
                    if isinstance(weekly, dict) and isinstance(weekly.get("raid"), list):
                        return [s for s in weekly["raid"][:16] if isinstance(s, str)]
            except Exception:
                pass
            return []

        (detail, _), weekly = await asyncio.gather(
            self._cached_request("/role/detail", {
                "server": server, "name": name, "history": 0, "token": self.token}),
            weekly_names(),
        )
        if isinstance(detail, APIErrorResponse) or not isinstance(detail, dict) or not detail.get("globalId"):
            result["msg"] = "未能取得角色标识，请检查服务器和角色名，或稍后重试。"
            return result
        if detail.get("roleName") != name or detail.get("serverName") != server:
            result["msg"] = "角色资料与查询的服务器或名称不一致，请使用角色当前的服务器和名称。"
            return result
        global_id = str(detail["globalId"])
        try:
            if self._cache:
                raids, metadata = await self._cache.request_api(
                    CD_CACHE_ENDPOINT,
                    {"role": global_id, "__credential_scope": hashlib.sha256(ticket.encode()).hexdigest()},
                    lambda: self._tuilan_cd.get(global_id, ticket),
                    lambda value: isinstance(value, list),
                )
            else:
                raids = await self._tuilan_cd.get(global_id, ticket)
                metadata = {"created_at": int(time.time()), "hit": False}
        except TuilanCDError as exc:
            result["msg"] = str(exc)
            return result
        result["data"] = build_cd_page(raids, weekly, self._config.get("dungeon_cd_focus"))
        result["data"].update(name=name, server=server)
        result["_cache"] = metadata
        result["temp"] = await load_template("fubenjilu.html")
        result["code"] = 200
        return result
    


    


    
        


    # ==================== 补全接口（JX3API 未覆盖部分） ====================

    @staticmethod
    def _short_number(value: Any) -> str:
        """把大数字格式化成带 万 / 亿 单位的短文本。"""
        if value is None or value == "":
            return "—"
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        if number >= 100000000:
            return f"{number / 100000000:.2f}亿"
        if number >= 10000:
            return f"{number / 10000:.2f}万"
        if number == int(number):
            return f"{int(number)}"
        return f"{number:.2f}"

    def _hint(self, *lines: str) -> Dict[str, Any]:
        """构造一条纯文本用法提示，避免缺参时报“参数错误”。"""
        return_data = self._init_return_data()
        return_data["code"] = 200
        return_data["data"] = "\n".join(lines)
        return return_data

    async def trial_seasons(self) -> Dict[str, Any]:
        """试炼之地赛季列表"""

        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            lines = []
            for item in data or []:
                if not isinstance(item, dict):
                    continue
                bosses = "、".join(str(boss) for boss in (item.get("bosses") or []))
                lines.append(
                    f"· {item.get('season') or '未知赛季'}"
                    f"（最高 {item.get('maxFloor') or '?'} 层）\n"
                    f"　{bosses or '暂无首领数据'}"
                )
            if lines:
                return_data["data"] = "【试炼之地 · 赛季一览】\n" + "\n".join(lines)
            else:
                return_data["data"] = "暂未获取到赛季数据"

        return await self._request_api(
            path="/trial/seasons",
            params={"token": self.token},
            processor=processor,
        )

    async def trial_bosses(self, season: str = "", floor: int = 1) -> Dict[str, Any]:
        """试炼之地各层首领血量与秒伤"""
        if not str(season or "").strip():
            seasons = await self.trial_seasons()
            return self._hint(
                "用法：试炼秒伤 赛季 层数（层数范围 1-120）",
                "例：试炼秒伤 太极秘录 1",
                "",
                str(seasons.get("data") or "暂未获取到赛季数据"),
            )

        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            if not isinstance(data, dict):
                return_data["msg"] = "未获取到试炼数据"
                return
            lines = []
            for item in data.get("bosses") or []:
                if not isinstance(item, dict):
                    continue
                line = (
                    f"· {item.get('boss') or '未知首领'}"
                    f"　血量 {self._short_number(item.get('hp'))}"
                    f"　完美 {self._short_number(item.get('perfectDps'))}"
                    f"　通关 {self._short_number(item.get('clearDps'))}"
                )
                if item.get("note"):
                    line += f"\n　备注：{item['note']}"
                lines.append(line)
            head = (
                f"【试炼之地 · {data.get('season') or season}"
                f" 第 {data.get('floor') or floor} 层】\n"
            )
            if lines:
                return_data["data"] = head + "\n".join(lines)
            else:
                return_data["data"] = head + "暂未获取到首领数据"

        return await self._request_api(
            path="/trial/bosses",
            params={"season": season, "floor": floor, "token": self.token},
            processor=processor,
        )

    async def trade_item_search(self, name: str = "") -> Dict[str, Any]:
        """外观搜索"""
        if not str(name or "").strip():
            return self._hint(
                "用法：外观搜索 外观名称",
                "例：外观搜索 幽昙",
            )

        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            items = [item for item in (data or []) if isinstance(item, dict)]
            lines = []
            for item in items[:10]:
                alias = item.get("alias")
                suffix = f"（{alias}）" if alias else ""
                lines.append(
                    f"· {item.get('name') or '未知'}{suffix}\n"
                    f"　{item.get('category') or '未分类'}"
                    f"｜原价 {item.get('retail') or '?'} 元"
                    f"｜上架 {item.get('date') or '未知'}"
                )
            if lines:
                return_data["data"] = (
                    f"【外观搜索 · {name}】共 {len(items)} 条\n" + "\n".join(lines)
                )
            else:
                return_data["data"] = f"未找到与「{name}」匹配的外观"

        return await self._request_api(
            path="/trade/item/search",
            params={"name": name, "token": self.token},
            processor=processor,
        )

    async def trade_item_records(
        self, name: str = "", server: str = ""
    ) -> Dict[str, Any]:
        """外观历史成交价"""
        if not str(name or "").strip():
            return self._hint(
                "用法：外观价格 外观名称 [服务器]",
                "例：外观价格 幽昙月影·阑珊·豪华 唯我独尊",
            )

        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            if not isinstance(data, dict) or not data.get("name"):
                return_data["msg"] = "未找到该外观的历史成交记录"
                return
            alias = data.get("alias")
            suffix = f"（{alias}）" if alias else ""
            head = (
                f"【{data.get('name')}{suffix}】\n"
                f"{data.get('category') or '未分类'}"
                f"｜原价 {data.get('retail') or '?'} 元"
                f"｜上架 {data.get('date') or '未知'}\n"
            )
            lines = []
            for group in data.get("list") or []:
                if not isinstance(group, dict):
                    continue
                rows = [
                    row for row in (group.get("list") or []) if isinstance(row, dict)
                ]
                if server:
                    matched = [
                        row
                        for row in rows
                        if str(row.get("server") or "") == str(server)
                    ]
                    if matched:
                        rows = matched
                if not rows:
                    continue
                lines.append(f"· {group.get('name') or '未知区'}")
                for row in rows[:5]:
                    lines.append(
                        f"　{row.get('date') or '?'}　{row.get('server') or '?'}"
                        f"　{self._short_number(row.get('value'))} 元"
                        f"　成交 {row.get('sale') or '?'}"
                    )
            if lines:
                return_data["data"] = head + "\n".join(lines)
            else:
                return_data["data"] = head + "暂无成交记录"

        params: Dict[str, Any] = {"name": name, "token": self.token}
        if str(server or "").strip():
            params["server"] = server

        return await self._request_api(
            path="/trade/item/records",
            params=params,
            processor=processor,
        )

    async def event_strategy(self, name: str = "") -> Dict[str, Any]:
        """奇遇攻略信息"""
        if not str(name or "").strip():
            return self._hint(
                "用法：奇遇攻略 奇遇名称",
                "例：奇遇攻略 塞外宝驹",
            )

        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            if not isinstance(data, dict) or not data.get("name"):
                return_data["msg"] = "未找到该奇遇"
                return
            box = data.get("box")
            return_data["data"] = (
                f"【{data.get('name')}】\n"
                f"类型：{data.get('type') or '未知'}\n"
                f"等级：{data.get('level') or '?'}\n"
                f"攻略盒子：{box if box else '暂无'}"
            )

        return await self._request_api(
            path="/event/strategy",
            params={"name": name, "token": self.token},
            processor=processor,
        )

    async def skill_calculate(self, cooldown: float = 1.5) -> Dict[str, Any]:
        """急速计算"""

        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            lines = []
            for row in data or []:
                if not isinstance(row, dict):
                    continue
                try:
                    percent = float(row.get("hastePercent") or 0)
                except (TypeError, ValueError):
                    percent = 0.0
                level = row.get("level")
                lines.append(
                    f"· {row.get('nowFrame') or '?'} 帧"
                    f"　读条 {self._short_number(row.get('duration'))} 秒"
                    f"　急速 {percent:.2f}%"
                    f"　等级需求 {level if level is not None else '?'}"
                )
            head = (
                f"【急速计算 · 技能 CD {self._short_number(cooldown)} 秒】\n"
                f"（帧数相同的档位只保留最低急速）\n"
            )
            if lines:
                return_data["data"] = head + "\n".join(lines)
            else:
                return_data["data"] = head + "暂未获取到急速档位"

        return await self._request_api(
            path="/skill/calculate",
            params={"cooldown": cooldown, "token": self.token},
            processor=processor,
        )

    async def school_search(
        self, kungfu: str = "", mode: str = ""
    ) -> Dict[str, Any]:
        """推栏推荐配装"""
        if not str(kungfu or "").strip():
            return self._hint(
                "用法：推栏配装 心法 [模式]",
                "例：推栏配装 太虚剑意　推栏配装 剑纯",
            )

        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            rows = [row for row in (data or []) if isinstance(row, dict)]
            lines = []
            for index, row in enumerate(rows[:5], 1):
                tuilan = row.get("tuilanInfo") or {}
                author = tuilan.get("nickname") or "未知"
                tags = "、".join(str(tag) for tag in (row.get("tags") or []))
                line = (
                    f"{index}. {row.get('title') or '无标题'}\n"
                    f"　作者：{author}"
                    f"　装分：{self._short_number(row.get('score'))}"
                )
                if tags:
                    line += f"　标签：{tags}"
                if row.get("content"):
                    line += f"\n　{str(row['content'])[:80]}"
                lines.append(line)
            if lines:
                return_data["data"] = (
                    f"【推栏配装 · {kungfu}】共 {len(rows)} 条\n" + "\n".join(lines)
                )
            else:
                return_data["data"] = f"未找到「{kungfu}」的推荐配装"

        params: Dict[str, Any] = {
            "name": kungfu,
            "ticket": self.ticket,
            "token": self.token,
        }
        if str(mode or "").strip():
            params["mode"] = mode

        return await self._request_api(
            path="/school/search",
            params=params,
            processor=processor,
        )

    async def card_preset(self, server: str, name: str) -> Dict[str, Any]:
        """推栏名片预设图（图片链接为短时效签名，必须绕过缓存）"""

        async def processor(data: Any, return_data: Dict[str, Any]) -> None:
            if not isinstance(data, dict):
                return_data["msg"] = "未获取到名片预设"
                return
            url = data.get("showCardPresetUrl")
            if not url:
                return_data["msg"] = "未获取到名片预设图片"
                return
            return_data["data"] = [
                Comp.Plain(f"{server}-{name}　名片预设"),
                Comp.Image.fromURL(url),
                Comp.Plain(f"获赞总数：{data.get('praiseTotalCount') or 0}"),
            ]

        return await self._request_api(
            path="/card/preset",
            params={
                "server": server,
                "name": name,
                "ticket": self.ticket,
                "token": self.token,
            },
            processor=processor,
            force_refresh=True,
        )

    async def card_remote(
        self, server: str, name: str, force_refresh: bool = False
    ) -> Dict[str, Any]:
        """请求上游刷新名片；更新失败时明确报错。"""
        result = self._init_return_data()
        data, metadata = await self._cached_request(
            "/card/record",
            {"server": server, "name": name, "token": self.token},
            force_refresh=force_refresh,
        )
        result["_cache"] = metadata
        if isinstance(data, APIErrorResponse):
            result["msg"] = data.message or "名片更新失败，请稍后重试"
            return result
        if not isinstance(data, dict):
            result["msg"] = "未获取到名片，请稍后重试"
            return result
        if data.get("roleName") and str(data["roleName"]) != name:
            result["msg"] = "返回的名片与查询角色不符，请稍后重试"
            return result
        url = str(data.get("showAvatar") or "").strip()
        try:
            parsed = urlsplit(url)
            valid_url = parsed.scheme in {"https", "http"} and bool(parsed.hostname)
        except ValueError:
            valid_url = False
        if not valid_url:
            result["msg"] = "未获取到名片图片，请稍后重试"
            return result

        # 该图片服务会覆盖同一路径，用快照版本避免聊天端复用旧图。
        # 带签名参数的 URL 保持原样，以免签名失效。
        version = data.get("cacheTime") or metadata.get("created_at")
        if parsed.hostname == "static.nicemoe.cn" and not parsed.query and version:
            revision = hashlib.sha256(str(version).encode()).hexdigest()[:12]
            url = urlunsplit(parsed._replace(query=urlencode({"v": revision})))
        raw_likes = data.get("showLike")
        likes = str(raw_likes) if type(raw_likes) in (int, str) else ""
        likes = str(int(likes)) if re.fullmatch(r"[0-9]+", likes) else "暂未获取"
        result["data"] = [
            Comp.Plain(f"{data.get('serverName') or server}-{data.get('roleName') or name}"),
            Comp.Image.fromURL(url),
            Comp.Plain(f"点赞：{likes}"),
        ]
        result["code"] = 200
        return result


    # ==================== 行情（外观价格走势） ====================

    @staticmethod
    def _parse_tieba_price(text: str, keyword: str = "") -> Optional[int]:
        """从贴吧正文里抠出价格，优先取紧跟外观名称的 3-6 位数字。"""
        if not text:
            return None
        if keyword:
            match = re.search(
                re.escape(keyword) + r"[^0-9]{0,6}([0-9]{3,6})", text
            )
            if match:
                return int(match.group(1))
        match = re.search(r"([0-9]{3,6})", text)
        return int(match.group(1)) if match else None

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        """宽松转数字，0 / 空 / 非数字都返回 None。"""
        try:
            number = float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @staticmethod
    def _parse_gold_amount(value: Any) -> Optional[float]:
        """把「4万6000金」「700万1200金」这类文本解析成数字。"""
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            return float(value) if value > 0 else None
        text = str(value)
        total = 0.0
        matched = False
        for match in re.finditer(r"([0-9]+(?:\.[0-9]+)?)\s*万", text):
            total += float(match.group(1)) * 10000
            matched = True
        rest = re.sub(r"[0-9]+(?:\.[0-9]+)?\s*万", "", text)
        for match in re.finditer(r"([0-9]+(?:\.[0-9]+)?)", rest):
            total += float(match.group(1))
            matched = True
        return total if matched and total > 0 else None

    @classmethod
    def _series_from_rows(cls, rows: Any) -> List[tuple]:
        """从结构未知的记录列表里尽力抽出 (日期, 数值)。"""
        if not isinstance(rows, list):
            return []
        date_keys = (
            "date", "time", "day", "datetime",
            "refreshTime", "auctionTime", "createTime",
        )
        value_keys = (
            "value", "price", "money", "amount",
            "wanbaolou", "auctionAmount", "jin",
        )
        series = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            day = ""
            for key in date_keys:
                if row.get(key):
                    day = str(row[key])[:10].replace("/", "-")
                    break
            if not day:
                continue
            value = None
            for key in value_keys:
                if row.get(key) not in (None, ""):
                    value = cls._parse_gold_amount(row[key])
                    if value:
                        break
            if value:
                series.append((day, value))
        return series

    @staticmethod
    def _nice_step(span: float, target: int = 4) -> float:
        """取一个"好看"的刻度步长（1/2/2.5/5/10 档）。"""
        if span <= 0:
            return 1.0
        raw = span / max(target, 1)
        magnitude = 10 ** int(math.floor(math.log10(raw))) if raw > 0 else 1
        for mult in (1, 2, 2.5, 5, 10):
            step = magnitude * mult
            if step >= raw:
                return float(step)
        return float(magnitude * 10)

    @staticmethod
    def _build_trend(series: List[tuple], limit: int = 12, fmt=None) -> Dict[str, Any]:
        """把 (日期, 价格) 序列换算成 SVG 坐标；同一天多条取均值。"""
        grouped: Dict[str, List[float]] = {}
        for day, value in series or []:
            if not day or value in (None, ""):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number <= 0:
                continue
            grouped.setdefault(str(day)[:10], []).append(number)

        merged = sorted(
            (day, sum(values) / len(values)) for day, values in grouped.items()
        )[-limit:]
        if len(merged) < 2:
            return {}

        formatter = fmt or (lambda value: f"{value:,.0f}")
        values = [value for _, value in merged]
        low, high = min(values), max(values)
        if high <= low:
            high = low + max(low * 0.05, 1.0)

        step = JX3APIService._nice_step(high - low)
        axis_low = math.floor(low / step) * step
        while axis_low + step * 4 < high:
            axis_low += step
        axis_high = axis_low + step * 4
        span = axis_high - axis_low or 1

        left, right, top, bottom = 104.0, 936.0, 32.0, 262.0
        x_step = (right - left) / (len(merged) - 1)

        coords = []
        for index, (day, value) in enumerate(merged):
            x = left + x_step * index
            y = bottom - (value - axis_low) / span * (bottom - top)
            coords.append((round(x, 1), round(y, 1), day, value))

        peak, lowest = max(values), min(values)
        if len(merged) <= 8:
            candidates = list(range(len(merged)))
        else:
            candidates = list(range(0, len(merged), 2)) + [
                values.index(peak),
                values.index(lowest),
            ]

        # 按横坐标排序后做避让，相邻标签太近就丢掉后一个
        keep, last_x = [], None
        for index in sorted(set(candidates)):
            x = coords[index][0]
            if last_x is not None and x - last_x < 62:
                continue
            keep.append(index)
            last_x = x
        for index in (values.index(peak), values.index(lowest)):
            if index not in keep:
                keep.append(index)
        keep = set(keep)

        trend_points = []
        for index, (x, y, day, value) in enumerate(coords):
            if index == 0:
                anchor = "start"
            elif index == len(coords) - 1:
                anchor = "end"
            else:
                anchor = "middle"
            trend_points.append(
                {
                    "x": x,
                    "y": y,
                    "anchor": anchor,
                    "peak": value == peak,
                    "label": formatter(value) if index in keep else "",
                }
            )

        area = (
            f"M {coords[0][0]},{bottom} "
            + " ".join(f"L {x},{y}" for x, y, _, _ in coords)
            + f" L {coords[-1][0]},{bottom} Z"
        )

        y_ticks = [
            {
                "y": round(top + (bottom - top) * i / 4, 1),
                "label": formatter(axis_high - (axis_high - axis_low) * i / 4),
            }
            for i in range(5)
        ]

        xtick_indexes = (0, len(coords) // 2, len(coords) - 1)
        anchors = ("start", "middle", "end")
        x_ticks = [
            {
                "x": coords[index][0],
                "label": coords[index][2],
                "anchor": anchors[position],
            }
            for position, index in enumerate(xtick_indexes)
        ]

        return {
            "trend_polyline": " ".join(f"{x},{y}" for x, y, _, _ in coords),
            "trend_area": area,
            "trend_points": trend_points,
            "y_ticks": y_ticks,
            "x_ticks": x_ticks,
            "trend_count": len(coords),
        }

    _TREND_STYLE = (
        "<style>"
        ".jx3-trend{max-width:1000px;margin:14px auto 0;padding:16px 20px 10px;"
        "background:#ffffff;border:1px solid #ecebe4;border-radius:14px;"
        "font-family:\"Microsoft YaHei\",\"PingFang SC\",\"Noto Sans CJK SC\",Arial,sans-serif}"
        ".jx3-trend__head{display:flex;align-items:baseline;gap:14px;margin-bottom:2px}"
        ".jx3-trend__title{font-size:18px;font-weight:700;color:#3f3a35}"
        ".jx3-trend__unit{font-size:14px;color:#b0ada4}"
        ".jx3-trend__hint{margin-left:auto;font-size:14px;color:#b0ada4}"
        ".jx3-trend svg{display:block;width:100%;height:auto}"
        ".jx3-trend .trend-area{fill:url(#jx3TrendFill)}"
        ".jx3-trend .axis-text{font-size:13px;fill:#bcb9b0}"
        ".jx3-trend .grid-line{stroke:#f4f2ec;stroke-width:1}"
        ".jx3-trend .trend-line{fill:none;stroke:#d97757;stroke-width:2.5;"
        "stroke-linecap:round;stroke-linejoin:round}"
        ".jx3-trend .trend-dot{fill:#ffffff;stroke:#d97757;stroke-width:2}"
        ".jx3-trend .peak-dot{fill:#c96442;stroke:#ffffff;stroke-width:2}"
        ".jx3-trend .trend-label{font-size:13px;fill:#c96442;font-weight:700}"
        ".jx3-trend__empty{padding:26px 0 20px;text-align:center;font-size:14px;color:#b5b2aa}"".jx3-trend__foot{margin-top:8px;padding-top:9px;border-top:1px solid #f2f0ea;"
        "font-size:13px;color:#bcb9b0;text-align:center}"
        "</style>"
    )

    @classmethod
    def _trend_svg(
        cls,
        series: Any,
        title: str = "历史价格走势",
        unit: str = "",
        limit: int = 12,
        fmt=None,
        foot: str = "数据来自 JX3API / 万宝楼 / 百度贴吧，仅供参考",
    ) -> str:
        """生成自包含的 SVG 走势图（内联样式，任何模板塞进去就能用）。"""
        geometry = cls._build_trend(series, limit=limit, fmt=fmt)
        if not geometry:
            days = sorted(
                {
                    str(day)[:10]
                    for day, value in (series or [])
                    if day and value not in (None, "")
                }
            )
            if not days:
                return ""
            if len(days) == 1:
                reason = f"成交记录都集中在 {days[0]} 一天，构不成走势"
            else:
                reason = "成交记录不足 2 天，暂不绘制走势"
            return (
                '<div class="jx3-trend">'
                + cls._TREND_STYLE
                + '<div class="jx3-trend__head">'
                + f'<span class="jx3-trend__title">{html.escape(title)}</span>'
                + '<span class="jx3-trend__hint">数据不足</span>'
                + "</div>"
                + f'<div class="jx3-trend__empty">{html.escape(reason)}</div>'
                + f'<div class="jx3-trend__foot">{html.escape(foot)}</div>'
                + "</div>"
            )

        parts = [
            '<div class="jx3-trend">',
            cls._TREND_STYLE,
            '<div class="jx3-trend__head">',
            f'<span class="jx3-trend__title">{html.escape(title)}</span>',
        ]
        if unit:
            parts.append(
                f'<span class="jx3-trend__unit">单位：{html.escape(unit)}</span>'
            )
        parts.append(
            f'<span class="jx3-trend__hint">共 {geometry["trend_count"]} 个数据点</span>'
        )
        parts.append("</div>")

        parts.append(
            '<svg viewBox="0 0 1000 320" width="100%" role="img">'
            "<defs>"
            '<linearGradient id="jx3TrendFill" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0%" stop-color="#d97757" stop-opacity="0.20"></stop>'
            '<stop offset="100%" stop-color="#d97757" stop-opacity="0"></stop>'
            "</linearGradient>"
            "</defs>"
        )
        parts.append(f'<path class="trend-area" d="{geometry["trend_area"]}"></path>')

        for tick in geometry["y_ticks"]:
            parts.append(
                f'<line class="grid-line" x1="104" y1="{tick["y"]}" '
                f'x2="936" y2="{tick["y"]}"></line>'
            )
            parts.append(
                f'<text class="axis-text" x="94" y="{tick["y"] + 5}" '
                f'text-anchor="end">{html.escape(tick["label"])}</text>'
            )

        parts.append(
            f'<polyline class="trend-line" points="{geometry["trend_polyline"]}">'
            "</polyline>"
        )

        for point in geometry["trend_points"]:
            css = "peak-dot" if point["peak"] else "trend-dot"
            parts.append(
                f'<circle class="{css}" cx="{point["x"]}" cy="{point["y"]}" r="3.6">'
                "</circle>"
            )
        for point in geometry["trend_points"]:
            if point["label"]:
                parts.append(
                    f'<text class="trend-label" x="{point["x"]}" '
                    f'y="{point["y"] - 12}" text-anchor="{point["anchor"]}">'
                    f'{html.escape(point["label"])}</text>'
                )

        for tick in geometry["x_ticks"]:
            parts.append(
                f'<text class="axis-text" x="{tick["x"]}" y="304" '
                f'text-anchor="{tick["anchor"]}">{html.escape(tick["label"])}</text>'
            )

        parts.append("</svg>")
        parts.append(f'<div class="jx3-trend__foot">{html.escape(foot)}</div>')
        parts.append("</div>")
        return "".join(parts)

    @classmethod
    def _market_render_data(
        cls,
        data: Any,
        tieba_source: Any = None,
        fallback_name: str = "",
        trend_title: str = "历史价格走势",
        trend_unit: str = "元",
    ) -> Dict[str, Any]:
        """把外观价格接口的返回整理成价格页需要的渲染数据。"""
        sale_status = {
            1: "出售", 2: "收购", 3: "想出", 4: "想收",
            5: "成交", 6: "正出", 7: "公示",
        }
        if not isinstance(data, dict):
            data = {}

        name = data.get("name") or fallback_name
        groups = {
            group.get("name"): group
            for group in (data.get("list") or [])
            if isinstance(group, dict)
        }

        notice = [
            item
            for item in ((groups.get("公示期") or {}).get("list") or [])
            if isinstance(item, dict)
        ]
        sale = [
            item
            for item in ((groups.get("在售期") or {}).get("list") or [])
            if isinstance(item, dict)
        ]

        deals = []
        for zone, group in groups.items():
            if zone in {"公示期", "在售期"}:
                continue
            for item in (group.get("list") or []):
                if not isinstance(item, dict):
                    continue
                deals.append(
                    {
                        "date": str(item.get("date") or ""),
                        "server": str(item.get("server") or ""),
                        "value": item.get("value"),
                        "status": sale_status.get(item.get("sale"), "记录"),
                    }
                )
        deals.sort(key=lambda row: row["date"], reverse=True)

        tieba = []
        for item in (tieba_source or []):
            if not isinstance(item, dict):
                continue
            context = str(item.get("context") or "")
            price = cls._parse_tieba_price(context, name)
            if not price:
                continue
            if "出" in context:
                status = "出售"
            elif "收" in context:
                status = "收购"
            else:
                status = "贴吧"
            try:
                day = datetime.fromtimestamp(
                    int(item.get("time") or 0)
                ).strftime("%Y-%m-%d")
            except (TypeError, ValueError, OSError, OverflowError):
                day = ""
            tieba.append(
                {
                    "date": day,
                    "server": str(item.get("server") or ""),
                    "value": price,
                    "status": status,
                }
            )
        tieba.sort(key=lambda row: row["date"], reverse=True)

        return market_page(dict(data, name=name), notice, sale, deals, tieba)

    async def role_online(self, name: str = "", server: str = "") -> Dict[str, Any]:
        """角色在线状态（依赖第三方在线接口，需在插件配置中填写）

        接口约定（参考 Inkar-Suki 项目）：
            GET  {online_api_url}/role_online
            参数 zone / server / name / role_id / global_role_id / token
            返回 {"data": [{"gameLogin": true|false, ...}]}
        未配置 online_api_url / online_api_token 时给出提示，不报错。
        """
        if not str(name or "").strip():
            return self._hint("用法：在线 服务器 角色名")

        base = str(self._config.get("online_api_url", "") or "").strip()
        api_token = str(self._config.get("online_api_token", "") or "").strip()
        if not base or not api_token:
            return self._hint(
                "角色在线查询暂不可用：尚未接入可验证的实时在线服务。",
                "当前推栏标识用于已有角色查询，单独填写它无法启用在线查询。",
            )

        detail, _ = await self._cached_request(
            "/role/detail",
            {"server": server, "name": name, "token": self.token, "ticket": self.ticket},
        )
        if isinstance(detail, APIErrorResponse) or not isinstance(detail, dict) \
                or not detail.get("roleName"):
            return self._hint(f"没找到角色「{name}」，检查下服务器和名字？")

        params = {
            "zone": detail.get("zoneName", ""),
            "server": detail.get("serverName") or server,
            "name": detail.get("roleName") or name,
            "role_id": detail.get("roleId", ""),
            "global_role_id": detail.get("globalId", ""),
            "token": api_token,
        }

        from urllib.parse import urlparse
        base = base if "://" in base else f"https://{base}"
        parsed = urlparse(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return self._hint("在线查询服务地址无效，当前无法判断角色在线状态。")
        endpoint = base.rstrip("/") if parsed.path.strip("/") else f"{base.rstrip('/')}/role_online"
        payload = await self._api.get(endpoint, params=params)
        online = parse_game_online(payload)
        if online is None:
            return self._hint("在线查询未取得有效状态，当前无法判断角色在线或离线。")

        # ★ 用接口返回的完整信息组装显示 ✅
        row = {}
        if isinstance(payload, dict):
            rows = payload.get("data")
            if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                row = rows[0]
        return_data = self._init_return_data()
        return_data["code"] = 200
        # ★ 一条消息：文字 + 门派/阵营图标嵌在文字里 ✅
        return_data["data"] = self._format_online_chain(row, online, name, server, params)
        return return_data

    @staticmethod
    def _format_online_chain(row: Dict[str, Any], online: bool, name: str,
                             server: str, params: Dict[str, Any]) -> list:
        """组装「在线」消息链 ✅ 图标直接嵌在文字中间"""
        import time as _time
        comps = Comp if "Comp" in globals() else None
        if comps is None:
            from astrbot.api import message_components as comps  # noqa

        api_name = row.get("name") or params.get("name") or name
        api_server = row.get("server") or server
        zone = row.get("zone") or ""
        force = str(row.get("forceName") or "").strip()
        level = row.get("level") or ""
        bodily = row.get("bodily") or ""
        camp = str(row.get("camp") or "").strip()
        map_name = row.get("mapName") or ""
        role_id = row.get("roleId") or row.get("gameRoleId") or params.get("role_id") or ""
        game_login = row.get("gameLogin")
        app_login = row.get("appLogin")

        def _yes(v) -> str:
            if v is None:
                return "未知"
            try:
                return "是" if int(v) else "否"
            except Exception:
                return "是" if v else "否"

        base = pathlib.Path(__file__).resolve().parent.parent / "templates"
        sect_icon = base / "sect" / f"{force}.png" if force else None
        camp_icon = base / "img" / f"{camp}.png" if camp else None

        def _img(path):
            """本地图片 → 消息组件 ✅"""
            try:
                if path is None or not path.exists():
                    return None
                # 用 base64 方式最稳 ✅
                import base64
                b64 = base64.b64encode(path.read_bytes()).decode()
                return comps.Image.from_base64(b64) if hasattr(comps.Image, "from_base64") \
                    else comps.Image(file=str(path))
            except Exception:
                return None

        chain = []
        chain.append(comps.Plain(
            f"{'🟢 在线' if online else '⚪ 离线'}  ✦ {api_name} ✦\n"
            f"{api_server}" + (f" · {zone}" if zone else "") + "\n"
            "━━━━━━━━━━━━━━\n"))

        # 门派行（纯文字 ✅ 不要图标）
        if force:
            chain.append(comps.Plain(f"⚔️  门派：{force}" + (f"  Lv.{level}" if level else "") + "\n"))
        elif level:
            chain.append(comps.Plain(f"⚔️  等级：Lv.{level}\n"))

        if bodily:
            chain.append(comps.Plain(f"🧍 体型：{bodily}\n"))
        if online and map_name:
            chain.append(comps.Plain(f"📍 地图：{map_name}\n"))

        # 阵营行（纯文字 ✅ 不要图标）
        if camp:
            chain.append(comps.Plain(f"🏳️  阵营：{camp}\n"))

        chain.append(comps.Plain(f"📱 手机在线：{_yes(app_login)}\n"))
        chain.append(comps.Plain(f"💻 电脑在线：{_yes(game_login)}\n"))
        if role_id:
            chain.append(comps.Plain(f"🆔 角色ID：{role_id}\n"))
        chain.append(comps.Plain("━━━━━━━━━━━━━━\n"))
        chain.append(comps.Plain(f"🕐 查询时间：{_time.strftime('%m-%d %H:%M')}"))
        if api_name and api_name != name:
            chain.append(comps.Plain(
                f"\n⚠️ 注意：在线服务返回的是「{api_name}」，不是你查的「{name}」"))
        return chain

    @staticmethod
    def _online_icons(row: Dict[str, Any]) -> list:
        """返回门派/阵营图标本地路径 ✅"""
        if not isinstance(row, dict):
            return []
        base = pathlib.Path(__file__).resolve().parent.parent / "templates"
        out = []
        force = str(row.get("forceName") or "").strip()
        camp = str(row.get("camp") or "").strip()
        if force:
            f = base / "sect" / f"{force}.png"
            if f.exists():
                out.append(str(f))
        if camp:
            f = base / "img" / f"{camp}.png"
            if f.exists():
                out.append(str(f))
        return out

    @staticmethod
    def _format_online(row: Dict[str, Any], online: bool, name: str,
                       server: str, params: Dict[str, Any]) -> str:
        """组装「角色在线」显示内容 ✅"""
        import time as _time

        api_name = row.get("name") or params.get("name") or name
        api_server = row.get("server") or server
        zone = row.get("zone") or ""
        force = row.get("forceName") or ""
        level = row.get("level") or ""
        bodily = row.get("bodily") or ""
        camp = row.get("camp") or ""
        map_name = row.get("mapName") or ""
        role_id = row.get("roleId") or row.get("gameRoleId") or params.get("role_id") or ""
        game_login = row.get("gameLogin")
        app_login = row.get("appLogin")

        def _yes(v) -> str:
            if v is None:
                return "未知"
            try:
                return "是" if int(v) else "否"
            except Exception:
                return "是" if v else "否"

        lines = []
        lines.append(f"{'🟢 在线' if online else '⚪ 离线'}  ✦ {api_name} ✦")
        lines.append(f"{api_server}" + (f" · {zone}" if zone else ""))
        lines.append("━━━━━━━━━━━━━━")
        if force:
            lines.append(f"⚔️  门派：{force}" + (f"  Lv.{level}" if level else ""))
        elif level:
            lines.append(f"⚔️  等级：Lv.{level}")
        if bodily:
            lines.append(f"🧍 体型：{bodily}")
        if camp:
            lines.append(f"🏳️  阵营：{camp}")
        if online:
            lines.append(f"📍 地图：{map_name or '（未知）'}")
        lines.append(f"📱 手机在线：{_yes(app_login)}")
        lines.append(f"💻 电脑在线：{_yes(game_login)}")
        if role_id:
            lines.append(f"🆔 角色ID：{role_id}")
        lines.append("━━━━━━━━━━━━━━")
        lines.append(f"🕐 查询时间：{_time.strftime('%m-%d %H:%M')}")
        # ⚠️ 服务返回的角色和查询不一致时提示 ✅
        if api_name and api_name != name:
            lines.append(f"⚠️ 注意：在线服务返回的是「{api_name}」，不是你查的「{name}」")
            lines.append("   （该服务只追踪单一账号 ⚠️）")
        return "\n".join(lines)


    async def online_rows(self, roles: list) -> list:
        """批量查在线状态 ✅ 返回 [{name, server, force, camp, status, icon}]

        status: 2=游戏在线 / 1=仅推栏在线 / 0=离线 / -1=查询失败
        """
        import base64
        base = pathlib.Path(__file__).resolve().parent.parent / "templates"
        icons_dir = base / "sect"
        out = []

        def _icon_b64(force: str) -> str:
            if not force:
                return ""
            f = icons_dir / f"{force}.png"
            if not f.exists():
                return ""
            try:
                return "data:image/png;base64," + base64.b64encode(f.read_bytes()).decode()
            except Exception:
                return ""

        api_base = str(self._config.get("online_api_url", "") or "").strip()
        api_token = str(self._config.get("online_api_token", "") or "").strip()

        for r in roles or []:
            name = str(r.get("name") or "")
            server = str(r.get("server") or "")
            item = {"name": name, "server": server,
                    "force": r.get("force", ""), "camp": r.get("camp", ""),
                    "status": -1, "icon": _icon_b64(r.get("force", ""))}
            try:
                # ① 拿 role_id ✅
                detail, _ = await self._cached_request(
                    "/role/detail",
                    {"server": server, "name": name,
                     "token": self.token, "ticket": self.ticket},
                )
                if not isinstance(detail, dict) or not detail.get("roleName"):
                    out.append(item); continue
                item["force"] = detail.get("forceName") or item["force"]
                item["camp"] = detail.get("campName") or item["camp"]
                item["icon"] = _icon_b64(item["force"])
                if not api_base or not api_token:
                    out.append(item); continue
                # ② 查在线 ✅
                params = {
                    "zone": detail.get("zoneName", ""),
                    "server": detail.get("serverName") or server,
                    "name": detail.get("roleName") or name,
                    "role_id": detail.get("roleId", ""),
                    "global_role_id": detail.get("globalId", ""),
                    "token": api_token,
                }
                from urllib.parse import urlparse
                url = api_base if "://" in api_base else f"https://{api_base}"
                parsed = urlparse(url)
                endpoint = (url.rstrip("/") if parsed.path.strip("/")
                            else f"{url.rstrip('/')}/role_online")
                payload = await self._api.get(endpoint, params=params)
                if isinstance(payload, dict):
                    rows = payload.get("data")
                    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                        row = rows[0]
                        gl, al = row.get("gameLogin"), row.get("appLogin")
                        def _on(v):
                            try: return bool(int(v))
                            except Exception: return bool(v)
                        if _on(gl):
                            item["status"] = 2
                        elif _on(al):
                            item["status"] = 1
                        else:
                            item["status"] = 0
            except Exception:
                pass
            out.append(item)
        return out

    async def role_state(self, name: str = "", server: str = "") -> Dict[str, Any]:
        """推栏角色状态：角色档案 + 名片状态 + 最近战绩

        注：JX3API 没有「角色是否在线」接口（文档 80 个接口里没有），
            名片字段 showActive 表示「名片是否在展示」，不是游戏在线。
            真正在线的实现见 role_online()。
        """
        if not str(name or "").strip():
            return self._hint("用法：角色状态 服务器 角色名")

        params = {"server": server, "name": name,
                  "token": self.token, "ticket": self.ticket}

        detail, _ = await self._cached_request("/role/detail", params)
        if isinstance(detail, APIErrorResponse):
            return self._hint(f"查询失败：{detail.message or '接口返回错误'}")
        if not isinstance(detail, dict) or not detail.get("roleName"):
            return self._hint(f"没找到角色「{name}」，检查下服务器和名字？")

        (card, _), (arena, _) = await asyncio.gather(
            self._cached_request("/card/record", params),
            self._cached_request("/arena/recent", {**params, "mode": "33"}),
        )
        card = card if isinstance(card, dict) else {}
        arena = arena if isinstance(arena, dict) else {}

        recent = arena.get("history") or ((arena.get("performance") or {}).get("3v3")) or []
        recent = recent if isinstance(recent, list) else []
        card_active = bool(card.get("showActive"))

        return_data = self._init_return_data()
        return_data["data"] = {
            "name": detail.get("roleName", ""),
            "server": detail.get("serverName", ""),
            "zone": detail.get("zoneName", ""),
            "force": detail.get("forceName", ""),
            "body": detail.get("bodyName", ""),
            "camp": detail.get("campName", ""),
            "tong": detail.get("tongName", ""),
            "roleId": detail.get("roleId", ""),
            "avatar": card.get("showAvatar", "") or "",
            "cardActive": card_active,
            "likes": card.get("showLike", 0),
            "recentCount": len(recent),
            "recent": recent[:6],
            "activeText": "名片展示中（≠ 游戏在线）" if card_active else "名片未展示",
        }
        try:
            return_data["temp"] = await self._template_repo().get("role_state.html")
        except FileNotFoundError as exc:
            logger.error(f"加载角色状态模板失败: {exc}")
            return_data["msg"] = "系统错误：模板文件不存在"
            return return_data
        return_data["code"] = 200
        return return_data

    async def hangqing(self, name: str = "") -> Dict[str, Any]:
        """外观行情：万宝楼公示/在售 + 贴吧 + 成交 + 历史价格走势"""
        if not str(name or "").strip():
            return self._hint("用法：行情 外观名称", "例：行情 云旌望捷礼盒")

        data, _ = await self._cached_request(
            "/trade/item/records", {"name": name, "token": self.token}
        )
        if isinstance(data, APIErrorResponse):
            return self._hint(f"查询失败：{data.message or '接口返回错误'}")
        if not isinstance(data, dict) or not data.get("name"):
            return self._hint(f"没找到外观「{name}」，换个名字试试？")

        tieba_raw, _ = await self._cached_request(
            "/tieba/item/records", {"name": name, "token": self.token}
        )

        return_data = self._init_return_data()
        return_data["data"] = await self._market_data_with_history(
            data,
            tieba_source=tieba_raw if isinstance(tieba_raw, list) else [],
        )
        try:
            return_data["temp"] = await self._hangqing_repo().get("hangqing.html")
        except FileNotFoundError as exc:
            logger.error(f"加载行情模板失败: {exc}")
            return_data["msg"] = "系统错误：模板文件不存在"
            return return_data
        return_data["code"] = 200
        return return_data

    @staticmethod
    def _template_repo():
        """通用模板仓库（复用单例）。"""
        from .template import get_template_repository
        return get_template_repository()

    @staticmethod
    def _hangqing_repo():
        """复用同一个模板仓库实例。"""
        return JX3APIService._template_repo()
