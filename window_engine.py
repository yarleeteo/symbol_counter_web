"""
window_engine.py
================
Window location detector for the drawing set.

The uploaded plans are mixed PDFs: the plan graphics are usually embedded
raster images, while the facade/direction highlights are vector overlays. This
engine therefore combines two conservative signals:

* raster callout bubbles around AW labels, projected back onto the nearest
  highlighted facade/window line;
* colored vector highlight segments as a fallback for unlabeled/unit-code sheets.

It intentionally returns one category only: "window". Legend/type detection is
not part of this workflow.
"""

import math

import fitz
from PIL import Image


WINDOW_KEY = "window"
WINDOW_LABEL = "Windows"
WINDOW_COLOR = (0.95, 0.18, 0.08)
WINDOW_DIRECTION_COLORS = {
    "North": (0.0, 0.2, 0.8),
    "East": (1.0, 1.0, 0.0),
    "West": (0.4, 0.0, 0.8),
    "South": (0.0, 0.6, 0.0),
}
_LEGEND_COLORS = {
    **WINDOW_DIRECTION_COLORS,
    "Not Included": (1.0, 0.0, 0.0),
}

_DPI = 200


def _xy(point):
    if isinstance(point, dict):
        return point["x"], point["y"]
    return point


def _dist2(a, b):
    a = _xy(a)
    b = _xy(b)
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def _is_cyan(color):
    return color and 0.35 < color[0] < 0.65 and color[1] > 0.8 and color[2] > 0.8


def _direction_for_color(color):
    if not color or _is_cyan(color):
        return None
    best = None
    best_d2 = None
    for direction, palette in _LEGEND_COLORS.items():
        d2 = sum((color[i] - palette[i]) ** 2 for i in range(3))
        if best is None or d2 < best_d2:
            best = direction
            best_d2 = d2
    return best if best_d2 is not None and best_d2 <= 0.08 else None


def _is_window_overlay_color(color):
    direction = _direction_for_color(color)
    return direction is not None and direction != "Not Included"


def _colored_segments(page):
    """Return colored overlay segments and drawing-group centers."""
    segments = []
    groups = []
    supplemental_groups = []
    for drawing in page.get_drawings():
        color = drawing.get("color") or drawing.get("fill")
        direction = _direction_for_color(color)
        if direction is None:
            continue

        rect = drawing["rect"]
        lines = []
        for item in drawing["items"]:
            if item[0] != "l":
                continue
            x0, y0 = item[1].x, item[1].y
            x1, y1 = item[2].x, item[2].y
            length = math.hypot(x1 - x0, y1 - y0)
            if length > 5:
                lines.append((x0, y0, x1, y1, length, direction))

        if not lines:
            continue
        segments.extend(lines)
        if direction == "Not Included":
            continue
        midpoints = []
        for line in lines:
            if line[4] < 12:
                continue
            midpoint = {"x": (line[0] + line[2]) / 2, "y": (line[1] + line[3]) / 2, "direction": direction}
            if all(_dist2(midpoint, old) > 8 * 8 for old in midpoints):
                midpoints.append(midpoint)
        longest = max(lines, key=lambda line: line[4])
        groups.append({"x": (longest[0] + longest[2]) / 2, "y": (longest[1] + longest[3]) / 2, "direction": direction})
        if 1 < len(midpoints) <= 3:
            supplemental_groups.extend(midpoints)

    return segments, _dedupe(groups, radius=5), _dedupe(supplemental_groups, radius=5)


def _plan_bbox(page):
    rects = []
    for drawing in page.get_drawings():
        color = drawing.get("color") or drawing.get("fill")
        if _direction_for_color(color) is None:
            continue
        rect = drawing["rect"]
        if rect.width > 1 or rect.height > 1:
            rects.append(rect)
    if not rects:
        return page.rect
    return fitz.Rect(
        min(r.x0 for r in rects),
        min(r.y0 for r in rects),
        max(r.x1 for r in rects),
        max(r.y1 for r in rects),
    )


def _nearest_point_on_segment(point, segment):
    px, py = _xy(point)
    x0, y0, x1, y1, _length = segment[:5]
    dx, dy = x1 - x0, y1 - y0
    denom = dx * dx + dy * dy
    if denom <= 0:
        return x0, y0
    t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / denom))
    return x0 + t * dx, y0 + t * dy


def _project_to_overlay(point, segments):
    if not segments:
        return point
    nearest = None
    nearest_d2 = None
    for segment in segments:
        q = _nearest_point_on_segment(point, segment)
        d2 = _dist2(point, q)
        if nearest is None or d2 < nearest_d2:
            nearest = q
            nearest_d2 = d2
    # Keep callouts if the nearest overlay line is implausibly far away.
    return nearest if nearest_d2 is not None and nearest_d2 <= 90 * 90 else point


def _nearest_projection(point, segments):
    nearest = None
    nearest_segment = None
    nearest_d2 = None
    for segment in segments:
        q = _nearest_point_on_segment(point, segment)
        d2 = _dist2(point, q)
        if nearest is None or d2 < nearest_d2:
            nearest = q
            nearest_segment = segment
            nearest_d2 = d2
    return nearest, nearest_segment, math.sqrt(nearest_d2) if nearest_d2 is not None else None


def _dark_gray_mask(image, max_value=230, max_spread=90):
    w, h = image.size
    pix = image.load()
    mask = bytearray(w * h)
    for y in range(h):
        row = y * w
        for x in range(w):
            r, g, b = pix[x, y]
            if max(r, g, b) < max_value and max(r, g, b) - min(r, g, b) < max_spread:
                mask[row + x] = 1
    return mask


def _component_boxes(
    image,
    max_value=230,
    max_spread=90,
    max_density=0.46,
    max_aspect=2.60,
    min_width=30,
    max_width=65,
):
    """Connected components for dark, low-saturation raster marks."""
    w, h = image.size
    mask = _dark_gray_mask(image, max_value=max_value, max_spread=max_spread)
    seen = bytearray(w * h)
    boxes = []

    for y in range(h):
        for x in range(w):
            idx = y * w + x
            if not mask[idx] or seen[idx]:
                continue

            stack = [idx]
            seen[idx] = 1
            minx = maxx = x
            miny = maxy = y
            count = 0

            while stack:
                pos = stack.pop()
                count += 1
                cy = pos // w
                cx = pos - cy * w
                minx = min(minx, cx)
                maxx = max(maxx, cx)
                miny = min(miny, cy)
                maxy = max(maxy, cy)

                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < w and 0 <= ny < h:
                        nidx = ny * w + nx
                        if mask[nidx] and not seen[nidx]:
                            seen[nidx] = 1
                            stack.append(nidx)

            bw = maxx - minx + 1
            bh = maxy - miny + 1
            if count < 80 or bh <= 0:
                continue
            aspect = bw / bh
            density = count / (bw * bh)
            if min_width <= bw <= max_width and 14 <= bh <= 36 and 1.45 <= aspect <= max_aspect and density < max_density:
                boxes.append((minx, miny, maxx, maxy))

    return boxes


def _raw_components(image, max_value=250, max_spread=150, min_pixels=10):
    """Low-level connected components used for fragmented raster labels."""
    w, h = image.size
    mask = _dark_gray_mask(image, max_value=max_value, max_spread=max_spread)
    seen = bytearray(w * h)
    out = []

    for y in range(h):
        for x in range(w):
            idx = y * w + x
            if not mask[idx] or seen[idx]:
                continue
            stack = [idx]
            seen[idx] = 1
            minx = maxx = x
            miny = maxy = y
            count = 0

            while stack:
                pos = stack.pop()
                count += 1
                cy = pos // w
                cx = pos - cy * w
                minx = min(minx, cx)
                maxx = max(maxx, cx)
                miny = min(miny, cy)
                maxy = max(maxy, cy)
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < w and 0 <= ny < h:
                        nidx = ny * w + nx
                        if mask[nidx] and not seen[nidx]:
                            seen[nidx] = 1
                            stack.append(nidx)

            bw = maxx - minx + 1
            bh = maxy - miny + 1
            if count >= min_pixels and bw <= 30 and bh <= 30:
                out.append((minx, miny, maxx, maxy, count))

    return out


def _cluster_label_fragments(image):
    parts = []
    for minx, miny, maxx, maxy, count in _raw_components(image):
        bw = maxx - minx + 1
        bh = maxy - miny + 1
        density = count / (bw * bh)
        if 3 <= bw <= 30 and 3 <= bh <= 24 and density >= 0.25:
            parts.append([minx, miny, maxx, maxy, count])

    clusters = []
    for part in sorted(parts, key=lambda item: (item[1], item[0])):
        placed = False
        for cluster in clusters:
            close_x = part[0] <= cluster[2] + 18 and part[2] >= cluster[0] - 18
            close_y = part[1] <= cluster[3] + 10 and part[3] >= cluster[1] - 10
            if close_x and close_y:
                cluster[0] = min(cluster[0], part[0])
                cluster[1] = min(cluster[1], part[1])
                cluster[2] = max(cluster[2], part[2])
                cluster[3] = max(cluster[3], part[3])
                cluster[4] += part[4]
                placed = True
                break
        if not placed:
            clusters.append(part[:])

    return [(x0, y0, x1, y1, n) for x0, y0, x1, y1, n in clusters]


def _raster_callouts(page, segments):
    pix = page.get_pixmap(matrix=fitz.Matrix(_DPI / 72, _DPI / 72), colorspace=fitz.csRGB)
    image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

    sx = pix.width / page.rect.width
    sy = pix.height / page.rect.height
    bbox = _plan_bbox(page)
    margin = 85
    allowed = (
        bbox.x0 * sx - margin,
        bbox.y0 * sy - margin,
        bbox.x1 * sx + margin,
        bbox.y1 * sy + margin,
    )

    points = []
    boxes = _component_boxes(image)
    boxes.extend(_component_boxes(image, max_value=245, max_spread=130))
    seen_boxes = set()
    for minx, miny, maxx, maxy in boxes:
        key = (minx // 3, miny // 3, maxx // 3, maxy // 3)
        if key in seen_boxes:
            continue
        seen_boxes.add(key)
        cx = (minx + maxx) / 2
        cy = (miny + maxy) / 2
        if not (allowed[0] <= cx <= allowed[2] and allowed[1] <= cy <= allowed[3]):
            continue
        pdf_point = (cx / sx, cy / sy)
        points.append(_project_to_overlay(pdf_point, segments))

    return _dedupe(points, radius=5)


def _raster_label_text(page, segments):
    """Find dense AW/AS-like label text that is too fragmented to form an oval."""
    if not segments:
        return []

    pix = page.get_pixmap(matrix=fitz.Matrix(_DPI / 72, _DPI / 72), colorspace=fitz.csRGB)
    image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    sx = pix.width / page.rect.width
    sy = pix.height / page.rect.height
    bbox = _plan_bbox(page)
    margin = 130
    allowed = (
        bbox.x0 * sx - margin,
        bbox.y0 * sy - margin,
        bbox.x1 * sx + margin,
        bbox.y1 * sy + margin,
    )

    points = []
    text_boxes = [
        (*box, None) for box in _component_boxes(
            image,
            max_value=250,
            max_spread=150,
            max_density=1.0,
            max_aspect=6.2,
            min_width=24,
            max_width=100,
        )
    ]
    text_boxes.extend(_cluster_label_fragments(image))

    seen_boxes = set()
    for minx, miny, maxx, maxy, count in text_boxes:
        key = (minx // 4, miny // 4, maxx // 4, maxy // 4)
        if key in seen_boxes:
            continue
        seen_boxes.add(key)
        bw = maxx - minx + 1
        bh = maxy - miny + 1
        if bh <= 0:
            continue
        cx = (minx + maxx) / 2
        cy = (miny + maxy) / 2
        if not (allowed[0] <= cx <= allowed[2] and allowed[1] <= cy <= allowed[3]):
            continue
        aspect = bw / bh
        # Dense 3-character label clusters such as AW1/AS2. The tight
        # projection-distance check below keeps room labels out.
        if not (24 <= bw <= 100 and 10 <= bh <= 20 and 1.45 <= aspect <= 6.2):
            continue

        # Recompute density from the component rectangle using the same mask.
        # This avoids carrying component internals through the public helper.
        crop = image.crop((minx, miny, maxx + 1, maxy + 1))
        mask = _dark_gray_mask(crop, max_value=250, max_spread=150)
        density = sum(mask) / (bw * bh)
        if density < 0.10:
            continue

        point = (cx / sx, cy / sy)
        projected, segment, distance = _nearest_projection(point, segments)
        if not segment or distance is None:
            continue
        x0, y0, x1, y1, _length = segment[:5]
        vertical = abs(y1 - y0) > abs(x1 - x0)
        lower_right_horizontal = not vertical and projected[1] > 330
        lower_plan_horizontal = not vertical and projected[1] > 280 and distance <= 30
        interior_label = 180 <= point[0] <= 340 and 190 <= point[1] <= 260 and distance <= 75
        compact_label = aspect <= 4.0
        wide_vertical_label = vertical and aspect <= 6.2
        near_right_edge = projected[0] >= bbox.x1 - 18
        right_side_label = point[0] > 450 and near_right_edge and (
            compact_label and (vertical or lower_right_horizontal) or wide_vertical_label
        )
        if right_side_label or lower_plan_horizontal or interior_label:
            near_lower_endpoint = vertical and abs(projected[1] - max(y0, y1)) <= 24
            if near_lower_endpoint and point[1] > 330:
                continue
            if interior_label and not (vertical or lower_plan_horizontal):
                points.append(point)
                continue
            points.append(projected)

    return _dedupe(points, radius=8)


def _interior_label_points(page, segments):
    """Keep large interior AW/AS labels that should not project to an edge."""
    if not segments:
        return []

    pix = page.get_pixmap(matrix=fitz.Matrix(_DPI / 72, _DPI / 72), colorspace=fitz.csRGB)
    image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    sx = pix.width / page.rect.width
    sy = pix.height / page.rect.height
    bbox = _plan_bbox(page)

    points = []
    boxes = _component_boxes(
        image,
        max_value=250,
        max_spread=150,
        max_density=1.0,
        max_aspect=6.2,
        min_width=35,
        max_width=70,
    )
    for minx, miny, maxx, maxy in boxes:
        bw = maxx - minx + 1
        bh = maxy - miny + 1
        cx = (minx + maxx) / 2
        cy = (miny + maxy) / 2
        point = (cx / sx, cy / sy)
        projected, segment, distance = _nearest_projection(point, segments)
        if not segment or distance is None:
            continue
        x0, y0, x1, y1, _length = segment[:5]
        horizontal = abs(x1 - x0) >= abs(y1 - y0)
        inside_plan = bbox.x0 + 90 <= point[0] <= bbox.x1 - 90 and bbox.y0 + 45 <= point[1] <= bbox.y1 - 45
        central_callout = 1.45 <= bw / bh <= 2.4 and 190 <= point[1] <= 255 and 180 <= point[0] <= 340
        separated_from_edge = horizontal and 42 <= distance <= 78 and point[1] > projected[1] + 35
        if inside_plan and central_callout and separated_from_edge:
            points.append(point)

    return _dedupe(points, radius=8)


def _lower_fragment_label_points(page, segments):
    """Recover fragmented AW labels sitting above lower horizontal facade lines."""
    if not segments:
        return []

    pix = page.get_pixmap(matrix=fitz.Matrix(_DPI / 72, _DPI / 72), colorspace=fitz.csRGB)
    image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    sx = pix.width / page.rect.width
    sy = pix.height / page.rect.height
    bbox = _plan_bbox(page)

    points = []
    for minx, miny, maxx, maxy, count in _cluster_label_fragments(image):
        bw = maxx - minx + 1
        bh = maxy - miny + 1
        if not (9 <= bw <= 28 and 8 <= bh <= 58 and count >= 80):
            continue
        cx = (minx + maxx) / 2
        cy = (miny + maxy) / 2
        point = (cx / sx, cy / sy)
        projected, segment, distance = _nearest_projection(point, segments)
        if not segment or distance is None:
            continue
        x0, y0, x1, y1, length = segment[:5]
        horizontal = abs(x1 - x0) >= abs(y1 - y0)
        above_lower_edge = horizontal and projected[1] > bbox.y1 - 40 and 14 <= distance <= 48
        near_lower_label_band = bbox.x0 + 40 <= point[0] <= bbox.x0 + 250 and point[1] < projected[1]
        if above_lower_edge and near_lower_label_band and length >= 45:
            points.append(projected)

    return _dedupe(points, radius=8)


def _dedupe(points, radius=6):
    kept = []
    r2 = radius * radius
    for point in points:
        if all(_dist2(point, old) > r2 for old in kept):
            kept.append(point)
    return kept


def _window_record(point, segments):
    x, y = _xy(point)
    if isinstance(point, dict) and point.get("direction"):
        direction = point["direction"]
    else:
        _projected, segment, _distance = _nearest_projection((x, y), segments)
        direction = segment[5] if segment and len(segment) > 5 else "Unassigned"
    return {"x": x, "y": y, "direction": direction}


def _dedupe_window_records(records):
    kept = []
    for record in records:
        aligned_duplicate = any(
            record["direction"] == old["direction"]
            and abs(record["x"] - old["x"]) <= 8
            and abs(record["y"] - old["y"]) <= 32
            for old in kept
        )
        if not aligned_duplicate:
            kept.append(record)
    return kept


def detect_windows(page):
    """Return a dict compatible with the existing overlay/count pipeline."""
    segments, colored_points, supplemental_colored_points = _colored_segments(page)
    callout_points = _raster_callouts(page, segments)
    text_points = _raster_label_text(page, segments)
    rescue_points = _interior_label_points(page, segments) + _lower_fragment_label_points(page, segments)

    if 5 <= len(colored_points) <= 7 and len(callout_points) >= len(colored_points):
        # Compact terrace sheets have reliable direction overlay groups, while
        # raster callout detection can pick up AD/door labels as false windows.
        points = _dedupe(colored_points + supplemental_colored_points, radius=14)
    elif 10 <= len(colored_points) <= 16 and len(callout_points) < len(colored_points):
        # Some sheets rasterize AW/AS labels unevenly: a few callout bubbles
        # split into text-only components and miss the strict raster pass. In
        # that case, the colored facade groups are the more complete locator.
        points = _dedupe(
            callout_points + text_points + rescue_points + colored_points + supplemental_colored_points,
            radius=14,
        )
    elif callout_points and len(callout_points) >= max(8, int(0.75 * len(colored_points))):
        points = _dedupe(callout_points + text_points + rescue_points, radius=14)
    elif 4 <= len(callout_points) >= len(colored_points) - 1 and len(colored_points) <= 6:
        points = _dedupe(callout_points + text_points + rescue_points, radius=14)
    else:
        points = colored_points or _dedupe(callout_points + text_points + rescue_points, radius=14)

    records = [
        _window_record(point, segments)
        for point in _dedupe(points, radius=6)
    ]
    records = [r for r in records if r["direction"] != "Not Included"]
    return {WINDOW_KEY: _dedupe_window_records(records)}
