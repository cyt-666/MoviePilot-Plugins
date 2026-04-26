import os
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


Color = Tuple[int, int, int, int]
FONT_EXTENSIONS = {".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2"}

COMMON_SYSTEM_FONT_PATHS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
    "/usr/share/fonts/adobe-source-han-sans/SourceHanSansSC-Regular.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
)


def _normalize_color(fill: Sequence[int]) -> Color:
    if len(fill) == 3:
        return int(fill[0]), int(fill[1]), int(fill[2]), 255
    return int(fill[0]), int(fill[1]), int(fill[2]), int(fill[3])


def _safe_text_bbox(font, text: str, stroke_width: int = 0) -> Optional[Tuple[int, int, int, int]]:
    if not text:
        return None

    probe = Image.new("L", (1, 1), 0)
    draw = ImageDraw.Draw(probe)
    try:
        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=max(0, int(stroke_width)))
    except TypeError:
        bbox = draw.textbbox((0, 0), text, font=font)
    except Exception:
        bbox = font.getbbox(text)

    if not bbox:
        return None

    left, top, right, bottom = [int(round(value)) for value in bbox]
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _core_to_image(core) -> Optional[Image.Image]:
    width, height = core.size
    if width <= 0 or height <= 0:
        return None
    try:
        image = Image.Image()._new(core)
    except Exception:
        mode = getattr(core, "mode", "L") or "L"
        data = core.tobytes() if hasattr(core, "tobytes") else bytes(core)
        image = Image.frombytes(mode, (width, height), data)
    if image.mode == "L":
        return image
    if image.mode in ("1", "P"):
        return image.convert("L")
    if image.mode in ("LA", "RGBA"):
        return image.getchannel("A")
    return image.convert("L")


def _freetype_text_to_mask(font, text: str) -> Tuple[Optional[Image.Image], Tuple[int, int]]:
    if not text:
        return None, (0, 0)

    if hasattr(font, "getmask2"):
        core, offset = font.getmask2(text, mode="L")
        mask = _core_to_image(core)
        return mask, (int(offset[0]), int(offset[1]))

    bbox = font.getbbox(text)
    core = font.getmask(text, mode="L")
    mask = _core_to_image(core)
    return mask, (int(bbox[0]), int(bbox[1]))


def text_to_mask(font, text: str) -> Tuple[Optional[Image.Image], Tuple[int, int]]:
    """
    Render text into an alpha mask and verify that visible pixels exist.

    The previous implementation trusted FreeType masks directly. In some
    MoviePilot/Pillow environments the font loads and reports a bbox but the
    FreeType mask is empty, so ImageDraw is now the primary path and FreeType
    is only a fallback.
    """
    bbox = _safe_text_bbox(font, text)
    if not bbox:
        return None, (0, 0)

    pad = 4
    width = bbox[2] - bbox[0] + pad * 2
    height = bbox[3] - bbox[1] + pad * 2
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    draw.text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=255)
    if mask.getbbox():
        return mask, (bbox[0] - pad, bbox[1] - pad)

    try:
        fallback, offset = _freetype_text_to_mask(font, text)
    except Exception:
        return None, (0, 0)
    if fallback is not None and fallback.getbbox():
        return fallback, offset
    return None, (0, 0)


def _composite_mask(target: Image.Image, xy: Tuple[int, int], mask: Image.Image, fill: Sequence[int]) -> bool:
    if target.mode != "RGBA":
        raise ValueError("target image must be RGBA")

    color = _normalize_color(fill)
    alpha = mask if color[3] >= 255 else mask.point(lambda value: value * color[3] // 255)

    dest_x, dest_y = int(round(xy[0])), int(round(xy[1]))
    src_left = max(0, -dest_x)
    src_top = max(0, -dest_y)
    src_right = min(mask.width, target.width - dest_x)
    src_bottom = min(mask.height, target.height - dest_y)
    if src_right <= src_left or src_bottom <= src_top:
        return False

    alpha = alpha.crop((src_left, src_top, src_right, src_bottom))
    layer = Image.new("RGBA", alpha.size, color[:3] + (0,))
    layer.putalpha(alpha)
    target.alpha_composite(layer, dest=(max(0, dest_x), max(0, dest_y)))
    return True


def _draw_with_pil(
    target: Image.Image,
    position: Tuple[float, float],
    text: str,
    font,
    fill: Sequence[int],
    stroke_width: int = 0,
    stroke_fill: Optional[Sequence[int]] = None,
) -> Optional[Tuple[int, int, int, int]]:
    if target.mode != "RGBA":
        raise ValueError("target image must be RGBA")
    if not text:
        return None

    layer = Image.new("RGBA", target.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    x, y = int(round(position[0])), int(round(position[1]))
    stroke_width = max(0, int(stroke_width))
    fill_color = _normalize_color(fill)
    stroke_color = _normalize_color(stroke_fill) if stroke_fill is not None else None

    try:
        if stroke_width > 0 and stroke_color is not None:
            draw.text((x, y), text, font=font, fill=fill_color,
                      stroke_width=stroke_width, stroke_fill=stroke_color)
        else:
            draw.text((x, y), text, font=font, fill=fill_color)
    except TypeError:
        # Old Pillow builds may not support stroke parameters.
        draw.text((x, y), text, font=font, fill=fill_color)

    alpha_bbox = layer.getchannel("A").getbbox()
    if not alpha_bbox:
        return None

    target.alpha_composite(layer)
    return alpha_bbox


def _draw_with_freetype_mask(
    target: Image.Image,
    position: Tuple[float, float],
    text: str,
    font,
    fill: Sequence[int],
    stroke_width: int = 0,
    stroke_fill: Optional[Sequence[int]] = None,
) -> Optional[Tuple[int, int, int, int]]:
    mask, offset = text_to_mask(font, text)
    if mask is None or not mask.getbbox():
        return None

    x = int(round(position[0])) + offset[0]
    y = int(round(position[1])) + offset[1]

    if stroke_width > 0 and stroke_fill is not None:
        stroke_width = int(stroke_width)
        stroke_mask = ImageOps.expand(mask, border=stroke_width, fill=0)
        stroke_mask = stroke_mask.filter(ImageFilter.MaxFilter(stroke_width * 2 + 1))
        _composite_mask(target, (x - stroke_width, y - stroke_width), stroke_mask, stroke_fill)

    if not _composite_mask(target, (x, y), mask, fill):
        return None
    return x, y, x + mask.width, y + mask.height


def draw_text_with_mask(
    target: Image.Image,
    position: Tuple[float, float],
    text: str,
    font,
    fill: Sequence[int],
    stroke_width: int = 0,
    stroke_fill: Optional[Sequence[int]] = None,
) -> Optional[Tuple[int, int, int, int]]:
    """
    Draw text and return the actual alpha bbox that was written.

    The name is kept for compatibility with existing style code, but the
    function now verifies visible pixels after drawing instead of assuming that
    a loaded font can rasterize.
    """
    bbox = _draw_with_pil(target, position, text, font, fill, stroke_width, stroke_fill)
    if bbox:
        return bbox
    return _draw_with_freetype_mask(target, position, text, font, fill, stroke_width, stroke_fill)


def draw_text_with_fallback(
    target: Image.Image,
    position: Tuple[float, float],
    text: str,
    font,
    fill: Sequence[int],
    stroke_width: int = 0,
    stroke_fill: Optional[Sequence[int]] = None,
) -> bool:
    return draw_text_with_mask(target, position, text, font, fill, stroke_width, stroke_fill) is not None


def has_visible_alpha(image: Image.Image) -> bool:
    if image.mode == "RGBA":
        return image.getchannel("A").getbbox() is not None
    if image.mode == "LA":
        return image.getchannel("A").getbbox() is not None
    return image.convert("L").getbbox() is not None


def font_can_render(font_path: Path, sample_text: Optional[str] = None, size: int = 32) -> bool:
    try:
        if not font_path or not Path(font_path).exists() or Path(font_path).stat().st_size <= 0:
            return False
        sample = sample_text or "MoviePilot"
        if not _font_has_required_glyphs(Path(font_path), sample):
            return False
        font = ImageFont.truetype(str(font_path), max(1, int(size)))
        mask, _ = text_to_mask(font, sample)
        return mask is not None and mask.getbbox() is not None
    except Exception:
        return False


def _font_has_required_glyphs(font_path: Path, sample_text: str) -> bool:
    required = {ord(char) for char in sample_text if char and not char.isspace()}
    if not required:
        return True

    try:
        from fontTools.ttLib import TTCollection, TTFont
    except Exception:
        return True

    def cmap_covers(tt_font) -> bool:
        cmap = set()
        for table in tt_font["cmap"].tables:
            cmap.update(table.cmap.keys())
        return required.issubset(cmap)

    try:
        suffix = font_path.suffix.lower()
        if suffix in {".ttc", ".otc"}:
            collection = TTCollection(str(font_path), lazy=True)
            try:
                return any(cmap_covers(font) for font in collection.fonts)
            finally:
                for font in collection.fonts:
                    font.close()

        font = TTFont(str(font_path), lazy=True)
        try:
            return cmap_covers(font)
        finally:
            font.close()
    except Exception:
        # Rendering validation is still authoritative if cmap inspection is not supported.
        return True


def _font_priority(path: Path, sample_text: str) -> int:
    name = path.name.lower()
    score = 0
    if any("\u4e00" <= char <= "\u9fff" for char in sample_text):
        for keyword in ("noto", "sourcehan", "source-han", "wqy", "wenquanyi", "cjk", "sc", "simsun", "simhei", "msyh"):
            if keyword in name:
                score -= 20
    if any(keyword in name for keyword in ("bold", "regular", "medium")):
        score -= 2
    return score


def iter_system_font_paths(sample_text: str = "MoviePilot") -> Iterable[Path]:
    seen = set()
    for raw_path in COMMON_SYSTEM_FONT_PATHS:
        path = Path(raw_path)
        if path.exists() and path.suffix.lower() in FONT_EXTENSIONS:
            resolved = str(path)
            if resolved not in seen:
                seen.add(resolved)
                yield path

    candidates = []
    for font_dir in ("/usr/share/fonts", "/usr/local/share/fonts", "/config/fonts"):
        root = Path(font_dir)
        if not root.exists():
            continue
        for current_root, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(dirnames)
            for filename in sorted(filenames):
                path = Path(current_root) / filename
                if path.suffix.lower() not in FONT_EXTENSIONS:
                    continue
                resolved = str(path)
                if resolved in seen:
                    continue
                seen.add(resolved)
                candidates.append(path)

    for path in sorted(candidates, key=lambda p: (_font_priority(p, sample_text), str(p))):
        yield path


def find_renderable_font(
    preferred_paths: Optional[Iterable[Path]] = None,
    sample_text: str = "MoviePilot",
    role: str = "字体",
) -> Optional[Path]:
    seen = set()

    def iter_candidates():
        if preferred_paths:
            for candidate in preferred_paths:
                if not candidate:
                    continue
                yield Path(candidate)
        for candidate in iter_system_font_paths(sample_text):
            yield candidate

    for candidate in iter_candidates():
        resolved = str(candidate)
        if resolved in seen:
            continue
        seen.add(resolved)
        if font_can_render(candidate, sample_text=sample_text):
            return candidate
    return None
