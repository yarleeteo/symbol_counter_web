"""
teach.py — teach-by-example symbol matcher.
===========================================
The engineer clicks one or more instances of a symbol on the drawing. We read
the vector primitives around each click, build a small geometric "signature",
and find every other place on the sheet whose local geometry matches it.

This is the accurate, scale-honest alternative to image matching: it works on
the drawing's actual shapes. It works best for symbols that contain a
distinctive component (a dome, an X, a specific arc) — the rarer that component
is on the sheet, the cleaner the match.

Public:
    find_matches(page, clicks, radius=8) -> list[(x, y)]   # PDF-point centres
"""

import math
from collections import Counter


def _tagged_primitives(page):
    """Return [(cx, cy, kind)] for every primitive, kind = bucketed descriptor."""
    prims = []
    for d in page.get_drawings():
        items = d["items"]
        ops = [it[0] for it in items]
        r = d["rect"]
        if ops and all(o == "c" for o in ops):                 # arc / circle path
            n = len(ops)
            md = max(r.width, r.height)
            near_sq = (min(r.width, r.height) >= 0.6 * md) if md > 0 else False
            kind = f"circle_{round(md)}" if (n == 4 and near_sq) else f"arc{n}_{round(md)}"
            prims.append(((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2, kind))
        elif "re" in ops:                                      # rectangle
            prims.append(((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2,
                          f"rect_{round(r.width)}x{round(r.height)}"))
        for it in items:                                       # line segments
            if it[0] == "l":
                x0, y0, x1, y1 = it[1].x, it[1].y, it[2].x, it[2].y
                L = math.hypot(x1 - x0, y1 - y0)
                if L < 0.4:
                    continue
                ang = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 180
                orient = "h" if (ang < 20 or ang > 160) else "v" if 70 < ang < 110 else "d"
                prims.append(((x0 + x1) / 2, (y0 + y1) / 2, f"line_{orient}_{round(L)}"))
    return prims


def _grid(prims, cell):
    g = {}
    for cx, cy, k in prims:
        g.setdefault((int(cx // cell), int(cy // cell)), []).append((cx, cy, k))
    return g


def _near(grid, cell, x, y, radius):
    out = []
    cx, cy = int(x // cell), int(y // cell)
    span = int(radius // cell) + 1
    for gx in range(cx - span, cx + span + 1):
        for gy in range(cy - span, cy + span + 1):
            for px, py, k in grid.get((gx, gy), ()):
                if (px - x) ** 2 + (py - y) ** 2 <= radius * radius:
                    out.append((px, py, k))
    return out


def _snap(grid, cell, x, y, radius):
    """Snap an approximate click to the centroid of the nearby primitives, so the
    result doesn't depend on clicking dead-centre."""
    near = _near(grid, cell, x, y, radius)
    if not near:
        return x, y
    return (sum(p[0] for p in near) / len(near),
            sum(p[1] for p in near) / len(near))


def find_matches(page, clicks, radius=8.0, min_score=0.55):
    prims = _tagged_primitives(page)
    if not prims:
        return []
    gc = Counter(k for _, _, k in prims)
    cell = radius
    grid = _grid(prims, cell)

    # Pick THE most distinctive primitive across all the clicked examples and use
    # it as the anchor — its local geometry is the signature. This is stable
    # wherever you click, and clicking more examples only improves seed choice.
    best = None
    gather = radius * 1.4
    for (qx, qy) in clicks:
        near = _near(grid, cell, qx, qy, gather)
        cands = [p for p in near if gc[p[2]] >= 3] or near
        if not cands:
            continue
        seed = min(cands, key=lambda p: gc[p[2]])
        if best is None or gc[seed[2]] < gc[best[2]]:
            best = seed
    if best is None:
        return []

    anchor = best[2]
    if gc[anchor] > 200:
        # No distinctive component — this symbol can't be taught reliably this way.
        return []
    tmpl = set(k for _, _, k in _near(grid, cell, best[0], best[1], radius))
    anchors = [(cx, cy) for cx, cy, k in prims if k == anchor]

    matches = []
    for ax, ay in anchors:
        kinds = set(k for _, _, k in _near(grid, cell, ax, ay, radius))
        if len(tmpl & kinds) / len(tmpl) >= min_score:
            if all((ax - mx) ** 2 + (ay - my) ** 2 > (radius * 0.7) ** 2 for mx, my in matches):
                matches.append((ax, ay))
    return matches
