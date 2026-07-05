"""
app.py — Phase 1 web backend.

Endpoints
  GET  /                       -> the single-page UI (static/index.html)
  POST /api/legend             -> crops each legend row's symbol + description
  POST /api/analyze            -> detects + counts on a layout PDF, returns counts + overlay PNG
  GET  /api/download/overlay.pdf -> the marked-up layout as a PDF
  GET  /api/download/counts.csv  -> the counts as a CSV (opens in Excel)

Run it:
  uvicorn app:app --reload --port 8000
  then open http://localhost:8000
"""

import base64
import io
import os
import tempfile
import time
import uuid

import fitz
from fastapi import FastAPI, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from vector_engine import (
    load_first_page, is_vector, count_fixtures,
    CATEGORY_LABELS, CATEGORY_COLORS,
)
from window_engine import detect_windows, WINDOW_KEY, WINDOW_LABEL, WINDOW_COLOR
from legend_reader import read_legend
import teach

app = FastAPI(title="Symbol Counter — Phase 1")

allowed_origins = [
    origin.strip()
    for origin in os.environ.get("ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Holds the most recent analysis so the Download buttons can fetch it.
_LAST = {}
_LAST_WINDOW = {}


def _color255(key):
    return [int(round(c * 255)) for c in CATEGORY_COLORS[key]]


def draw_markers(page, results):
    """Draw a coloured marker for every detected fixture onto the page."""
    for cat, items in results.items():
        color = CATEGORY_COLORS.get(cat, (0.3, 0.3, 0.3))
        for it in items:
            if isinstance(it, dict):     # a box (EL / exit / box) -> rectangle
                page.draw_rect(
                    fitz.Rect(it["x0"] - 2, it["y0"] - 2, it["x1"] + 2, it["y1"] + 2),
                    color=color, width=1)
            else:                        # a circle fixture -> (cx, cy)
                page.draw_circle((it[0], it[1]), 5, color=color, width=1)


def draw_window_markers(page, results):
    """Draw filled dots for every detected window location."""
    for point in results.get(WINDOW_KEY, []):
        page.draw_circle(point, 4.5, color=(1, 1, 1), fill=(1, 1, 1), width=0.6)
        page.draw_circle(point, 3.2, color=WINDOW_COLOR, fill=WINDOW_COLOR, width=0.6)


def page_to_png(page, dpi=200):
    pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72))
    return pix.tobytes("png")


def build_csv(counts):
    buf = io.StringIO()
    import csv as _csv
    w = _csv.writer(buf)
    w.writerow(["Type", "Count"])
    total = 0
    for c in counts:
        w.writerow([c["type"], c["count"]])
        total += c["count"]
    w.writerow(["Total", total])
    return buf.getvalue()


def build_window_csv(points):
    buf = io.StringIO()
    import csv as _csv
    w = _csv.writer(buf)
    w.writerow(["No", "X", "Y"])
    for i, (x, y) in enumerate(points, 1):
        w.writerow([i, round(x, 2), round(y, 2)])
    w.writerow(["Total", len(points), ""])
    return buf.getvalue()


def _aws_clients():
    import boto3
    from botocore.config import Config

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    s3_config = Config(
        signature_version="s3v4",
        s3={"addressing_style": "virtual"},
    )
    return (
        boto3.client("s3", region_name=region, config=s3_config),
        boto3.resource("dynamodb", region_name=region),
        boto3.client("lambda", region_name=region),
    )


def _aws_config():
    bucket = os.environ.get("FILE_BUCKET")
    table = os.environ.get("JOB_TABLE")
    processor = os.environ.get("PROCESSOR_FUNCTION")
    if not bucket or not table or not processor:
        return None
    return bucket, table, processor


def _job_ttl(days=7):
    return int(time.time()) + days * 24 * 60 * 60


@app.post("/api/jobs")
async def create_job(req: Request):
    """AWS deployment: create a job and return a presigned S3 upload URL."""
    cfg = _aws_config()
    if not cfg:
        return JSONResponse({"error": "AWS job mode is not configured."}, status_code=501)
    body = await req.json()
    filename = os.path.basename(body.get("filename") or "layout.pdf")
    content_type = body.get("contentType") or "application/pdf"
    size = int(body.get("size") or 0)
    if content_type != "application/pdf" and not filename.lower().endswith(".pdf"):
        return JSONResponse({"error": "Upload a PDF layout."}, status_code=400)
    if size > 50 * 1024 * 1024:
        return JSONResponse({"error": "PDF is too large. Limit is 50 MB."}, status_code=400)

    bucket, table_name, _processor = cfg
    s3, dynamodb, _lambda_client = _aws_clients()
    job_id = uuid.uuid4().hex
    input_key = f"uploads/{job_id}/{filename}"
    table = dynamodb.Table(table_name)
    table.put_item(Item={
        "jobId": job_id,
        "status": "waiting_upload",
        "filename": filename,
        "inputKey": input_key,
        "createdAt": int(time.time()),
        "expiresAt": _job_ttl(),
    })
    upload_url = s3.generate_presigned_url(
        ClientMethod="put_object",
        Params={"Bucket": bucket, "Key": input_key, "ContentType": "application/pdf"},
        ExpiresIn=900,
    )
    return {"jobId": job_id, "uploadUrl": upload_url, "inputKey": input_key}


@app.post("/api/jobs/{job_id}/start")
def start_job(job_id: str):
    """AWS deployment: queue processing after the browser uploads to S3."""
    cfg = _aws_config()
    if not cfg:
        return JSONResponse({"error": "AWS job mode is not configured."}, status_code=501)
    _bucket, table_name, processor = cfg
    _s3, dynamodb, lambda_client = _aws_clients()
    table = dynamodb.Table(table_name)
    got = table.get_item(Key={"jobId": job_id}).get("Item")
    if not got:
        return JSONResponse({"error": "Job not found."}, status_code=404)
    table.update_item(
        Key={"jobId": job_id},
        UpdateExpression="SET #s = :s, updatedAt = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "queued", ":u": int(time.time())},
    )
    import json
    lambda_client.invoke(
        FunctionName=processor,
        InvocationType="Event",
        Payload=json.dumps({"jobId": job_id, "inputKey": got["inputKey"],
                            "filename": got.get("filename", "layout.pdf")}).encode(),
    )
    return {"jobId": job_id, "status": "queued"}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    """AWS deployment: return job status and presigned result URLs when ready."""
    cfg = _aws_config()
    if not cfg:
        return JSONResponse({"error": "AWS job mode is not configured."}, status_code=501)
    bucket, table_name, _processor = cfg
    s3, dynamodb, _lambda_client = _aws_clients()
    item = dynamodb.Table(table_name).get_item(Key={"jobId": job_id}).get("Item")
    if not item:
        return JSONResponse({"error": "Job not found."}, status_code=404)
    out = {
        "jobId": job_id,
        "status": item.get("status"),
        "error": item.get("error"),
        "counts": item.get("counts", []),
    }
    if item.get("status") == "done":
        for field, name in [
            ("previewKey", "previewUrl"),
            ("overlayPdfKey", "overlayPdfUrl"),
            ("countsCsvKey", "countsCsvUrl"),
        ]:
            key = item.get(field)
            if key:
                out[name] = s3.generate_presigned_url(
                    ClientMethod="get_object",
                    Params={"Bucket": bucket, "Key": key},
                    ExpiresIn=3600,
                )
    return out


@app.post("/api/legend")
async def legend(file: UploadFile = File(...)):
    """REAL (local): crop each legend row's symbol + description from the upload."""
    data = await file.read()
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.write(data)
    tmp.close()
    try:
        rows = read_legend(tmp.name)
        if not rows:
            return JSONResponse(
                {"error": "Couldn't find a legend table (a 'SYMBOL' column with ruled "
                          "rows) on this page. Make sure you uploaded the legend sheet, "
                          "and that its headers are real text — not a scanned image."},
                status_code=400)
        return {
            "rows": rows,
            "note": "Read from your legend. Type a short name for each type you want "
                    "to count — the counter will learn to find them in the next step.",
        }
    except Exception as e:
        return JSONResponse({"error": f"Could not read this legend: {e}"}, status_code=400)
    finally:
        os.unlink(tmp.name)


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)):
    """REAL: run the vector engine on a layout PDF, return counts + overlay."""
    data = await file.read()
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.write(data)
    tmp.close()
    try:
        doc, page = load_first_page(tmp.name)
        if not is_vector(page):
            return JSONResponse(
                {"error": "This PDF looks scanned/raster. The vector engine "
                          "can't read it yet — see the raster engine roadmap."},
                status_code=400)

        _LAST["pdf"] = data            # keep the original for teach-by-example

        results = count_fixtures(page)
        counts = [
            {"key": k, "type": CATEGORY_LABELS[k], "color": _color255(k),
             "count": len(items)}
            for k, items in results.items()
        ]

        # Draw markers once, then make: PNG (display), marked-up PDF + CSV (downloads).
        draw_markers(page, results)
        overlay = "data:image/png;base64," + base64.b64encode(page_to_png(page)).decode()

        out_pdf = tmp.name + "-overlay.pdf"
        doc.save(out_pdf)
        with open(out_pdf, "rb") as f:
            _LAST["overlay_pdf"] = f.read()
        os.unlink(out_pdf)
        _LAST["csv"] = build_csv(counts)
        _LAST["name"] = os.path.splitext(file.filename or "layout")[0]

        return {"counts": counts, "overlay": overlay, "downloads": True}
    finally:
        os.unlink(tmp.name)


@app.post("/api/analyze-windows")
async def analyze_windows(file: UploadFile = File(...)):
    """Detect window locations on a layout PDF and return a dot overlay."""
    data = await file.read()
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.write(data)
    tmp.close()
    try:
        doc, page = load_first_page(tmp.name)
        results = detect_windows(page)
        points = results.get(WINDOW_KEY, [])
        counts = [{
            "key": WINDOW_KEY,
            "type": WINDOW_LABEL,
            "color": [int(round(c * 255)) for c in WINDOW_COLOR],
            "count": len(points),
        }]

        draw_window_markers(page, results)
        overlay = "data:image/png;base64," + base64.b64encode(page_to_png(page)).decode()

        out_pdf = tmp.name + "-windows.pdf"
        doc.save(out_pdf)
        with open(out_pdf, "rb") as f:
            _LAST_WINDOW["overlay_pdf"] = f.read()
        os.unlink(out_pdf)
        _LAST_WINDOW["csv"] = build_window_csv(points)
        _LAST_WINDOW["name"] = os.path.splitext(file.filename or "layout")[0]

        return {"counts": counts, "overlay": overlay, "downloads": True}
    except Exception as e:
        return JSONResponse({"error": f"Could not detect windows: {e}"}, status_code=400)
    finally:
        os.unlink(tmp.name)


@app.get("/api/download/overlay.pdf")
def download_overlay():
    if "overlay_pdf" not in _LAST:
        return JSONResponse({"error": "Run an analysis first."}, status_code=404)
    name = _LAST.get("name", "layout")
    return Response(
        _LAST["overlay_pdf"], media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}_marked.pdf"'})


@app.get("/api/download/counts.csv")
def download_csv():
    if "csv" not in _LAST:
        return JSONResponse({"error": "Run an analysis first."}, status_code=404)
    name = _LAST.get("name", "layout")
    return Response(
        _LAST["csv"], media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}_counts.csv"'})


@app.get("/api/download/windows-overlay.pdf")
def download_windows_overlay():
    if "overlay_pdf" not in _LAST_WINDOW:
        return JSONResponse({"error": "Run window detection first."}, status_code=404)
    name = _LAST_WINDOW.get("name", "layout")
    return Response(
        _LAST_WINDOW["overlay_pdf"], media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}_windows_marked.pdf"'})


@app.get("/api/download/windows.csv")
def download_windows_csv():
    if "csv" not in _LAST_WINDOW:
        return JSONResponse({"error": "Run window detection first."}, status_code=404)
    name = _LAST_WINDOW.get("name", "layout")
    return Response(
        _LAST_WINDOW["csv"], media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}_windows.csv"'})


@app.post("/api/teach")
async def teach_endpoint(req: Request):
    """Teach-by-example: given clicked example points (PDF coords) on the last
    analysed layout, find every matching symbol and return count + overlay."""
    if "pdf" not in _LAST:
        return JSONResponse({"error": "Analyse a layout first."}, status_code=400)
    body = await req.json()
    try:
        clicks = [(float(x), float(y)) for x, y in body.get("clicks", [])]
    except Exception:
        clicks = []
    radius = float(body.get("radius", 8))
    if not clicks:
        return JSONResponse({"error": "Click at least one example on the drawing."},
                            status_code=400)
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.write(_LAST["pdf"])
    tmp.close()
    try:
        doc, page = load_first_page(tmp.name)
        matches = teach.find_matches(page, clicks, radius=radius)
        for x, y in matches:                       # matches in green
            page.draw_circle((x, y), 6, color=(0.0, 0.70, 0.20), width=1.5)
        for x, y in clicks:                         # the examples you clicked in magenta
            page.draw_circle((x, y), 9, color=(1.0, 0.0, 1.0), width=1.8)
        overlay = "data:image/png;base64," + base64.b64encode(page_to_png(page)).decode()
        return {"count": len(matches), "overlay": overlay}
    finally:
        os.unlink(tmp.name)


# Serve the UI at "/" (declared last so /api/* routes win).
app.mount("/", StaticFiles(directory="static", html=True), name="static")
