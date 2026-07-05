# Symbol Counter — Phase 1 web app (starter)

**Phase 1 goal:** a web page where you *upload a legend* (it picks up the light
types), then *upload a layout* — the app searches, maps, circles each type in its
own colour, counts them, and shows the quantity per type.

This starter already does the whole loop in a browser. The detection is real
(the vector engine you validated). The legend-reading step is a clearly-marked
stub so you can see everything work before building generic legend extraction.

```
symbol_counter_web/
├─ app.py                 # FastAPI backend (legend + analyze endpoints)
├─ vector_engine.py       # the proven detection engine
├─ static/index.html      # the single-page UI (upload → overlay + counts)
├─ requirements.txt
└─ README.md
```

## Run it

```bash
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
# open http://localhost:8000
```

Upload the legend (Step 1), then a **vector** layout PDF (Step 2). You'll get a
colour-coded overlay and a per-type count table (Step 3).

---

## How to build Phase 1, step by step

You're standing at the end of **Step 1**. Here's the whole sequence.

### Step 1 — Web skeleton ✅ (this starter)
Upload a layout → engine counts → browser shows overlay + counts.
*Why first:* it proves the end-to-end loop and gives you something to demo on day
one. Everything else slots into this shell.
- `app.py` `/api/analyze` runs `count_fixtures()` and returns an overlay PNG + counts.
- `static/index.html` is the UI.

### Step 2 — Read the legend for real
Replace the stub in `app.py` `/api/legend` with a vision-LLM call:
1. Rasterise the uploaded legend page (PyMuPDF).
2. Send it to a vision model and ask for **structured JSON** — one row per legend
   entry: `{ description, symbol_bounding_box }`.
3. Crop each symbol image from the box; return `[{ id, description, crop_png }]`.
4. Show those in the UI for the user to confirm / rename (the legend is small, so
   a quick human check makes this reliable).
*Output:* a "symbol library" for this project.

### Step 3 — Make detection legend-driven
Right now the engine recognises a **fixed** set of types by hard-coded geometry.
To honour "upload any legend and find *those* symbols", match the **library's
symbols** against the layout:
- **Vector layouts:** derive each symbol's geometric signature (as we did:
  circle + tube-line count, box stroke, etc.) and match by geometry. Most reliable.
- **Any layout:** render the layout, template-match each legend crop
  (multi-rotation + non-max-suppression). Generalises automatically.
Map each detection to its library entry, assign it a colour.

### Step 4 — Output & verify
- Distinct colour per type on the overlay (already wired by category).
- Counts table + downloadable overlay PDF and CSV.
- Let the user toggle a type on/off and correct mis-marks. **Save the
  corrections** — that's the labeled data later phases need.

---

## What's real vs stub right now

| Piece | Status |
|---|---|
| Web upload → overlay + counts loop | ✅ real |
| Detection on vector PDFs | ✅ real (vector engine) |
| Legend pickup (`/api/legend`) | 🔶 stub returns the engine's known types |
| Scanned/raster layouts | ⛔ not yet (raster engine is a later phase) |
| EL vs exit "K" split | ⛔ needs shape-matching (letters are vectorised) |

## Stack & why

- **FastAPI** backend — same language as the engine, trivial to wire.
- **Plain HTML/JS** UI — zero build step to start. Swap in React when the UI grows.
- **PyMuPDF** for parsing + overlay rendering.
- Add **OpenCV** when you build Step 3's template matcher, and a **vision-LLM API**
  for Step 2.

## Notes

- The engine's thresholds (in `vector_engine.py`) are calibrated to one drawing.
  Expect to expose them as per-project settings as you onboard more sheets.
- Keep the overlay front-and-centre in the UI — being able to *see* the marks is
  what makes the count trustworthy.
