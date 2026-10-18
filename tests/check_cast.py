#!/usr/bin/env python3
"""Checks a termlog recording before you upload it, the way the graders describe.

    python3 tests/check_cast.py q1/httpbin.cast     # Q1: 7 HTTPBin captures
    python3 tests/check_cast.py q15/model.cast      # Q15: Modelfile export
Standard library only.
"""

import json
import os
import re
import sys

TAG = "http-34e4166a"
MODEL = "ga2-assistant-34e4166a"
SYSTEM = "You are the deployment assistant for project 34e4166a. Give concise answers and never invent commands."
PARAMS = {"temperature": "0.1", "num_ctx": "2048", "seed": "448", "num_predict": "77"}
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\x1b[()][0-9A-Za-z]")
FAILS = []


def check(cond, label):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


def load(path):
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    header = json.loads(lines[0])
    events = [json.loads(line) for line in lines[1:] if line.strip()]
    out = "".join(e[2] for e in events if len(e) > 2 and e[1] == "o")
    inp = "".join(e[2] for e in events if len(e) > 2 and e[1] == "i")
    text = ANSI.sub("", out).replace("\r\n", "\n").replace("\r", "")
    return header, events, text, inp


def common(path, header, events):
    check(os.path.exists(path + ".jwt"), f"receipt {os.path.basename(path)}.jwt exists next to the cast")
    check(header.get("version") == 2, "asciicast v2 header")
    check("proof" in header, "signed proof object in header")
    check(any(len(e) > 1 and e[1] == "x" for e in events), "exit event recorded (you typed exit)")


def q1(text, inp):
    blocks = {}
    for m in re.finditer(rf"HTTPBIN_BEGIN {TAG} (\w+)\n(.*?)\nHTTPBIN_END {TAG} \1(?=\n|$)", text, re.S):
        blocks[m.group(1)] = m.group(2).strip()  # last complete capture per label wins

    def body(label):
        try:
            return json.loads(blocks.get(label, ""))
        except Exception:
            return None

    for label in ("GET", "POST", "FORM", "AUTH", "REDIRECT", "COOKIES", "STATUS"):
        check(label in blocks, f"{label} capture present")
    j = body("GET") or {}
    check(j.get("args") == {"course": "TDS", "tag": TAG} and j.get("headers", {}).get("X-Exam-Tag") == TAG,
          "GET: query params + X-Exam-Tag header")
    j = body("POST") or {}
    check(j.get("json") == {"tag": TAG, "quantity": 5, "active": True}
          and "application/json" in j.get("headers", {}).get("Content-Type", ""), "POST: typed JSON body")
    j = body("FORM") or {}
    check(j.get("form") == {"tag": TAG, "quantity": "5"} and j.get("url", "").endswith("/put"), "FORM: PUT form fields")
    check(body("AUTH") == {"authenticated": True, "token": TAG}, "AUTH: bearer accepted")
    j = body("REDIRECT") or {}
    check(j.get("args") == {"tag": TAG} and j.get("url") == f"https://httpbin.org/get?tag={TAG}",
          "REDIRECT: followed to the final /get JSON")
    check(body("COOKIES") == {"cookies": {"exam": TAG}}, "COOKIES: jar cookie sent")
    check(blocks.get("STATUS") == "418", f"STATUS: only the code 418 (got {blocks.get('STATUS')!r})")
    check(" -i " not in inp and "--include" not in inp, "no -i / --include used")


def q15(text, inp, header):
    check(header.get("width", 0) >= 120, f"terminal was wide enough, {header.get('width')} columns (maximize the window before termlog rec)")
    check(re.search(rf"ollama create {MODEL} -f Modelfile", text) is not None, "ollama create command recorded")
    check("success" in text, "ollama create printed success")
    m = re.findall(rf"(?m)^MODELFILE_BEGIN {MODEL}\n(.*?)\nMODELFILE_END {MODEL}$", text, re.S)
    check(bool(m), "MODELFILE_BEGIN/END markers on their own lines")
    block = m[-1] if m else ""
    check(f"ollama show --modelfile {MODEL}" in text, "ollama show --modelfile command recorded")
    check(SYSTEM in block, "SYSTEM prompt exported exactly")
    for k, v in PARAMS.items():
        lines = re.findall(rf"(?m)^PARAMETER {k} (\S+)\s*$", block)
        check(lines == [v], f"PARAMETER {k} {v} appears exactly once (found {lines})")
    check(re.search(r"(?m)^FROM ", block) is not None, "FROM line exported")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = sys.argv[1]
    header, events, text, inp = load(path)
    print(f"{path}: {len(events)} events, {os.path.getsize(path)} bytes")
    common(path, header, events)
    if "HTTPBIN_BEGIN" in text:
        q1(text, inp)
    else:
        q15(text, inp, header)
    print("\nREADY TO PACK AND UPLOAD" if not FAILS else f"\n{len(FAILS)} problem(s): fix by re-running that step and recording again")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
