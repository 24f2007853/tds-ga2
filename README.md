# TDS GA2 - runbook (24f2007853)

Everything runs from WSL inside `~/notes/GA2/tds-ga2`.

| Step | Command | Answers it gives |
|---|---|---|
| 1 | `bash publish.sh` then click **Deploy Blueprint** on the Render page it opens | Q13, Q14 (printed right away) |
| 2 | `python3 tests/check_live.py https://tds-ga2-24f2007853.onrender.com` (once Render says Live) | Q3, Q4, Q5, Q7, Q8, Q11, Q12 |
| 3 | `bash local/run_local.sh` (keep this WSL window open while grading) | Q6, Q9, Q10 |
| 4 | `python3 q2/test_prompts.py` (asks for your AI Pipe token, hidden) | Q2 |
| 5 | `q1/RECORD.md`, then `q15/RECORD.md` (termlog recordings), then `bash publish.sh` | Q1, Q15 |

Render fallback if the Blueprint page does not work: New > Web Service > Public Git
Repository > `https://github.com/24f2007853/tds-ga2` > Root Directory `cloud`, Build
`pip install -r requirements.txt`, Start `uvicorn main:app --host 0.0.0.0 --port $PORT`,
Instance type Free.

Render's free tier sleeps after 15 idle minutes. Before clicking Check on Q3-Q12,
open the service URL once (or re-run step 2) so it is awake.

Stop the local stack later with `bash local/stop_local.sh`.
