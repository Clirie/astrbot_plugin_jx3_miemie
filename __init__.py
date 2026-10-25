"""astrbot_plugin_jx3_miemie —— 剑网三咩咩（纯阳弟子 ✅）

⚠️ 本文件是 AstrBot 加载插件的入口，请勿删除
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from .main import Jx3ApiPlugin  # noqa: E402

__all__ = ["Jx3ApiPlugin"]
