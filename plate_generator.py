"""Licence plate design + registration generation for exported conversions.

Generates BeamNG licence plate design mods (design JSON, background textures,
font sprite atlas + character layout, rear-plate parts) from the plate settings
stored in a conversion config, and applies them to the exported .pc files.

The user-facing model is deliberately constrained to three plate families:
EU (wide), US and JP (both 2:1). BeamNG's internal format ids ("52-11",
"30-15") never appear in the UI; they only exist in the generated assets.

Kept separate from the handedness transform logic on purpose: the only shared
touch point is build_batch() calling apply_to_build() on the unpacked output.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import re
import string
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

import beamng_transform_helpers as transform_helpers

PLATE_SIZE_EU = "EU"
PLATE_SIZE_US = "US"
PLATE_SIZE_JP = "JP"
PLATE_SIZES = (PLATE_SIZE_EU, PLATE_SIZE_US, PLATE_SIZE_JP)

BAND_NONE = "none"
BAND_EU = "eu"
BAND_CUSTOM = "custom"
BAND_CHOICES = (BAND_NONE, BAND_EU, BAND_CUSTOM)

# style -> (background colour, text colour) following the real JP plate classes
JP_STYLES = {
    "private": ("#f4f6f3", "#1d5c3c"),
    "kei": ("#f7c50c", "#141414"),
    "commercial": ("#1d5c3c", "#f4f6f3"),
    "kei commercial": ("#141414", "#f7c50c"),
}
JP_KANA_CHOICES = tuple("あいうえかきくけこさすせそたちつてとなにぬねのはひふほまみむめもやゆよらりるれろわ")
JP_REGION_CHOICES = ("品川", "横浜", "名古屋", "大阪", "なにわ", "神戸", "京都", "札幌", "仙台", "広島", "福岡", "沖縄", "富士山", "練馬")

# BeamNG internal plate format ids (never shown in the UI).
_FORMAT_WIDE = "52-11"
_FORMAT_2_1 = "30-15"
_REAR_FORMAT_WIDE = "bhdc-rear-wide"
_REAR_FORMAT_2_1 = "bhdc-rear-2-1"
_CANVAS = {
    _FORMAT_WIDE: (1024, 196),
    _FORMAT_2_1: (512, 256),
    _REAR_FORMAT_WIDE: (1024, 196),
    _REAR_FORMAT_2_1: (512, 256),
}
_DESIGN_SLOT = "licenseplate_design_2_1"
_REAR_MESH_WIDE = "licenseplate-bhdc-rear-wide"
_REAR_MESH_2_1 = "licenseplate-bhdc-rear-2-1"
_REAR_FALLBACK_MESHES = {_REAR_MESH_WIDE, _REAR_MESH_2_1}
_EU_BAND_FRACTION = 0.11
_EU_BLUE = "#003399"
_ATLAS_WIDTH = 1024
_ATLAS_PAD = 6
_CAP_TARGET = 100  # target capital letter height in atlas pixels
_ASSET_VERSION = 7  # bump to invalidate hashed asset folders
EMBOSS_MAX_UI = 2.0  # upper bound of the emboss slider in the UI
_EMBOSS_BLUR_RADIUS = 1.2
_EMBOSS_NORMAL_HEIGHT = 6.0

_LETTERS = string.ascii_uppercase
_DIGITS = string.digits
_ALNUM = _LETTERS + _DIGITS
FONT_EXTENSIONS = (".ttf", ".otf", ".ttc")
_CENTER_DOT_CHAR = "."


class PlateError(RuntimeError):
    """A plate configuration or asset problem the user can fix."""


# --------------------------------------------------------------------------
# Configuration model


def default_plate_config() -> dict[str, object]:
    return {
        "enabled": False,
        "size": PLATE_SIZE_EU,
        "font": {"source": "default", "path": ""},
        "border": {
            "enabled": False,
            "color": "#101010",
            "offset": 8,
            "thickness": 3,
            "cornerRadius": 10,
        },
        "eu": {
            "pattern": "@@## @@@",
            "frontColor": "#ffffff",
            "rearColor": "#ffffff",
            "textColor": "#101010",
            "spacing": 0,
            "sideBand": BAND_NONE,
            "bandCode": "",
            "bandCodeColor": "#ffffff",
            "bandColor": _EU_BLUE,
            "bandImage": "",
            "bandFullImage": "",
        },
        "embossStrength": 1.0,
        "us": {
            "pattern": "@## @@@",
            "bgColor": "#ffffff",
            "bgImage": "",
            "textColor": "#1a3378",
            "textScale": 1.0,
            "textX": 0.0,
            "textY": 0.0,
            "spacing": 0,
        },
        "jp": {
            "pattern": "##-##",
            "style": "private",
            "region": "品川",
            "classification": "300",
            "kana": "さ",
        },
    }


def normalized_plate_config(raw: object) -> dict[str, object]:
    """Merge a stored plate config over the defaults so missing keys are safe."""
    merged = default_plate_config()
    if not isinstance(raw, dict):
        return merged
    for key, value in raw.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        elif key in merged:
            merged[key] = value
    if merged.get("size") not in PLATE_SIZES:
        merged["size"] = PLATE_SIZE_EU
    return merged


def variant_plate_mode(variant_settings: object) -> str:
    """How a trim relates to the general plate settings: general/custom/off."""
    if not isinstance(variant_settings, dict):
        return "general"
    override = variant_settings.get("plate")
    if not isinstance(override, dict):
        return "general"
    return str(override.get("mode") or "general")


def effective_plate_config(conversion: dict[str, object], config_name: str) -> dict[str, object] | None:
    """The plate config that applies to one trim, or None when plates are off."""
    general = conversion.get("plate")
    variants = conversion.get("variants", {})
    settings = variants.get(config_name) if isinstance(variants, dict) else None
    mode = variant_plate_mode(settings)
    if mode == "off":
        return None
    if mode == "custom":
        override = settings.get("plate", {}).get("config")
        cfg = normalized_plate_config(override)
        cfg["enabled"] = True
        return cfg
    cfg = normalized_plate_config(general)
    return cfg if cfg.get("enabled") else None


def active_section(cfg: dict[str, object]) -> dict[str, object]:
    """The per-family ("eu"/"us"/"jp") settings block for the active size."""
    section = cfg.get(str(cfg.get("size", PLATE_SIZE_EU)).lower(), {})
    return section if isinstance(section, dict) else {}


def active_pattern(cfg: dict[str, object]) -> str:
    return str(active_section(cfg).get("pattern") or "")


def plate_summary_label(conversion: dict[str, object]) -> str:
    cfg = normalized_plate_config(conversion.get("plate"))
    if not cfg.get("enabled"):
        return "Off"
    return f"{cfg['size']}  ·  {active_pattern(cfg) or 'no pattern'}"


def emboss_strength(cfg: dict[str, object]) -> float:
    """The user's emboss setting clamped to the UI's 0..EMBOSS_MAX_UI scale."""
    try:
        value = float(cfg.get("embossStrength", 1.0))
    except (TypeError, ValueError):
        value = 1.0
    return max(0.0, min(EMBOSS_MAX_UI, value))


def _effective_emboss_strength(cfg: dict[str, object]) -> float:
    """Convert the UI's compact 0-2 scale into a stronger normal-map value."""
    strength = emboss_strength(cfg)
    return strength * (2.0 + strength)


def _default_user_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "BeamHDC"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "BeamHDC"
    return Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "BeamHDC"


def user_fonts_dir() -> Path:
    return Path(os.environ.get("BEAMHDC_DATA_DIR") or _default_user_data_dir()) / "fonts"


def ensure_user_fonts_dir() -> Path:
    path = user_fonts_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_font_files() -> list[Path]:
    folder = ensure_user_fonts_dir()
    try:
        return sorted(
            (
                path
                for path in folder.iterdir()
                if path.is_file() and path.suffix.lower() in FONT_EXTENSIONS
            ),
            key=lambda path: path.name.casefold(),
        )
    except OSError:
        return []


# --------------------------------------------------------------------------
# Registration patterns


def validate_pattern(pattern: str) -> str | None:
    text = str(pattern or "").strip()
    if not text:
        return "Enter a registration pattern (e.g. @@## @@@)."
    if len(text) > 14:
        return "Registration pattern is too long (max 14 characters)."
    for ch in text:
        if ch in '"\\':
            return "Registration pattern cannot contain quotes or backslashes."
        if not ch.isprintable():
            return "Registration pattern contains unprintable characters."
    return None


def generate_registration(pattern: str, rng: random.Random | None = None) -> str:
    pick = (rng or random).choice
    out = []
    for ch in str(pattern or "").strip():
        if ch == "@":
            out.append(pick(_LETTERS))
        elif ch == "#":
            out.append(pick(_DIGITS))
        elif ch == "~":
            out.append(pick(_ALNUM))
        else:
            out.append(ch.upper())
    return "".join(out)


def _split_two_line_text(text: str) -> tuple[str, str]:
    words = [word for word in str(text or "").strip().split() if word]
    if len(words) <= 1:
        compact = "".join(words) if words else str(text or "").strip()
        midpoint = max(1, (len(compact) + 1) // 2)
        return compact[:midpoint], compact[midpoint:]

    best_index = 1
    best_score: tuple[int, int] | None = None
    for idx in range(1, len(words)):
        left = " ".join(words[:idx])
        right = " ".join(words[idx:])
        score = (abs(len(left) - len(right)), -len(left))
        if best_score is None or score < best_score:
            best_index = idx
            best_score = score
    return " ".join(words[:best_index]), " ".join(words[best_index:])


def _pattern_glyphs(pattern: str) -> set[str]:
    glyphs = set(_ALNUM) | {" "}
    for ch in str(pattern or "").strip():
        if ch not in "@#~":
            glyphs.add(ch.upper())
    return glyphs


# --------------------------------------------------------------------------
# Fonts

_FONT_DIR_CANDIDATES = (
    Path("C:/Windows/Fonts"),
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts/Supplemental"),
)
_DEFAULT_FONT_NAMES = (
    "bahnschrift.ttf",
    "arialbd.ttf",
    "arial.ttf",
    "calibrib.ttf",
    "tahomabd.ttf",
    "verdanab.ttf",
    "DejaVuSans-Bold.ttf",
    "Arial Bold.ttf",
    "Arial.ttf",
)
_JP_FONT_NAMES = ("YuGothB.ttc", "yugothb.ttc", "meiryob.ttc", "meiryo.ttc", "msgothic.ttc", "YuGothM.ttc")


def _find_font(names: tuple[str, ...]) -> Path | None:
    for folder in _FONT_DIR_CANDIDATES:
        for name in names:
            candidate = folder / name
            if candidate.is_file():
                return candidate
    return None


def resolve_font_path(font_cfg: object) -> Path:
    source = "default"
    custom = ""
    library_name = ""
    if isinstance(font_cfg, dict):
        source = str(font_cfg.get("source") or "default")
        custom = str(font_cfg.get("path") or "")
        library_name = str(font_cfg.get("name") or "")
    if source == "library":
        name = library_name or (Path(custom).name if custom else "")
        if not name or Path(name).name != name:
            raise PlateError("Choose a font from the BeamHDC fonts folder.")
        path = ensure_user_fonts_dir() / name
        if not path.is_file():
            raise PlateError(f"Plate font file not found in BeamHDC fonts folder: {name}")
        return path
    if source == "custom":
        path = Path(custom)
        if not custom or not path.is_file():
            raise PlateError(f"Plate font file not found: {custom or '(none selected)'}")
        return path
    found = _find_font(_DEFAULT_FONT_NAMES)
    if found is None:
        raise PlateError("No default plate font found on this system; choose a TTF/OTF font file instead.")
    return found


def resolve_jp_font_path() -> Path | None:
    return _find_font(_JP_FONT_NAMES)


def _load_font(path: Path, size: int):
    from PIL import ImageFont

    try:
        return ImageFont.truetype(str(path), size=size)
    except Exception as exc:
        raise PlateError(f"Could not read font '{path.name}': {exc}") from exc


# --------------------------------------------------------------------------
# Font sprite atlas + character layout


@dataclass(frozen=True)
class _FontMetrics:
    """Vertical metrics of the plate font at its atlas pixel size."""

    font_px: int
    line_height: int
    base: int  # baseline offset from the tile top (= ascent)
    cap_height: float
    cap_top: int  # top of a capital's ink relative to the tile top


def _plate_font_metrics(font_path: Path):
    """Load the plate font at the size that puts capitals at _CAP_TARGET px."""
    probe = _load_font(font_path, 128)
    cap_box = probe.getbbox("H")
    if not cap_box or cap_box[3] <= cap_box[1]:
        raise PlateError(f"Font '{font_path.name}' has no usable uppercase glyphs.")
    font_px = max(16, round(128 * _CAP_TARGET / (cap_box[3] - cap_box[1])))
    font = _load_font(font_path, font_px)
    ascent, descent = font.getmetrics()
    cap_box = font.getbbox("H")
    metrics = _FontMetrics(
        font_px=font_px,
        line_height=max(1, ascent + descent),
        base=ascent,
        cap_height=float(cap_box[3] - cap_box[1]),
        cap_top=cap_box[1],
    )
    return font, metrics


@dataclass(frozen=True)
class FontAtlas:
    key: str
    layout: dict[str, object]
    image_d: object
    image_n: object
    image_s: object
    metrics: _FontMetrics


def _font_atlas_key(font_path: Path, glyphs: set[str], spacing: int, emboss_strength: float) -> str:
    digest = hashlib.sha1()
    digest.update(str(_ASSET_VERSION).encode())
    try:
        digest.update(hashlib.sha1(font_path.read_bytes()).digest())
    except OSError as exc:
        raise PlateError(f"Could not read font '{font_path}': {exc}") from exc
    digest.update("".join(sorted(glyphs)).encode("utf-8"))
    digest.update(str(int(spacing)).encode())
    digest.update(f"{emboss_strength:.3f}".encode())
    return digest.hexdigest()[:10]


def build_font_atlas(font_path: Path, glyphs: set[str], spacing: int = 0, emboss_strength: float = 3.0) -> FontAtlas:
    """Rasterise a TTF/OTF into a BeamNG plate font atlas + BMFont-style layout.

    Glyph tiles are full line height with the baseline at 'base', yoffset 0;
    the runtime plate generator then only needs xoffset/xadvance which come
    straight from the font metrics (plus the user's extra character spacing).
    """
    from PIL import Image, ImageDraw

    key = _font_atlas_key(font_path, glyphs, spacing, emboss_strength)
    font, metrics = _plate_font_metrics(font_path)
    line_height = metrics.line_height
    cap_height = metrics.cap_height

    entries = []
    for ch in sorted(glyphs):
        if ch == _CENTER_DOT_CHAR:
            advance = max(font.getlength(ch), cap_height * 0.32)
            entries.append({"ch": ch, "w": max(1, round(advance)), "l": 0, "advance": advance, "box": None, "centerDot": True})
            continue
        advance = font.getlength(ch)
        box = font.getbbox(ch)
        if ch == " " or not box or box[2] <= box[0]:
            entries.append({"ch": ch, "w": 0, "l": 0, "advance": advance, "box": None})
            continue
        left, _top, right, _bottom = box
        entries.append({"ch": ch, "w": right - left, "l": left, "advance": advance, "box": box})

    # simple shelf packing
    x = _ATLAS_PAD
    y = _ATLAS_PAD
    row_h = line_height + _ATLAS_PAD
    for entry in entries:
        if entry["w"] <= 0:
            entry["x"], entry["y"] = 0, 0
            continue
        if x + entry["w"] + _ATLAS_PAD > _ATLAS_WIDTH:
            x = _ATLAS_PAD
            y += row_h
        entry["x"], entry["y"] = x, y
        x += entry["w"] + _ATLAS_PAD
    used_height = y + row_h
    atlas_height = 1
    while atlas_height < used_height:
        atlas_height *= 2

    image_d = Image.new("RGBA", (_ATLAS_WIDTH, atlas_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image_d)
    for entry in entries:
        if entry["w"] <= 0:
            continue
        if entry.get("centerDot"):
            diameter = max(4, round(cap_height * 0.16))
            left = entry["x"] + (entry["w"] - diameter) / 2
            top = entry["y"] + metrics.cap_top + (cap_height - diameter) / 2
            draw.ellipse((left, top, left + diameter, top + diameter), fill=(255, 255, 255, 255))
            continue
        # baseline sits at 'base' inside the tile: PIL's text origin is the
        # ascent top, so drawing at tile_y keeps ink at its natural offset.
        draw.text((entry["x"] - entry["l"], entry["y"]), entry["ch"], font=font, fill=(255, 255, 255, 255))

    image_s = _specular_from_alpha(image_d.getchannel("A"))
    image_n = _normal_from_alpha(image_d, emboss_strength)

    chars = []
    for entry in entries:
        chars.append({
            "id": str(ord(entry["ch"])),
            "x": str(entry["x"]),
            "y": str(entry["y"]),
            "width": str(entry["w"]),
            "height": str(line_height),
            "xoffset": str(int(entry["l"])),
            "yoffset": "0",
            "xadvance": str(int(round(entry["advance"])) + int(spacing)),
            "page": "0",
            "chnl": "15",
        })
    layout = {
        "info": {
            "face": font_path.stem,
            "size": str(-metrics.font_px),
            "bold": "0", "italic": "0", "charset": "", "unicode": "1",
            "stretchH": "100", "smooth": "1", "aa": "2",
            "padding": "0,0,0,0", "spacing": f"{_ATLAS_PAD},{_ATLAS_PAD}", "outline": "0",
        },
        "common": {
            "lineHeight": str(line_height),
            "base": str(metrics.base),
            "scaleW": str(_ATLAS_WIDTH),
            "scaleH": str(atlas_height),
            "pages": "1", "packed": "0",
            "alphaChnl": "1", "redChnl": "0", "greenChnl": "0", "blueChnl": "0",
        },
        "pages": [{"id": "0", "file": "platefont_d.png"}],
        "chars": {"count": str(len(chars)), "char": chars},
    }
    return FontAtlas(key, layout, image_d, image_n, image_s, metrics)


def _specular_from_alpha(alpha):
    """A flat light-grey specular map wherever the coverage mask has ink."""
    from PIL import Image

    grey = alpha.point(lambda _v: 210)
    return Image.merge("RGBA", (grey, grey, grey, alpha))


def _normal_from_alpha(image, emboss_strength: float):
    """Derive an embossed tangent-space normal map from glyph coverage."""
    import numpy as np
    from PIL import Image, ImageFilter

    height_map = image.getchannel("A").filter(ImageFilter.GaussianBlur(_EMBOSS_BLUR_RADIUS))
    field = (
        np.asarray(height_map, dtype=np.float32)
        / 255.0
        * (_EMBOSS_NORMAL_HEIGHT * max(0.0, float(emboss_strength)))
    )
    gy, gx = np.gradient(field)
    nz = np.ones_like(field)
    length = np.sqrt(gx * gx + gy * gy + nz * nz)
    encode = lambda component: ((component / length) * 0.5 + 0.5) * 255.0
    rgb = np.stack([encode(-gx), encode(gy), encode(nz)], axis=-1).astype(np.uint8)
    out = Image.fromarray(rgb, "RGB").convert("RGBA")
    out.putalpha(image.getchannel("A"))
    return out


# --------------------------------------------------------------------------
# Plate backgrounds


def _open_user_image(path_text: str, purpose: str):
    from PIL import Image

    path = Path(str(path_text))
    if not path.is_file():
        raise PlateError(f"{purpose} image not found: {path_text}")
    try:
        with Image.open(path) as raw:
            return raw.convert("RGBA")
    except Exception as exc:
        raise PlateError(f"Could not read {purpose.lower()} image '{path.name}': {exc}") from exc


def _bounded_int(value: object, default: int, low: int, high: int) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))


def _border_cfg(cfg: dict[str, object]) -> dict[str, object]:
    raw = cfg.get("border", {})
    return raw if isinstance(raw, dict) else {}


def _corner_radius(cfg: dict[str, object], size: tuple[int, int]) -> int:
    border = _border_cfg(cfg)
    return _bounded_int(border.get("cornerRadius"), 10, 0, min(size) // 2)


def _border_enabled(cfg: dict[str, object]) -> bool:
    border = _border_cfg(cfg)
    return bool(border.get("enabled"))


def _active_side_band_width(cfg: dict[str, object], size: tuple[int, int]) -> int:
    if str(cfg.get("size")) != PLATE_SIZE_EU:
        return 0
    eu = cfg.get("eu", {})
    if not isinstance(eu, dict) or str(eu.get("sideBand") or BAND_NONE) == BAND_NONE:
        return 0
    return max(1, round(size[0] * _EU_BAND_FRACTION))


def _border_geometry(cfg: dict[str, object], size: tuple[int, int]) -> tuple[tuple[float, float, float, float], int, float] | None:
    if not _border_enabled(cfg):
        return None
    border = _border_cfg(cfg)
    max_dim = max(1, min(size))
    offset = _bounded_int(border.get("offset"), 8, 0, max_dim // 3)
    thickness = _bounded_int(border.get("thickness"), 3, 1, max_dim // 4)
    inset = offset + thickness / 2
    left = _active_side_band_width(cfg, size) + inset
    top = inset
    right = size[0] - 1 - inset
    bottom = size[1] - 1 - inset
    if left >= right or top >= bottom:
        return None
    radius = max(0, _corner_radius(cfg, size) - offset)
    radius = min(radius, (right - left) / 2, (bottom - top) / 2)
    return (left, top, right, bottom), thickness, radius


def _rounded_plate_mask(cfg: dict[str, object], size: tuple[int, int]):
    from PIL import Image, ImageDraw

    radius = _corner_radius(cfg, size)
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


def _border_alpha(cfg: dict[str, object], size: tuple[int, int]):
    from PIL import Image, ImageChops, ImageDraw

    geometry = _border_geometry(cfg, size)
    if geometry is None:
        return None
    box, thickness, radius = geometry
    alpha = Image.new("L", size, 0)
    draw = ImageDraw.Draw(alpha)
    draw.rounded_rectangle(
        box,
        radius=radius,
        outline=255,
        width=thickness,
    )
    return ImageChops.multiply(alpha, _rounded_plate_mask(cfg, size))


def _decorate_plate_background(cfg: dict[str, object], plate):
    from PIL import ImageDraw

    size = plate.size
    geometry = _border_geometry(cfg, size)
    if geometry is not None:
        box, thickness, radius = geometry
        draw = ImageDraw.Draw(plate)
        draw.rounded_rectangle(
            box,
            radius=radius,
            outline=str(_border_cfg(cfg).get("color") or "#101010"),
            width=thickness,
        )
    plate.putalpha(_rounded_plate_mask(cfg, size))
    return plate


def _render_border_mode_images(cfg: dict[str, object], size: tuple[int, int], emboss_strength: float):
    from PIL import Image

    alpha = _border_alpha(cfg, size)
    if alpha is None:
        return None
    border = Image.new("RGBA", size, (255, 255, 255, 0))
    border.putalpha(alpha)
    return _normal_from_alpha(border, emboss_strength), _specular_from_alpha(alpha)


def _draw_star(draw, center: tuple[float, float], radius: float, fill) -> None:
    import math

    points = []
    for i in range(10):
        angle = -math.pi / 2 + i * math.pi / 5
        r = radius if i % 2 == 0 else radius * 0.42
        points.append((center[0] + r * math.cos(angle), center[1] + r * math.sin(angle)))
    draw.polygon(points, fill=fill)


def _fit_inside(src: tuple[int, int], dst: tuple[int, int]) -> tuple[int, int]:
    src_w, src_h = max(1, src[0]), max(1, src[1])
    dst_w, dst_h = max(1, dst[0]), max(1, dst[1])
    scale = min(dst_w / src_w, dst_h / src_h)
    return max(1, round(src_w * scale)), max(1, round(src_h * scale))


def _band_base_color(cfg_eu: dict[str, object], kind: str) -> str:
    return _EU_BLUE if kind == BAND_EU else str(cfg_eu.get("bandColor") or _EU_BLUE)


def _render_side_band_art(
    cfg_eu: dict[str, object],
    size: tuple[int, int],
    font_path: Path,
    kind: str,
    code_color: str,
):
    import math

    from PIL import Image, ImageDraw, ImageOps

    band_w, height = size
    full_image_path = str(cfg_eu.get("bandFullImage") or "")

    if full_image_path:
        band = ImageOps.fit(_open_user_image(full_image_path, "Side band image"), (band_w, height))
        draw = ImageDraw.Draw(band)
        return _draw_band_code(cfg_eu, band, draw, font_path, code_color)

    band = Image.new("RGBA", (band_w, height), _band_base_color(cfg_eu, kind))
    draw = ImageDraw.Draw(band)
    if kind == BAND_EU:
        ring_center = (band_w / 2, height * 0.34)
        ring_radius = height * 0.20
        for i in range(12):
            angle = -math.pi / 2 + i * math.pi / 6
            star_center = (ring_center[0] + ring_radius * math.cos(angle), ring_center[1] + ring_radius * math.sin(angle))
            _draw_star(draw, star_center, height * 0.045, "#f7d117")
    else:
        emblem_path = str(cfg_eu.get("bandImage") or "")
        if emblem_path:
            emblem = _open_user_image(emblem_path, "Side band emblem")
            box = (round(band_w * 0.82), round(height * 0.42))
            emblem.thumbnail(box)
            band.alpha_composite(emblem, (round((band_w - emblem.width) / 2), round(height * 0.34 - emblem.height / 2)))

    return _draw_band_code(cfg_eu, band, draw, font_path, code_color)


def _draw_band_code(cfg_eu: dict[str, object], band, draw, font_path: Path, code_color: str):
    """Draw the country/code text on the lower part of the side band."""
    band_w, height = band.size
    code = str(cfg_eu.get("bandCode") or "").strip().upper()[:3]
    if code:
        code_font = _load_font(font_path, max(10, round(height * 0.26)))
        left, top, right, bottom = code_font.getbbox(code)
        draw.text(
            ((band_w - (right - left)) / 2 - left, height * 0.80 - (bottom - top) / 2 - top),
            code, font=code_font, fill=code_color,
        )
    return band


def _render_side_band(cfg_eu: dict[str, object], size: tuple[int, int], font_path: Path, code_color: str):
    """The optional EU/custom side band, composited onto the plain background."""
    from PIL import Image

    width, height = size
    band_w = max(1, round(width * _EU_BAND_FRACTION))
    kind = str(cfg_eu.get("sideBand") or BAND_NONE)
    if kind == BAND_NONE:
        return None

    target = Image.new("RGBA", (band_w, height), _band_base_color(cfg_eu, kind))
    canonical_band = (max(1, round(_CANVAS[_FORMAT_WIDE][0] * _EU_BAND_FRACTION)), _CANVAS[_FORMAT_WIDE][1])
    art_w, art_h = _fit_inside(canonical_band, (band_w, height))
    art = _render_side_band_art(cfg_eu, (art_w, art_h), font_path, kind, code_color)
    target.alpha_composite(art, (round((band_w - art_w) / 2), round((height - art_h) / 2)))
    return target


def _render_eu_background(cfg_eu: dict[str, object], color: str, size: tuple[int, int], font_path: Path):
    from PIL import Image

    plate = Image.new("RGBA", size, str(color))
    band = _render_side_band(cfg_eu, size, font_path, str(color))
    if band is not None:
        plate.alpha_composite(band, (0, 0))
    return plate


def _render_us_background(cfg_us: dict[str, object], size: tuple[int, int]):
    from PIL import Image, ImageOps

    image_path = str(cfg_us.get("bgImage") or "")
    if image_path:
        image = _open_user_image(image_path, "Plate background")
        return ImageOps.fit(image, size)
    return Image.new("RGBA", size, str(cfg_us.get("bgColor") or "#ffffff"))


def _jp_style(cfg_jp: dict[str, object]) -> tuple[str, str]:
    return JP_STYLES.get(str(cfg_jp.get("style") or "private"), JP_STYLES["private"])


def _render_jp_background(cfg_jp: dict[str, object], size: tuple[int, int], plate_font_path: Path):
    """JP plate background with the region/classification/kana fields baked in.

    Only the main serial number is live registration text in-game; the other
    logical fields are static per design, which matches how the BeamNG plate
    pipeline renders a single text string.
    """
    from PIL import Image, ImageDraw

    width, height = size
    bg_color, text_color = _jp_style(cfg_jp)
    plate = Image.new("RGBA", size, bg_color)
    draw = ImageDraw.Draw(plate)

    region = str(cfg_jp.get("region") or "").strip()
    classification = str(cfg_jp.get("classification") or "").strip()[:3]
    kana = str(cfg_jp.get("kana") or "").strip()[:1]

    jp_font_path = resolve_jp_font_path()

    def field_font(text: str, px: int):
        if any(ord(ch) > 0x7F for ch in text):
            if jp_font_path is None:
                raise PlateError(
                    "No Japanese-capable font found on this system; use ASCII text for the region/kana fields."
                )
            return _load_font(jp_font_path, px)
        return _load_font(plate_font_path, px)

    top_line = " ".join(part for part in (region, classification) if part)
    if top_line:
        font = field_font(top_line, max(10, round(height * 0.26)))
        left, top, right, bottom = font.getbbox(top_line)
        draw.text(
            (width * 0.52 - (right - left) / 2 - left, height * 0.185 - (bottom - top) / 2 - top),
            top_line, font=font, fill=text_color,
        )
    if kana:
        font = field_font(kana, max(10, round(height * 0.34)))
        left, top, right, bottom = font.getbbox(kana)
        draw.text(
            (width * 0.115 - (right - left) / 2 - left, height * 0.62 - (bottom - top) / 2 - top),
            kana, font=font, fill=text_color,
        )
    return plate


# --------------------------------------------------------------------------
# Design text metrics


def _text_y_fraction(metrics: _FontMetrics, canvas_h: int, scale: float, center_frac: float) -> float:
    """Solve the design JSON text.y so the caps line lands on center_frac.

    Mirrors the game's plate generator placement:
      lineY = H*ty - lineHeight/2 ; tile top = lineY - (lineHeight - base)
      baseline = tile top + base*scale
    """
    lh, base, cap = metrics.line_height, metrics.base, metrics.cap_height
    target = center_frac * canvas_h
    return (target + (lh - base) + lh * 0.5 - (base - cap * 0.5) * scale) / canvas_h


def _family_text_params(cfg: dict[str, object], fmt: str, metrics: _FontMetrics) -> dict[str, object]:
    """text block (x/y/scale/color/limit) for one format of the active family."""
    size_family = str(cfg.get("size"))
    width, height = _CANVAS[fmt]
    wide = width >= 1024
    limit = max(12, len(active_pattern(cfg)))
    if size_family == PLATE_SIZE_EU:
        eu = cfg["eu"]
        tx = 0.5 + (_EU_BAND_FRACTION / 2 if str(eu.get("sideBand")) != BAND_NONE else 0.0)
        color = str(eu.get("textColor") or "#101010")
        if not wide:
            # A wide EU registration on a 2:1 plate wraps onto two lines.
            scale = (height * 0.31) / metrics.cap_height
            line = {
                "x": round(tx, 4),
                "scale": round(scale, 4),
                "maxWidth": round(0.84 if str(eu.get("sideBand")) == BAND_NONE else 0.78, 4),
            }
            return {
                "layout": "two-line",
                "x": round(tx, 4),
                "y": round(_text_y_fraction(metrics, height, scale, 0.5), 4),
                "scale": round(scale, 4),
                "color": color,
                "limit": limit,
                "lines": [
                    {**line, "y": round(_text_y_fraction(metrics, height, scale, 0.34), 4)},
                    {**line, "y": round(_text_y_fraction(metrics, height, scale, 0.70), 4)},
                ],
            }
        scale = (height * 0.60) / metrics.cap_height
        cy = 0.5
    elif size_family == PLATE_SIZE_US:
        us = cfg["us"]
        user_scale = max(0.3, min(2.5, float(us.get("textScale") or 1.0)))
        scale = (height * (0.52 if wide else 0.30)) / metrics.cap_height * user_scale
        tx = 0.5 + max(-0.4, min(0.4, float(us.get("textX") or 0.0)))
        cy = 0.5 + max(-0.4, min(0.4, float(us.get("textY") or 0.0)))
        color = str(us.get("textColor") or "#101010")
    else:
        jp = cfg["jp"]
        scale = (height * (0.50 if wide else 0.38)) / metrics.cap_height
        tx = 0.58
        cy = 0.62
        _bg, color = _jp_style(jp)
    return {
        "x": round(tx, 4),
        "y": round(_text_y_fraction(metrics, height, scale, cy), 4),
        "scale": round(scale, 4),
        "color": color,
        "limit": limit,
    }


def _active_spacing(cfg: dict[str, object]) -> int:
    return _bounded_int(active_section(cfg).get("spacing"), 0, -20, 60)


def _render_background_for(cfg: dict[str, object], fmt: str, font_path: Path, *, rear: bool = False):
    size = _CANVAS[fmt]
    family = str(cfg.get("size"))
    if family == PLATE_SIZE_EU:
        eu = cfg["eu"]
        color = str((eu.get("rearColor") if rear else eu.get("frontColor")) or "#ffffff")
        return _decorate_plate_background(cfg, _render_eu_background(eu, color, size, font_path))
    if family == PLATE_SIZE_US:
        return _decorate_plate_background(cfg, _render_us_background(cfg["us"], size))
    return _decorate_plate_background(cfg, _render_jp_background(cfg["jp"], size, font_path))


def _eu_rear_differs(cfg: dict[str, object]) -> bool:
    if str(cfg.get("size")) != PLATE_SIZE_EU:
        return False
    eu = cfg["eu"]
    front = str(eu.get("frontColor") or "#ffffff").lower()
    rear = str(eu.get("rearColor") or "#ffffff").lower()
    return front != rear


# --------------------------------------------------------------------------
# Validation


def validate_plate_config(cfg: object) -> list[str]:
    """User-fixable problems with a plate config; empty list when buildable."""
    cfg = normalized_plate_config(cfg)
    errors: list[str] = []
    pattern_error = validate_pattern(active_pattern(cfg))
    if pattern_error:
        errors.append(pattern_error)
    try:
        font_path = resolve_font_path(cfg.get("font"))
        _load_font(font_path, 32)
    except PlateError as exc:
        errors.append(str(exc))
        font_path = None
    family = str(cfg.get("size"))
    try:
        if family == PLATE_SIZE_US and str(cfg["us"].get("bgImage") or ""):
            _open_user_image(str(cfg["us"]["bgImage"]), "Plate background")
        if family == PLATE_SIZE_EU and str(cfg["eu"].get("sideBand")) != BAND_NONE and str(cfg["eu"].get("bandFullImage") or ""):
            _open_user_image(str(cfg["eu"]["bandFullImage"]), "Side band image")
        if family == PLATE_SIZE_EU and str(cfg["eu"].get("sideBand")) == BAND_CUSTOM and str(cfg["eu"].get("bandImage") or ""):
            _open_user_image(str(cfg["eu"]["bandImage"]), "Side band emblem")
        if family == PLATE_SIZE_JP and font_path is not None:
            _render_jp_background(cfg["jp"], (256, 128), font_path)
    except PlateError as exc:
        errors.append(str(exc))
    return errors


# --------------------------------------------------------------------------
# Preview rendering (UI helper; approximates the in-game compositor)


def render_plate_preview(
    cfg: object,
    registration: str | None = None,
    *,
    fmt: str | None = None,
    rear: bool = False,
):
    """A PIL image of the primary plate format for the current settings."""
    cfg = normalized_plate_config(cfg)
    family = str(cfg.get("size"))
    if fmt in {_REAR_FORMAT_WIDE, _FORMAT_WIDE}:
        fmt = _FORMAT_WIDE
    elif fmt in {_REAR_FORMAT_2_1, _FORMAT_2_1}:
        fmt = _FORMAT_2_1
    else:
        fmt = _FORMAT_WIDE if family == PLATE_SIZE_EU else _FORMAT_2_1
    font_path = resolve_font_path(cfg.get("font"))
    if registration is None:
        registration = generate_registration(active_pattern(cfg))

    plate = _render_background_for(cfg, fmt, font_path, rear=rear)
    # Same maths as the generated design JSON, just rendered directly.
    font, metrics = _plate_font_metrics(font_path)
    params = _family_text_params(cfg, fmt, metrics)
    spacing = _active_spacing(cfg)

    from PIL import ImageDraw

    draw = ImageDraw.Draw(plate)
    width, height = plate.size
    text = registration[: int(params["limit"])]

    def draw_line(line: str, line_params: dict[str, object]) -> None:
        line_scale = float(line_params.get("scale", params.get("scale", 1.0)))

        def char_advance(ch: str) -> float:
            if ch == _CENTER_DOT_CHAR:
                return max(font.getlength(ch), metrics.cap_height * 0.32)
            return font.getlength(ch)

        advances = [(char_advance(ch) + spacing) * line_scale + 2 for ch in line]
        total = sum(advances)
        max_width = float(line_params.get("maxWidth", params.get("maxWidth", 0)) or 0)
        if max_width and total > width * max_width and total > 0:
            line_scale *= (width * max_width) / total
            advances = [(char_advance(ch) + spacing) * line_scale + 2 for ch in line]
            total = sum(advances)
        draw_font = _load_font(font_path, max(8, round(metrics.font_px * line_scale)))
        x = width * float(line_params.get("x", params.get("x", 0.5))) - total / 2
        cap_box2 = draw_font.getbbox("H")
        caps_center_offset = cap_box2[1] + (cap_box2[3] - cap_box2[1]) / 2
        lh, base, cap = metrics.line_height, metrics.base, metrics.cap_height
        target_center = float(line_params.get("y", params.get("y", 0.5))) * height - (
            (lh - base) + lh * 0.5 - (base - cap * 0.5) * line_scale
        )
        y = target_center - caps_center_offset
        for ch, advance in zip(line, advances):
            if ch == _CENTER_DOT_CHAR:
                diameter = max(2, round((cap_box2[3] - cap_box2[1]) * 0.16))
                center_x = x + advance / 2
                center_y = y + caps_center_offset
                draw.ellipse(
                    (
                        center_x - diameter / 2,
                        center_y - diameter / 2,
                        center_x + diameter / 2,
                        center_y + diameter / 2,
                    ),
                    fill=str(params["color"]),
                )
            else:
                draw.text((x, y), ch, font=draw_font, fill=str(params["color"]))
            x += advance

    if params.get("layout") == "two-line":
        pieces = _split_two_line_text(text)
        line_params = params.get("lines")
        lines = line_params if isinstance(line_params, list) else []
        draw_line(pieces[0], lines[0] if len(lines) > 0 and isinstance(lines[0], dict) else params)
        draw_line(pieces[1], lines[1] if len(lines) > 1 and isinstance(lines[1], dict) else params)
    else:
        draw_line(text, params)
    return plate


# --------------------------------------------------------------------------
# Asset emission


def _design_key(cfg: dict[str, object], font_key: str) -> str:
    hashable = json.dumps({k: v for k, v in cfg.items() if k != "enabled"}, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(f"{_ASSET_VERSION}|{font_key}|{hashable}".encode("utf-8"))
    return digest.hexdigest()[:10]


def _save_png(image, path: Path) -> None:
    import io

    import beamng_hand_drive_core as core

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    core.write_bytes_file(path, buffer.getvalue())  # fs_path handles long paths


def _diffuse_fill_style(cfg: dict[str, object], *, rear: bool = False) -> str:
    family = str(cfg.get("size"))
    if family == PLATE_SIZE_EU:
        eu = cfg["eu"]
        return str((eu.get("rearColor") if rear else eu.get("frontColor")) or "#ffffff")
    if family == PLATE_SIZE_US:
        return str(cfg["us"].get("bgColor") or "#ffffff")
    bg_color, _text_color = _jp_style(cfg["jp"])
    return bg_color


def _plate_generator_html() -> str:
    return r"""<!doctype html>
<html>
<body>
<style>
body {
  margin: 0;
  padding: 0;
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  overflow: hidden;
}
#canvas {
  width: 100%;
  height: 100%;
  margin: 0;
  padding: 0;
}
</style>
<canvas id="canvas" width="512" height="256"></canvas>
<script>
function localUrl(path) {
  if (!path) return "";
  if (path.indexOf("://") !== -1) return path;
  return "local://local/" + path;
}

function init(mode, text, design) {
  text = (text || "").toUpperCase();
  if (!design || !design.data || !design.data[mode]) {
    finish();
    return;
  }

  var data = design.data;
  var modeData = data[mode];
  var canvas = document.getElementById("canvas");
  canvas.width = data.size.x;
  canvas.height = data.size.y;
  var ctx = canvas.getContext("2d");
  ctx.fillStyle = modeData.fillStyle || "white";
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  var textParams = data.text || {};
  if (textParams.limit) {
    text = text.substring(0, textParams.limit);
  }

  var font = data.characterLayout;
  if (font && font.chars && font.chars.char) {
    font.charMap = {};
    for (var i = 0; i < font.chars.count; i++) {
      for (var key in font.chars.char[i]) {
        font.chars.char[i][key] = parseInt(font.chars.char[i][key]);
      }
      font.charMap[parseInt(font.chars.char[i].id)] = font.chars.char[i];
    }
  }

  var pending = 0;
  var background = null;
  var sprites = null;

  function loadImage(path, callback) {
    if (!path) {
      callback(null);
      return;
    }
    pending++;
    var img = new Image();
    img.addEventListener("load", function() {
      pending--;
      callback(img);
      renderIfReady();
    }, false);
    img.addEventListener("error", function() {
      pending--;
      callback(null);
      renderIfReady();
    }, false);
    img.src = localUrl(path);
  }

  function renderIfReady() {
    if (pending > 0) return;

    if (background) {
      ctx.drawImage(background, 0, 0, background.naturalWidth, background.naturalHeight, 0, 0, canvas.width, canvas.height);
    }

    if (sprites && font && font.charMap) {
      var tintSprites = sprites;
      if (mode === "diffuse") {
        tintSprites = document.createElement("canvas");
        tintSprites.width = sprites.width;
        tintSprites.height = sprites.height;
        var tintCtx = tintSprites.getContext("2d");
        tintCtx.fillStyle = textParams.color || "black";
        tintCtx.fillRect(0, 0, tintSprites.width, tintSprites.height);
        tintCtx.globalCompositeOperation = "multiply";
        tintCtx.drawImage(sprites, 0, 0);
        tintCtx.globalCompositeOperation = "destination-in";
        tintCtx.drawImage(sprites, 0, 0);
      }

      var lineHeight = parseInt(font.common.lineHeight);
      var lineBase = parseInt(font.common.base);

      function splitTwoLine(raw) {
        var source = (raw || "").replace(/^\s+|\s+$/g, "");
        var rawWords = source.split(/\s+/);
        var words = [];
        for (var wi = 0; wi < rawWords.length; wi++) {
          if (rawWords[wi]) words.push(rawWords[wi]);
        }
        if (words.length <= 1) {
          var compact = words.length ? words[0] : source;
          var midpoint = Math.max(1, Math.ceil(compact.length / 2));
          return [compact.substring(0, midpoint), compact.substring(midpoint)];
        }
        var bestIndex = 1;
        var bestScore = 999999;
        for (var si = 1; si < words.length; si++) {
          var left = words.slice(0, si).join(" ");
          var right = words.slice(si).join(" ");
          var score = Math.abs(left.length - right.length) * 100 - left.length;
          if (score < bestScore) {
            bestIndex = si;
            bestScore = score;
          }
        }
        return [words.slice(0, bestIndex).join(" "), words.slice(bestIndex).join(" ")];
      }

      function measureLine(line, scale) {
        var width = 0;
        for (var ci = 0; ci < line.length; ci++) {
          var code = line.charCodeAt(ci);
          if (font.charMap[code] === undefined) continue;
          width += font.charMap[code].xadvance * scale + 2;
        }
        return width;
      }

      function drawLine(line, lineParams) {
        if (!line) return;
        lineParams = lineParams || textParams;
        var scale = lineParams.scale || textParams.scale || 1;
        var maxWidth = lineParams.maxWidth || textParams.maxWidth || 0;
        if (maxWidth) {
          var measured = measureLine(line, scale);
          var available = canvas.width * maxWidth;
          if (measured > available && measured > 0) {
            scale = scale * available / measured;
          }
        }
        var textWidth = measureLine(line, scale);
        var x = canvas.width * (lineParams.x || textParams.x || 0.5) - textWidth * 0.5;
        var y = canvas.height * (lineParams.y || textParams.y || 0.5) - lineHeight * 0.5;
        for (var ti = 0; ti < line.length; ti++) {
          var ch = line.charCodeAt(ti);
          if (font.charMap[ch] === undefined) continue;
          var glyph = font.charMap[ch];
          ctx.drawImage(
            tintSprites,
            glyph.x, glyph.y, glyph.width, glyph.height,
            x + glyph.xoffset * scale,
            y - (lineHeight - lineBase) - glyph.yoffset * scale,
            glyph.width * scale,
            glyph.height * scale
          );
          x += glyph.xadvance * scale + 2;
        }
      }

      if (textParams.layout === "two-line") {
        var pieces = splitTwoLine(text);
        var lineParams = textParams.lines || [];
        drawLine(pieces[0], lineParams[0] || textParams);
        drawLine(pieces[1], lineParams[1] || textParams);
      } else {
        drawLine(text, textParams);
      }
    }

    finish();
  }

  function finish() {
    if (typeof beamng !== "undefined") {
      beamng.uiUpdate();
      beamng.uiDestroy();
    }
  }

  loadImage(modeData.backgroundImg, function(img) { background = img; });
  loadImage(modeData.spriteImg, function(img) { sprites = img; });
  renderIfReady();
}
</script>
</body>
</html>
"""


def _mode_blocks(
    bg_rel: str | None,
    font_rel: str,
    *,
    fill_style: str,
    bump_bg_rel: str | None = None,
    specular_bg_rel: str | None = None,
) -> dict[str, object]:
    diffuse: dict[str, object] = {"spriteImg": f"{font_rel}/platefont_d.png", "fillStyle": fill_style}
    if bg_rel:
        diffuse["backgroundImg"] = bg_rel
    bump = {"spriteImg": f"{font_rel}/platefont_n.png", "fillStyle": "rgb(127,127,255)"}
    if bump_bg_rel:
        bump["backgroundImg"] = bump_bg_rel
    specular = {"spriteImg": f"{font_rel}/platefont_s.png", "fillStyle": "rgb(233,233,233)"}
    if specular_bg_rel:
        specular["backgroundImg"] = specular_bg_rel
    return {
        "diffuse": diffuse,
        "bump": bump,
        "specular": specular,
    }


class _DesignOutput:
    def __init__(self, key: str, part_id: str, rear_formats: dict[str, str]):
        self.key = key
        self.part_id = part_id
        self.rear_formats = rear_formats  # vanilla fmt -> rear fmt id (empty when front==rear)


def _emit_design(cfg: dict[str, object], output_root: Path, prefix: str, cache: dict[str, _DesignOutput]) -> _DesignOutput:
    import beamng_hand_drive_core as core

    font_path = resolve_font_path(cfg.get("font"))
    glyphs = _pattern_glyphs(active_pattern(cfg))
    spacing = _active_spacing(cfg)
    emboss_strength = _effective_emboss_strength(cfg)
    font_key = _font_atlas_key(font_path, glyphs, spacing, emboss_strength)
    design_key = _design_key(cfg, font_key)
    cached = cache.get(design_key)
    if cached is not None:
        return cached

    plates_root = output_root / "vehicles" / "common" / "licenseplates" / prefix
    font_rel = f"vehicles/common/licenseplates/{prefix}/font_{font_key}"
    font_dir = plates_root / f"font_{font_key}"
    generator_rel = f"vehicles/common/licenseplates/{prefix}/bhdc-licenseplate.html"
    core.write_text_file(plates_root / "bhdc-licenseplate.html", _plate_generator_html(), encoding="utf-8")
    if not (font_dir / "platefont.json").exists():
        atlas = build_font_atlas(font_path, glyphs, spacing, emboss_strength)
        _save_png(atlas.image_d, font_dir / "platefont_d.png")
        _save_png(atlas.image_n, font_dir / "platefont_n.png")
        _save_png(atlas.image_s, font_dir / "platefont_s.png")
        core.write_text_file(font_dir / "platefont.json", json.dumps(atlas.layout, indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        atlas = build_font_atlas(font_path, glyphs, spacing, emboss_strength)

    design_rel = f"vehicles/common/licenseplates/{prefix}/design_{design_key}"
    design_dir = plates_root / f"design_{design_key}"
    rear_formats = (
        {_FORMAT_WIDE: _REAR_FORMAT_WIDE, _FORMAT_2_1: _REAR_FORMAT_2_1} if _eu_rear_differs(cfg) else {}
    )

    formats: dict[str, object] = {}
    for fmt in (_FORMAT_WIDE, _FORMAT_2_1):
        border_maps = _render_border_mode_images(cfg, _CANVAS[fmt], emboss_strength)
        border_n_rel = None
        border_s_rel = None
        if border_maps is not None:
            border_n_name = f"border_{fmt.replace('-', '_')}_n.png"
            border_s_name = f"border_{fmt.replace('-', '_')}_s.png"
            _save_png(border_maps[0], design_dir / border_n_name)
            _save_png(border_maps[1], design_dir / border_s_name)
            border_n_rel = f"{design_rel}/{border_n_name}"
            border_s_rel = f"{design_rel}/{border_s_name}"

        def format_block(bg_stem: str, *, rear: bool) -> dict[str, object]:
            """One format entry: background texture on disk + design JSON block."""
            bg_name = f"bg_{bg_stem.replace('-', '_')}_d.png"
            _save_png(_render_background_for(cfg, fmt, font_path, rear=rear), design_dir / bg_name)
            block: dict[str, object] = {
                "size": {"x": _CANVAS[fmt][0], "y": _CANVAS[fmt][1]},
                "text": _family_text_params(cfg, fmt, atlas.metrics),
                "characterLayout": f"{font_rel}/platefont.json",
                "generator": generator_rel,
            }
            block.update(_mode_blocks(
                f"{design_rel}/{bg_name}",
                font_rel,
                fill_style=_diffuse_fill_style(cfg, rear=rear),
                bump_bg_rel=border_n_rel,
                specular_bg_rel=border_s_rel,
            ))
            return block

        formats[fmt] = format_block(fmt, rear=False)
        rear_fmt = rear_formats.get(fmt)
        if rear_fmt:
            formats[rear_fmt] = format_block(rear_fmt, rear=True)

    design_json = {
        "name": f"BeamHDC {cfg.get('size')} plate design",
        "version": 2,
        "data": {"format": formats},
    }
    core.write_text_file(design_dir / "licensePlate.json", json.dumps(design_json, indent=2, ensure_ascii=False), encoding="utf-8")

    part_id = f"bhdc_plate_design_{design_key}"
    out = _DesignOutput(design_key, part_id, rear_formats)
    out.design_json_rel = f"{design_rel}/licensePlate.json"
    cache[design_key] = out
    return out


def _design_part_body(out: _DesignOutput, size_label: str) -> str:
    return json.dumps({
        "information": {"authors": "BeamHDC", "name": f"BeamHDC {size_label} Plate Design", "value": 0},
        "slotType": _DESIGN_SLOT,
        "licenseplate_path": out.design_json_rel,
    }, indent=4)


# --------------------------------------------------------------------------
# Rear plate part cloning (EU front/rear colour split)

_REAR_NAME_RE = re.compile(r"(?:^|[_\-])(?:r|rear)(?:[_\-]|$)", re.IGNORECASE)
_FRONT_NAME_RE = re.compile(r"(?:^|[_\-])(?:f|front)(?:[_\-]|$)", re.IGNORECASE)


def _looks_rear(slot_type: str, part_id: str, part_body: str) -> bool:
    for name in (slot_type, part_id):
        if _REAR_NAME_RE.search(name):
            return True
        if _FRONT_NAME_RE.search(name):
            return False
    # fall back to the flexbody's global position: +Y is rearwards in BeamNG
    match = re.search(r'"pos"\s*:\s*\{[^}]*"y"\s*:\s*([-+0-9.eE]+)', part_body)
    if match:
        try:
            return float(match.group(1)) > 0.2
        except ValueError:
            return False
    return False


def _clone_rear_plate_part(
    context,
    part_id: str,
    part_body: str,
    new_part_id: str,
    clone_sources: dict[str, _RearMeshClone],
) -> tuple[str, str, list[str]] | None:
    """Clone a rear plate part onto the BeamHDC rear plate mesh/format.

    Returns (new_body, rear_format, output_meshes) or None when the part carries no
    recognisable plate mesh.
    """
    meshes = transform_helpers.extract_part_mesh_names(part_body)
    plate_meshes = sorted(m for m in meshes if "licenseplate" in m.lower())
    if not plate_meshes:
        return None
    wide = _part_uses_wide_plate(part_body)
    rear_fmt = _REAR_FORMAT_WIDE if wide else _REAR_FORMAT_2_1
    fallback_mesh = _REAR_MESH_WIDE if wide else _REAR_MESH_2_1
    mesh_map: dict[str, str] = {}
    for mesh in plate_meshes:
        mesh_map[mesh] = _rear_clone_for_mesh(context, mesh, clone_sources) or fallback_mesh

    body = transform_helpers.replace_first(part_body, f'"{part_id}"', f'"{new_part_id}"')
    body = _rewrite_part_mesh_names(body, mesh_map)
    # Real mesh clones already carry the vehicle's intended plate plane. Only
    # nudge the old generic fallback quad, where the source mesh could not be
    # cloned and the replacement may otherwise sit inside the mount.
    if set(mesh_map.values()).issubset(_REAR_FALLBACK_MESHES):
        body = _offset_plate_y(body, 0.01)
    if '"licenseplateFormat"' in body:
        body = re.sub(r'("licenseplateFormat"\s*:\s*)"[^"]*"', rf'\g<1>"{rear_fmt}"', body, count=1)
    else:
        brace = body.find("{")
        if brace < 0:
            return None
        body = body[: brace + 1] + f'\n    "licenseplateFormat": "{rear_fmt}",' + body[brace + 1 :]
    return body, rear_fmt, sorted(set(mesh_map.values()))


def _rewrite_part_mesh_names(part_body: str, mesh_map: dict[str, str]) -> str:
    if not mesh_map:
        return part_body

    body = transform_helpers.replace_array_region(
        part_body,
        "flexbodies",
        lambda array_text: transform_helpers.rewrite_flexbody_meshes(array_text, mesh_map),
    )

    def rewrite_props(array_text: str) -> str:
        out = array_text
        for old_mesh, new_mesh in sorted(mesh_map.items(), key=lambda item: len(item[0]), reverse=True):
            out = re.sub(
                rf'(\[\s*"((?:[^"\\]|\\.)*)"\s*(?:,\s*|\s+))"{re.escape(old_mesh)}"(?=\s*(?:,|"))',
                rf'\1"{new_mesh}"',
                out,
            )
        return out

    return transform_helpers.replace_array_region(body, "props", rewrite_props)


_REAR_QUADS = {
    _REAR_MESH_WIDE: (0.52, 0.11),
    _REAR_MESH_2_1: (0.30, 0.15),
}


@dataclass(frozen=True)
class _RearMeshClone:
    source_zip: Path
    dae_path: str
    source_node_id: str
    source_mesh: str
    output_mesh: str


_JBEAM_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_HIDDEN_PLATE_COORD = 1000.0


def _plate_positions(part_body: str) -> list[tuple[float, float, float]]:
    positions: list[tuple[float, float, float]] = []
    for match in re.finditer(r'"pos"\s*:\s*\{(?P<body>[^}]*)\}', part_body):
        body = match.group("body")
        coords: dict[str, float] = {}
        for axis in ("x", "y", "z"):
            axis_match = re.search(rf'"{axis}"\s*:\s*({_JBEAM_NUMBER})', body)
            if axis_match:
                try:
                    coords[axis] = float(axis_match.group(1))
                except ValueError:
                    pass
        if all(axis in coords for axis in ("x", "y", "z")):
            positions.append((coords["x"], coords["y"], coords["z"]))
    return positions


def _plate_part_is_hidden(part_body: str) -> bool:
    positions = _plate_positions(part_body)
    return bool(positions) and all(max(abs(value) for value in pos) > _HIDDEN_PLATE_COORD for pos in positions)


def _part_plate_width(part_body: str) -> bool | None:
    if re.search(r'"licenseplateFormat"\s*:\s*"52-11"', part_body):
        return True
    if re.search(r'"licenseplateFormat"\s*:\s*"30-15"', part_body):
        return False
    if "52-11" in part_body:
        return True
    if "30-15" in part_body:
        return False
    return None


def _part_uses_wide_plate(part_body: str) -> bool:
    return _part_plate_width(part_body) is True


def _iter_selected_plate_parts(context, config_name: str):
    """Yield (slot_type, part_id, part_body) for a config's licence plate
    parts, skipping the design slot and parts without a resolvable body."""
    import beamng_hand_drive_core as core

    selected = core.selected_parts_for_config(context, config_name)
    selected_by_slot = selected.get("selected_by_slot", {}) if isinstance(selected, dict) else {}
    if not isinstance(selected_by_slot, dict):
        return
    for slot_type, part_id in sorted(selected_by_slot.items()):
        slot_text = str(slot_type)
        part_text = str(part_id)
        slot_lower = slot_text.lower()
        part_lower = part_text.lower()
        if "design" in slot_lower or "design" in part_lower:
            continue
        if "licenseplate" not in slot_lower and "licenseplate" not in part_lower:
            continue
        found = core.part_body_for_context(context, part_text)
        if found is None:
            continue
        yield slot_text, part_text, found[0]


def preview_format_for_config(context, config_name: str, side: str = "front") -> str | None:
    """Return the stock BeamNG plate format used by one side of a config."""
    if context is None or not config_name:
        return None
    try:
        plate_parts = list(_iter_selected_plate_parts(context, config_name))
    except Exception:
        return None

    want_rear = str(side).lower() == "rear"
    fallback: list[str] = []
    for slot_text, part_text, part_body in plate_parts:
        side_matches = _looks_rear(slot_text, part_text, part_body) == want_rear
        if want_rear and side_matches and _plate_part_is_hidden(part_body):
            donor = _visible_rear_plate_donor(context, slot_text, part_text, part_body)
            if donor is not None:
                _donor_id, part_body = donor
        fmt = _FORMAT_WIDE if _part_plate_width(part_body) else _FORMAT_2_1
        if side_matches:
            return fmt
        fallback.append(fmt)
    return fallback[0] if fallback else None


def _plate_part_prefix(part_id: str) -> str:
    lowered = part_id.lower()
    idx = lowered.find("licenseplate")
    if idx >= 0:
        return lowered[:idx].strip("_-")
    return lowered.split("_", 1)[0]


def _force_slot_type(part_body: str, slot_type: str) -> str:
    return re.sub(r'("slotType"\s*:\s*)"[^"]*"', rf'\g<1>"{slot_type}"', part_body, count=1)


def _offset_plate_y(part_body: str, amount: float) -> str:
    def replace_pos(match: re.Match[str]) -> str:
        body = match.group("body")

        def replace_y(y_match: re.Match[str]) -> str:
            try:
                value = float(y_match.group(1)) + amount
            except ValueError:
                return y_match.group(0)
            return f'"y":{value:.5g}'

        body = re.sub(r'"y"\s*:\s*(' + _JBEAM_NUMBER + r')', replace_y, body, count=1)
        return '"pos":{' + body + "}"

    return re.sub(r'"pos"\s*:\s*\{(?P<body>[^}]*)\}', replace_pos, part_body)


def _rear_clone_mesh_name(context, source_mesh: str, obj) -> str:
    import beamng_hand_drive_core as core

    source_zip = obj.dae_source_zip or context.source_zip
    digest = hashlib.sha1(
        f"{source_zip}|{obj.dae_path}|{obj.id}|{source_mesh}".encode("utf-8", errors="replace")
    ).hexdigest()[:8]
    return core.safe_id(f"bhdc_rear_{source_mesh}_{digest}")


def _rear_clone_for_mesh(
    context,
    source_mesh: str,
    clone_sources: dict[str, _RearMeshClone],
) -> str | None:
    obj = context.objects.get(source_mesh)
    if obj is None or not obj.dae_path:
        return None
    output_mesh = _rear_clone_mesh_name(context, source_mesh, obj)
    if output_mesh not in clone_sources:
        clone_sources[output_mesh] = _RearMeshClone(
            source_zip=obj.dae_source_zip or context.source_zip,
            dae_path=obj.dae_path,
            source_node_id=obj.id,
            source_mesh=source_mesh,
            output_mesh=output_mesh,
        )
    return output_mesh


def _namespace_tag(tag: str) -> str:
    return f"{{{transform_helpers.NS['c']}}}{tag}"


def _clear_children(elem: ET.Element) -> None:
    for child in list(elem):
        elem.remove(child)


def _find_or_create_library(root: ET.Element, tag: str) -> ET.Element:
    found = root.find(f"c:{tag}", transform_helpers.NS)
    if found is not None:
        return found
    created = ET.Element(_namespace_tag(tag))
    root.append(created)
    return created


def _append_plate_effect(library_effects: ET.Element, output_mesh: str) -> None:
    effect = ET.SubElement(library_effects, _namespace_tag("effect"), {"id": f"{output_mesh}-effect"})
    profile = ET.SubElement(effect, _namespace_tag("profile_COMMON"))
    technique = ET.SubElement(profile, _namespace_tag("technique"), {"sid": "common"})
    lambert = ET.SubElement(technique, _namespace_tag("lambert"))
    diffuse = ET.SubElement(lambert, _namespace_tag("diffuse"))
    color = ET.SubElement(diffuse, _namespace_tag("color"))
    color.text = "0.8 0.8 0.8 1"


def _append_plate_material(library_materials: ET.Element, output_mesh: str) -> None:
    material = ET.SubElement(
        library_materials,
        _namespace_tag("material"),
        {"id": f"{output_mesh}-material", "name": output_mesh},
    )
    ET.SubElement(material, _namespace_tag("instance_effect"), {"url": f"#{output_mesh}-effect"})


def _set_geometry_material(geometry: ET.Element, material_id: str) -> None:
    for tag in ("triangles", "polylist", "polygons"):
        for primitive in geometry.findall(f".//c:{tag}", transform_helpers.NS):
            primitive.set("material", material_id)


def _set_instance_materials(node: ET.Element, material_id: str) -> None:
    for instance_material in node.findall(".//c:instance_material", transform_helpers.NS):
        instance_material.set("symbol", material_id)
        instance_material.set("target", f"#{material_id}")


def _rear_clone_dae_name(source_zip: Path, dae_path: str) -> str:
    digest = hashlib.sha1(f"{source_zip}|{dae_path}".encode("utf-8", errors="replace")).hexdigest()[:8]
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(dae_path).stem).strip("._-") or "source"
    return f"bhdc_plate_rear_{stem}_{digest}.dae"


def _write_rear_clone_daes(
    context,
    output_vehicle_dir: Path,
    clone_sources: dict[str, _RearMeshClone],
    summary: dict[str, object],
) -> None:
    import beamng_hand_drive_core as core

    grouped: dict[tuple[Path, str], list[_RearMeshClone]] = {}
    for clone in clone_sources.values():
        grouped.setdefault((clone.source_zip, clone.dae_path), []).append(clone)

    for (source_zip, dae_path), clones in sorted(grouped.items(), key=lambda item: (str(item[0][0]).lower(), item[0][1].lower())):
        tree = core.parse_dae(source_zip, dae_path)
        root = tree.getroot()
        library_geometries = root.find("c:library_geometries", transform_helpers.NS)
        library_visual_scenes = root.find("c:library_visual_scenes", transform_helpers.NS)
        if library_geometries is None or library_visual_scenes is None:
            summary["warnings"].append(f"Could not clone rear plate mesh from {dae_path}: DAE has no geometry scene")
            continue
        library_materials = _find_or_create_library(root, "library_materials")
        library_effects = _find_or_create_library(root, "library_effects")

        geometries_by_id = {
            geom.get("id"): geom
            for geom in library_geometries.findall("c:geometry", transform_helpers.NS)
            if geom.get("id")
        }
        cloned_nodes: list[ET.Element] = []
        cloned_geometries: dict[str, ET.Element] = {}
        material_meshes: set[str] = set()

        for clone in sorted(clones, key=lambda item: item.output_mesh):
            source_node = core.find_dae_node(root, clone.source_node_id)
            if source_node is None:
                summary["warnings"].append(
                    f"Could not clone rear plate mesh '{clone.source_mesh}': node missing in {Path(dae_path).name}"
                )
                continue
            new_node = copy.deepcopy(source_node)
            new_node.set("id", clone.output_mesh)
            new_node.set("name", clone.output_mesh)
            material_id = f"{clone.output_mesh}-material"
            _set_instance_materials(new_node, material_id)

            for inst in new_node.findall(".//c:instance_geometry", transform_helpers.NS):
                url = inst.get("url", "")
                if not url.startswith("#"):
                    continue
                old_geom_id = url[1:]
                old_geom = geometries_by_id.get(old_geom_id)
                if old_geom is None:
                    continue
                new_geom_id = core.safe_id(f"{old_geom_id}_{clone.output_mesh}")
                if new_geom_id not in cloned_geometries:
                    new_geom = transform_helpers.copied_geometry(old_geom, new_geom_id)
                    _set_geometry_material(new_geom, material_id)
                    cloned_geometries[new_geom_id] = new_geom
                inst.set("url", f"#{new_geom_id}")
                if inst.get("name"):
                    inst.set("name", clone.output_mesh)

            if new_node.findall(".//c:instance_geometry", transform_helpers.NS):
                cloned_nodes.append(new_node)
                material_meshes.add(clone.output_mesh)

        if not cloned_nodes or not cloned_geometries:
            continue

        _clear_children(library_geometries)
        for geometry in cloned_geometries.values():
            library_geometries.append(geometry)
        _clear_children(library_materials)
        _clear_children(library_effects)
        for output_mesh in sorted(material_meshes):
            _append_plate_effect(library_effects, output_mesh)
            _append_plate_material(library_materials, output_mesh)
        for visual_scene in library_visual_scenes.findall("c:visual_scene", transform_helpers.NS):
            _clear_children(visual_scene)
            for node in cloned_nodes:
                visual_scene.append(node)

        core.write_xml_tree(tree, output_vehicle_dir / _rear_clone_dae_name(source_zip, dae_path))


def _rear_plate_dae(mesh_names: list[str]) -> str:
    """A minimal COLLADA file holding centred plate quads (normal -Y, Z up),
    matching the orientation/UV conventions of the vanilla plate meshes."""
    geoms = []
    nodes = []
    materials = []
    effects = []
    for mesh in mesh_names:
        w, h = _REAR_QUADS[mesh]
        x, z = w / 2, h / 2
        positions = f"-{x} 0 -{z} {x} 0 -{z} {x} 0 {z} -{x} 0 {z}"
        effects.append(
            f'<effect id="{mesh}-effect"><profile_COMMON><technique sid="common"><lambert>'
            f'<diffuse><color>0.8 0.8 0.8 1</color></diffuse>'
            f"</lambert></technique></profile_COMMON></effect>"
        )
        materials.append(f'<material id="{mesh}-material" name="{mesh}"><instance_effect url="#{mesh}-effect"/></material>')
        uv_source = (
            f'<source id="{mesh}-uv0"><float_array id="{mesh}-uv0-array" count="8">0 0 1 0 1 1 0 1</float_array>'
            f'<technique_common><accessor source="#{mesh}-uv0-array" count="4" stride="2">'
            f'<param name="S" type="float"/><param name="T" type="float"/></accessor></technique_common></source>'
            f'<source id="{mesh}-uv1"><float_array id="{mesh}-uv1-array" count="8">0 0 1 0 1 1 0 1</float_array>'
            f'<technique_common><accessor source="#{mesh}-uv1-array" count="4" stride="2">'
            f'<param name="S" type="float"/><param name="T" type="float"/></accessor></technique_common></source>'
        )
        geoms.append(
            f'<geometry id="{mesh}-mesh" name="{mesh}"><mesh>'
            f'<source id="{mesh}-pos"><float_array id="{mesh}-pos-array" count="12">{positions}</float_array>'
            f'<technique_common><accessor source="#{mesh}-pos-array" count="4" stride="3">'
            f'<param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>'
            f'<source id="{mesh}-nrm"><float_array id="{mesh}-nrm-array" count="3">0 -1 0</float_array>'
            f'<technique_common><accessor source="#{mesh}-nrm-array" count="1" stride="3">'
            f'<param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>'
            f"{uv_source}"
            f'<vertices id="{mesh}-verts"><input semantic="POSITION" source="#{mesh}-pos"/></vertices>'
            f'<triangles material="{mesh}-material" count="2">'
            f'<input semantic="VERTEX" source="#{mesh}-verts" offset="0"/>'
            f'<input semantic="NORMAL" source="#{mesh}-nrm" offset="1"/>'
            f'<input semantic="TEXCOORD" source="#{mesh}-uv0" offset="2" set="0"/>'
            f'<input semantic="TEXCOORD" source="#{mesh}-uv1" offset="2" set="1"/>'
            f"<p>0 0 0 1 0 1 2 0 2 0 0 0 2 0 2 3 0 3</p></triangles></mesh></geometry>"
        )
        nodes.append(
            f'<node id="{mesh}" name="{mesh}" type="NODE">'
            f'<matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>'
            f'<instance_geometry url="#{mesh}-mesh" name="{mesh}"><bind_material><technique_common>'
            f'<instance_material symbol="{mesh}-material" target="#{mesh}-material">'
            f'<bind_vertex_input semantic="{mesh}-uv0" input_semantic="TEXCOORD" input_set="0"/>'
            f'<bind_vertex_input semantic="{mesh}-uv1" input_semantic="TEXCOORD" input_set="1"/>'
            f"</instance_material>"
            f"</technique_common></bind_material></instance_geometry></node>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">'
        "<asset><contributor><authoring_tool>BeamHDC</authoring_tool></contributor>"
        "<unit name=\"meter\" meter=\"1\"/><up_axis>Z_UP</up_axis></asset>"
        f"<library_effects>{''.join(effects)}</library_effects>"
        f"<library_materials>{''.join(materials)}</library_materials>"
        f"<library_geometries>{''.join(geoms)}</library_geometries>"
        f'<library_visual_scenes><visual_scene id="Scene" name="Scene">{"".join(nodes)}</visual_scene></library_visual_scenes>'
        '<scene><instance_visual_scene url="#Scene"/></scene></COLLADA>\n'
    )


def _rear_material_entry(mesh: str, fmt: str, normal_strength: float) -> dict[str, object]:
    tag = f"@licenseplate-{fmt}"
    return {
        "name": mesh,
        "mapTo": mesh,
        "class": "Material",
        "Stages": [
            {
                "baseColorMap": tag,
                "normalMap": f"{tag}-normal",
                "normalMapStrength": round(normal_strength, 3),
                "normalMapUseUV": 1,
                "roughnessFactor": 0.8,
                "roughnessMap": f"{tag}-specular",
            },
            {}, {}, {},
        ],
        "activeLayers": 1,
        "dynamicCubemap": True,
        "version": 1.5,
    }


def _visible_rear_plate_donor(
    context,
    slot_type: str,
    selected_part_id: str,
    selected_body: str,
) -> tuple[str, str] | None:
    """Find a usable rear plate part when the selected one is a hidden proxy.

    Some community mods keep the real BeamNG license plate flexbody far outside
    the vehicle and use a bumper-baked plate face instead. If that hidden proxy
    is selected, cloning it preserves the offscreen position. Prefer a sibling
    rear plate part with the same plate format and a plausible vehicle-space
    position, then force it into the active slot.
    """
    import beamng_transform_helpers as transform_helpers

    selected_wide = _part_uses_wide_plate(selected_body)
    selected_prefix = _plate_part_prefix(selected_part_id)
    candidates: list[tuple[int, str, str]] = []
    for part_id, (body, _filename) in context.part_body_index.items():
        part_text = str(part_id)
        if part_text == selected_part_id:
            continue
        meshes = transform_helpers.extract_part_mesh_names(body)
        if not any("licenseplate" in mesh.lower() for mesh in meshes):
            continue
        if _part_uses_wide_plate(body) != selected_wide:
            continue
        if _plate_part_is_hidden(body):
            continue
        slot_types = transform_helpers.extract_part_slot_types(body)
        slot_text = " ".join(slot_types)
        if not _looks_rear(slot_text, part_text, body):
            continue

        score = 0
        if slot_type in slot_types:
            score += 100
        if _plate_part_prefix(part_text) == selected_prefix:
            score += 50
        if selected_part_id.lower().replace("_eu", "") in part_text.lower():
            score += 20
        if "wide" in part_text.lower():
            score += 5
        candidates.append((score, part_text, body))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]))
    _score, part_id, body = candidates[0]
    return part_id, _force_slot_type(body, slot_type)


# --------------------------------------------------------------------------
# Build integration


def apply_to_build(
    context,
    conversion: dict[str, object],
    output_root: Path,
    output_vehicle_dir: Path,
    variant_targets: dict[str, str],
) -> dict[str, object]:
    """Generate plate assets for the built configs and update the written .pc
    files with a per-config random registration and the generated design."""
    import beamng_hand_drive_core as core

    summary: dict[str, object] = {
        "configsUpdated": 0,
        "designs": 0,
        "rearPartsCloned": 0,
        "warnings": [],
    }
    plans: list[tuple[str, str, dict[str, object]]] = []
    for config_name, target_hand in sorted(variant_targets.items()):
        cfg = effective_plate_config(conversion, config_name)
        if cfg is None:
            continue
        errors = validate_plate_config(cfg)
        if errors:
            raise PlateError(
                f"Licence plate settings for '{config_name}' are not buildable:\n- " + "\n- ".join(errors)
            )
        plans.append((config_name, target_hand, cfg))
    if not plans:
        return summary

    prefix = f"bhdc_{core.safe_id(context.vehicle_id)}"
    design_cache: dict[str, _DesignOutput] = {}
    part_bodies: dict[str, str] = {}
    rear_meshes_used: dict[str, tuple[str, float]] = {}  # mesh -> (rear format, max normal strength)
    rear_mesh_clone_sources: dict[str, _RearMeshClone] = {}
    rng = random.Random()

    for config_name, target_hand, cfg in plans:
        design = _emit_design(cfg, output_root, prefix, design_cache)
        part_bodies.setdefault(design.part_id, _design_part_body(design, str(cfg.get("size"))))

        output_config = core.variant_output_name(config_name, target_hand)
        pc_path = output_vehicle_dir / f"{output_config}.pc"
        if not pc_path.is_file():
            summary["warnings"].append(f"{config_name}: expected config file missing ({pc_path.name})")
            continue
        pc = core.load_beamng_json_file(pc_path)
        parts = dict(pc.get("parts", {}))
        parts[_DESIGN_SLOT] = design.part_id

        if design.rear_formats:
            cloned = _clone_rear_parts_for_config(
                context,
                config_name,
                cfg,
                design,
                part_bodies,
                rear_meshes_used,
                rear_mesh_clone_sources,
                parts,
                summary,
            )
            if not cloned:
                summary["warnings"].append(
                    f"{config_name}: no rear plate part found; rear plate keeps the front colour"
                )

        pc["parts"] = parts
        pc["licenseName"] = generate_registration(active_pattern(cfg), rng)
        core.write_text_file(pc_path, json.dumps(pc, indent=2), encoding="utf-8")
        summary["configsUpdated"] += 1

    if rear_meshes_used:
        fallback_meshes = sorted(mesh for mesh in rear_meshes_used if mesh in _REAR_FALLBACK_MESHES)
        if fallback_meshes:
            core.write_text_file(output_vehicle_dir / "bhdc_plate_rear.dae", _rear_plate_dae(fallback_meshes), encoding="utf-8")
        if rear_mesh_clone_sources:
            _write_rear_clone_daes(context, output_vehicle_dir, rear_mesh_clone_sources, summary)
        materials = {
            mesh: _rear_material_entry(mesh, fmt, normal_strength)
            for mesh, (fmt, normal_strength) in sorted(rear_meshes_used.items())
        }
        core.write_text_file(output_vehicle_dir / "bhdc_plate_rear.materials.json", json.dumps(materials, indent=2), encoding="utf-8")

    if part_bodies:
        jbeam_dir = output_vehicle_dir / "jbeam"
        jbeam_dir.mkdir(parents=True, exist_ok=True)
        body_text = ",\n".join(f'"{part_id}": {body}' if not body.lstrip().startswith(f'"{part_id}"') else body
                               for part_id, body in sorted(part_bodies.items()))
        contents = "{\n// Generated licence plate designs (BeamHDC).\n" + body_text + "\n}\n"
        core.write_text_file(jbeam_dir / "bhdc_licenseplates.jbeam", contents, encoding="utf-8")

    summary["designs"] = len(design_cache)
    return summary


def _clone_rear_parts_for_config(
    context,
    config_name: str,
    cfg: dict[str, object],
    design: _DesignOutput,
    part_bodies: dict[str, str],
    rear_meshes_used: dict[str, tuple[str, float]],
    rear_mesh_clone_sources: dict[str, _RearMeshClone],
    parts: dict[str, object],
    summary: dict[str, object],
) -> bool:
    import beamng_hand_drive_core as core

    cloned_any = False
    for slot_text, part_text, part_body in _iter_selected_plate_parts(context, config_name):
        if not _looks_rear(slot_text, part_text, part_body):
            continue
        new_part_id = f"bhdc_rear_{core.safe_id(part_text)}"
        if new_part_id not in part_bodies:
            clone_source_id = part_text
            clone_source_body = part_body
            if _plate_part_is_hidden(part_body):
                donor = _visible_rear_plate_donor(context, slot_text, part_text, part_body)
                if donor is None:
                    continue
                clone_source_id, clone_source_body = donor
            cloned = _clone_rear_plate_part(context, clone_source_id, clone_source_body, new_part_id, rear_mesh_clone_sources)
            if cloned is None:
                continue
            body, rear_fmt, output_meshes = cloned
            part_bodies[new_part_id] = body
            normal_strength = _effective_emboss_strength(cfg)
            for rear_mesh in output_meshes:
                existing = rear_meshes_used.get(rear_mesh)
                rear_meshes_used[rear_mesh] = (
                    rear_fmt,
                    max(normal_strength, existing[1] if existing else 0.0),
                )
            summary["rearPartsCloned"] = int(summary.get("rearPartsCloned", 0)) + 1
        parts[slot_text] = new_part_id
        cloned_any = True
    return cloned_any
