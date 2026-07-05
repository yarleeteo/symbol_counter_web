"""
box_classifier.py
=================
Splits the small 'boxed fixture' symbols into Emergency Light (EL) vs Exit /
Keluar sign (K) by matching the letter inside each box against reference
templates (built from this drawing set). Local, no AI.

Falls back gracefully: if Pillow isn't installed, the caller keeps the boxes
lumped together as one 'box' category.
"""

import fitz

try:
    from PIL import Image
    _PIL = True
except Exception:
    _PIL = False

SZ = 24
_EL_HEX = "fffffffffffffff855555552fffffffffffffff833333320ffff9332222ffff833333300ffff7222222ffff833333100ffff6222222ffff833333100ffff5222222ffff833332111ffff5122222ffff833321111ffff6022222ffff833211111ffffeddd222ffff832111111ffffffffd23ffff832001111fffffffff23ffff822000000fffffffff23ffff822000000ffffffff823ffff822000000ffff8355222ffff822000000ffff5022222ffff822000000ffff5022222ffff822000000ffff5022222ffff822000000ffff5022222ffff822001100ffff5022222ffff822111110ffff5022222ffff933111111ffffa677777ffffc77666511fffffffffffffffffffffa50ffffffffffffffffffffff90ffffffffffffffffffffff90"
_K_HEX = "000000afff9009ffff600000000000afff900affff600000000000afff909ffff9000000000000afff40affff6000000000000afff69ffff90000000000000afffffffff40000000000000affffffff800000000000000affffffff400000000000000afffffff4000000000000000afffffff4000000000000000afffffff4000000000000000afffffff4000000000000000affffffff000000000000000affffffff400000000000000affffffff800000000000000afffffffff40000000000000afff84ffff40000000000000afff60afffe0000000000000afffa0affff6000000000000afffa02ffff8000000000000afffa00afffe300000000000afffa008ffff600000000000afffa000efff600000000000afff8000cfff900000"

def _decode(h):
    return [int(c, 16) / 15.0 for c in h]

_EL_REF = _decode(_EL_HEX)
_K_REF = _decode(_K_HEX)

def available():
    return _PIL

def _interior_vec(page, box):
    clip = fitz.Rect(box["x0"] + 0.7, box["y0"] + 0.7, box["x1"] - 0.7, box["y1"] - 0.7)
    pix = page.get_pixmap(matrix=fitz.Matrix(22, 22), clip=clip, colorspace=fitz.csGRAY)
    im = Image.frombytes("L", [pix.width, pix.height], pix.samples).resize((SZ, SZ))
    return [1.0 if p < 110 else 0.0 for p in im.getdata()]

def _cos(a, b):
    s = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return s / (na * nb) if na > 0 and nb > 0 else 0.0

def classify_box(page, box):
    """Return 'EL' or 'exit' for one box."""
    if not _PIL:
        return "box"
    v = _interior_vec(page, box)
    if sum(v) < 2:
        return "EL"
    return "exit" if _cos(v, _K_REF) > _cos(v, _EL_REF) + 0.02 else "EL"
