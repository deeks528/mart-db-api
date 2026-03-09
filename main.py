import io
import random
import csv
from dotenv import load_dotenv
load_dotenv()   # load .env into os.environ before anything else reads it
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageDraw, ImageFont

# ── EAN-13 encoder ────────────────────────────────────────────────────────────
_EAN = {
    "L": {"0":"0001101","1":"0011001","2":"0010011","3":"0111101","4":"0100011",
          "5":"0110001","6":"0101111","7":"0111011","8":"0110111","9":"0001011"},
    "G": {"0":"0100111","1":"0110011","2":"0011011","3":"0100001","4":"0011101",
          "5":"0111001","6":"0000101","7":"0010001","8":"0001001","9":"0010111"},
    "R": {"0":"1110010","1":"1100110","2":"1101100","3":"1000010","4":"1011100",
          "5":"1001110","6":"1010000","7":"1000100","8":"1001000","9":"1110100"},
}
_PARITY = ["LLLLLL","LLGLGG","LLGGLG","LLGGGL","LGLLGG","LGGLLG",
           "LGGGLL","LGLGLG","LGLGGL","LGGLGL"]


def _ean13_bits(code: str) -> str:
    code = code.strip().zfill(13)[:13]
    first, rest = code[0], code[1:]
    pattern = _PARITY[int(first)]
    bits = "101"
    for i, d in enumerate(rest[:6]):
        bits += _EAN[pattern[i]][d]
    bits += "01010"
    for d in rest[6:]:
        bits += _EAN["R"][d]
    bits += "101"
    return bits


def _ean13_check_digit(digits_12: str) -> int:
    s1 = sum(int(d) for d in digits_12[0::2])
    s3 = sum(int(d) for d in digits_12[1::2])
    return (10 - ((s1 + 3 * s3) % 10)) % 10


def _fix_barcode(barcode: str) -> str:
    barcode = barcode.strip().zfill(13)[:13]
    correct = _ean13_check_digit(barcode[:12])
    if int(barcode[12]) != correct:
        barcode = barcode[:12] + str(correct)
    return barcode


# ── Barcode renderer ──────────────────────────────────────────────────────────
def make_barcode_png(barcode: str, module: int = 4) -> bytes:
    barcode    = _fix_barcode(barcode)
    bits       = _ean13_bits(barcode)
    quiet_zone = 11 * module
    pad_top    = 20
    pad_bot    = 30
    bar_h      = max(80, module * 30)
    font_size  = max(12, module * 3)

    total_w = quiet_zone + len(bits) * module + quiet_zone
    total_h = pad_top + bar_h + pad_bot

    img  = Image.new("RGB", (total_w, total_h), "white")
    draw = ImageDraw.Draw(img)
    x = quiet_zone
    for bit in bits:
        if bit == "1":
            draw.rectangle([x, pad_top, x + module - 1, pad_top + bar_h - 1], fill="black")
        x += module

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), barcode, font=font)
    draw.text(((total_w - (bbox[2] - bbox[0])) // 2, pad_top + bar_h + 8), barcode, fill="black", font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── Variation definitions ─────────────────────────────────────────────────────
VARIATIONS = [
    {"module": 2,  "label": "Extra Small",     "hint": "2px/module"},
    {"module": 3,  "label": "Small",           "hint": "3px/module"},
    {"module": 4,  "label": "Medium",          "hint": "4px/module · default"},
    {"module": 6,  "label": "Large",           "hint": "6px/module · best for cameras"},
    {"module": 8,  "label": "Extra Large",     "hint": "8px/module · handheld scanners"},
    {"module": 12, "label": "Print Quality",   "hint": "12px/module · labels & print"},
]


# ── Data — loaded once at startup ─────────────────────────────────────────────
CSV_PATH = Path("data.csv")
DB: list[dict] = []          # in-memory store, populated in lifespan
DB_INDEX: dict[str, dict] = {}   # barcode → row, for O(1) lookup


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── startup ──
    if not CSV_PATH.exists():
        raise RuntimeError(f"{CSV_PATH} not found — place data.csv next to main.py")
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    DB.extend(rows)
    first_col = list(rows[0].keys())[0]
    for row in rows:
        key = _fix_barcode(row[first_col].strip())
        DB_INDEX[key] = row
    print(f"✓ Loaded {len(DB)} products from {CSV_PATH}")
    yield
    # ── shutdown (nothing to clean up) ──


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Product Lookup API", version="1.0.0", lifespan=lifespan)

# ── Invoice routes (separated into invoice.py) ───────────────────────────────
import invoice
app.include_router(invoice.router)
templates = Jinja2Templates(directory="templates")


def find_by_barcode(barcode: str) -> Optional[dict]:
    return DB_INDEX.get(_fix_barcode(barcode))


def _template_ctx(record: dict) -> dict:
    """Build the common template variables from a product record."""
    keys        = list(record.keys())
    barcode_col = keys[0]
    barcode_val = _fix_barcode(record[barcode_col].strip())
    img_col     = next((c for c in keys if "image" in c.lower() or "img" in c.lower()), None)
    title_col   = next((c for c in keys if "title" in c.lower() or "name" in c.lower()), keys[1])
    skip        = {barcode_col, img_col} if img_col else {barcode_col}
    return {
        "barcode_val":   barcode_val,
        "title_val":     record.get(title_col, "Unknown product"),
        "img_url":       record.get(img_col, "") if img_col else "",
        "detail_fields": [(k, v) for k, v in record.items() if k not in skip and v and str(v).strip()],
        "variations":    VARIATIONS,
        "raw":           record,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/barcode-image", summary="Barcode PNG — add ?module=2–12 to change size")
def barcode_image(
    barcode: str = Query(...),
    module:  int = Query(4, ge=2, le=12),
):
    return StreamingResponse(io.BytesIO(make_barcode_png(barcode, module)), media_type="image/png")


@app.get("/barcode", response_class=JSONResponse, summary="Product JSON by barcode")
def get_by_barcode(barcode: str = Query(...)):
    record = find_by_barcode(barcode)
    if record is None:
        raise HTTPException(404, detail=f"Barcode '{barcode}' not found")
    return record


@app.get("/{branch}/barcode", summary="Product JSON by barcode + branch")
def get_by_branch_barcode_json(branch: str, barcode: str = Query(...)):
    record = find_by_barcode(barcode)
    if record is None:
        raise HTTPException(404, detail=f"Barcode '{barcode}' not found in branch '{branch}'")
    return record


@app.get("/b-{branch}/barcode", summary="Product HTML page by barcode + branch")
def get_by_branch_barcode_html(request: Request, branch: str, barcode: str = Query(...)):
    record = find_by_barcode(barcode)
    if record is None:
        raise HTTPException(404, detail=f"Barcode '{barcode}' not found in branch '{branch}'")
    return templates.TemplateResponse("barcode.html", {"request": request, "branch": branch, **_template_ctx(record)})


@app.get("/", summary="Random product + all barcode size variations")
def home(request: Request, refresh: Optional[str] = Query(None)):
    record = random.choice(DB)
    return templates.TemplateResponse("index.html", {"request": request, **_template_ctx(record)})