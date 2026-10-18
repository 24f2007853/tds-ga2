"""End-to-end checks for cloud/main.py, mirroring what each grader says it checks.

Run: python3 tests/test_cloud.py   (starts uvicorn itself on a free port)
"""

import base64
import json
import os
import random
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parent.parent
EMAIL = "24f2007853@ds.study.iitm.ac.in"
FAILS = []


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# a throwaway IdP key so we can mint tokens the way the grader does
priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pub_pem = priv.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
).decode()

port = free_port()
env = dict(os.environ, VERIFY_PUBLIC_KEY_PEM=pub_pem)
for k in ("APP_WORKERS", "APP_LOG_LEVEL", "APP_API_KEY", "APP_ENV"):
    env.pop(k, None)
proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
    cwd=ROOT / "cloud",
    env=env,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
BASE = f"http://127.0.0.1:{port}"
c = httpx.Client(base_url=BASE, timeout=10)
for _ in range(100):
    try:
        c.get("/healthz")
        break
    except Exception:
        time.sleep(0.1)

try:
    # ---------------- Q3 ----------------
    ALLOWED = "https://dash-66r4pd.example.com"
    r = c.options("/stats", headers={"Origin": ALLOWED, "Access-Control-Request-Method": "GET"})
    check(r.status_code == 200 and r.headers.get("access-control-allow-origin") == ALLOWED, "Q3 preflight allowed origin gets ACAO")
    evil = f"https://evil-{uuid.uuid4().hex[:6]}.example.com"
    r = c.options("/stats", headers={"Origin": evil, "Access-Control-Request-Method": "GET"})
    check("access-control-allow-origin" not in r.headers, "Q3 preflight evil origin gets NO ACAO")
    r = c.get("/stats", headers={"Origin": evil}, params={"values": "1,2"})
    check("access-control-allow-origin" not in r.headers, "Q3 GET evil origin gets NO ACAO")
    vals = [random.randint(-1000, 1000) for _ in range(random.randint(5, 30))]
    r = c.get("/stats", params={"values": ",".join(map(str, vals))}, headers={"Origin": ALLOWED})
    j = r.json()
    check(
        j["email"] == EMAIL and j["count"] == len(vals) and j["sum"] == sum(vals) and j["min"] == min(vals)
        and j["max"] == max(vals) and abs(j["mean"] - sum(vals) / len(vals)) < 0.01,
        "Q3 stats values correct",
    )
    check(r.headers.get("access-control-allow-origin") == ALLOWED, "Q3 GET allowed origin gets ACAO")
    rid = r.headers.get("x-request-id", "")
    pt = r.headers.get("x-process-time", "")
    check(len(rid) >= 8 and float(pt) >= 0, f"Q3 X-Request-ID ({rid}) and X-Process-Time ({pt}) present")
    r2 = c.get("/stats", params={"values": "5"})
    check(r2.headers.get("x-request-id") != rid, "Q3 request ids are unique")
    check(r2.headers.get("access-control-allow-origin") is None, "Q3 no ACAO without Origin (no wildcard)")

    # ---------------- Q4 ----------------
    now = int(time.time())
    base_claims = {"iss": "https://idp.exam.local", "aud": "tds-sem571do.apps.exam.local", "sub": "user-123",
                   "email": "someone@example.com", "iat": now, "exp": now + 600}
    good = jwt.encode(base_claims, priv, algorithm="RS256")
    r = c.post("/verify", json={"token": good})
    j = r.json()
    check(r.status_code == 200 and j == {"valid": True, "email": "someone@example.com", "sub": "user-123",
                                         "aud": "tds-sem571do.apps.exam.local"}, f"Q4 valid token -> 200 {j}")
    expired = jwt.encode({**base_claims, "exp": now - 60, "iat": now - 3600}, priv, algorithm="RS256")
    r = c.post("/verify", json={"token": expired})
    check(r.status_code == 401 and r.json() == {"valid": False}, "Q4 expired token rejected")
    wrong_aud = jwt.encode({**base_claims, "aud": "someone-else.apps.exam.local"}, priv, algorithm="RS256")
    r = c.post("/verify", json={"token": wrong_aud})
    check(r.status_code == 401, "Q4 wrong audience rejected")
    h, p, s = good.split(".")
    payload = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    payload["email"] = "attacker@example.com"
    p2 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    r = c.post("/verify", json={"token": f"{h}.{p2}.{s}"})
    check(r.status_code == 401, "Q4 tampered payload rejected")
    wrong_iss = jwt.encode({**base_claims, "iss": "https://evil.local"}, priv, algorithm="RS256")
    check(c.post("/verify", json={"token": wrong_iss}).status_code == 401, "Q4 wrong issuer rejected")
    hs = jwt.encode(base_claims, "secret", algorithm="HS256")
    check(c.post("/verify", json={"token": hs}).status_code == 401, "Q4 HS256 token rejected")
    check(c.post("/verify", content=b"garbage").status_code == 401, "Q4 garbage body -> 401 not 500")
    future_iat = jwt.encode({**base_claims, "iat": now + 30}, priv, algorithm="RS256")
    check(c.post("/verify", json={"token": future_iat}).status_code == 200, "Q4 small clock skew on iat tolerated")

    # ---------------- Q5 ----------------
    r = c.get("/effective-config")
    j = r.json()
    check(j == {"port": 8208, "workers": 1, "debug": True, "log_level": "debug", "api_key": "****"},
          f"Q5 base config {j}")
    r = c.get("/effective-config", params=[("set", "port=9000"), ("set", "debug=false"), ("set", "workers=7"),
                                           ("set", "log_level=WARNING"), ("set", "api_key=zzz")])
    j = r.json()
    check(j == {"port": 9000, "workers": 7, "debug": False, "log_level": "WARNING", "api_key": "****"},
          f"Q5 CLI overrides {j}")
    check(type(j["port"]) is int and type(j["workers"]) is int and type(j["debug"]) is bool, "Q5 types")
    r = c.get("/effective-config", params=[("set", "debug=Yes")])
    check(r.json()["debug"] is True, "Q5 debug=Yes -> true")
    r = c.get("/effective-config", params=[("set", "NUM_WORKERS=5")])
    check(r.json()["workers"] == 5, "Q5 NUM_WORKERS alias")
    check(c.get("/effective-config", headers={"Origin": "https://exam.example"}).headers.get(
        "access-control-allow-origin") == "*", "Q5 CORS open")

    # ---------------- Q7 ----------------
    key = "ak_jh8vwze3fd0zu5xm7nbeds50"
    check(c.post("/analytics", json={"events": []}).status_code == 401, "Q7 missing key -> 401")
    check(c.post("/analytics", json={"events": []}, headers={"X-API-Key": "wrong"}).status_code == 401,
          "Q7 wrong key -> 401")
    users = [f"u{i}" for i in range(random.randint(4, 7))]
    events = [{"user": random.choice(users), "amount": round(random.uniform(-50, 120), 2), "ts": 1700000000 + i}
              for i in range(random.randint(8, 17))]
    r = c.post("/analytics", json={"events": events}, headers={"X-API-Key": key, "Origin": "https://x.y"})
    j = r.json()
    pos = [e for e in events if e["amount"] > 0]
    tot = {}
    for e in pos:
        tot[e["user"]] = tot.get(e["user"], 0) + e["amount"]
    exp_top = max(tot.items(), key=lambda kv: kv[1])[0] if tot else None
    check(j["email"] == EMAIL and j["total_events"] == len(events) and j["unique_users"] == len({e["user"] for e in events})
          and abs(j["revenue"] - sum(e["amount"] for e in pos)) < 0.01 and j["top_user"] == exp_top,
          f"Q7 aggregation {j}")
    check(r.headers.get("access-control-allow-origin") == "*", "Q7 CORS *")
    r = c.options("/analytics", headers={"Origin": "https://exam.sanand.workers.dev",
                                         "Access-Control-Request-Method": "POST",
                                         "Access-Control-Request-Headers": "content-type,x-api-key"})
    check(r.status_code == 200 and "x-api-key" in r.headers.get("access-control-allow-headers", "").lower(),
          "Q7 preflight allows X-API-Key")

    # ---------------- Q8 ----------------
    def counter():
        txt = c.get("/metrics").text
        for line in txt.splitlines():
            if line.startswith("http_requests_total"):
                return float(line.split()[-1])
    before = counter()
    k_calls = random.randint(3, 6)
    for _ in range(k_calls):
        n = random.randint(1, 500)
        j = c.get("/work", params={"n": n}).json()
        assert j == {"email": EMAIL, "done": n}, j
    after = counter()
    check(after - before >= k_calls, f"Q8 counter increased {before} -> {after}")
    h = c.get("/healthz").json()
    check(h["status"] == "ok" and isinstance(h["uptime_s"], float) and h["uptime_s"] >= 0, f"Q8 healthz {h}")
    logs = c.get("/logs/tail", params={"limit": 10}).json()
    check(isinstance(logs, list) and len(logs) <= 10 and all(
        all(f in e for f in ("level", "ts", "path", "request_id")) for e in logs)
          and any("/work" in e["path"] for e in logs), "Q8 logs tail shape + /work present")
    check(c.get("/logs/tail", params={"limit": 0}).json() == [], "Q8 limit=0 -> []")

    # ---------------- Q11 ----------------
    ik = str(uuid.uuid4())
    r1 = c.post("/orders", json={"item": "x"}, headers={"Idempotency-Key": ik})
    r2 = c.post("/orders", json={"item": "x"}, headers={"Idempotency-Key": ik})
    check(r1.status_code == 201 and r1.json()["id"] == r2.json()["id"], "Q11 idempotent POST")
    r3 = c.post("/orders", json={"item": "x"}, headers={"Idempotency-Key": str(uuid.uuid4())})
    check(r3.json()["id"] != r1.json()["id"], "Q11 new key -> new order")
    for page in (1, 3, 7, 10, 56, 100):
        seen, cur, pages = [], None, 0
        while True:
            params = {"limit": page}
            if cur:
                params["cursor"] = cur
            j = c.get("/orders", params=params).json()
            assert len(j["items"]) <= page
            seen += [it["id"] for it in j["items"]]
            cur = j["next_cursor"]
            pages += 1
            if not cur:
                break
        check(seen == list(range(1, 57)), f"Q11 full scan limit={page} ({pages} pages)")
    cid = "client-" + uuid.uuid4().hex[:6]
    codes = [c.get("/orders", params={"limit": 1}, headers={"X-Client-Id": cid}) for _ in range(17)]
    check(all(x.status_code == 200 for x in codes[:16]) and codes[16].status_code == 429
          and codes[16].headers.get("retry-after", "").isdigit(), "Q11 17th request -> 429 + Retry-After")
    check(c.get("/orders", params={"limit": 1}, headers={"X-Client-Id": cid + "b"}).status_code == 200,
          "Q11 different client accepted")
    r = c.get("/orders", headers={"X-Client-Id": cid, "Origin": "https://exam.sanand.workers.dev"})
    check(r.status_code == 429 and r.headers.get("access-control-allow-origin") == "*"
          and "retry-after" in r.headers.get("access-control-expose-headers", "").lower(),
          "Q11 429 still carries CORS + exposes Retry-After")

    # ---------------- Q12 ----------------
    APP = "https://app-ysr4wl.example.com"
    r = c.options("/ping", headers={"Origin": APP, "Access-Control-Request-Method": "GET",
                                    "Access-Control-Request-Headers": "x-request-id,x-client-id"})
    check(r.status_code == 200 and r.headers.get("access-control-allow-origin") == APP, "Q12 preflight allowed")
    r = c.options("/ping", headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "GET"})
    check("access-control-allow-origin" not in r.headers, "Q12 preflight evil rejected")
    r = c.get("/ping", headers={"Origin": "https://evil.example.com"})
    check("access-control-allow-origin" not in r.headers, "Q12 GET evil no ACAO")
    r = c.get("/ping", headers={"Origin": "https://exam.sanand.workers.dev"})
    check(r.headers.get("access-control-allow-origin") == "https://exam.sanand.workers.dev", "Q12 exam origin allowed")
    my_id = "req-" + uuid.uuid4().hex
    r = c.get("/ping", headers={"X-Request-ID": my_id, "Origin": APP})
    j = r.json()
    check(j["request_id"] == my_id and r.headers["x-request-id"] == my_id and j["email"] == EMAIL,
          "Q12 supplied X-Request-ID echoed in body + header")
    check("x-request-id" in r.headers.get("access-control-expose-headers", "").lower(), "Q12 X-Request-ID exposed")
    r = c.get("/ping")
    check(r.json()["request_id"] == r.headers["x-request-id"] and len(r.json()["request_id"]) >= 32,
          "Q12 generated id in body + header")
    cid = "pinger-" + uuid.uuid4().hex[:6]
    codes = [c.get("/ping", headers={"X-Client-Id": cid}).status_code for _ in range(13)]
    check(codes[:8] == [200] * 8 and all(x == 429 for x in codes[8:]), f"Q12 13 requests -> {codes}")
    check(c.get("/ping", headers={"X-Client-Id": cid + "x"}).status_code == 200, "Q12 fresh client accepted")

    # ---------------- Q13 / Q14 backups ----------------
    check(c.get("/q13").json() == json.loads((ROOT / "answers/q13.json").read_text()), "Q13 backup matches file")
    check(c.get("/q14").json() == json.loads((ROOT / "answers/q14.json").read_text()), "Q14 backup matches file")
finally:
    proc.terminate()

print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
