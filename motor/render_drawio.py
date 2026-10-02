#!/usr/bin/env python3
"""Minimal drawio -> PNG renderer for mainbrain workflow diagrams.
Handles the subset of mxGraph produced by create_workflow_diagram:
rounded rects, ellipses, rhombus; orthogonal edges with arrowheads;
HTML-lite labels (<b>, <i>, <br>). Scales 2x for crisp text."""
import html
import re
import sys
import xml.etree.ElementTree as ET
from PIL import Image, ImageDraw, ImageFont

SCALE = 2

def load_font(size, bold=False, italic=False):
    candidates = []
    if bold:
        candidates += ["DejaVuSans-Bold.ttf"]
    if italic:
        candidates += ["DejaVuSans-Oblique.ttf"]
    candidates += ["DejaVuSans.ttf"]
    for name in candidates:
        for path in (f"/usr/share/fonts/truetype/dejavu/{name}",):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()

def parse_style(style):
    d = {}
    for part in (style or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            d[k] = v
        elif part:
            d[part] = True
    return d

def shape_of(style):
    if "ellipse" in style:
        return "ellipse"
    if "rhombus" in style:
        return "rhombus"
    return "rect"

# crude HTML-lite -> list of (text, bold, italic) segments split into lines
def parse_label(value):
    if not value:
        return []
    value = value.replace("<br>", "\n").replace("<br/>", "\n")
    lines = []
    for raw_line in value.split("\n"):
        segs = []
        pos = 0
        bold = italic = False
        for m in re.finditer(r"<(/?)(b|i)>", raw_line):
            text = raw_line[pos:m.start()]
            if text:
                segs.append((html.unescape(text), bold, italic))
            closing, tag = m.group(1), m.group(2)
            if tag == "b":
                bold = not closing
            else:
                italic = not closing
            pos = m.end()
        tail = raw_line[pos:]
        if tail:
            segs.append((html.unescape(tail), bold, italic))
        lines.append(segs or [("", False, False)])
    return lines

def wrap_line_segs(draw, segs, max_w, base_size):
    # flatten to words keeping style, then greedily wrap
    words = []
    for text, b, i in segs:
        for w in text.split(" "):
            words.append((w, b, i))
    out, cur, cur_w = [], [], 0
    space = draw.textlength(" ", font=load_font(base_size))
    for w, b, i in words:
        f = load_font(base_size, b, i)
        wlen = draw.textlength(w, font=f)
        if cur and cur_w + space + wlen > max_w:
            out.append(cur)
            cur, cur_w = [], 0
        cur.append((w, b, i))
        cur_w += (space if cur_w else 0) + wlen
    if cur:
        out.append(cur)
    return out

def draw_centered(draw, cx, cy_top, wrapped, base_size, color="#000000"):
    line_h = int(base_size * 1.35)
    y = cy_top
    for line in wrapped:
        total = 0
        for w, b, i in line:
            total += draw.textlength(w + " ", font=load_font(base_size, b, i))
        x = cx - total / 2
        for w, b, i in line:
            f = load_font(base_size, b, i)
            draw.text((x, y), w, font=f, fill=color)
            x += draw.textlength(w + " ", font=f)
        y += line_h
    return y

def render(path, out_path):
    tree = ET.parse(path)
    root = tree.getroot()
    model = root.find(".//mxGraphModel")
    pw = int(float(model.get("pageWidth", "850")))
    ph = int(float(model.get("pageHeight", "1100")))
    W, H = pw * SCALE, ph * SCALE

    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    cells = {}
    edges = []
    for cell in model.iter("mxCell"):
        cid = cell.get("id")
        geo = cell.find("mxGeometry")
        if cell.get("vertex") == "1" and geo is not None:
            cells[cid] = {
                "value": cell.get("value", ""),
                "style": cell.get("style", ""),
                "x": float(geo.get("x", 0)) * SCALE,
                "y": float(geo.get("y", 0)) * SCALE,
                "w": float(geo.get("width", 0)) * SCALE,
                "h": float(geo.get("height", 0)) * SCALE,
            }
        elif cell.get("edge") == "1":
            edges.append((cell.get("source"), cell.get("target")))

    def center(c):
        return (c["x"] + c["w"] / 2, c["y"] + c["h"] / 2)

    # edges first (orthogonal: vertical then horizontal)
    for s, t in edges:
        if s not in cells or t not in cells:
            continue
        sc, tc = cells[s], cells[t]
        x1, y1 = center(sc)
        x2, y2 = center(tc)
        # exit bottom of source, enter top of target when going down
        if abs(y2 - y1) >= abs(x2 - x1):
            p1 = (x1, sc["y"] + sc["h"])
            p2 = (x2, tc["y"])
            midy = (p1[1] + p2[1]) / 2
            pts = [p1, (p1[0], midy), (p2[0], midy), p2]
        else:
            p1 = (sc["x"] + sc["w"], y1)
            p2 = (tc["x"], y2)
            midx = (p1[0] + p2[0]) / 2
            pts = [p1, (midx, p1[1]), (midx, p2[1]), p2]
        d.line(pts, fill="#4d4d4d", width=2 * SCALE // 2 or 1)
        # arrowhead at p2
        ax, ay = pts[-1]
        px, py = pts[-2]
        import math
        ang = math.atan2(ay - py, ax - px)
        ah = 7 * SCALE // 2
        for da in (math.radians(150), math.radians(-150)):
            d.line([(ax, ay), (ax + ah * math.cos(ang + da),
                                ay + ah * math.sin(ang + da))],
                   fill="#4d4d4d", width=2)

    # nodes
    for cid, c in cells.items():
        st = parse_style(c["style"])
        fill = "#" + st.get("fillColor", "#ffffff").lstrip("#")
        stroke = "#" + st.get("strokeColor", "#000000").lstrip("#")
        shape = shape_of(c["style"])
        x, y, w, h = c["x"], c["y"], c["w"], c["h"]
        box = [x, y, x + w, y + h]
        if shape == "ellipse":
            d.ellipse(box, fill=fill, outline=stroke, width=2)
        elif shape == "rhombus":
            d.polygon([(x + w / 2, y), (x + w, y + h / 2),
                       (x + w / 2, y + h), (x, y + h / 2)],
                      fill=fill, outline=stroke)
        else:
            d.rounded_rectangle(box, radius=8 * SCALE // 2,
                                fill=fill, outline=stroke, width=2)
        base_size = 9 * SCALE
        lines = parse_label(c["value"])
        wrapped = []
        for segs in lines:
            wrapped += wrap_line_segs(d, segs, w - 10 * SCALE, base_size)
        line_h = int(base_size * 1.35)
        text_h = line_h * len(wrapped)
        draw_centered(d, x + w / 2, y + (h - text_h) / 2, wrapped, base_size)

    img.save(out_path)
    print(f"wrote {out_path} ({W}x{H})")

if __name__ == "__main__":
    for src in sys.argv[1:]:
        render(src, src.rsplit(".", 1)[0] + ".png")
