"""
legend_reader.py
================
Reads a LEGEND page and returns one entry per row: a cropped image of the
symbol and a cropped image of its description. Runs entirely locally, no AI.

Note: in these CAD legends the description text is drawn as OUTLINES, not real
text, so we can't auto-read the words. Instead the app crops the symbol and the
description picture for each row; the user types a short name once per project.

Public function:
    read_legend(pdf_path) -> list of {"symbol_png": dataURL, "description_png": dataURL}
"""

import base64
import fitz  # PyMuPDF

SYM_ZOOM = 5.0
DESC_ZOOM = 2.5
MIN_ROW_HEIGHT = 8.0
MAX_ROW_HEIGHT = 95.0


def _horizontal_lines(page):
    lines = []
    for d in page.get_drawings():
        for it in d["items"]:
            if it[0] == "l":
                x0, y0, x1, y1 = it[1].x, it[1].y, it[2].x, it[2].y
                if abs(y1 - y0) < 0.6 and abs(x1 - x0) > 40:
                    lines.append((round((y0 + y1) / 2, 1), min(x0, x1), max(x0, x1)))
    return lines


def _table_right_edge(lines, sym_x0, header_y):
    """Right edge of a table = furthest extent of its row rules (lines starting near its left)."""
    xs = [x1 for (y, x0, x1) in lines if y > header_y and abs(x0 - sym_x0) < 40]
    return max(xs) if xs else None


def _find_tables(words, lines):
    """Find legend tables by anchoring on 'SYMBOL' and taking the next column
    to its right as the name/description column — whatever it's headed
    ('DESCRIPTION', 'SCIENTIFIC NAME', etc.).
    """
    syms = sorted([w for w in words if w[4].upper() == "SYMBOL"], key=lambda w: w[0])
    tables = []
    for s in syms:
        sy_mid = (s[1] + s[3]) / 2
        # right limit of this table = next SYMBOL on the same header row
        right_limit = None
        for n in syms:
            if n[0] > s[2] + 30 and abs(((n[1] + n[3]) / 2) - sy_mid) < 12:
                right_limit = n[0] - 8
                break
        if right_limit is None:
            right_limit = s[0] + 1200

        # header words on the same row, to the right of SYMBOL, within this table
        hdr = [w for w in words
               if w[0] > s[2] - 1 and w[0] < right_limit
               and abs(((w[1] + w[3]) / 2) - sy_mid) < 7]
        hdr.sort(key=lambda w: w[0])
        if not hdr:
            continue
        # cluster header words into columns by x-gap (multi-word headers stay together)
        cols = [[hdr[0]]]
        for w in hdr[1:]:
            if w[0] - cols[-1][-1][2] > 14:
                cols.append([w])
            else:
                cols[-1].append(w)
        div_x = cols[0][0][0] - 5            # left edge of the name column
        if len(cols) > 1:
            right_x = cols[1][0][0] - 5      # name column ends where the next column starts
        else:
            edge = _table_right_edge(lines, s[0] - 6, s[3])  # last column -> table right edge
            right_x = min(edge, right_limit) if edge else right_limit
        tables.append({"sym_x0": s[0] - 6, "div_x": div_x,
                       "right_x": right_x, "header_y": s[3]})
    return tables


def _row_boundaries(lines, table):
    width = table["right_x"] - table["sym_x0"]
    ys = []
    for y, x0, x1 in lines:
        if y <= table["header_y"]:
            continue
        overlap = min(x1, table["right_x"]) - max(x0, table["sym_x0"])
        if overlap > width * 0.5:
            ys.append(y)
    ys = sorted(set(round(y, 1) for y in ys))
    merged = []
    for y in ys:
        if not merged or y - merged[-1] > 2:
            merged.append(y)
    return merged


def _has_ink(page, clip, zoom=2.0, thresh=110, min_frac=0.004):
    """True if the clipped area has enough dark pixels to be a real cell."""
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
    s = pix.samples
    n = pix.n
    dark = 0
    total = pix.width * pix.height
    if total == 0:
        return False
    step = n  # one pixel at a time
    for i in range(0, len(s), step):
        # average of the colour channels (ignore alpha)
        chans = s[i:i + min(3, n)]
        if chans and (sum(chans) / len(chans)) < thresh:
            dark += 1
    return (dark / total) > min_frac


def _png(page, clip, zoom):
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
    return "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode()


def read_legend(pdf_path):
    doc = fitz.open(pdf_path)
    page = doc[0]
    words = page.get_text("words")
    lines = _horizontal_lines(page)
    tables = _find_tables(words, lines)

    entries = []
    for t in tables:
        bounds = _row_boundaries(lines, t)
        for y_top, y_bot in zip(bounds, bounds[1:]):
            h = y_bot - y_top
            if h < MIN_ROW_HEIGHT or h > MAX_ROW_HEIGHT:
                continue
            desc_clip = fitz.Rect(t["div_x"], y_top, t["right_x"], y_bot)
            # skip blank rows (no description ink)
            if not _has_ink(page, desc_clip):
                continue
            sym_clip = fitz.Rect(t["sym_x0"], y_top, t["div_x"], y_bot)
            entries.append({
                "symbol_png": _png(page, sym_clip, SYM_ZOOM),
                "description_png": _png(page, desc_clip, DESC_ZOOM),
            })
    return entries


if __name__ == "__main__":
    import sys
    rows = read_legend(sys.argv[1])
    print(f"Found {len(rows)} legend rows (symbol + description crops).")
