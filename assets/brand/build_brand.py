#!/usr/bin/env python3
"""Regenerate the katet brand assets from the two source fonts.

Dev-only helper (not part of katet's runtime). Needs:
    pip install fonttools uharfbuzz cairosvg pillow
and the fonts from https://github.com/google/fonts (OFL):
    ofl/bricolagegrotesque/BricolageGrotesque[opsz,wdth,wght].ttf
    ofl/jetbrainsmono/JetBrainsMono[wght].ttf

Usage: python build_brand.py <bricolage.ttf> <jetbrainsmono.ttf> <repo-root>
All text is converted to outlines, so no output depends on installed fonts.
"""
import io
import json
import sys
from pathlib import Path

import cairosvg
import uharfbuzz as hb
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from PIL import Image

INK = "#15171C"
PAPER = "#F5F6F8"
ACCENT = "#D07A1F"
MUTED = "#A3A9B5"

BIG = {"opsz": 96, "wght": 800, "wdth": 100}    # wordmark, app icon
SMALL = {"opsz": 14, "wght": 800, "wdth": 100}  # favicon, menu bar


class Font:
    def __init__(self, path, loc):
        self.path, self.loc = path, loc
        self.tt = TTFont(path)
        self.upm = self.tt["head"].unitsPerEm
        self.glyphs = self.tt.getGlyphSet(location=loc)
        self.order = self.tt.getGlyphOrder()
        face = hb.Face(hb.Blob.from_file_path(path))
        self.hb = hb.Font(face)
        self.hb.set_variations(loc)

    def run(self, text, x0=0.0, tracking=0.0):
        """Shape text; return [(glyph, x, y)] in font units and the end x."""
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(self.hb, buf, {"kern": True, "liga": True})
        out, x = [], x0
        for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
            out.append((self.order[info.codepoint], x + pos.x_offset, pos.y_offset))
            x += pos.x_advance + tracking * self.upm
        return out, x


class Layout:
    """Glyphs and dots in font units (y up), with a role per element for coloring."""

    def __init__(self):
        self.items = []  # ("glyph", font, name, x, y, role) | ("dot", cx, cy, r, role)

    def add_run(self, font, run, role):
        for name, x, y in run:
            self.items.append(("glyph", font, name, x, y, role))

    def add_dot(self, cx, cy, r, role):
        self.items.append(("dot", cx, cy, r, role))

    def bounds(self):
        xs0, ys0, xs1, ys1 = [], [], [], []
        for it in self.items:
            if it[0] == "glyph":
                _, font, name, x, y, _ = it
                bp = BoundsPen(font.glyphs)
                font.glyphs[name].draw(bp)
                if bp.bounds:
                    b = bp.bounds
                    xs0.append(b[0] + x); ys0.append(b[1] + y)
                    xs1.append(b[2] + x); ys1.append(b[3] + y)
            else:
                _, cx, cy, r, _ = it
                xs0.append(cx - r); ys0.append(cy - r)
                xs1.append(cx + r); ys1.append(cy + r)
        return min(xs0), min(ys0), max(xs1), max(ys1)

    def svg_body(self, scale, tx, ty, colors, skip=()):
        """SVG elements. A font point (x, y) lands at (tx + s*x, ty - s*y)."""
        paths = {}
        out = []
        for it in self.items:
            role = it[-1]
            if role in skip:
                continue
            if it[0] == "glyph":
                _, font, name, x, y, _ = it
                pen = SVGPathPen(font.glyphs)
                font.glyphs[name].draw(TransformPen(pen, (scale, 0, 0, -scale, tx + scale * x, ty - scale * y)))
                paths.setdefault(role, []).append(pen.getCommands())
            else:
                _, cx, cy, r, _ = it
                out.append(f'<circle cx="{tx + scale * cx:.2f}" cy="{ty - scale * cy:.2f}" r="{scale * r:.2f}" fill="{colors[role]}"/>')
        glyphs = [f'<path fill="{colors[role]}" d="{" ".join(d)}"/>' for role, d in paths.items()]
        return "\n".join(glyphs + out)

    def fit(self, box_x, box_y, box_w, box_h, height=None, width=None):
        """Scale so the layout's height (or width) matches, centred in the box."""
        x0, y0, x1, y1 = self.bounds()
        s = height / (y1 - y0) if height else width / (x1 - x0)
        tx = box_x + (box_w - s * (x1 - x0)) / 2 - s * x0
        ty = box_y + (box_h + s * (y1 - y0)) / 2 + s * y0
        return s, tx, ty


def svg_doc(w, h, body, label=None, extra=""):
    a11y = f' role="img" aria-label="{label}"' if label else ' aria-hidden="true"'
    title = f"<title>{label}</title>\n" if label else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:g}" height="{h:g}" viewBox="0 0 {w:g} {h:g}"{a11y}>\n'
            f"{title}{extra}{body}\n</svg>\n")


# ---------------------------------------------------------------- layouts

def wordmark(big):
    """'ka' + accent dot + 'tet', spaced as on the design canvas."""
    lay = Layout()
    run, x = big.run("ka", 0, tracking=-0.04)
    lay.add_run(big, run, "text")
    d = 203
    left = x + 94
    lay.add_dot(left + d / 2, 156 + d / 2, d / 2, "dot")
    run, _ = big.run("tet", left + d + 78, tracking=-0.04)
    lay.add_run(big, run, "text")
    return lay


def mark(font, dot=200, gap=70, lift=158):
    """'k' + dot. Returns the layout and the dot as a separate role."""
    lay = Layout()
    run, x = font.run("k")
    lay.add_run(font, run, "text")
    lay.add_dot(x + gap + dot / 2, lift + dot / 2, dot / 2, "dot")
    return lay


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data)
    else:
        path.write_bytes(data)


def png(svg, w, h=None):
    data = cairosvg.svg2png(bytestring=svg.encode(), output_width=w, output_height=h or w)
    return Image.open(io.BytesIO(data)).convert("RGBA")


def main(bricolage, jbmono, root):
    root = Path(root)
    brand = root / "assets/brand"
    big, small = Font(bricolage, BIG), Font(bricolage, SMALL)
    mono = Font(jbmono, {"wght": 500})

    # --- wordmark, light and dark backgrounds (height 120 px, padded 8%)
    wm = wordmark(big)
    x0, y0, x1, y1 = wm.bounds()
    h = 120
    s = h / (y1 - y0)
    pad = 0.08 * h
    W, H = s * (x1 - x0) + 2 * pad, h + 2 * pad
    for name, text in (("katet-wordmark.svg", INK), ("katet-wordmark-dark.svg", PAPER)):
        body = wm.svg_body(s, pad - s * x0, pad + s * y1, {"text": text, "dot": ACCENT})
        write(brand / name, svg_doc(round(W, 1), round(H, 1), body, "katet"))

    # --- mark "k·", light and dark backgrounds
    mk = mark(big)
    for name, text in (("katet-mark.svg", INK), ("katet-mark-dark.svg", PAPER)):
        s, tx, ty = mk.fit(8, 8, 112, 112, height=112)
        write(brand / name, svg_doc(128, 128, mk.svg_body(s, tx, ty, {"text": text, "dot": ACCENT}), "katet"))

    # --- macOS app icon: 1024 canvas, 824 body (Apple's icon grid), dark tile
    def app_icon_svg():
        s, tx, ty = mk.fit(100, 100, 824, 824, height=824 * 0.50)
        tx += 28  # optical centring: the k carries most of the visual weight
        body = (f'<rect x="100" y="100" width="824" height="824" rx="185" fill="{INK}"/>\n'
                + mk.svg_body(s, tx, ty, {"text": PAPER, "dot": ACCENT}))
        return svg_doc(1024, 1024, body, "katet app icon")

    icon_svg = app_icon_svg()
    write(brand / "katet-app-icon.svg", icon_svg)
    png(icon_svg, 1024).save(brand / "katet-app-icon-1024.png")

    # AppIcon.appiconset (doubles as AppIcon.iconset for `iconutil -c icns`)
    iconset = root / "assets/brand/build/Assets.xcassets/AppIcon.appiconset"
    images, rendered = [], {}
    for pt in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = pt * scale
            fname = f"icon_{pt}x{pt}{'@2x' if scale == 2 else ''}.png"
            img = rendered.setdefault(px, png(icon_svg, px))
            iconset.mkdir(parents=True, exist_ok=True)
            img.save(iconset / fname)
            images.append({"filename": fname, "idiom": "mac", "scale": f"{scale}x", "size": f"{pt}x{pt}"})
    write(iconset / "Contents.json", json.dumps({"images": images, "info": {"author": "xcode", "version": 1}}, indent=2) + "\n")
    write(root / "assets/brand/build/Assets.xcassets/Contents.json", json.dumps({"info": {"author": "xcode", "version": 1}}, indent=2) + "\n")

    # AppIcon.icns for builds without an asset catalog (e.g. SwiftPM + hand-made .app bundle)
    base = rendered[1024]
    others = [rendered[p] for p in (16, 32, 64, 128, 256, 512)]
    base.save(root / "app/icon/AppIcon.icns", append_images=others)

    # --- menu-bar template images: black + alpha, 18 pt tall, same canvas for both states
    mb = mark(small, dot=260, gap=60, lift=150)
    x0, y0, x1, y1 = mb.bounds()
    k_only = Layout()
    k_only.items = [it for it in mb.items if it[-1] == "text"]
    kx0, ky0, kx1, ky1 = k_only.bounds()
    pt_h, glyph_h = 18, 14
    s = glyph_h / (ky1 - ky0)
    mb_w = max(18, round(s * (x1 - x0) + 2))
    tx = (mb_w - s * (x1 - x0)) / 2 - s * x0
    ty = (pt_h + s * (ky1 - ky0)) / 2 + s * ky0
    variants = {
        "MenuBarIdle": mb.svg_body(s, tx, ty, {"text": "#000", "dot": "#000"}, skip=("dot",)),
        "MenuBarAttention": mb.svg_body(s, tx, ty, {"text": "#000", "dot": "#000"}),
    }
    for name, body in variants.items():
        svg = svg_doc(mb_w, pt_h, body)
        write(brand / f"{name.lower().replace('menubar', 'menubar-')}.svg", svg)
        imageset = root / f"assets/brand/build/Assets.xcassets/{name}.imageset"
        imageset.mkdir(parents=True, exist_ok=True)
        imgs = []
        for scale in (1, 2):
            fname = f"{name}{'@2x' if scale == 2 else ''}.png"
            img = png(svg, mb_w * scale, pt_h * scale)
            img.save(imageset / fname)
            (root / "app/icon").mkdir(parents=True, exist_ok=True)
            img.save(root / f"app/icon/{name}Template{'@2x' if scale == 2 else ''}.png")
            imgs.append({"filename": fname, "idiom": "universal", "scale": f"{scale}x"})
        imgs.append({"idiom": "universal", "scale": "3x"})
        write(imageset / "Contents.json", json.dumps({
            "images": imgs, "info": {"author": "xcode", "version": 1},
            "properties": {"template-rendering-intent": "template"}}, indent=2) + "\n")

    # --- web board favicons
    fav = mark(small, dot=260, gap=60, lift=150)
    s, tx, ty = fav.fit(0, 0, 32, 32, width=30)
    style = (f"<style>.t{{fill:{INK}}}@media (prefers-color-scheme: dark){{.t{{fill:{PAPER}}}}}</style>\n")
    body = fav.svg_body(s, tx, ty, {"text": "CLASS_T", "dot": ACCENT}).replace('fill="CLASS_T"', 'class="t"')
    write(root / "board/favicon.svg", svg_doc(32, 32, body, "katet", extra=style))

    def tile_svg():
        s, tx, ty = fav.fit(0, 0, 64, 64, height=64 * 0.62)
        body = (f'<rect width="64" height="64" rx="14" fill="{INK}"/>\n'
                + fav.svg_body(s, tx - 0.5, ty, {"text": PAPER, "dot": ACCENT}))
        return svg_doc(64, 64, body)

    tile = tile_svg()
    ico = [png(tile, p) for p in (48, 32, 16)]
    ico[0].save(root / "board/favicon.ico", sizes=[(48, 48), (32, 32), (16, 16)], append_images=ico[1:])

    # --- GitHub social preview, 1280x640
    s = 600 / (wm.bounds()[2] - wm.bounds()[0])
    x0, y0, x1, y1 = wm.bounds()
    wm_h = s * (y1 - y0)
    tag = Layout()
    run, _ = mono.run("A markdown-file workflow for working with coding agents.")
    tag.add_run(mono, run, "text")
    ts, ttx, _ = tag.fit(0, 0, 1280, 0, height=26)
    tb = tag.bounds()
    tag_h = ts * (tb[3] - tb[1])
    gap = 56
    top = (640 - (wm_h + gap + tag_h)) / 2
    body = (f'<rect width="1280" height="640" fill="{INK}"/>\n'
            + wm.svg_body(s, (1280 - s * (x1 - x0)) / 2 - s * x0, top + s * y1, {"text": PAPER, "dot": ACCENT}) + "\n"
            + tag.svg_body(ts, ttx, top + wm_h + gap + ts * tb[3], {"text": MUTED}))
    social = svg_doc(1280, 640, body, "katet")
    png(social, 1280, 640).convert("RGB").save(brand / "social-preview.png")


if __name__ == "__main__":
    main(*sys.argv[1:4])
