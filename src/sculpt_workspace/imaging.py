"""Image validation, bounded previews, crops, overlays and comparisons (Pillow)."""

from __future__ import annotations

import io
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .blender_scripts import sculpt_math
from .errors import InvalidRequest
from .models import Crop

Annotator = Callable[[Image.Image], Image.Image]

AXIS_COLORS = {"X": (230, 70, 70), "Y": (90, 200, 90), "Z": (80, 140, 245)}
GRID_COLOR = (255, 255, 255, 46)
INK = (245, 245, 245)
SHADOW = (0, 0, 0)
REFERENCE_FORMATS = {"PNG", "JPEG", "WEBP", "GIF", "BMP"}
MAX_REFERENCE_SIDE = 2048


def validate_png(path: Path, width: int, height: int) -> None:
    with Image.open(path) as img:
        img.verify()
    with Image.open(path) as img:
        if img.format != "PNG" or img.size != (width, height):
            raise ValueError(f"{path.name}: expected {width}x{height} PNG, got {img.format} {img.size}")


def _encode(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _load(path: Path, annotate: Annotator | None) -> Image.Image:
    with Image.open(path) as src:
        img = src.convert("RGB")
    return annotate(img) if annotate is not None else img


def preview(path: Path, max_size: int, annotate: Annotator | None = None) -> tuple[bytes, tuple[int, int]]:
    img = _load(path, annotate)
    img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    return _encode(img), img.size


def crop(
    path: Path, rect: Crop, max_size: int, annotate: Annotator | None = None
) -> tuple[bytes, tuple[int, int], tuple[int, int, int, int]]:
    """Crop a normalised rectangle (top-left origin) from the full image."""
    img = _load(path, annotate)
    w, h = img.size
    box = (
        round(rect.x * w),
        round(rect.y * h),
        round((rect.x + rect.width) * w),
        round((rect.y + rect.height) * h),
    )
    if box[2] - box[0] < 1 or box[3] - box[1] < 1:
        raise InvalidRequest("Crop is smaller than one pixel")
    out = img.crop(box)
    if max(out.size) > max_size:
        out.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    return _encode(out), out.size, box


def side_by_side(
    before: Path, after: Path, max_size: int, labels: tuple[str, str], annotate: Annotator | None = None
) -> tuple[bytes, tuple[int, int]]:
    """Two panels, each up to max_size, with a caption over each."""
    panels = []
    for path, label in zip((before, after), labels, strict=True):
        img = _load(path, annotate)
        img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        _caption(img, label, corner="top")
        panels.append(img)
    gap = max(2, max_size // 128)
    w = panels[0].width + gap + panels[1].width
    h = max(p.height for p in panels)
    out = Image.new("RGB", (w, h), (20, 20, 20))
    out.paste(panels[0], (0, 0))
    out.paste(panels[1], (panels[0].width + gap, 0))
    return _encode(out), out.size


def normalize_reference(data: bytes) -> tuple[bytes, tuple[int, int]]:
    """Validate an uploaded reference image and re-encode it as a bounded PNG.

    Re-encoding strips metadata and guarantees the stored file is a plain PNG.
    """
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as src:
            if src.format not in REFERENCE_FORMATS:
                raise InvalidRequest(
                    f"Reference images must be PNG, JPEG, WebP, GIF or BMP, not {src.format}.",
                    code="invalid_image",
                )
            img = src.convert("RGB")
    except InvalidRequest:
        raise
    except Exception as exc:
        raise InvalidRequest("The file is not a readable image.", code="invalid_image") from exc
    img.thumbnail((MAX_REFERENCE_SIDE, MAX_REFERENCE_SIDE), Image.Resampling.LANCZOS)
    return _encode(img), img.size


# -- overlays -----------------------------------------------------------------


def _font(size: int) -> Any:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow without scalable default font
        return ImageFont.load_default()


def _text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, size: int, fill: Any = INK) -> None:
    font = _font(size)
    x, y = xy
    off = max(1, size // 12)
    draw.text((x + off, y + off), text, font=font, fill=SHADOW)
    draw.text((x, y), text, font=font, fill=fill)


def _caption(img: Image.Image, text: str, corner: str = "top") -> None:
    size = max(10, img.width // 32)
    draw = ImageDraw.Draw(img)
    pad = size // 2
    y = pad if corner == "top" else img.height - size - pad * 2
    _text(draw, (pad, y), text, size)


def _nice(x: float) -> float:
    """Largest 1, 2 or 5 x 10^k that is <= x."""
    if x <= 0:
        return 0.0
    exp = math.floor(math.log10(x))
    base = x / 10**exp
    step = 5.0 if base >= 5 else 2.0 if base >= 2 else 1.0
    return step * 10**exp


def _fmt(value: float) -> str:
    return f"{value:.6g}"


def _axis_label(direction: tuple[float, float, float]) -> str | None:
    for i, name in enumerate("XYZ"):
        if abs(abs(direction[i]) - 1.0) < 1e-4:
            return name
    return None


def _length_label(units: float, scale_length: float | None) -> str:
    text = f"{_fmt(units)} u"
    if scale_length:
        mm = units * scale_length * 1000.0
        text += f" = {_fmt(mm)} mm" if mm < 10000 else f" = {_fmt(mm / 1000.0)} m"
    return text


def overlay_annotator(meta: dict[str, Any], scale_length: float | None = None) -> Annotator | None:
    """Return a function drawing a grid, scale bar, axis gizmo and view label.

    Grid lines and the scale bar are drawn only for orthographic cameras,
    where image distance maps linearly to world distance. Returns None when
    the artifact lacks camera metadata.
    """
    camera = meta.get("camera") or {}
    mw = camera.get("matrix_world")
    if not mw or "type" not in camera:
        return None
    right = (mw[0][0], mw[1][0], mw[2][0])
    up = (mw[0][1], mw[1][1], mw[2][1])
    back = (mw[0][2], mw[1][2], mw[2][2])
    location = (mw[0][3], mw[1][3], mw[2][3])
    ortho = camera["type"] == "orthographic" and camera.get("ortho_scale")
    label = f"{meta.get('view', '?')}  ({'ortho' if ortho else 'persp'})"

    def draw_overlay(img: Image.Image) -> Image.Image:
        w, h = img.size
        lw = max(1, round(w / 512))
        fs = max(10, w // 40)
        base = img.convert("RGBA")
        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        if ortho:
            scale = float(camera["ortho_scale"])
            px_per_unit = w / scale
            spacing = _nice(scale / 6.0)
            for axis, along_x in ((right, True), (up, False)):
                center = sculpt_math.dot(location, axis)
                first = math.ceil((center - scale / 2) / spacing)
                last = math.floor((center + scale / 2) / spacing)
                name = _axis_label(axis)
                for k in range(first, last + 1):
                    world = k * spacing
                    offset = (world - center) * px_per_unit
                    if along_x:
                        x = w / 2 + offset
                        draw.line([(x, 0), (x, h)], fill=GRID_COLOR, width=lw)
                        if name:
                            sign = -1 if axis[["X", "Y", "Z"].index(name)] < 0 else 1
                            _text(draw, (x + 3, h - fs * 1.4), f"{name}={_fmt(sign * world)}", int(fs * 0.7))
                    else:
                        y = h / 2 - offset
                        draw.line([(0, y), (w, y)], fill=GRID_COLOR, width=lw)
                        if name:
                            sign = -1 if axis[["X", "Y", "Z"].index(name)] < 0 else 1
                            _text(draw, (3, y + 2), f"{name}={_fmt(sign * world)}", int(fs * 0.7))
            bar_units = _nice(scale / 4.0)
            bar_px = bar_units * px_per_unit
            x0, y0 = fs, h - fs * 3
            draw.rectangle([x0, y0, x0 + bar_px, y0 + max(3, lw * 3)], fill=INK, outline=SHADOW)
            _text(draw, (x0, y0 - fs * 1.3), _length_label(bar_units, scale_length), fs)
        # Axis gizmo: world X/Y/Z projected onto the image plane.
        gx, gy, glen = w - fs * 4, h - fs * 4, fs * 2.6
        for i, name in enumerate("XYZ"):
            unit = [0.0, 0.0, 0.0]
            unit[i] = 1.0
            dx = sculpt_math.dot(unit, right)
            dy = sculpt_math.dot(unit, up)
            depth = sculpt_math.dot(unit, back)
            color = AXIS_COLORS[name]
            if math.hypot(dx, dy) < 0.15:
                draw.ellipse([gx - fs / 3, gy - fs / 3, gx + fs / 3, gy + fs / 3], outline=color, width=lw * 2)
                _text(draw, (gx + fs / 2, gy - fs / 2), f"{name}{'+' if depth > 0 else '-'}", fs, color)
                continue
            ex, ey = gx + dx * glen, gy - dy * glen
            draw.line([(gx, gy), (ex, ey)], fill=color, width=lw * 2)
            _text(draw, (ex + 2, ey - fs / 2), name, fs, color)
        _text(draw, (fs / 2, fs / 2), label, fs)
        return Image.alpha_composite(base, layer).convert("RGB")

    return draw_overlay
