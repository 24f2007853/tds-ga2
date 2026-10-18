"""Checks for the Q6 compose api (against a real redis-server) and the Q10 extractor.

The extractor is tested twice: with Ollama unreachable (pure validation/regex
path) and with a fake Ollama that returns imperfect answers, to prove the
cross-checks fix them.
"""

import json
import os
import random
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
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


def wait(url):
    for _ in range(150):
        try:
            httpx.get(url, timeout=2)
            return
        except Exception:
            time.sleep(0.1)


procs = []


def start(args, cwd, env=None):
    p = subprocess.Popen(args, cwd=cwd, env={**os.environ, **(env or {})}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(p)
    return p


try:
    # ---------------- Q6 ----------------
    rport, aport = free_port(), free_port()
    start(["redis-server", "--port", str(rport), "--save", "", "--appendonly", "no"], cwd="/tmp")
    time.sleep(0.5)
    start([sys.executable, "-m", "uvicorn", "main:app", "--port", str(aport)], cwd=ROOT / "compose/api",
          env={"REDIS_URL": f"redis://127.0.0.1:{rport}/0"})
    base = f"http://127.0.0.1:{aport}"
    wait(base + "/healthz")
    check(httpx.get(base + "/healthz").json() == {"status": "ok", "redis": "up"}, "Q6 healthz pings redis")
    k1, k2 = f"k{random.randint(1, 10**9)}", f"k{random.randint(1, 10**9)}"
    seq = [random.choice([k1, k2]) for _ in range(random.randint(5, 20))]
    last = {}
    for k in seq:
        last[k] = httpx.post(f"{base}/hit/{k}").json()
    check(all(httpx.get(f"{base}/count/{k}").json() == {"key": k, "count": seq.count(k)} for k in (k1, k2)),
          f"Q6 counts match ({seq.count(k1)}, {seq.count(k2)})")
    check(all(v["count"] == seq.count(k) for k, v in last.items()), "Q6 POST returns running count")
    check(httpx.get(f"{base}/count/unseen-{random.random()}").json()["count"] == 0, "Q6 unseen key -> 0")
    # restart api process: counts must survive (state lives in redis)
    procs[1].terminate()
    procs[1].wait()
    start([sys.executable, "-m", "uvicorn", "main:app", "--port", str(aport)], cwd=ROOT / "compose/api",
          env={"REDIS_URL": f"redis://127.0.0.1:{rport}/0"})
    wait(base + "/healthz")
    check(httpx.get(f"{base}/count/{k1}").json()["count"] == seq.count(k1), "Q6 counts survive api restart")
    r = httpx.options(f"{base}/hit/x", headers={"Origin": "https://exam.sanand.workers.dev",
                                                 "Access-Control-Request-Method": "POST"})
    check(r.headers.get("access-control-allow-origin") in ("*", "https://exam.sanand.workers.dev"), "Q6 CORS")

    # ---------------- Q10 ----------------
    cases = []
    for i in range(40):
        vendor = f"Acme-{random.randbytes(2).hex()} Industries Ltd."
        amount = round(random.uniform(50, 9050), 2)
        cur = random.choice(["USD", "EUR", "GBP"])
        sym = {"USD": "$", "EUR": "€", "GBP": "£"}[cur]
        due = f"2026-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"
        issued = f"2026-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"
        sub = round(amount / 1.2, 2)
        amt_fmt = f"{amount:,.2f}"
        templates = [
            f"INVOICE INV-{random.randint(10000, 99999)}\nVendor: {vendor}\nBill to: Northwind Traders Inc.\n"
            f"Invoice date: {issued}\nDue date: {due}\nSubtotal: {cur} {sub:,.2f}\nTax: {cur} {amount - sub:,.2f}\n"
            f"Total due: {cur} {amt_fmt}",
            f"Hello, please find our invoice. {vendor} requests payment of {cur} {amt_fmt} by {due}. "
            f"This invoice was issued on {issued}. Thank you for your business.",
            f"From: {vendor}\nTo: Contoso Corp.\nAmount due: {sym}{amt_fmt}\nPayment due: {due}\nRef 4471-AB",
            f"{vendor}\n12 Industrial Road, Leeds\nInvoice #{random.randint(100, 999)} dated {issued}\n"
            f"Total: {amt_fmt} {cur}\nPlease pay by {due}.",
            f"Invoice from {vendor} -- total amount due {amt_fmt} {cur}, payable no later than {due} (issued {issued}).",
        ]
        cases.append(({"vendor": vendor, "amount": amount, "currency": cur, "date": due}, templates[i % len(templates)]))

    def run_cases(base, label):
        ok_all = True
        for exp, text in cases:
            r = httpx.post(base + "/extract", json={"text": text}, timeout=30)
            got = r.json() if r.status_code == 200 else {}
            good = (r.status_code == 200 and exp["vendor"].lower() in got.get("vendor", "").lower()
                    and abs(got.get("amount", -1) - exp["amount"]) <= 0.01 and got.get("currency") == exp["currency"]
                    and exp["date"] in got.get("date", "") and set(got) == {"vendor", "amount", "currency", "date"})
            if not good:
                ok_all = False
                print("   MISS", r.status_code, got, "\n   TEXT:", text.replace("\n", " | "), "\n   EXP:", exp)
        check(ok_all, f"Q10 {label}: {len(cases)} randomized invoices")
        for bad in ({"text": ""}, {"text": "   "}, {"text": "asdf qwerty 123"}, {}, [], "nope", {"text": 42}):
            r = httpx.post(base + "/extract", json=bad, timeout=30)
            if r.status_code == 500:
                ok_all = False
        r = httpx.post(base + "/extract", content=b"{not json", headers={"Content-Type": "application/json"}, timeout=30)
        check(r.status_code != 500 and ok_all, f"Q10 {label}: garbage/empty input never 500 (last={r.status_code})")

    # (a) Ollama unreachable
    xport = free_port()
    start([sys.executable, "-m", "uvicorn", "extract_app:app", "--port", str(xport)], cwd=ROOT / "local",
          env={"OLLAMA_URL": f"http://127.0.0.1:{free_port()}"})
    wait(f"http://127.0.0.1:{xport}/healthz")
    run_cases(f"http://127.0.0.1:{xport}", "no-LLM path")

    # (b) fake Ollama that answers with imperfect fields (subtotal, issue date, no trailing period, lowercase currency)
    fake = ROOT / "tests" / "_fake_ollama.py"
    fake.write_text(
        "import json, re\n"
        "from fastapi import FastAPI, Request\n"
        "app = FastAPI()\n"
        "@app.get('/api/version')\n"
        "async def v():\n    return {'version': 'fake'}\n"
        "@app.post('/api/chat')\n"
        "async def chat(req: Request):\n"
        "    body = await req.json()\n"
        "    text = body['messages'][-1]['content']\n"
        "    v = re.search(r'Acme-[0-9a-f]{4} Industries Ltd', text)\n"
        "    nums = re.findall(r'\\d{1,3}(?:,\\d{3})*\\.\\d{2}', text)\n"
        "    dates = re.findall(r'2026-\\d{2}-\\d{2}', text)\n"
        "    out = {'vendor': v.group(0) if v else 'Unknown', 'amount': float(nums[0].replace(',', '')) if nums else 0,\n"
        "           'currency': 'usd', 'date': dates[0] if dates else '2026-01-01'}\n"
        "    return {'message': {'role': 'assistant', 'content': json.dumps(out)}}\n"
    )
    fport, x2port = free_port(), free_port()
    start([sys.executable, "-m", "uvicorn", "_fake_ollama:app", "--port", str(fport)], cwd=ROOT / "tests")
    wait(f"http://127.0.0.1:{fport}/api/version")
    start([sys.executable, "-m", "uvicorn", "extract_app:app", "--port", str(x2port)], cwd=ROOT / "local",
          env={"OLLAMA_URL": f"http://127.0.0.1:{fport}"})
    wait(f"http://127.0.0.1:{x2port}/healthz")
    run_cases(f"http://127.0.0.1:{x2port}", "imperfect-LLM path")
    fake.unlink()
finally:
    for p in procs:
        p.terminate()

print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
