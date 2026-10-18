"""Q10 - POST /extract: invoice fields from free text using a local LLM (Ollama).

The LLM proposes the four fields with Ollama structured outputs (JSON schema).
Every proposal is then checked against the text itself (the amount must be a
number that appears in the text, the date must be a date that appears in the
text, the vendor must be a substring), with keyword-scored regex extraction as
the fallback, and a Pydantic model guarantees the response schema.

Run:  uv run --with fastapi --with "uvicorn[standard]" --with httpx \
        uvicorn extract_app:app --host 127.0.0.1 --port 8001
"""

from __future__ import annotations

import json
import os
import re
from datetime import date

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("EXTRACT_MODEL", "qwen2.5:3b")
LLM_TIMEOUT = float(os.environ.get("EXTRACT_LLM_TIMEOUT", "40"))


class Invoice(BaseModel):
    vendor: str = Field(min_length=1)
    amount: float
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


app = FastAPI(title="Q10 invoice extractor")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --------------------------------------------------------------------------- #
# Regex side
# --------------------------------------------------------------------------- #
CODES = "USD|EUR|GBP|INR|JPY|CAD|AUD|CHF|CNY|SGD|AED|NZD|SEK|NOK|DKK|ZAR|HKD|MXN|BRL"
SYMBOLS = {"US$": "USD", "$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY"}
CUR_PAT = r"US\$|[$€£₹¥]|\b(?:" + CODES + r")(?![A-Za-z])"
NUM_PAT = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{1,3}(?:\.\d{3})+,\d{1,2}|\d+(?:[.,]\d{1,2})?"
MONEY_CUR_FIRST = re.compile(r"(?P<cur>" + CUR_PAT + r")\s?(?P<num>" + NUM_PAT + r")(?![\d])")
MONEY_NUM_FIRST = re.compile(r"(?<![\w.,])(?P<num>" + NUM_PAT + r")\s?(?P<cur>" + CUR_PAT + r")")
BARE_LABELLED = re.compile(
    r"(?i)(?:total|amount|balance|due|payable)[^\d\n]{0,30}?(?<![\w.,])(?P<num>" + NUM_PAT + r")(?![\d\-/])"
)
CODE_ANYWHERE = re.compile(r"\b(" + CODES + r")\b")
WORD_CURRENCY = {"dollars": "USD", "euros": "EUR", "pounds": "GBP", "sterling": "GBP", "rupees": "INR"}

MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"), ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}
MONTH_PAT = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DATE_PATTERNS = [
    ("ymd", re.compile(r"\b(?P<y>\d{4})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})\b")),
    ("mdy_name", re.compile(r"(?i)\b(?P<m>" + MONTH_PAT + r")\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y>\d{4})\b")),
    ("dmy_name", re.compile(r"(?i)\b(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m>" + MONTH_PAT + r")\.?,?\s+(?P<y>\d{4})\b")),
    ("dmy_num", re.compile(r"\b(?P<a>\d{1,2})[/.](?P<b>\d{1,2})[/.](?P<y>\d{4})\b")),
]

SUFFIX = (
    r"(?:Industries|Industry|Corporation|Corp|Incorporated|Inc|LLC|LLP|Ltd|Limited|GmbH|AG|PLC|Plc|Pvt|"
    r"Co|Company|Group|Holdings|Solutions|Systems|Technologies|Enterprises|Partners|Associates|"
    r"Services|Supplies|Trading|Labs|Manufacturing|International|Logistics|Ventures)"
)
CORP_RE = re.compile(
    r"\b(?P<v>[A-Z][\w&'\-]*(?:\s+(?:[A-Z0-9][\w&'\-]*|&|and|of))*?\s+" + SUFFIX + r"\b\.?(?:,?\s+" + SUFFIX + r"\b\.?)*)"
)
LABEL_RE = re.compile(
    r"(?im)^[ \t]*(?:vendor|supplier|seller|from|billed\s+by|bill\s+from|issued\s+by|invoice\s+from|"
    r"company|payee|pay\s+to|remit\s+to|merchant|provider)(?:\s+name)?\s*[:\-–—]\s*(?P<v>[^\n]+)"
)
CUSTOMER_CTX = re.compile(r"(?i)\b(bill(?:ed)?\s+to|ship(?:ped)?\s+to|customer|client|buyer|sold\s+to|attn|attention)\b")


def parse_number(s: str) -> float | None:
    s = s.strip().replace(" ", "")
    try:
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "," in s:
            head, tail = s.rsplit(",", 1)
            s = s.replace(",", "") if len(tail) == 3 else head.replace(",", "") + "." + tail
        return float(s)
    except ValueError:
        return None


def norm_currency(token: str | None) -> str | None:
    if not token:
        return None
    t = token.strip()
    if t in SYMBOLS:
        return SYMBOLS[t]
    t = t.upper()
    return t if re.fullmatch(r"[A-Z]{3}", t) else None


def context_before(text: str, start: int, floor: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    return text[max(line_start, floor, start - 60) : start].lower()


def amount_score(ctx: str) -> int:
    score = 0
    if re.search(r"sub-?\s?total", ctx):
        score -= 6
    elif re.search(r"grand\s+total|total\s+(?:amount\s+)?(?:due|payable)|amount\s+(?:due|payable)|balance\s+due|due\s+amount|total\s+to\s+pay", ctx):
        score += 8
    elif "total" in ctx:
        score += 5
    if re.search(r"\b(?:due|payable|pay|remit|owe|outstanding|balance)\b", ctx):
        score += 3
    if re.search(r"\b(?:tax|vat|gst|discount|shipping|freight|deposit|paid|credit|fee|previous|late|unit|rate|price)\b", ctx):
        score -= 4
    if "amount" in ctx:
        score += 1
    return score


def find_amounts(text: str) -> list[dict]:
    found: dict[int, dict] = {}
    for rx in (MONEY_CUR_FIRST, MONEY_NUM_FIRST):
        for m in rx.finditer(text):
            value = parse_number(m.group("num"))
            if value is None:
                continue
            found.setdefault(m.start(), {"value": value, "currency": norm_currency(m.group("cur")), "start": m.start(), "end": m.end()})
    for m in BARE_LABELLED.finditer(text):
        s = m.start("num")
        if any(c["start"] <= s < c["end"] for c in found.values()):
            continue
        value = parse_number(m.group("num"))
        if value is not None:
            found.setdefault(s, {"value": value, "currency": None, "start": s, "end": m.end("num")})
    cands = sorted(found.values(), key=lambda c: c["start"])
    prev_end = 0
    for c in cands:
        c["score"] = amount_score(context_before(text, c["start"], prev_end))
        prev_end = c["end"]
    if cands:
        biggest = max(c["value"] for c in cands)
        for c in cands:
            if c["value"] == biggest:
                c["score"] += 1
    return cands


def find_dates(text: str) -> list[dict]:
    out: dict[int, dict] = {}
    for kind, rx in DATE_PATTERNS:
        for m in rx.finditer(text):
            try:
                if kind == "ymd":
                    d = date(int(m["y"]), int(m["m"]), int(m["d"]))
                elif kind in ("mdy_name", "dmy_name"):
                    d = date(int(m["y"]), MONTHS[m["m"].lower().rstrip(".")], int(m["d"]))
                else:
                    a, b = int(m["a"]), int(m["b"])
                    day, month = (a, b) if a > 12 or b <= 12 else (b, a)
                    d = date(int(m["y"]), month, day)
            except (ValueError, KeyError):
                continue
            out.setdefault(m.start(), {"value": d.isoformat(), "start": m.start(), "end": m.end()})
    cands = sorted(out.values(), key=lambda c: c["start"])
    prev_end = 0
    for c in cands:
        ctx = context_before(text, c["start"], prev_end)
        score = 0
        if re.search(r"\bdue\b|payable|pay\s+by|payment|deadline|no\s+later\s+than|\bby\b", ctx):
            score += 8
        if re.search(r"invoice\s+date|issue|issued|dated|order\s+date|ship|delivery|service\s+date|period|created", ctx):
            score -= 5
        c["score"] = score
        prev_end = c["end"]
    return cands


def find_vendors(text: str) -> tuple[list[str], list[str], list[str]]:
    """strong = labelled line with a company suffix, corp = any company-looking
    name not in a customer context, weak = labelled line without a suffix."""
    strong, corp, weak = [], [], []
    for m in LABEL_RE.finditer(text):
        value = m.group("v").strip()
        hit = CORP_RE.search(value)
        if hit:
            strong.append(hit.group("v").strip())
        else:
            cut = re.split(r"\s{2,}|\t|\s\|\s|,\s|\s-\s|\(", value)[0].strip()
            if cut and "@" not in cut:
                weak.append(cut)
    for m in CORP_RE.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        prefix = text[max(line_start, m.start() - 40) : m.start()]
        if not CUSTOMER_CTX.search(prefix):
            corp.append(m.group("v").strip())
    return strong, corp, weak


def extend_vendor(text: str, vendor: str) -> str | None:
    """Return the vendor as it appears in the text (keeping a trailing '.')."""
    idx = text.lower().find(vendor.lower())
    if idx < 0:
        return None
    exact = text[idx : idx + len(vendor)]
    if not exact.endswith(".") and text[idx + len(vendor) : idx + len(vendor) + 1] == ".":
        exact += "."
    return exact.strip()


# --------------------------------------------------------------------------- #
# LLM side
# --------------------------------------------------------------------------- #
SCHEMA = {
    "type": "object",
    "properties": {
        "vendor": {"type": "string"},
        "amount": {"type": "number"},
        "currency": {"type": "string"},
        "date": {"type": "string"},
    },
    "required": ["vendor", "amount", "currency", "date"],
}
SYSTEM = (
    "You extract fields from invoices and reply with JSON only.\n"
    "vendor: the company that issued the invoice and must be paid (seller/supplier), copied exactly as written, "
    "including suffixes such as Ltd. or Inc. Never the customer.\n"
    "amount: the final total amount due (after tax and discounts) as a plain number, no symbols or separators.\n"
    "currency: ISO 4217 code in uppercase, e.g. USD, EUR, GBP. $ means USD, € means EUR, £ means GBP.\n"
    "date: the payment DUE date as YYYY-MM-DD (not the invoice/issue date)."
)


async def llm_extract(text: str) -> dict | None:
    payload = {
        "model": MODEL,
        "stream": False,
        "format": SCHEMA,
        "keep_alive": "60m",
        "options": {"temperature": 0, "num_predict": 200},
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
    }
    try:
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
            r = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            r.raise_for_status()
            data = json.loads(r.json()["message"]["content"])
            return data if isinstance(data, dict) else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Merge: LLM proposals, validated against the text, regex as fallback
# --------------------------------------------------------------------------- #
def merge(text: str, llm: dict | None) -> dict:
    llm = llm or {}
    out: dict = {}

    amounts = find_amounts(text)
    best_amt = max(amounts, key=lambda c: (c["score"], c["value"])) if amounts else None
    llm_amt = None
    try:
        llm_amt = float(llm.get("amount")) if llm.get("amount") is not None else None
    except (TypeError, ValueError):
        llm_amt = None
    llm_amt_match = next((c for c in amounts if llm_amt is not None and abs(c["value"] - llm_amt) < 0.011), None)
    if best_amt and best_amt["score"] >= 8:
        chosen_amt = best_amt
    elif llm_amt_match:
        chosen_amt = llm_amt_match
    else:
        chosen_amt = best_amt
    if chosen_amt:
        out["amount"] = round(chosen_amt["value"], 2)
    elif llm_amt is not None:
        out["amount"] = round(llm_amt, 2)

    currencies_in_text = {norm_currency(m.group(0)) for m in re.finditer(CUR_PAT, text)}
    for word, code in WORD_CURRENCY.items():
        if re.search(r"\b" + word + r"\b", text, re.I):
            currencies_in_text.add(code)
    currencies_in_text.discard(None)
    llm_cur = norm_currency(str(llm.get("currency") or ""))
    if chosen_amt and chosen_amt.get("currency"):
        out["currency"] = chosen_amt["currency"]
    elif llm_cur and llm_cur in currencies_in_text:
        out["currency"] = llm_cur
    elif currencies_in_text:
        counts = {c: len(re.findall(re.escape(c), text)) for c in currencies_in_text}
        out["currency"] = max(counts, key=counts.get)
    elif llm_cur:
        out["currency"] = llm_cur

    dates = find_dates(text)
    best_date = max(dates, key=lambda c: (c["score"], c["value"])) if dates else None
    llm_date = str(llm.get("date") or "")
    m = re.search(r"\d{4}-\d{2}-\d{2}", llm_date)
    llm_date = m.group(0) if m else None
    date_values = {c["value"] for c in dates}
    if best_date and best_date["score"] >= 8:
        out["date"] = best_date["value"]
    elif llm_date and llm_date in date_values:
        out["date"] = llm_date
    elif best_date:
        out["date"] = best_date["value"]
    elif llm_date:
        out["date"] = llm_date

    strong, corp, weak = find_vendors(text)
    raw_llm_vendor = str(llm.get("vendor") or "").strip()
    llm_vendor = extend_vendor(text, raw_llm_vendor) if len(raw_llm_vendor) >= 3 else None
    vendor = (strong or [None])[0] or llm_vendor or (corp or [None])[0] or (weak or [None])[0]
    if vendor:
        out["vendor"] = extend_vendor(text, vendor) or vendor
    elif llm.get("vendor"):
        out["vendor"] = str(llm["vendor"]).strip()
    return out


@app.post("/extract", response_model=Invoice)
async def extract(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": 'Body must be JSON like {"text": "..."}'}, status_code=422)
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str) or not text.strip():
        return JSONResponse({"detail": "text must be a non-empty string"}, status_code=422)
    text = text[:20000]
    try:
        llm = await llm_extract(text)
        fields = merge(text, llm)
        return Invoice(**fields)
    except ValidationError as exc:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return JSONResponse({"detail": "could not extract all invoice fields", "errors": errors}, status_code=422)
    except Exception as exc:  # never a 500
        return JSONResponse({"detail": f"could not process input: {exc}"}, status_code=422)


# --------------------------------------------------------------------------- #
# Q9: OpenAI-compatible chat endpoint = Ollama's /v1 behind this app's CORS.
# The browser's Origin header is not forwarded, so Ollama's own OLLAMA_ORIGINS
# setting does not matter; CORSMiddleware above answers preflights with "*".
# --------------------------------------------------------------------------- #
SKIP_HEADERS = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding", "te", "trailer",
    "upgrade", "origin", "referer", "accept-encoding", "proxy-authorization", "proxy-authenticate",
}


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def ollama_openai_proxy(path: str, request: Request):
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in SKIP_HEADERS and not k.lower().startswith(("sec-", "access-control-", "cf-", "x-forwarded"))
    }
    try:
        async with httpx.AsyncClient(timeout=float(os.environ.get("PROXY_TIMEOUT", "180"))) as client:
            r = await client.request(
                request.method, f"{OLLAMA_URL}/v1/{path}", params=request.query_params,
                content=await request.body(), headers=headers,
            )
    except Exception as exc:
        return JSONResponse({"error": {"message": f"local model unavailable: {exc}"}}, status_code=502)
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"))


@app.get("/healthz")
async def healthz():
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(f"{OLLAMA_URL}/api/version")
            ollama = r.json().get("version", "up")
    except Exception:
        ollama = "down"
    return {"status": "ok", "model": MODEL, "ollama": ollama}
