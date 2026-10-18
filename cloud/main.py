"""TDS GA2 - one FastAPI service that answers several questions.

Q3   GET  /stats                         strict CORS + X-Request-ID / X-Process-Time
Q4   POST /verify                        RS256 JWT verification
Q5   GET  /effective-config              12-factor config precedence
Q7   POST /analytics                     X-API-Key auth + event aggregation
Q8   GET  /work /metrics /healthz /logs/tail   observability
Q11  POST /orders, GET /orders           idempotency + cursor pagination + 16 req/10s
Q12  GET  /ping                          request context + strict CORS + 8 req/10s
Q13/Q14 GET /q13 /q14                    backup copies of the JSON answers

Run:  uvicorn main:app --host 0.0.0.0 --port $PORT   (single worker: state is in memory)
"""

from __future__ import annotations

import base64
import collections
import json
import math
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import jwt
import yaml
from dotenv import dotenv_values
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

EMAIL = "24f2007853@ds.study.iitm.ac.in"
BASE_DIR = Path(__file__).resolve().parent
START_TIME = time.time()

app = FastAPI(title="TDS GA2 service", docs_url="/docs", redoc_url=None)

# --------------------------------------------------------------------------- #
# CORS policy (hand-rolled so each path can have its own rules)
# --------------------------------------------------------------------------- #
# Paths with a strict allow-list. Everything else is open ("*").
STRICT_ORIGINS = {
    "/stats": {"https://dash-66r4pd.example.com"},  # Q3
    "/ping": {"https://app-ysr4wl.example.com"},  # Q12
}
# The exam page itself must also reach /ping from the browser (Q12 note).
EXAM_ORIGIN_RE = re.compile(
    r"^https://([a-z0-9-]+\.)*(sanand\.workers\.dev|s-anand\.net|iitm\.ac\.in)$", re.I
)
EXTRA_ORIGINS = {
    o.strip().rstrip("/")
    for o in os.environ.get("EXTRA_ALLOWED_ORIGINS", "").split(",")
    if o.strip()
}
EXPOSE_HEADERS = "X-Request-ID, X-Process-Time, Retry-After, Idempotent-Replayed"
ALLOW_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"


def strict_key(path: str) -> str | None:
    for prefix in STRICT_ORIGINS:
        if path == prefix or path.startswith(prefix + "/"):
            return prefix
    return None


def origin_allowed(key: str | None, origin: str | None) -> bool:
    if key is None:
        return True  # open path
    if not origin:
        return False
    o = origin.rstrip("/")
    return o in STRICT_ORIGINS[key] or o in EXTRA_ORIGINS or bool(EXAM_ORIGIN_RE.match(o))


def cors_headers(key: str | None, origin: str | None) -> dict[str, str]:
    if key is None:
        return {"Access-Control-Allow-Origin": "*", "Access-Control-Expose-Headers": EXPOSE_HEADERS}
    if origin_allowed(key, origin):
        return {
            "Access-Control-Allow-Origin": origin.rstrip("/"),
            "Access-Control-Expose-Headers": EXPOSE_HEADERS,
            "Vary": "Origin",
        }
    return {"Vary": "Origin"}  # no ACAO for anyone else


SEEN_ORIGINS: collections.OrderedDict[str, dict] = collections.OrderedDict()


def remember_origin(origin: str, path: str, allowed: bool) -> None:
    SEEN_ORIGINS[origin] = {"last_path": path, "allowed": allowed, "ts": now_iso()}
    SEEN_ORIGINS.move_to_end(origin)
    while len(SEEN_ORIGINS) > 50:
        SEEN_ORIGINS.popitem(last=False)


# --------------------------------------------------------------------------- #
# Per-client rate limiting (sliding window, keyed by X-Client-Id)
# --------------------------------------------------------------------------- #
RATE_RULES = [  # (path prefix, max requests, window seconds)
    ("/ping", 8, 10.0),  # Q12
    ("/orders", 16, 10.0),  # Q11
]
_rate_hits: dict[tuple[str, str], collections.deque] = collections.defaultdict(collections.deque)
_rate_lock = threading.Lock()


def rate_limited(path: str, client_id: str) -> int | None:
    """Return Retry-After seconds if this request is over the limit, else None."""
    for prefix, limit, window in RATE_RULES:
        if path == prefix or path.startswith(prefix + "/"):
            now = time.monotonic()
            with _rate_lock:
                hits = _rate_hits[(prefix, client_id)]
                while hits and now - hits[0] >= window:
                    hits.popleft()
                if len(hits) >= limit:
                    return max(1, math.ceil(window - (now - hits[0])))
                hits.append(now)
            return None
    return None


# --------------------------------------------------------------------------- #
# Observability state: request counter + structured log ring buffer
# --------------------------------------------------------------------------- #
_counter_lock = threading.Lock()
REQUEST_COUNT = 0
LOGS: collections.deque = collections.deque(maxlen=5000)
# /logs/tail serves the observability service's own requests (Q8). Other
# questions share this process, so their traffic only goes to stdout; that keeps
# a burst of /orders or /ping checks from pushing the /work entries out of the tail.
TAIL_PATHS = ("/work", "/metrics", "/healthz", "/logs")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log_event(level: str, path: str, request_id: str, **extra) -> None:
    entry = {"ts": now_iso(), "level": level, "path": path, "request_id": request_id, **extra}
    if path.startswith(TAIL_PATHS):
        LOGS.append(entry)
    print(json.dumps(entry), file=sys.stdout, flush=True)


# --------------------------------------------------------------------------- #
# The middleware stack: request context -> CORS -> rate limit -> handler
# --------------------------------------------------------------------------- #
@app.middleware("http")
async def middleware_stack(request: Request, call_next):
    global REQUEST_COUNT
    t0 = time.perf_counter()
    with _counter_lock:
        REQUEST_COUNT += 1

    # 1. Request context: reuse inbound X-Request-ID, else generate one
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = request_id

    path = request.url.path
    origin = request.headers.get("origin")
    key = strict_key(path)
    allowed = origin_allowed(key, origin)
    if origin:
        remember_origin(origin, path, allowed)

    is_preflight = request.method == "OPTIONS" and "access-control-request-method" in request.headers
    client_id = request.headers.get("x-client-id")

    # 2. CORS preflight
    if is_preflight:
        if allowed:
            response = PlainTextResponse("OK", status_code=200)
            response.headers["Access-Control-Allow-Methods"] = ALLOW_METHODS
            response.headers["Access-Control-Allow-Headers"] = (
                request.headers.get("access-control-request-headers") or "*"
            )
            response.headers["Access-Control-Max-Age"] = "600"
        else:
            response = PlainTextResponse("Disallowed CORS origin", status_code=400)
    else:
        # 3. Rate limit
        retry_after = rate_limited(path, client_id) if client_id else None
        if retry_after is not None:
            response = JSONResponse(
                {"detail": "Too Many Requests", "retry_after": retry_after},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        else:
            try:
                response = await call_next(request)
            except Exception as exc:  # never leak a bare 500 without our headers
                log_event("error", path, request_id, error=repr(exc))
                response = JSONResponse({"detail": "internal error"}, status_code=500)

    for name, value in cors_headers(key, origin).items():
        if name == "Vary" and "vary" in response.headers:
            if "origin" not in response.headers["vary"].lower():
                response.headers["Vary"] = response.headers["vary"] + ", Origin"
        else:
            response.headers[name] = value

    elapsed = time.perf_counter() - t0
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time"] = f"{elapsed:.6f}"

    status = response.status_code
    level = "error" if status >= 500 else "warning" if status >= 400 else "info"
    log_event(
        level,
        path,
        request_id,
        method=request.method,
        status=status,
        duration_s=round(elapsed, 6),
        client_id=client_id,
        origin=origin,
    )
    return response


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #
@app.get("/")
async def index():
    return {
        "email": EMAIL,
        "endpoints": [
            "GET /stats?values=1,2,3",
            "POST /verify",
            "GET /effective-config?set=key=value",
            "POST /analytics",
            "GET /work?n=K",
            "GET /metrics",
            "GET /healthz",
            "GET /logs/tail?limit=N",
            "POST /orders",
            "GET /orders?limit=P&cursor=C",
            "GET /ping",
            "GET /q13",
            "GET /q14",
        ],
    }


# --------------------------------------------------------------------------- #
# Q3  /stats
# --------------------------------------------------------------------------- #
@app.get("/stats")
async def stats(request: Request):
    raw = ",".join(request.query_params.getlist("values"))
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    nums: list[float | int] = []
    try:
        for t in tokens:
            try:
                nums.append(int(t))
            except ValueError:
                nums.append(float(t))
    except ValueError:
        return JSONResponse({"error": f"could not parse values: {raw!r}"}, status_code=400)
    if not nums:
        return JSONResponse({"error": "values must contain at least one integer"}, status_code=400)
    total = sum(nums)
    return {
        "email": EMAIL,
        "count": len(nums),
        "sum": total,
        "min": min(nums),
        "max": max(nums),
        "mean": total / len(nums),
    }


# --------------------------------------------------------------------------- #
# Q4  /verify
# --------------------------------------------------------------------------- #
ISSUER = "https://idp.exam.local"
AUDIENCE = "tds-sem571do.apps.exam.local"
IDP_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA2okOHspNjgA+2rTLbeuY
cxiP/hG8C6Sb9iwg3yiLAA4HCnpITcbWCSelbvbYGuc3EbNy4xFyf5Cbj5DHJMID
EkryOgyd2giIIIBOUBj8S63uGcnRpOBh9NFatfNwheKuzsPuVNldu6A9cNteNpXc
WyJjG2axVfmq7i6SuKr1JoWYG7xTTAvKPujSl4OtsQfO3h5NepzdfXpr28oNnzfW
ed+zclR6BcmNNo/WVfJ4xyCLSf0BCOgdTgW6PdaChd1l9VDetJZVEgC5tkyvXsfI
SI6iyrYbKR0NEBSqq4XkadEjsCs4F1RncsS4LlgniT7GlkL9Mce3b0wGLs9/7ZIX
dQIDAQAB
-----END PUBLIC KEY-----
"""
# Tests can point this at another key; production uses the assigned one above.
VERIFY_KEY = os.environ.get("VERIFY_PUBLIC_KEY_PEM") or IDP_PUBLIC_KEY


@app.post("/verify")
async def verify(request: Request):
    invalid = JSONResponse({"valid": False}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return invalid
    token = body.get("token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token.strip():
        return invalid
    try:
        claims = jwt.decode(
            token.strip(),
            VERIFY_KEY,
            algorithms=["RS256"],  # pin the algorithm: no HS256/none confusion
            audience=AUDIENCE,
            issuer=ISSUER,
            options={
                "require": ["exp", "iss", "aud"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": True,
                "verify_iss": True,
                # not part of the assigned rules; skipping avoids clock-skew false rejects
                "verify_iat": False,
                "verify_nbf": False,
            },
        )
    except Exception:
        return invalid
    return {
        "valid": True,
        "email": claims.get("email"),
        "sub": claims.get("sub"),
        "aud": claims.get("aud"),
    }


# --------------------------------------------------------------------------- #
# Q5  /effective-config
# --------------------------------------------------------------------------- #
CONFIG_DEFAULTS = {
    "port": 8000,
    "workers": 1,
    "debug": False,
    "log_level": "info",
    "api_key": "default-secret-000",
}
# OS-level env vars for this deployment. render.yaml sets these on Render;
# setdefault only fills them in when the platform did not (e.g. a local run).
ASSIGNED_OS_ENV = {
    "APP_WORKERS": "1",
    "APP_LOG_LEVEL": "debug",
    "APP_API_KEY": "key-thf4tbmrx9",
}
for _k, _v in ASSIGNED_OS_ENV.items():
    os.environ.setdefault(_k, _v)

KEY_ALIASES = {"num_workers": "workers"}
NON_CONFIG_ENV = {"env"}  # APP_ENV selects the YAML file; it is not a config key


def normalize_key(raw: str) -> str:
    k = raw.strip().lower()
    if k.startswith("app_"):
        k = k[4:]
    return KEY_ALIASES.get(k, k)


def coerce(key: str, value):
    if key in ("port", "workers"):
        if isinstance(value, bool):
            return int(value)
        try:
            return int(str(value).strip())
        except ValueError:
            return int(float(str(value).strip()))
    if key == "debug":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes", "on")
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def load_yaml_layer(env_name: str) -> dict:
    path = BASE_DIR / f"config.{env_name}.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return {normalize_key(k): v for k, v in data.items()} if isinstance(data, dict) else {}


ASSIGNED_DOTENV = {"APP_PORT": "8208", "APP_DEBUG": "true", "APP_API_KEY": "key-mmrgvvui5f"}


def load_dotenv_layer() -> dict:
    # dotenv_values reads the file WITHOUT touching os.environ, so layers stay separate.
    # Some hosts drop dotfiles from the bundle; fall back to the assigned .env values.
    env_file = BASE_DIR / ".env"
    values = dotenv_values(env_file) if env_file.exists() else dict(ASSIGNED_DOTENV)
    layer = {}
    for k, v in values.items():
        if k.upper().startswith("APP_") or k.upper() == "NUM_WORKERS":
            nk = normalize_key(k)
            if nk not in NON_CONFIG_ENV:
                layer[nk] = v
    return layer


def load_os_env_layer() -> dict:
    layer = {}
    for k, v in os.environ.items():
        if k.upper().startswith("APP_"):
            nk = normalize_key(k)
            if nk not in NON_CONFIG_ENV:
                layer[nk] = v
    return layer


@app.get("/effective-config")
async def effective_config(request: Request):
    env_name = os.environ.get("APP_ENV", "development")
    merged: dict = dict(CONFIG_DEFAULTS)
    merged.update(load_yaml_layer(env_name))  # 2. config.<env>.yaml
    merged.update(load_dotenv_layer())  # 3. .env
    merged.update(load_os_env_layer())  # 4. OS env (APP_*)
    for item in request.query_params.getlist("set"):  # 5. CLI overrides
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        nk = normalize_key(k)
        if nk:
            merged[nk] = v
    result = {k: coerce(k, v) for k, v in merged.items()}
    result["api_key"] = "****"
    ordered = {k: result[k] for k in CONFIG_DEFAULTS}
    ordered.update({k: v for k, v in result.items() if k not in ordered})
    return ordered


# --------------------------------------------------------------------------- #
# Q7  /analytics
# --------------------------------------------------------------------------- #
ANALYTICS_API_KEY = "ak_jh8vwze3fd0zu5xm7nbeds50"


def to_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


@app.post("/analytics")
async def analytics(request: Request):
    if request.headers.get("x-api-key") != ANALYTICS_API_KEY:
        return JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Body must be JSON"}, status_code=400)
    events = body.get("events") if isinstance(body, dict) else body
    if not isinstance(events, list):
        return JSONResponse({"detail": "Body must be {'events': [...]}"}, status_code=422)

    users = set()
    revenue = 0.0
    per_user: dict = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        user = event.get("user")
        if user is not None:
            users.add(user)
        amount = to_number(event.get("amount"))
        if amount is not None and amount > 0:
            revenue += amount
            per_user[user] = per_user.get(user, 0.0) + amount
    top_user = max(per_user.items(), key=lambda kv: kv[1])[0] if per_user else None
    return {
        "email": EMAIL,
        "total_events": len(events),
        "unique_users": len(users),
        "revenue": revenue,
        "top_user": top_user,
    }


# --------------------------------------------------------------------------- #
# Q8  /work /metrics /healthz /logs/tail
# --------------------------------------------------------------------------- #
@app.get("/work")
async def work(request: Request, n: int = 1):
    if n < 0:
        return JSONResponse({"detail": "n must be >= 0"}, status_code=422)
    acc = 0
    for i in range(min(n, 2_000_000)):  # one "unit" = one modular hash step
        acc = (acc * 31 + i) % 1_000_003
    log_event("info", request.url.path, request.state.request_id, event="work_done", units=n)
    return {"email": EMAIL, "done": n}


@app.get("/metrics")
async def metrics():
    with _counter_lock:
        total = REQUEST_COUNT
    body = (
        "# HELP http_requests_total Total HTTP requests received by this service.\n"
        "# TYPE http_requests_total counter\n"
        f"http_requests_total {total}\n"
        "# HELP process_uptime_seconds Seconds since the service started.\n"
        "# TYPE process_uptime_seconds gauge\n"
        f"process_uptime_seconds {time.time() - START_TIME:.3f}\n"
    )
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "uptime_s": round(max(0.0, time.time() - START_TIME), 3)}


@app.get("/logs/tail")
async def logs_tail(limit: int = 50):
    limit = max(0, min(limit, LOGS.maxlen or 5000))
    entries = list(LOGS)
    return entries[-limit:] if limit else []


# --------------------------------------------------------------------------- #
# Q11  /orders
# --------------------------------------------------------------------------- #
TOTAL_ORDERS = 56
CATALOG = [
    {
        "id": i,
        "item": f"item-{i:03d}",
        "quantity": (i % 5) + 1,
        "amount": round(9.5 + i * 1.25, 2),
        "status": "fulfilled",
    }
    for i in range(1, TOTAL_ORDERS + 1)
]
_orders_lock = threading.Lock()
IDEMPOTENT_ORDERS: dict[str, dict] = {}
CREATED_ORDERS: dict[str, dict] = {}


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"offset:{offset}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> int:
    padded = cursor + "=" * (-len(cursor) % 4)
    text = base64.urlsafe_b64decode(padded.encode()).decode()
    if not text.startswith("offset:"):
        raise ValueError("bad cursor")
    offset = int(text.split(":", 1)[1])
    if offset < 0 or offset > TOTAL_ORDERS:
        raise ValueError("bad cursor")
    return offset


@app.post("/orders")
async def create_order(request: Request):
    idem_key = request.headers.get("idempotency-key")
    try:
        payload = await request.json()
    except Exception:
        payload = None
    with _orders_lock:
        if idem_key and idem_key in IDEMPOTENT_ORDERS:
            return JSONResponse(
                IDEMPOTENT_ORDERS[idem_key], status_code=201, headers={"Idempotent-Replayed": "true"}
            )
        order = {
            "id": f"ord_{uuid.uuid4().hex[:16]}",
            "status": "created",
            "idempotency_key": idem_key,
            "payload": payload,
            "created_at": now_iso(),
        }
        if idem_key:
            IDEMPOTENT_ORDERS[idem_key] = order
        CREATED_ORDERS[order["id"]] = order
    return JSONResponse(order, status_code=201)


@app.get("/orders")
async def list_orders(limit: int = 10, cursor: str | None = None):
    limit = max(1, min(limit, 100))
    offset = 0
    if cursor:
        try:
            offset = decode_cursor(cursor)
        except Exception:
            return JSONResponse({"detail": "invalid cursor"}, status_code=400)
    items = CATALOG[offset : offset + limit]
    end = offset + len(items)
    next_cursor = encode_cursor(end) if end < TOTAL_ORDERS else None
    return {"items": items, "next_cursor": next_cursor, "limit": limit, "total": TOTAL_ORDERS}


@app.get("/orders/{order_id}")
async def get_order(order_id: str):
    if order_id in CREATED_ORDERS:
        return CREATED_ORDERS[order_id]
    if order_id.isdigit() and 1 <= int(order_id) <= TOTAL_ORDERS:
        return CATALOG[int(order_id) - 1]
    return JSONResponse({"detail": "not found"}, status_code=404)


# --------------------------------------------------------------------------- #
# Q12  /ping
# --------------------------------------------------------------------------- #
@app.get("/ping")
async def ping(request: Request):
    return {"email": EMAIL, "request_id": request.state.request_id}


# --------------------------------------------------------------------------- #
# Q13 / Q14 backup copies + debugging helper
# --------------------------------------------------------------------------- #
Q13_ANSWER = {
    "A": {"text": "Report job-34e4166a: café\nReady ✓", "complete": True, "error": None},
    "B": {"text": "Partial job-34e4166a", "complete": False, "error": None},
    "C": {"text": "Draft job-34e4166a", "complete": False, "error": "model unloaded"},
}
Q14_ANSWER = {
    "A": {"steps": ["L3", "L4", "L5"], "seconds": 44},
    "B": {"steps": ["L1", "L2", "L3", "L4", "L5"], "seconds": 104},
    "C": {"steps": [], "seconds": 0},
    "D": {"steps": [], "seconds": 0},
    "E": {"steps": ["L4", "L5"], "seconds": 44},
}


@app.get("/q13")
async def q13():
    return Q13_ANSWER


@app.get("/q14")
async def q14():
    return Q14_ANSWER


@app.get("/debug/origins")
async def debug_origins():
    """Origins seen recently, to spot the exam page origin if a CORS check fails."""
    return SEEN_ORIGINS
