"""Read-only Tuilan H5 dungeon progress; account credentials never enter logs."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from typing import Any

import aiohttp


CD_URL = "https://w.pvp.xoyo.com:31727/api/h5/parser/cd-process/get-by-role"
CD_CACHE_ENDPOINT = "/tuilan/cd-process"
# Public application constant from the official H5 bundle, not a user credential.
_H5_SIGNING_KEY = b'y5BKu8Kb_}y;S#?U65B:TC,T'
DEFAULT_FOCUS = (
    "阆风悬城·元心殿|25人挑战",
    "阆风悬城|25人英雄",
    "阆风悬城|25人普通",
    "阆风悬城|10人普通",
    "会战弓月城|25人英雄",
)


class TuilanCDError(Exception):
    """Only fixed, user-readable messages; no response bodies or credentials."""


def signed_cd_body(global_role_id: str) -> dict[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", global_role_id):
        raise TuilanCDError("角色标识无效，请检查服务器和角色名。")
    body = {
        "globalRoleId": global_role_id,
        "ts": datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")[:-3],
    }
    canonical = "&".join(key + "=" + value for key, value in sorted(body.items()))
    body["sign"] = hmac.new(_H5_SIGNING_KEY, canonical.encode(), hashlib.sha1).hexdigest()
    return body


def normalize_progress(data: Any) -> list[dict]:
    """Validate before caching: malformed data must never become 'no CD'."""
    if not isinstance(data, list) or len(data) > 128:
        raise TuilanCDError("推栏副本数据格式发生变化，请稍后重试或联系管理员。")
    raids = []
    seen = set()
    for item in data:
        if not isinstance(item, dict):
            raise TuilanCDError("推栏副本数据不完整，请稍后重试。")
        name, mode = item.get("mapName"), item.get("mapType")
        bosses = item.get("bossProgress")
        if (not isinstance(name, str) or not name.strip() or len(name) > 100
                or not isinstance(mode, str) or not mode.strip() or len(mode) > 30
                or not isinstance(bosses, list) or len(bosses) > 64):
            raise TuilanCDError("推栏副本数据不完整，请稍后重试。")
        key = (name.strip(), mode.strip())
        if key in seen:
            raise TuilanCDError("推栏返回了重复的副本进度，请稍后重试。")
        seen.add(key)
        progress = []
        for boss in bosses:
            if not isinstance(boss, dict) or not isinstance(boss.get("name"), str) or not boss["name"].strip():
                raise TuilanCDError("推栏首领数据不完整，请稍后重试。")
            # Unknown status stays unknown; strings such as 'false' are not truthy flags.
            finished = boss.get("finished")
            if type(finished) is not bool:
                finished = None
            progress.append({"name": boss["name"][:100], "finished": finished})
        raids.append({"name": key[0], "mode": key[1], "bosses": progress,
                      "finished": sum(b["finished"] is True for b in progress),
                      "total": len(progress), "recorded": True})
    return raids


class TuilanCDClient:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def get(self, global_role_id: str, token: str) -> list[dict]:
        if not token.strip():
            raise TuilanCDError("副本 CD 需要推栏登录凭据，请管理员配置推栏标识。")
        body = signed_cd_body(global_role_id)
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8, connect=3),
                cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
            )
        try:
            # A dedicated cookie-free session and disabled redirects keep the token
            # on this one verified official origin. TLS verification is mandatory.
            async with self._session.post(
                CD_URL, json=body, headers={"token": token, "Accept": "application/json"},
                allow_redirects=False, ssl=True,
            ) as response:
                status = response.status
                chunks = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    chunks.extend(chunk)
                    if len(chunks) > 1024 * 1024:
                        raise TuilanCDError("推栏副本数据过大，请稍后重试。")
                try:
                    payload = json.loads(chunks)
                except (ValueError, UnicodeError):
                    payload = None
        except asyncio.TimeoutError:
            raise TuilanCDError("推栏副本查询超时，请稍后重试。") from None
        except aiohttp.ClientError:
            raise TuilanCDError("暂时无法连接推栏，请稍后重试。") from None
        code = payload.get("code") if isinstance(payload, dict) else None
        if status == 401 or code in (-700, "-700"):
            raise TuilanCDError("推栏登录已失效，请管理员更新推栏标识。")
        if code in (-200, "-200"):
            raise TuilanCDError("推栏请求签名校验失败，请联系管理员更新插件。")
        if status == 429:
            raise TuilanCDError("推栏查询过于频繁，请稍后再试。")
        if status == 403:
            raise TuilanCDError("当前推栏账号无权读取该角色的副本进度。")
        if status != 200 or type(code) not in (int, str) or code not in (0, "0"):
            raise TuilanCDError("推栏未能返回该角色的副本进度，请核对角色信息后重试。")
        return normalize_progress(payload.get("data"))


def build_cd_page(raids: list[dict], weekly: list[str], focus: Any = None) -> dict:
    """Merge a maintained focus list, current weekly names and real progress."""
    configured = DEFAULT_FOCUS if focus is None else focus
    if not isinstance(configured, (list, tuple)):
        configured = DEFAULT_FOCUS
    rows: dict[tuple[str, str], dict] = {}
    weekly_names = {name.strip() for name in weekly if isinstance(name, str) and name.strip()}

    def add(name, mode, category):
        key = (name, mode)
        if key not in rows:
            rows[key] = {"name": name, "mode": mode, "category": category,
                         "weekly": name in weekly_names and mode == "10人普通",
                         "recorded": False, "bosses": [], "finished": 0, "total": 0}

    for value in configured[:32]:
        if not isinstance(value, str) or "|" not in value:
            continue
        name, mode = (part.strip() for part in value.split("|", 1))
        if name and re.fullmatch(r"(?:10|25)人(?:普通|英雄|挑战)", mode):
            add(name[:100], mode, "主流秘境")
    # Calendar's weekly.raid names describe the 10-player weekly route. Avoid
    # inferring completion of the weekly quest from individual boss CD flags.
    for name in weekly:
        if isinstance(name, str) and name.strip():
            add(name.strip()[:100], "10人普通", "本周周常")
    for raid in raids:
        key = (raid["name"], raid["mode"])
        add(*key, "其他进度")
        rows[key].update(raid)
    return {"raids": list(rows.values()), "recorded_count": len(raids),
            "finished_count": sum(r["finished"] for r in raids)}
