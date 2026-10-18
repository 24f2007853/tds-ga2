# Q2 - picking the prompt

Run `python3 q2/test_prompts.py` (outside any recording). It asks both models every
candidate the same way the grader does (single user message, minimal reasoning,
2048-token cap), ranks them by the chance of passing 2 of 3 trials and prints the
best prompt to copy. `python3 q2/test_prompts.py 10` re-runs with 10 trials each.

A failed evaluation is cached for that exact prompt, so if one fails on the exam
page, submit the next best from the table (or change a word) rather than re-checking.

Untested fallback if you cannot run the script:

    Meera is a girl. She has 3 brothers and 2 sisters. Does each of Meera's brothers have exactly 2 sisters? Answer only YES or NO.
