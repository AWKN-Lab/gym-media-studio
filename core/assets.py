"""画面素材层：自有实拍素材优先，无素材时程序化生成品牌化竖屏卡片。

素材库约定：把门店/教练/器械的照片或视频丢进 assets/gym/ 即自动启用，
文件名含关键词（如 深蹲、教练、器械、门店）会优先匹配对应分镜。
后续接入 AI 生图/生视频只需替换 pick_background() 的实现。
"""
from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .util import ASSETS, font_path, ffmpeg, run_cmd

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".m4v"}

PALETTES = [
    ("#121826", "#FF5A1F", "#FFD166"),
    ("#101B24", "#22D3A6", "#FFD166"),
    ("#1A1420", "#A78BFA", "#FF7A45"),
    ("#0F1C2E", "#38BDF8", "#FFD166"),
    ("#1C1414", "#FF6B6B", "#FFD166"),
]


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path(bold), size, index=0)


def _gradient(w: int, h: int, top: str, bottom: str) -> Image.Image:
    a, b = _hex(top), _hex(bottom)
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        # 轻微非线性，让上部更沉
        t = t ** 0.8
        d.line([(0, y), (w, y)], fill=tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)))
    return img


def _glow(img: Image.Image, cx: int, cy: int, radius: int, color: str, alpha: int = 90) -> None:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    r, g, b = _hex(color)
    d.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=(r, g, b, alpha))
    layer = layer.filter(ImageFilter.GaussianBlur(radius // 2))
    img.paste(Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB"))


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        trial = cur + ch
        if draw.textlength(trial, font=font) > max_width and cur:
            lines.append(cur)
            cur = ch
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def _text_block(draw, xy, text, font, fill, max_width, line_gap=18, anchor_shadow=True):
    x, y = xy
    for line in _wrap(draw, text, font, max_width):
        if anchor_shadow:
            draw.text((x + 3, y + 4), line, font=font, fill=(0, 0, 0, 120))
        draw.text((x, y), line, font=font, fill=fill)
        y += font.size + line_gap
    return y


def _decor(draw, w, h, accent: str, seed: int, alpha: int = 70):
    """抽象几何装饰：圆环 + 斜线，暗示器械与运动轨迹。"""
    rnd = random.Random(seed)
    c = _hex(accent) + (alpha,)
    for i in range(3):
        r = 210 + i * 130
        cx = int(w * (0.78 + 0.05 * i))
        cy = int(h * (0.20 + 0.06 * i))
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=c, width=3)
    for i in range(7):
        x0 = rnd.randint(int(w * 0.55), w)
        y0 = rnd.randint(int(h * 0.55), h)
        draw.line([(x0, y0), (x0 - rnd.randint(80, 260), y0 + rnd.randint(60, 200))],
                  fill=c, width=2)


def _from_video(path: Path, out: Path, w: int, h: int, t: float = 1.0) -> Path | None:
    try:
        ffmpeg(["-ss", str(t), "-i", str(path), "-frames:v", "1",
                "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
                "-q:v", "2", str(out)], timeout=90)
        return out if out.exists() else None
    except Exception:
        return None


def pick_background(scene: dict, index: int, workdir: Path, w: int, h: int) -> Image.Image | None:
    gym_dir = ASSETS / "gym"
    files = [p for p in gym_dir.iterdir() if p.is_file() and
             p.suffix.lower() in IMAGE_EXT | VIDEO_EXT] if gym_dir.exists() else []
    if not files:
        return None
    hint = (scene.get("visual_prompt", "") + scene.get("on_screen", "")).lower()
    scored = []
    for p in files:
        s = 0
        for kw in ["教练", "器械", "深蹲", "训练", "门店", "前台", "拉伸", "跑步", "团课", "gym", "coach"]:
            if kw in p.stem.lower() and (kw in hint or kw in "gymcoach"):
                s += 2
        scored.append((s, p))
    scored.sort(key=lambda x: -x[0])
    chosen = scored[index % max(1, min(3, len(scored)))][1] if scored else files[0]

    workdir.mkdir(parents=True, exist_ok=True)
    if chosen.suffix.lower() in VIDEO_EXT:
        frame = _from_video(chosen, workdir / f"bg_{index:02d}.jpg", w, h)
        path = frame
    else:
        path = chosen
    if not path or not Path(path).exists():
        return None
    try:
        im = Image.open(path).convert("RGB")
        ratio = max(w / im.width, h / im.height)
        im = im.resize((int(im.width * ratio), int(im.height * ratio)), Image.LANCZOS)
        left, top = (im.width - w) // 2, (im.height - h) // 2
        return im.crop((left, top, left + w, top + h))
    except Exception:
        return None


def _draw_overlay(scene: dict, cfg: dict, index: int, total: int) -> Image.Image:
    """纯文字/装饰透明层：真实视频用它叠加，静态卡片也用它，保证两种模式版式一致。"""
    w, h = cfg["video"]["width"], cfg["video"]["height"]
    _, accent, gold = PALETTES[(index - 1) % len(PALETTES)]
    brand = cfg["brand"]
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    _decor(d, w, h, accent, seed=index * 17)

    # 顶部品牌条：中文主名 + 英文品牌名 + 栏目说明
    d.rounded_rectangle([72, 96, 72 + 14, 96 + 54], radius=7, fill=_hex(accent) + (255,))
    name_font = _font(40)
    d.text((106, 96), brand["name"], font=name_font, fill=(255, 255, 255, 245))
    en = brand.get("name_en", "")
    if en:
        name_w = d.textlength(brand["name"], font=name_font)
        d.text((106 + name_w + 16, 110), en, font=_font(23), fill=_hex(gold) + (240,))
    d.text((106, 150), f"{brand.get('slogan', '科学训练')} · 60 秒训练笔记",
           font=_font(24, False), fill=(190, 200, 215, 210))
    d.line([(72, 208), (w - 72, 208)], fill=(255, 255, 255, 45), width=2)

    # 分镜序号
    d.rounded_rectangle([72, 262, 226, 322], radius=30, fill=_hex(accent) + (235,))
    d.text((100, 268), f"{index:02d} / {total:02d}", font=_font(34), fill=(12, 16, 24, 255))

    # 主文案
    main = scene.get("on_screen", "") or "科学训练"
    main_font = _font(104 if len(main) <= 6 else 82)
    y = _text_block(d, (72, 400), main, main_font, (255, 255, 255, 255), w - 160, line_gap=22)
    d.rounded_rectangle([72, y + 26, 72 + 128, y + 36], radius=5, fill=_hex(gold) + (255,))

    # 副文案
    sub = scene.get("sub", "")
    if sub:
        _text_block(d, (72, y + 86), sub, _font(42, False), (215, 224, 238, 230),
                    w - 190, line_gap=14, anchor_shadow=True)

    # 口播全文作为底部小字字幕（烧录），提高完播与无声可看性
    if cfg["video"].get("subtitle", True):
        nar = scene.get("narration", "")
        nar_lines = _wrap(d, nar, _font(38, False), w - 190)[:3]
        box_top, box_bottom = h - 470, h - 205
        d.rounded_rectangle([60, box_top, w - 60, box_bottom], radius=28, fill=(6, 10, 18, 225))
        d.text((92, box_top + 18), "· 口播字幕 ·", font=_font(24, False), fill=_hex(accent) + (255,))
        ny = box_top + 66
        for line in nar_lines[:3]:
            d.text((92, ny), line, font=_font(38, False), fill=(245, 247, 252, 255))
            ny += 52

    # 底部品牌与进度
    # 实拍背景明暗不定，水印加暗色描边保证任何画面上都读得清
    d.text((72, h - 132), brand["watermark"], font=_font(30, False), fill=(205, 214, 228, 235),
           stroke_width=2, stroke_fill=(6, 10, 18, 200))
    d.text((w - 300, h - 132), brand["handle"], font=_font(28, False), fill=(180, 192, 210, 225),
           stroke_width=2, stroke_fill=(6, 10, 18, 200))
    d.rounded_rectangle([72, h - 82, w - 72, h - 74], radius=4, fill=(255, 255, 255, 40))
    d.rounded_rectangle([72, h - 82, 72 + int((w - 144) * index / total), h - 74],
                        radius=4, fill=_hex(accent) + (255,))
    return layer


def make_overlay(scene: dict, cfg: dict, index: int, workdir: Path, total: int) -> Path:
    """真实视频模式使用：输出带 alpha 的透明文字层 PNG。"""
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / f"overlay_{index:02d}.png"
    _draw_overlay(scene, cfg, index, total).save(out, "PNG")
    return out


def make_scene_image(scene: dict, cfg: dict, index: int, workdir: Path,
                     total: int, script: dict) -> Path:
    """无真实视频时的兜底：品牌卡片底图 + 同一套文字层。"""
    w, h = cfg["video"]["width"], cfg["video"]["height"]
    bg_hex, accent, gold = PALETTES[(index - 1) % len(PALETTES)]

    photo = pick_background(scene, index, workdir, w, h)
    if photo is not None:
        base = photo
        veil = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        vd = ImageDraw.Draw(veil)
        for y in range(h):
            a = int(90 + 145 * (y / h) ** 0.9)
            vd.line([(0, y), (w, y)], fill=(8, 12, 20, a))
        base = Image.alpha_composite(base.convert("RGBA"), veil).convert("RGB")
        _glow(base, int(w * 0.2), int(h * 0.18), 340, accent, 55)
    else:
        base = _gradient(w, h, bg_hex, "#05070C")
        _glow(base, int(w * 0.82), int(h * 0.22), 480, accent, 95)
        _glow(base, int(w * 0.12), int(h * 0.78), 380, gold, 40)

    img = Image.alpha_composite(base.convert("RGBA"),
                                _draw_overlay(scene, cfg, index, total)).convert("RGB")
    out = workdir / f"scene_{index:02d}.png"
    img.save(out, "PNG", optimize=True)
    return out


def make_cover(script: dict, cfg: dict, workdir: Path, photo: Path | None = None) -> Path:
    w, h = cfg["video"]["width"], cfg["video"]["height"]
    accent, gold = cfg["brand"]["color_primary"], cfg["brand"]["color_accent"]
    if photo and Path(photo).exists():
        # 真实素材模式：用首个分镜的实拍帧做封面底图，与成片风格一致
        im = Image.open(photo).convert("RGB")
        ratio = max(w / im.width, h / im.height)
        im = im.resize((int(im.width * ratio), int(im.height * ratio)), Image.LANCZOS)
        left, top = (im.width - w) // 2, (im.height - h) // 2
        base = im.crop((left, top, left + w, top + h))
        veil = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        vd = ImageDraw.Draw(veil)
        for y in range(h):
            a = int(80 + 150 * (y / h) ** 0.85)
            vd.line([(0, y), (w, y)], fill=(6, 10, 18, a))
        base = Image.alpha_composite(base.convert("RGBA"), veil).convert("RGB")
        _glow(base, int(w * 0.8), int(h * 0.24), 430, accent, 70)
    else:
        base = _gradient(w, h, cfg["brand"]["color_bg"], "#04060A")
        _glow(base, int(w * 0.8), int(h * 0.26), 520, accent, 115)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    _decor(d, w, h, accent, seed=99, alpha=85)
    d.text((76, 88), cfg["brand"].get("full_name", cfg["brand"]["name"]),
           font=_font(34), fill=(255, 255, 255, 235))
    d.rounded_rectangle([72, 150, 400, 214], radius=32, fill=_hex(accent) + (255,))
    d.text((104, 158), "60 秒训练笔记", font=_font(38), fill=(10, 14, 22, 255))
    y = _text_block(d, (72, 420), script.get("cover_text", "科学训练"), _font(126),
                    (255, 255, 255, 255), w - 160, line_gap=30)
    d.rounded_rectangle([72, y + 40, 72 + 160, y + 52], radius=6, fill=_hex(gold) + (255,))
    _text_block(d, (72, y + 120), script["title"], _font(46, False), (215, 226, 240, 240),
                w - 180, line_gap=16)
    d.text((72, h - 150), cfg["brand"]["watermark"], font=_font(34, False),
           fill=(220, 228, 240, 240), stroke_width=2, stroke_fill=(6, 10, 18, 210))
    img = Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB")
    out = workdir / "cover.png"
    img.save(out, "PNG", optimize=True)
    return out


def build_scene_images(script: dict, cfg: dict, workdir: Path, run=None,
                       static: bool = True,
                       cover_photo: Path | None = None) -> tuple[list[Path], Path, list[Path]]:
    """返回 (静态图列表, 封面, 透明文字层列表)。

    真实视频模式下 static=False：只生成透明文字层，省掉整屏卡片渲染。
    """
    scenes = script["scenes"]
    overlays = [make_overlay(s, cfg, s["index"], workdir, len(scenes)) for s in scenes]
    paths = ([make_scene_image(s, cfg, s["index"], workdir, len(scenes), script) for s in scenes]
             if static else list(overlays))
    cover = make_cover(script, cfg, workdir, cover_photo)
    if run:
        run.step("画面生成", "done",
                 f"{len(scenes)} 层文字 | 模式：{'真实视频叠加' if not static else '静态卡片兜底'}")
    return paths, cover, overlays
