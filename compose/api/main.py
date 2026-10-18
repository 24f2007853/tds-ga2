"""Q6 - Redis-backed hit counter (runs inside docker compose)."""

import os

import redis.asyncio as redis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
r = redis.from_url(REDIS_URL, decode_responses=True)

app = FastAPI(title="Q6 hit counter")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def redis_key(key: str) -> str:
    return f"hits:{key}"


@app.post("/hit/{key}")
async def hit(key: str):
    count = await r.incr(redis_key(key))  # atomic INCR, survives api restarts
    return {"key": key, "count": int(count)}


@app.get("/count/{key}")
async def count(key: str):
    value = await r.get(redis_key(key))
    return {"key": key, "count": int(value) if value is not None else 0}


@app.get("/healthz")
async def healthz():
    try:
        ok = await r.ping()  # real round trip to Redis
    except Exception as exc:
        return JSONResponse({"status": "error", "redis": "down", "detail": str(exc)}, status_code=503)
    if ok:
        return {"status": "ok", "redis": "up"}
    return JSONResponse({"status": "error", "redis": "down"}, status_code=503)
