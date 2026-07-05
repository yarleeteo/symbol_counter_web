"""
vector_engine.py
================
Deterministic fixture counting for VECTOR (CAD-exported) layout PDFs.

This is the proven core of the pipeline. It parses the drawing's actual
vector geometry with PyMuPDF instead of looking at pixels, so the counts
are exact and run on CPU — no model, no API cost.

Each classifier below encodes the geometric "signature" of one fixture
type. They are deliberately simple and explicit so you can read them,
re-tune the thresholds for a new drawing, and add new symbol types.

How a fixture is recognised (validated on a real carpark sheet):
  * fixture circle  -> a path of 4 bezier curves that is near-circular
  * 18W  (single)   -> circle with ONE black tube line through its centre
  * 2x18W (double)  -> circle with TWO parallel black tube lines straddling it
  * downlight (X)   -> circle with no tube, but >=2 short black crossing strokes
  * boxed fixture   -> small squarish shape with a BOLD dark stroke
                       (this is what separates EL/exit/isolator boxes from the
                        thin grey structural column squares)
"""

import math
import fitz  # PyMuPDF

# --------------------------------------------------------------------------
# Tunable thresholds — per-drawing calibration lives here.
# Units are PDF points (1/72").
# --------------------------------------------------------------------------
CIRCLE_RADIUS = (0.9, 2.0)    # fixture circle radius range
TUBE_MIN_LEN  = 7.0           # a "long" line = a tube body
TUBE_OFFSET   = 1.6           # how close a tube line must hug the circle centre
SHORT_LINE    = (1.2, 5.5)    # short crossing strokes (the downlight 'X')
BOX_SIDE      = (4.0, 18.0)   # fixture-box side length
BOX_MIN_WIDTH = 0.5           # stroke weight that separates bold fixture boxes
                              # from thin grey structural squares
BLACK_MAX     = 0.25          # colour <= this on every channel counts as "black"

# Metal-clad socket: its dome is filled solid, drawn as a tight cluster of tiny
# black filled slivers. A plain socket's dome is hollow (no such slivers).
SOCKET_FILL_NEAR   = 2.2      # how close a fill sliver must be to the dome centre
SOCKET_FILL_MIN    = 5        # >= this many slivers in the dome -> metal clad

# Human-readable labels + overlay colours (RGB 0..1) per category.
CATEGORY_LABELS = {
    "18W":       "18W LED fitting (single tube)",
    "2x18W":     "2x18W LED fitting (double tube)",
    "downlight": "18/24W LED downlight (recessed)",
    "EL":        "Emergency light (EL)",
    "exit":      "Exit / Keluar sign (K)",
    "socket":    "13A switch socket outlet (plain)",
    "socket_mc":  "13A switch socket outlet (metal clad)",
    "MS":        "MS symbol",
    "box":       "Boxed fixture (EL / exit)",   # fallback if box classifier unavailable
}
CATEGORY_COLORS = {
    "18W":       (0.00, 0.00, 1.00),  # blue
    "2x18W":     (0.90, 0.00, 0.00),  # red
    "downlight": (1.00, 0.50, 0.00),  # orange
    "EL":        (0.50, 0.00, 0.70),  # purple
    "exit":      (0.00, 0.65, 0.60),  # teal
    "socket":    (0.55, 0.27, 0.07),  # brown
    "socket_mc":  (0.15, 0.35, 0.65),  # steel blue (metal clad)
    "MS":        (0.85, 0.10, 0.50),  # pink
    "box":       (0.50, 0.00, 0.70),  # purple (fallback)
}


# --------------------------------------------------------------------------
# Loading & routing
# --------------------------------------------------------------------------
def load_first_page(path):
    doc = fitz.open(path)
    return doc, doc[0]


def is_vector(page):
    """Vector PDFs expose real drawing paths; scans return (almost) none."""
    return len(page.get_drawings()) > 50


def _is_black(color):
    return color is not None and max(color) <= BLACK_MAX


# --------------------------------------------------------------------------
# Primitive extraction
# --------------------------------------------------------------------------
def extract_primitives(page):
    """Pull the raw geometry we care about out of the page.

    Returns (circles, black_lines, boxes):
      circles     : list of {cx, cy, r}
      black_lines : list of (x0,y0,x1,y1, length, midx, midy)
      boxes       : list of {x0,y0,x1,y1}
    """
    circles, black_lines, boxes = [], [], []

    for d in page.get_drawings():
        items = d["items"]
        rect = d["rect"]
        w, h = rect.width, rect.height
        ops = [it[0] for it in items]

        # --- circles: 4 bezier curves forming a near-circular path ---
        if ops == ["c", "c", "c", "c"] and w > 0 and h > 0 and abs(w - h) / max(w, h) < 0.25:
            rad = (w + h) / 4
            if CIRCLE_RADIUS[0] <= rad <= CIRCLE_RADIUS[1]:
                circles.append({"cx": (rect.x0 + rect.x1) / 2,
                                "cy": (rect.y0 + rect.y1) / 2,
                                "r": rad})

        # --- black line segments (tube bodies, end caps, downlight crosses) ---
        if _is_black(d.get("color")):
            for it in items:
                if it[0] == "l":
                    x0, y0, x1, y1 = it[1].x, it[1].y, it[2].x, it[2].y
                    L = math.hypot(x1 - x0, y1 - y0)
                    if L >= SHORT_LINE[0]:
                        black_lines.append((x0, y0, x1, y1, L,
                                            (x0 + x1) / 2, (y0 + y1) / 2))

        # --- fixture boxes: small, squarish, BOLD dark stroke ---
        if (BOX_SIDE[0] <= w <= BOX_SIDE[1] and BOX_SIDE[0] <= h <= BOX_SIDE[1]
                and 0.6 <= w / h <= 1.66
                and ("re" in ops or "qu" in ops or ops.count("l") >= 3)
                and _is_black(d.get("color")) and (d.get("width") or 0) >= BOX_MIN_WIDTH):
            boxes.append({"x0": rect.x0, "y0": rect.y0, "x1": rect.x1, "y1": rect.y1})

    return circles, black_lines, boxes


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------
def _perp_offset(cx, cy, seg):
    """Signed perpendicular distance from a point to the line through seg."""
    x0, y0, x1, y1 = seg[:4]
    dx, dy = x1 - x0, y1 - y0
    L = math.hypot(dx, dy)
    return ((cx - x0) * dy - (cy - y0) * dx) / L


def classify_circle(circle, black_lines):
    """Classify one fixture circle -> '18W' | '2x18W' | 'downlight' | 'other'."""
    cx, cy, r = circle["cx"], circle["cy"], circle["r"]
    near = [s for s in black_lines if abs(s[5] - cx) < 7 and abs(s[6] - cy) < 7]
    longs = [s for s in near if s[4] >= TUBE_MIN_LEN]
    shorts = [s for s in near if SHORT_LINE[0] <= s[4] <= SHORT_LINE[1]]

    if longs:
        # Determine the tube axis, then count distinct parallel tube lines
        # that hug the circle centre. 1 line = single tube, 2 = double.
        vert = sum(1 for s in longs
                   if 65 < (math.degrees(math.atan2(s[3] - s[1], s[2] - s[0])) % 180) < 115)
        axis_vert = vert >= (len(longs) - vert)

        offsets = []
        for s in longs:
            ang = math.degrees(math.atan2(s[3] - s[1], s[2] - s[0])) % 180
            if (65 < ang < 115) == axis_vert:
                off = _perp_offset(cx, cy, s)
                if abs(off) < TUBE_OFFSET:
                    offsets.append(off)

        offsets.sort()
        clusters = []
        for o in offsets:
            if clusters and abs(o - clusters[-1][-1]) < 0.5:
                clusters[-1].append(o)
            else:
                clusters.append([o])
        return "2x18W" if len(clusters) >= 2 else "18W"

    # No tube body: downlight if its centre has a crossing 'X' (two diagonal
    # strokes). The X is exactly two diagonals, so requiring three strokes
    # (the old rule) missed every cleanly-drawn one.
    diagonals = 0
    for s in shorts:
        if abs(s[5] - cx) < 2.6 and abs(s[6] - cy) < 2.6:
            ang = math.degrees(math.atan2(s[3] - s[1], s[2] - s[0])) % 180
            if 28 < ang < 62 or 118 < ang < 152:
                diagonals += 1
    return "downlight" if diagonals >= 2 else "other"


def _black_fill_slivers(page):
    """Centres of the tiny black FILLED paths that make up a solid (metal-clad)
    socket dome. A plain socket's dome is a hollow outline with none of these."""
    pts = []
    for d in page.get_drawings():
        fill = d.get("fill")
        if fill is None or max(fill) > BLACK_MAX:
            continue
        r = d["rect"]
        if r.width < 1.6 and r.height < 1.6:
            pts.append(((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2))
    return pts


def socket_is_metalclad(cx, cy, slivers):
    """A socket is metal-clad if its dome is filled solid: i.e. a tight cluster
    of small black fill slivers sits over the dome centre."""
    n = sum(1 for sx, sy in slivers
            if abs(sx - cx) < SOCKET_FILL_NEAR and abs(sy - cy) < SOCKET_FILL_NEAR)
    return n >= SOCKET_FILL_MIN


def extract_sockets(page):
    """13A socket = a small 3-curve semicircle 'dome' with a straight stem stroke
    beside it. Derived from a real example on the drawing (teach-by-example):
    the dome is 3 bezier curves (~2-3pt); the DB-schedule domes are 2 curves, so
    they don't match. Requiring an adjacent stem stroke removes stray arcs."""
    draws = page.get_drawings()
    domes, strokes = [], []
    for d in draws:
        items = d["items"]
        if tuple(it[0] for it in items) == ("c", "c", "c"):
            r = d["rect"]
            if 1.3 < r.width < 3.6 and 1.3 < r.height < 3.6:
                domes.append(((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2))
        for it in items:
            if it[0] == "l":
                x0, y0, x1, y1 = it[1].x, it[1].y, it[2].x, it[2].y
                if 1.0 < ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 < 6.0:
                    strokes.append(((x0 + x1) / 2, (y0 + y1) / 2))
    sockets = []
    for cx, cy in domes:
        if any(abs(sx - cx) < 5 and abs(sy - cy) < 5 for sx, sy in strokes):
            if all((cx - ux) ** 2 + (cy - uy) ** 2 > 9 for ux, uy in sockets):
                sockets.append((cx, cy))
    return sockets


def extract_ms(page):
    """MS symbol = a ~4.2pt circle with the outlined 'MS' letters inside it. We
    key on a circle of that size whose interior holds a moderate cluster of text
    line-segments (the letters): plain fitting circles have none, and stray
    circles sitting over large label text have far more. Derived by example."""
    draws = page.get_drawings()
    circles, linesegs = [], []
    for d in draws:
        ops = [it[0] for it in d["items"]]
        r = d["rect"]
        if ops == ["c", "c", "c", "c"] and abs(r.width - r.height) < 1.2 \
                and 3.8 <= (r.width + r.height) / 2 <= 4.6:
            circles.append(((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2))
        nl = ops.count("l")
        if nl:
            linesegs.append(((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2, nl))
    ms = []
    for cx, cy in circles:
        s = sum(nl for lx, ly, nl in linesegs
                if (lx - cx) ** 2 + (ly - cy) ** 2 <= 2.6 * 2.6)
        # Needs interior text (a plain fitting circle has none). No upper bound:
        # a real MS sitting under a big area label ("13.71 sq m") has extra strokes.
        if s >= 16:
            if all((cx - mx) ** 2 + (cy - my) ** 2 > 9 for mx, my in ms):
                ms.append((cx, cy))
    return ms


def count_fixtures(page):
    """Run the full vector pipeline on a page.

    Returns a dict: category -> list of detections.
      circle categories -> (cx, cy)
      'box'             -> the box dict (so the overlay can draw its rectangle)
    """
    circles, black_lines, boxes = extract_primitives(page)
    results = {"18W": [], "2x18W": [], "downlight": [], "EL": [], "exit": [], "box": []}

    for c in circles:
        cat = classify_circle(c, black_lines)
        if cat in results:                      # 'other' = grid bubbles / schedule marks -> ignored
            results[cat].append((c["cx"], c["cy"]))

    # split boxes into Emergency Light (EL) vs Exit (K); fall back to one 'box' group
    try:
        import box_classifier
        use_split = box_classifier.available()
    except Exception:
        use_split = False
    for b in boxes:
        cat = box_classifier.classify_box(page, b) if use_split else "box"
        results.setdefault(cat, []).append(b)

    slivers = _black_fill_slivers(page)
    for s in extract_sockets(page):
        cat = "socket_mc" if socket_is_metalclad(s[0], s[1], slivers) else "socket"
        results.setdefault(cat, []).append(s)

    for m in extract_ms(page):
        results.setdefault("MS", []).append(m)

    # drop empty categories so the UI only shows what's present
    return {k: v for k, v in results.items() if v}
