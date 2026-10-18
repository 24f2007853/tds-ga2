# Q1 - HTTPBin exercises inside one termlog recording

Maximize the WSL terminal window first, then:

```bash
cd ~/notes/GA2/tds-ga2/q1
termlog rec httpbin.cast
```

Inside the recording, paste these ONE LINE AT A TIME (Enter after each) and glance at
each output. Every capture should show JSON (STATUS shows just `418`).

```bash
capture() { task="$1"; shift; printf '\nHTTPBIN_BEGIN http-34e4166a %s\n' "$task"; "$@"; printf '\nHTTPBIN_END http-34e4166a %s\n' "$task"; }
capture GET curl -sS -H 'X-Exam-Tag: http-34e4166a' 'https://httpbin.org/get?course=TDS&tag=http-34e4166a'
capture POST curl -sS -X POST -H 'Content-Type: application/json' -d '{"tag":"http-34e4166a","quantity":5,"active":true}' https://httpbin.org/post
capture FORM curl -sS -X PUT --data-urlencode 'tag=http-34e4166a' --data-urlencode 'quantity=5' https://httpbin.org/put
capture AUTH curl -sS -H 'Authorization: Bearer http-34e4166a' https://httpbin.org/bearer
capture REDIRECT curl -sS -L 'https://httpbin.org/redirect-to?url=https%3A%2F%2Fhttpbin.org%2Fget%3Ftag%3Dhttp-34e4166a'
curl -sS -c cookies.txt -o /dev/null 'https://httpbin.org/cookies/set?exam=http-34e4166a'
capture COOKIES curl -sS -b cookies.txt https://httpbin.org/cookies
capture STATUS curl -sS -o /dev/null -w '%{http_code}' https://httpbin.org/status/418
exit
```

If HTTPBin hiccups (an HTML error page or a 502/503 instead of JSON), paste that same
line again before `exit`. The last complete capture per label is what gets graded.

After `exit`:

```bash
python3 ../tests/check_cast.py httpbin.cast
tar -czf submission.tar.gz httpbin.cast httpbin.cast.jwt
cd .. && bash publish.sh "Q1 recording"
```
