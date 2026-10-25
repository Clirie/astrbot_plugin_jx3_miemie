"""角色绑定存储 ✅

按【用户】记录绑定的角色，支持多角色 ✅

数据结构：
{
    "用户ID": [
        {"server": "唯我独尊", "name": "清唳", "zone": "电信区",
         "roleId": "13299022", "force": "纯阳", "camp": "浩气盟",
         "bound_at": "2026-10-24 19:01"},
        ...
    ]
}
"""

from __future__ import annotations

import json
import pathlib
import threading
import time
from typing import Any, Dict, List


class RoleBindingService:
    """角色绑定（按用户 ✅）"""

    def __init__(self, data_path: pathlib.Path | str | None = None):
        if data_path is None:
            data_path = pathlib.Path(__file__).resolve().parent.parent / "data" / "role_bindings.json"
        self.path = pathlib.Path(data_path)
        self._lock = threading.Lock()
        self._cache: Dict[str, List[Dict[str, Any]]] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    def _load(self) -> Dict[str, List[Dict[str, Any]]]:
        if self._loaded:
            return self._cache
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._cache = raw
        except Exception:
            self._cache = {}
        self._loaded = True
        return self._cache

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass

    # ------------------------------------------------------------------
    @staticmethod
    def _key(uid: Any) -> str:
        return str(uid or "").strip()

    def list_of(self, uid: Any) -> List[Dict[str, Any]]:
        """该用户绑定列表 ✅"""
        with self._lock:
            data = self._load()
            return list(data.get(self._key(uid), []))

    def find(self, uid: Any, server: str = "", name: str = "") -> Dict[str, Any] | None:
        """查找某个绑定 ✅"""
        name = str(name or "").strip()
        server = str(server or "").strip()
        for item in self.list_of(uid):
            if item.get("name") != name:
                continue
            if server and item.get("server") != server:
                continue
            return item
        return None

    def bind(self, uid: Any, info: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        """绑定 ✅ 返回 (状态, 条目)

        状态：added / exists / limit
        """
        key = self._key(uid)
        name = str(info.get("name") or "").strip()
        server = str(info.get("server") or "").strip()
        with self._lock:
            data = self._load()
            rows = data.setdefault(key, [])
            for item in rows:
                if item.get("name") == name and item.get("server") == server:
                    return "exists", item
            if len(rows) >= 20:
                return "limit", {}
            entry = {
                "server": server,
                "name": name,
                "zone": info.get("zone", ""),
                "roleId": info.get("roleId", ""),
                "force": info.get("force", ""),
                "camp": info.get("camp", ""),
                "bound_at": time.strftime("%Y-%m-%d %H:%M"),
            }
            rows.append(entry)
            self._save()
            return "added", entry

    def unbind(self, uid: Any, server: str = "", name: str = "") -> Dict[str, Any] | None:
        """解绑 ✅ 返回被删条目（没找到返回 None）"""
        key = self._key(uid)
        name = str(name or "").strip()
        server = str(server or "").strip()
        with self._lock:
            data = self._load()
            rows = data.get(key, [])
            for i, item in enumerate(rows):
                if item.get("name") != name:
                    continue
                if server and item.get("server") != server:
                    continue
                removed = rows.pop(i)
                self._save()
                return removed
            return None

    def clear(self, uid: Any) -> int:
        """清空该用户的绑定 ✅"""
        key = self._key(uid)
        with self._lock:
            data = self._load()
            n = len(data.get(key, []))
            data[key] = []
            self._save()
            return n


_global: RoleBindingService | None = None


def get_binding_service() -> RoleBindingService:
    global _global
    if _global is None:
        _global = RoleBindingService()
    return _global
