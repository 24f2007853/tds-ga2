#!/usr/bin/env python3
"""Live check of the deployed cloud service + the raw JSON answers, then prints
exactly what to paste for Q3, Q4, Q5, Q7, Q8, Q11, Q12, Q13, Q14.

Usage (from the repo folder in WSL):
    python3 tests/check_live.py https://tds-ga2-24f2007853.onrender.com
Standard library only.
"""

import json
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

EMAIL = "24f2007853@ds.study.iitm.ac.in"
BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://tds-ga2-24f2007853.onrender.com").rstrip("/")
EXAM = "https://exam.sanand.workers.dev"
FAILS = []


def check(cond, label):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


def call(method, path, headers=None, body=None, timeout=60, base=None):
    url = path if path.startswith("http") else (base or BASE) + path
    data = None
    headers = dict(headers or {})
    headers.setdefault("User-Agent", "tds-ga2-live-check")
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def js(raw):
    try:
        return json.loads(raw)
    except Exception:
        return None


print(f"Waking {BASE} (a sleeping Render service takes up to a minute) ...")
for i in range(40):
    try:
        s, _, _ = call("GET", "/healthz", timeout=30)
        if s == 200:
            break
    except Exception:
        pass
    time.sleep(3)
else:
    sys.exit("Service did not answer /healthz. Check the Render dashboard logs.")

print("Q3 /stats")
ALLOWED = "https://dash-66r4pd.example.com"
s, h, _ = call("OPTIONS", "/stats", {"Origin": ALLOWED, "Access-Control-Request-Method": "GET"})
check(s < 300 and h.get("access-control-allow-origin") == ALLOWED, "preflight from allowed origin gets ACAO")
s, h, _ = call("OPTIONS", "/stats", {"Origin": f"https://evil-{uuid.uuid4().hex[:6]}.com",
                                     "Access-Control-Request-Method": "GET"})
check("access-control-allow-origin" not in h, "preflight from evil origin gets no ACAO")
vals = [random.randint(-500, 500) for _ in range(random.randint(5, 25))]
s, h, raw = call("GET", "/stats?values=" + ",".join(map(str, vals)), {"Origin": ALLOWED})
j = js(raw) or {}
check(s == 200 and j.get("email") == EMAIL and j.get("count") == len(vals) and j.get("sum") == sum(vals)
      and j.get("min") == min(vals) and j.get("max") == max(vals)
      and abs(j.get("mean", 1e9) - sum(vals) / len(vals)) < 0.01, f"stats correct {j}")
check(bool(h.get("x-request-id")) and float(h.get("x-process-time", "-1")) >= 0, "X-Request-ID + X-Process-Time")

print("Q4 /verify")
s, _, raw = call("POST", "/verify", body={"token": "a.b.c"})
check(s == 401 and js(raw) == {"valid": False}, "junk token -> 401 {valid:false}")
s, _, _ = call("POST", "/verify", body=b"not json")
check(s != 500 and s != 200, f"garbage body -> {s}")

print("Q5 /effective-config")
s, h, raw = call("GET", "/effective-config", {"Origin": EXAM})
check(js(raw) == {"port": 8208, "workers": 1, "debug": True, "log_level": "debug", "api_key": "****"},
      f"base config {js(raw)}")
check(h.get("access-control-allow-origin") in ("*", EXAM), "CORS open")
s, _, raw = call("GET", "/effective-config?set=port=9123&set=debug=false&set=NUM_WORKERS=4")
check(js(raw) == {"port": 9123, "workers": 4, "debug": False, "log_level": "debug", "api_key": "****"},
      f"overrides + alias {js(raw)}")

print("Q7 /analytics")
KEY = "ak_jh8vwze3fd0zu5xm7nbeds50"
check(call("POST", "/analytics", body={"events": []})[0] == 401, "missing key -> 401")
check(call("POST", "/analytics", {"X-API-Key": "nope"}, {"events": []})[0] == 401, "wrong key -> 401")
users = [f"user{i}" for i in range(5)]
events = [{"user": random.choice(users), "amount": round(random.uniform(-40, 150), 2), "ts": 1700000000 + i}
          for i in range(12)]
s, h, raw = call("POST", "/analytics", {"X-API-Key": KEY, "Origin": EXAM}, {"events": events})
j = js(raw) or {}
tot = {}
for e in events:
    if e["amount"] > 0:
        tot[e["user"]] = tot.get(e["user"], 0) + e["amount"]
check(s == 200 and j.get("email") == EMAIL and j.get("total_events") == 12
      and j.get("unique_users") == len({e["user"] for e in events})
      and abs(j.get("revenue", -1) - sum(tot.values())) < 0.01
      and j.get("top_user") == max(tot, key=tot.get), f"aggregation {j}")
check(h.get("access-control-allow-origin") in ("*", EXAM), "CORS open")


def counter():
    txt = call("GET", "/metrics")[2].decode()
    for line in txt.splitlines():
        if line.startswith("http_requests_total"):
            return float(line.split()[-1])
    return -1


print("Q8 observability")
before = counter()
k = random.randint(3, 6)
for _ in range(k):
    n = random.randint(1, 50)
    j = js(call("GET", f"/work?n={n}")[2])
    assert j == {"email": EMAIL, "done": n}, j
after = counter()
check(after - before >= k, f"http_requests_total {before} -> {after} after {k} /work calls")
j = js(call("GET", "/healthz")[2]) or {}
check(j.get("status") == "ok" and isinstance(j.get("uptime_s"), (int, float)) and j["uptime_s"] >= 0, f"healthz {j}")
logs = js(call("GET", "/logs/tail?limit=20")[2])
check(isinstance(logs, list) and logs and all(all(f in e for f in ("level", "ts", "path", "request_id")) for e in logs)
      and any("/work" in e["path"] for e in logs), "logs/tail entries + /work present")

print("Q11 /orders")
ik = str(uuid.uuid4())
s1, _, r1 = call("POST", "/orders", {"Idempotency-Key": ik}, {"item": "x"})
s2, _, r2 = call("POST", "/orders", {"Idempotency-Key": ik}, {"item": "x"})
check(s1 == 201 and js(r1)["id"] == js(r2)["id"], "same Idempotency-Key -> same id")
seen, cur = [], None
for _ in range(80):
    j = js(call("GET", "/orders?limit=9" + (f"&cursor={cur}" if cur else ""))[2])
    assert len(j["items"]) <= 9
    seen += [it["id"] for it in j["items"]]
    cur = j.get("next_cursor")
    if not cur:
        break
check(seen == list(range(1, 57)), f"full scan 1..56 ({len(seen)} ids)")
cid = "live-" + uuid.uuid4().hex[:8]
codes, last_h = [], {}
for _ in range(17):
    s, last_h, _ = call("GET", "/orders?limit=1", {"X-Client-Id": cid, "Origin": EXAM})
    codes.append(s)
check(codes[:16] == [200] * 16 and codes[16] == 429 and "retry-after" in last_h, f"17th -> 429 + Retry-After {codes}")
check(call("GET", "/orders?limit=1", {"X-Client-Id": cid + "-other"})[0] == 200, "different client accepted")

print("Q12 /ping")
APP = "https://app-ysr4wl.example.com"
s, h, _ = call("OPTIONS", "/ping", {"Origin": APP, "Access-Control-Request-Method": "GET",
                                    "Access-Control-Request-Headers": "x-request-id,x-client-id"})
check(s < 300 and h.get("access-control-allow-origin") == APP, "preflight from allowed origin")
s, h, _ = call("OPTIONS", "/ping", {"Origin": "https://evil.example.org", "Access-Control-Request-Method": "GET"})
check("access-control-allow-origin" not in h, "preflight from other origin rejected")
rid = "rid-" + uuid.uuid4().hex
s, h, raw = call("GET", "/ping", {"X-Request-ID": rid, "Origin": EXAM})
j = js(raw) or {}
check(j.get("request_id") == rid and h.get("x-request-id") == rid and j.get("email") == EMAIL
      and h.get("access-control-allow-origin") == EXAM, "supplied X-Request-ID echoed (body + header), exam origin allowed")
s, h, raw = call("GET", "/ping")
check((js(raw) or {}).get("request_id") == h.get("x-request-id") != None, "generated id in body + header")
cid = "live-" + uuid.uuid4().hex[:8]
codes = [call("GET", "/ping", {"X-Client-Id": cid})[0] for _ in range(13)]
check(codes[:8] == [200] * 8 and 429 in codes, f"13 requests -> {codes}")
check(call("GET", "/ping", {"X-Client-Id": cid + "-fresh"})[0] == 200, "fresh client accepted")

# ------------------------------------------------------------ Q13 / Q14 raw files
raw_base = None
try:
    origin = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
    if "github.com" in origin:
        slug = origin.split("github.com")[-1].lstrip(":/").removesuffix(".git")
        raw_base = f"https://raw.githubusercontent.com/{slug}/main"
except Exception:
    pass
if raw_base:
    print("Q13 / Q14 raw JSON")
    for q in ("q13", "q14"):
        s, _, raw = call("GET", f"{raw_base}/answers/{q}.json", base="")
        local = json.load(open(f"answers/{q}.json"))
        check(s == 200 and js(raw) == local, f"{q}.json served publicly")

print()
if FAILS:
    print(f"{len(FAILS)} check(s) FAILED - paste this output to Claude:")
    for f in FAILS:
        print("   -", f)
else:
    print("ALL LIVE CHECKS PASSED")
print("\n================ PASTE THESE ================")
print(f"Q3  {BASE}")
print(f"Q4  {BASE}/verify")
print(f"Q5  {BASE}/effective-config")
print(f"Q7  {BASE}/analytics")
print(f"Q8  {BASE}")
print(f"Q11 {BASE}")
print(f"Q12 {BASE}")
if raw_base:
    print(f"Q13 {raw_base}/answers/q13.json")
    print(f"Q14 {raw_base}/answers/q14.json")
print("=============================================")
