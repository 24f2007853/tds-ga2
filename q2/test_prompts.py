#!/usr/bin/env python3
"""Q2 - find a prompt where gpt-5-nano says YES and gpt-5-mini says NO.

Calls both models through AI Pipe exactly the way the grader describes:
one user message, no system prompt, reasoning_effort=minimal,
max_completion_tokens=2048. Every candidate below has NO as the correct
answer and a tempting YES for a model that skims.

Usage (outside any termlog recording, the token must never be recorded):
    export AIPIPE_TOKEN=...            # or the script asks for it, hidden
    python3 q2/test_prompts.py         # screen all candidates, 4 trials each
    python3 q2/test_prompts.py 10      # more trials per prompt
Only uses the Python standard library.
"""

import concurrent.futures as cf
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

URL = "https://aipipe.org/openai/v1/chat/completions"
NANO = "gpt-5-nano-2025-08-07"
MINI = "gpt-5-mini-2025-08-07"
END = " Answer only YES or NO."

CANDIDATES = [
    "Meera is a girl. She has 3 brothers and 2 sisters. Does each of Meera's brothers have exactly 2 sisters?",
    "A bat and a ball cost $1.10 in total. The bat costs $1.00. Does the ball cost 5 cents?",
    "A kilogram of feathers and two kilograms of steel are placed on a balance. Do they weigh the same?",
    "A man and his son are in a car crash. The son is rushed to the ER. The surgeon on duty, who is the boy's father, says: \"I can't operate on this boy, he is my son.\" Is the surgeon the boy's mother?",
    "On a game show there are 3 doors: one hides a car, two hide goats. You pick door 1. The host, who does not know where the car is, opens door 3 at random and it happens to show a goat. Does switching to door 2 give you a 2/3 chance of winning the car?",
    "Is the following sentence true: \"This sentence contains exactly five words.\"",
    "Does the binary string 101101110 contain exactly five 1s?",
    "Is 9.11 greater than 9.9?",
    "Is 899 a prime number?",
    "If the day before yesterday was Thursday, is the day after tomorrow a Sunday?",
    "All squares are rectangles. Some rectangles are rhombuses. Does it logically follow from these two premises alone that some squares are rhombuses?",
    "Is it false that the sun does not rise in the west?",
    "In a race, you overtake the runner who is in second place. Are you now in first place?",
    "A farmer has 17 sheep. All but 9 of them run away. Are there 8 sheep left?",
    "Mary's mother has four children: April, May, June and one more. Is the fourth child named July?",
    "If it takes 5 machines 5 minutes to make 5 widgets, would it take 100 machines 100 minutes to make 100 widgets?",
    "A patch of lily pads doubles in size every day. It takes 48 days to cover the whole lake. Does it take 24 days to cover half the lake?",
    "Does the word \"cranberry\" contain exactly two letter r's?",
]


def verdict(text: str) -> str:
    """Grader rule: case/whitespace ignored, one final . or ! allowed."""
    t = (text or "").strip().lower()
    t = re.sub(r"[.!]$", "", t)
    return t.upper() if t in ("yes", "no") else "OTHER"


def ask(token: str, model: str, prompt: str) -> str:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "reasoning_effort": "minimal",
        "max_completion_tokens": 2048,
    }).encode()
    for attempt in range(4):
        # A browser-like User-Agent: Cloudflare in front of AI Pipe blocks Python's
        # default one with "error code: 1010".
        req = urllib.request.Request(URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.load(r)
            return data["choices"][0]["message"]["content"] or ""
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:200]
            if "1010" in msg:
                sys.exit(f"Cloudflare blocked this client ({e.code}, {msg}); tell Claude")
            if e.code in (401, 403):
                sys.exit(f"AI Pipe rejected the token ({e.code}): {msg}")
            if attempt == 3:
                return f"[HTTP {e.code}] {msg}"
        except Exception as e:  # network hiccup
            if attempt == 3:
                return f"[error] {e}"
        time.sleep(2 * (attempt + 1))
    return "[error]"


def pass_probability(p: float) -> float:
    """P(at least 2 of 3 trials succeed) if each trial succeeds with p."""
    return 3 * p * p * (1 - p) + p ** 3


def main() -> None:
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    token = os.environ.get("AIPIPE_TOKEN") or getpass.getpass("AI Pipe token (hidden): ").strip()
    prompts = [c + END for c in CANDIDATES]
    jobs = {}
    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        for i, p in enumerate(prompts):
            for t in range(trials):
                for model in (NANO, MINI):
                    jobs[pool.submit(ask, token, model, p)] = (i, t, model)
        replies: dict = {}
        done = 0
        for fut in cf.as_completed(jobs):
            replies[jobs[fut]] = fut.result()
            done += 1
            print(f"\r  {done}/{len(jobs)} calls", end="", flush=True)
    print()

    rows = []
    for i, p in enumerate(prompts):
        nano = [verdict(replies[(i, t, NANO)]) for t in range(trials)]
        mini = [verdict(replies[(i, t, MINI)]) for t in range(trials)]
        p_nano = nano.count("YES") / trials
        p_mini = mini.count("NO") / trials
        p_trial = p_nano * p_mini
        rows.append((pass_probability(p_trial), i, nano, mini))
    rows.sort(reverse=True)

    print(f"\n{'pass%':>6}  {'nano':<{trials * 4}} {'mini':<{trials * 4}} prompt")
    for prob, i, nano, mini in rows:
        n = " ".join(x[0] for x in nano)  # Y / N / O
        m = " ".join(x[0] for x in mini)
        print(f"{prob * 100:5.0f}%  {n:<{trials * 4}} {m:<{trials * 4}} #{i + 1}: {CANDIDATES[i][:70]}")
    odd = [(k, v) for k, v in replies.items() if verdict(v) == "OTHER"]
    if odd:
        k, v = odd[0]
        print(f"\n(note) {len(odd)} replies were not a bare YES/NO, e.g. {k[2]} on #{k[0] + 1}: {v[:120]!r}")

    best = rows[0]
    print("\n================ BEST PROMPT (copy exactly) ================")
    print(prompts[best[1]])
    print("=============================================================")
    print(f"Estimated chance of passing the 3-trial check: {best[0] * 100:.0f}%")
    print("Tip: re-run with more trials, e.g. `python3 q2/test_prompts.py 10`, to confirm the top one.")


if __name__ == "__main__":
    main()
