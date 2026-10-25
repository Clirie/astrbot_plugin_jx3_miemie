"""后处理文字层 —— 用真实字体把角色名和印章画到图上

为什么需要：
    AI 生图模型不会"打字"，只会"画字形"，生僻字（如「唳」）经常画错，
    小印章也容易糊。这里用真实字体绘制，保证正确与清晰。

字体：默认用插件自带的 Zhuque 书法体（data/Zhuque.ttf）。
      想换成更清晰的黑体/宋体，改 FONT_MODE。
"""

import asyncio
from pathlib import Path

from astrbot.api import logger

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

SEAL_RED = (178, 42, 38)
SEAL_TEXT = (255, 250, 244)
INK = (26, 24, 22)


class TextOverlay:
    """在生成图上叠加「角色名（左侧竖排）」与「红色印章」。"""

    # 字体模式： "brush" = 书法体（Zhuque）｜ "clear" = 清晰无衬线
    FONT_MODE = "brush"
    SYS_CLEAR = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"

    def __init__(self, plugin_root: Path):
        self.root = Path(plugin_root)
        self.zhuque = self.root / "data" / "Zhuque.ttf"
        self.font_path = self._pick_font()
        self._fonts: dict[int, "ImageFont.FreeTypeFont"] = {}

    def _pick_font(self) -> Path:
        if self.FONT_MODE == "brush" and self.zhuque.is_file():
            return self.zhuque
        p = Path(self.SYS_CLEAR)
        return p if p.is_file() else self.zhuque

    def _font(self, size: int):
        size = max(int(size), 8)
        if size not in self._fonts:
            try:
                self._fonts[size] = ImageFont.truetype(str(self.font_path), size)
            except Exception:
                logger.warning("字体加载失败: %s", self.font_path)
                self._fonts[size] = ImageFont.load_default()
        return self._fonts[size]

    # ---------- 主入口 ----------
    def draw(self, img_path: str, name: str = "", seal_text: str = "剑网3") -> str:
        if not _PIL_OK:
            return img_path
        name = str(name or "").strip()
        seal_text = str(seal_text or "").strip()
        try:
            im = Image.open(img_path).convert("RGB")
        except Exception as exc:
            logger.warning("文字层：打开图片失败 %s", exc)
            return img_path

        W, H = im.size
        draw = ImageDraw.Draw(im)

        # ================= ① 左侧竖排角色名 =================
        name_bottom = int(H * 0.05)
        if name:
            fs = max(int(H * 0.078), 30)
            font = self._font(fs)
            line = int(fs * 1.12)
            x = int(W * 0.055)
            y = int(H * 0.05)
            # 淡墨投影，增加层次（只画一次，避免糊）
            for i, ch in enumerate(name):
                draw.text((x + 3, y + i * line + 3), ch, font=font, fill=(206, 200, 192))
            for i, ch in enumerate(name):
                draw.text((x, y + i * line), ch, font=font, fill=INK)
            name_bottom = y + len(name) * line

        # ================= ② 红色印章（保证完整不裁切）=================
        if seal_text:
            chars = list(seal_text)[:4]
            n = len(chars)
            cols = 2 if n > 1 else 1
            rows = 2 if n > 2 else 1          # 3~4 字用 2×2，1~2 字用 1 行

            side = max(int(H * 0.080), 56)
            pad = max(int(side * 0.13), 6)

            sx = int(W * 0.055)
            sy = name_bottom + max(int(H * 0.022), 14)
            # ★ 关键：把印章夹在画布内，绝不出界
            sx = max(6, min(sx, W - side - 6))
            sy = max(6, min(sy, H - side - 6))

            draw.rounded_rectangle(
                [sx, sy, sx + side, sy + side],
                radius=int(side * 0.10), fill=SEAL_RED,
            )
            inner_w = side - 2 * pad
            inner_h = side - 2 * pad
            cell_w = inner_w / cols
            cell_h = inner_h / rows
            cfs = int(min(cell_w, cell_h) * 0.88)
            sfont = self._font(cfs)
            for i, ch in enumerate(chars):
                c, r = i % cols, i // cols
                cx = sx + pad + cell_w * (c + 0.5)
                cy = sy + pad + cell_h * (r + 0.5)
                draw.text((cx, cy), ch, font=sfont, fill=SEAL_TEXT, anchor="mm")

        out_path = Path(str(img_path))
        new_path = out_path.with_name(out_path.stem + "_txt.jpg")
        try:
            im.save(new_path, "JPEG", quality=94)
            logger.info("文字层已绘制: %s（名字=%s 印章=%s）", new_path, name, seal_text)
            return str(new_path)
        except Exception as exc:
            logger.warning("文字层：保存失败 %s", exc)
            return img_path

    async def draw_async(self, img_path: str, name: str = "", seal_text: str = "剑网3") -> str:
        return await asyncio.to_thread(self.draw, img_path, name, seal_text)
