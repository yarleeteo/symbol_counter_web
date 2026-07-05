import csv
import io
import os
import tempfile
import time

import boto3
import fitz

from vector_engine import (
    CATEGORY_COLORS,
    CATEGORY_LABELS,
    count_fixtures,
    is_vector,
    load_first_page,
)


s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")


def _color255(key):
    return [int(round(c * 255)) for c in CATEGORY_COLORS[key]]


def _table():
    return dynamodb.Table(os.environ["JOB_TABLE"])


def _update(job_id, **values):
    names = {"#s": "status"}
    expr_values = {":u": int(time.time())}
    parts = ["updatedAt = :u"]
    for i, (key, value) in enumerate(values.items()):
        name_key = "#s" if key == "status" else f"#k{i}"
        value_key = f":v{i}"
        names[name_key] = key
        expr_values[value_key] = value
        parts.append(f"{name_key} = {value_key}")
    _table().update_item(
        Key={"jobId": job_id},
        UpdateExpression="SET " + ", ".join(parts),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=expr_values,
    )


def _draw_markers(page, results):
    for cat, items in results.items():
        color = CATEGORY_COLORS.get(cat, (0.3, 0.3, 0.3))
        for it in items:
            if isinstance(it, dict):
                page.draw_rect(
                    fitz.Rect(it["x0"] - 2, it["y0"] - 2, it["x1"] + 2, it["y1"] + 2),
                    color=color,
                    width=1,
                )
            else:
                page.draw_circle((it[0], it[1]), 5, color=color, width=1)


def _page_to_png_bytes(page, dpi=200):
    pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72))
    return pix.tobytes("png")


def _build_csv(counts):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Type", "Count"])
    total = 0
    for item in counts:
        writer.writerow([item["type"], item["count"]])
        total += item["count"]
    writer.writerow(["Total", total])
    return buf.getvalue().encode("utf-8")


def _put_bytes(bucket, key, body, content_type):
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)


def handler(event, _context):
    bucket = os.environ["FILE_BUCKET"]
    job_id = event["jobId"]
    input_key = event["inputKey"]
    filename = event.get("filename") or "layout.pdf"
    base_name = os.path.splitext(os.path.basename(filename))[0] or "layout"

    _update(job_id, status="processing")
    local_pdf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    local_pdf.close()

    try:
        s3.download_file(bucket, input_key, local_pdf.name)
        doc, page = load_first_page(local_pdf.name)
        if not is_vector(page):
            _update(
                job_id,
                status="failed",
                error=("This PDF looks scanned/raster. The vector engine can't read it yet."),
            )
            return {"jobId": job_id, "status": "failed"}

        results = count_fixtures(page)
        counts = [
            {
                "key": key,
                "type": CATEGORY_LABELS[key],
                "color": _color255(key),
                "count": len(items),
            }
            for key, items in results.items()
        ]

        _draw_markers(page, results)
        preview_key = f"results/{job_id}/{base_name}_preview.png"
        overlay_key = f"results/{job_id}/{base_name}_marked.pdf"
        csv_key = f"results/{job_id}/{base_name}_counts.csv"

        _put_bytes(bucket, preview_key, _page_to_png_bytes(page), "image/png")
        out_pdf = local_pdf.name + "-overlay.pdf"
        doc.save(out_pdf)
        with open(out_pdf, "rb") as f:
            _put_bytes(bucket, overlay_key, f.read(), "application/pdf")
        os.unlink(out_pdf)
        _put_bytes(bucket, csv_key, _build_csv(counts), "text/csv")

        _update(
            job_id,
            status="done",
            counts=counts,
            previewKey=preview_key,
            overlayPdfKey=overlay_key,
            countsCsvKey=csv_key,
        )
        return {"jobId": job_id, "status": "done"}
    except Exception as exc:
        _update(job_id, status="failed", error=str(exc))
        raise
    finally:
        try:
            os.unlink(local_pdf.name)
        except FileNotFoundError:
            pass
