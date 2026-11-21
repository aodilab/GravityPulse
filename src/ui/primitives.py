"""Anti-aliased Apple-Neo UI primitives rendered with Pillow for high-DPI Tkinter."""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFilter, ImageTk


def make_rounded_card_image(
    width: int,
    height: int,
    radius: int,
    fill: str = "#ffffff",
    bg: str = "#f5f5f7",
    shadow_pad: int = 12,
) -> ImageTk.PhotoImage:
    """Render a crisp anti-aliased rounded card with soft diffused drop shadow."""
    ss = 2
    total_w = width + shadow_pad * 2
    total_h = height + shadow_pad * 2

    canvas = Image.new("RGBA", (total_w * ss, total_h * ss), bg)
    shadow_layer = Image.new("RGBA", (total_w * ss, total_h * ss), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_layer)

    sx0 = shadow_pad * ss
    sy0 = (shadow_pad + 3) * ss
    sx1 = (shadow_pad + width) * ss - 1
    sy1 = (shadow_pad + 3 + height) * ss - 1
    sd.rounded_rectangle([sx0, sy0, sx1, sy1], radius=radius * ss, fill=(15, 38, 74, 22))
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=6 * ss))
    canvas = Image.alpha_composite(canvas, shadow_layer)

    cd = ImageDraw.Draw(canvas)
    cx0 = shadow_pad * ss
    cy0 = shadow_pad * ss
    cx1 = (shadow_pad + width) * ss - 1
    cy1 = (shadow_pad + height) * ss - 1
    cd.rounded_rectangle([cx0, cy0, cx1, cy1], radius=radius * ss, fill=fill)

    out = canvas.resize((total_w, total_h), Image.Resampling.LANCZOS).convert("RGB")
    return ImageTk.PhotoImage(out)


def make_pill_image(
    width: int,
    height: int,
    fill: str,
    bg: str = "#f5f5f7",
    border: str | None = None,
    border_width: int = 1,
    shadow: bool = False,
) -> ImageTk.PhotoImage:
    """Render a crisp anti-aliased pill background."""
    ss = 2
    pad = 3 if shadow else 0
    total_w = width + pad * 2
    total_h = height + pad * 2

    canvas = Image.new("RGBA", (total_w * ss, total_h * ss), bg)
    if shadow:
        sh = Image.new("RGBA", (total_w * ss, total_h * ss), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sh)
        sd.rounded_rectangle(
            [pad * ss, (pad + 1) * ss, (pad + width) * ss - 1, (pad + 1 + height) * ss - 1],
            radius=(height * ss) // 2,
            fill=(0, 0, 0, 18),
        )
        sh = sh.filter(ImageFilter.GaussianBlur(radius=2 * ss))
        canvas = Image.alpha_composite(canvas, sh)

    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        [pad * ss, pad * ss, (pad + width) * ss - 1, (pad + height) * ss - 1],
        radius=(height * ss) // 2,
        fill=fill,
        outline=border,
        width=border_width * ss if border else 0,
    )
    out = canvas.resize((total_w, total_h), Image.Resampling.LANCZOS).convert("RGB")
    return ImageTk.PhotoImage(out)


def make_brand_badge_image(size: int) -> ImageTk.PhotoImage:
    """Render the dark rounded-square monochrome clock badge."""
    ss = 4
    s_px = size * ss
    img = Image.new("RGBA", (s_px, s_px), "#f5f5f7")
    d = ImageDraw.Draw(img)
    r = int(s_px * 0.28)
    d.rounded_rectangle([0, 0, s_px - 1, s_px - 1], radius=r, fill="#1d1d1f")

    cx = cy = s_px / 2.0
    cr = s_px * 0.31
    lw = max(2, int(round(s_px * 0.068)))
    d.ellipse([cx - cr, cy - cr, cx + cr, cy + cr], outline="#ffffff", width=lw)
    d.line([cx, cy, cx, cy - cr * 0.56], fill="#ffffff", width=lw)
    d.line([cx, cy, cx + cr * 0.44, cy + cr * 0.24], fill="#ffffff", width=lw)

    out = img.resize((size, size), Image.Resampling.LANCZOS).convert("RGB")
    return ImageTk.PhotoImage(out)


def make_chevron_icon(size: int, direction: str = "left", color: str = "#6e6e73", bg: str = "#ffffff") -> ImageTk.PhotoImage:
    """Render a crisp anti-aliased chevron arrow."""
    ss = 4
    s_px = size * ss
    img = Image.new("RGBA", (s_px, s_px), bg)
    d = ImageDraw.Draw(img)
    lw = max(2, int(round(s_px * 0.11)))
    if direction == "left":
        pts = [(s_px * 0.62, s_px * 0.24), (s_px * 0.34, s_px * 0.50), (s_px * 0.62, s_px * 0.76)]
    else:
        pts = [(s_px * 0.38, s_px * 0.24), (s_px * 0.66, s_px * 0.50), (s_px * 0.38, s_px * 0.76)]
    d.line(pts, fill=color, width=lw, joint="curve")
    out = img.resize((size, size), Image.Resampling.LANCZOS).convert("RGB")
    return ImageTk.PhotoImage(out)


def make_refresh_icon(size: int, color: str = "#ffffff", bg: str = "#1d1d1f") -> ImageTk.PhotoImage:
    """Render a clean circular refresh arrow icon."""
    ss = 4
    s_px = size * ss
    img = Image.new("RGBA", (s_px, s_px), bg)
    d = ImageDraw.Draw(img)
    lw = max(2, int(round(s_px * 0.11)))
    pad = s_px * 0.18
    d.arc([pad, pad, s_px - pad, s_px - pad], start=35, end=315, fill=color, width=lw)
    ax, ay = s_px * 0.77, s_px * 0.29
    d.polygon([(ax, ay), (ax - s_px * 0.18, ay - s_px * 0.04), (ax + s_px * 0.02, ay - s_px * 0.19)], fill=color)
    out = img.resize((size, size), Image.Resampling.LANCZOS).convert("RGB")
    return ImageTk.PhotoImage(out)


def make_timeline_image(
    width: int,
    height: int,
    segments: list[tuple[float, float]],
    track_color: str = "#f5f5f7",
    seg_color: str = "#1d1d1f",
    bg: str = "#ffffff",
) -> ImageTk.PhotoImage:
    """Render a supersampled 24-hour pill timeline track with active intervals."""
    ss = 2
    w_px, h_px = width * ss, height * ss
    img = Image.new("RGBA", (w_px, h_px), bg)
    d = ImageDraw.Draw(img)
    r = h_px // 2
    d.rounded_rectangle([0, 0, w_px - 1, h_px - 1], radius=r, fill=track_color)

    for start_pct, width_pct in segments:
        x0 = max(0, int(round((start_pct / 100.0) * w_px)))
        x1 = min(w_px - 1, max(x0 + h_px, int(round(((start_pct + width_pct) / 100.0) * w_px))))
        d.rounded_rectangle([x0, 0, x1, h_px - 1], radius=r, fill=seg_color)

    out = img.resize((width, height), Image.Resampling.LANCZOS).convert("RGB")
    return ImageTk.PhotoImage(out)
