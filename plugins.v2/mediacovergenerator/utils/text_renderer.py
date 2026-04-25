from typing import Optional, Sequence, Tuple

from PIL import Image, ImageFilter, ImageOps


Color = Tuple[int, int, int, int]


def _normalize_color(fill: Sequence[int]) -> Color:
    if len(fill) == 3:
        return int(fill[0]), int(fill[1]), int(fill[2]), 255
    return int(fill[0]), int(fill[1]), int(fill[2]), int(fill[3])


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


def text_to_mask(font, text: str) -> Tuple[Optional[Image.Image], Tuple[int, int]]:
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


def draw_text_with_mask(
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
