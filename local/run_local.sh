#!/usr/bin/env bash
# TDS GA2 - local services, each behind a Cloudflare quick tunnel:
#   Q6  docker compose (FastAPI api + Redis)      -> port 8000
#   Q9  Ollama OpenAI-compatible chat endpoint    -> port 11434
#   Q10 /extract service (local LLM + validation) -> port 8001
# Installs what is missing, starts everything, self-tests the PUBLIC urls and
# prints the three answers. Run from WSL:   bash local/run_local.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="$ROOT/.run"
mkdir -p "$RUN" "$HOME/.local/bin"
export PATH="$HOME/.local/bin:$PATH"
MODEL="${MODEL:-qwen2.5:3b}"

info() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !! %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

wait_http() { # url seconds
  local i
  for ((i = 0; i < ${2:-60}; i++)); do
    curl -fsS -m 5 -o /dev/null "$1" 2>/dev/null && return 0
    sleep 1
  done
  return 1
}

# --------------------------------------------------------------------------- tools
info "Checking tools"
printf '  Docker and the Ollama CORS setting need sudo. Type your WSL password if asked (it is not shown).\n'
sudo -v || die "sudo is needed for Docker and the Ollama CORS setting"
command -v curl >/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq curl; }
if ! command -v cloudflared >/dev/null; then
  curl -fsSL -o "$HOME/.local/bin/cloudflared" \
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 ||
    die "could not download cloudflared"
  chmod +x "$HOME/.local/bin/cloudflared"
fi
ok "$(cloudflared --version 2>/dev/null | head -1)"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
ok "$(uv --version)"

# Docker Desktop with WSL integration switched off leaves only a placeholder
# 'docker' command here, so judge Docker by whether 'docker info' works.
pick_docker() {
  DOCKER=""
  if docker info >/dev/null 2>&1; then DOCKER="docker"
  elif sudo docker info >/dev/null 2>&1; then DOCKER="sudo docker"
  fi
}
start_dockerd() {
  { sudo systemctl enable --now docker || sudo service docker start; } >/dev/null 2>&1
  sleep 4
}
pick_docker
if [ -z "$DOCKER" ] && systemctl cat docker.service >/dev/null 2>&1; then
  start_dockerd
  pick_docker
fi
if [ -z "$DOCKER" ]; then
  warn "No working Docker in this WSL distro - installing Docker Engine here (about 2 minutes)"
  curl -fsSL https://get.docker.com -o "$RUN/get-docker.sh" || die "could not download the Docker installer"
  sudo sh "$RUN/get-docker.sh" || die "Docker install failed"
  sudo usermod -aG docker "$USER" || true
  start_dockerd
  pick_docker
  if [ -z "$DOCKER" ]; then
    warn "dockerd did not start; retrying with legacy iptables"
    sudo update-alternatives --set iptables /usr/sbin/iptables-legacy >/dev/null 2>&1
    sudo update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy >/dev/null 2>&1
    sudo systemctl restart docker >/dev/null 2>&1 || sudo service docker restart >/dev/null 2>&1
    sleep 4
    pick_docker
  fi
  [ -n "$DOCKER" ] || die "Docker installed but the daemon is not running: sudo systemctl status docker"
fi
$DOCKER compose version >/dev/null 2>&1 || die "'docker compose' missing: sudo apt-get install -y docker-compose-plugin"
ok "$($DOCKER compose version | head -1)"

# --------------------------------------------------------------------------- Ollama
info "Ollama ($MODEL)"
command -v ollama >/dev/null || { curl -fsSL https://ollama.com/install.sh | sh || die "Ollama install failed"; }

ollama_up() { curl -fsS -m 3 -o /dev/null http://127.0.0.1:11434/api/version 2>/dev/null; }
ollama_cors_ok() {
  curl -sS -m 5 -o /dev/null -D - -X OPTIONS http://127.0.0.1:11434/v1/chat/completions \
    -H 'Origin: https://cors-check.example.com' -H 'Access-Control-Request-Method: POST' \
    -H 'Access-Control-Request-Headers: content-type' 2>/dev/null |
    grep -qi '^access-control-allow-origin'
}
restart_ollama_with_cors() {
  if [ "$(ps -p 1 -o comm= 2>/dev/null)" = "systemd" ] && systemctl list-unit-files ollama.service >/dev/null 2>&1 &&
    systemctl cat ollama >/dev/null 2>&1; then
    sudo mkdir -p /etc/systemd/system/ollama.service.d
    printf '[Service]\nEnvironment="OLLAMA_ORIGINS=*"\n' | sudo tee /etc/systemd/system/ollama.service.d/cors.conf >/dev/null
    sudo systemctl daemon-reload && sudo systemctl restart ollama
  else
    pkill -f 'ollama serve' 2>/dev/null
    sleep 1
    OLLAMA_ORIGINS='*' nohup ollama serve >"$RUN/ollama.log" 2>&1 &
  fi
}
if ! ollama_up || ! ollama_cors_ok; then
  restart_ollama_with_cors
  wait_http http://127.0.0.1:11434/api/version 60 || die "Ollama did not start (see $RUN/ollama.log)"
fi
ollama_cors_ok || die "Ollama is up but without OLLAMA_ORIGINS=*. If the Windows Ollama app is running, quit it from the tray and re-run."
ok "Ollama $(curl -fsS http://127.0.0.1:11434/api/version) with CORS for all origins"
if ! ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -qx "$MODEL"; then
  info "Downloading $MODEL (one-time)"
  ollama pull "$MODEL" || die "ollama pull failed"
fi
curl -fsS -m 180 http://127.0.0.1:11434/api/generate \
  -d "{\"model\":\"$MODEL\",\"prompt\":\"hi\",\"stream\":false,\"keep_alive\":\"60m\",\"options\":{\"num_predict\":1}}" \
  >/dev/null && ok "model loaded and warm"

# --------------------------------------------------------------------------- Q6
info "Q6: docker compose up (api + redis)"
(cd "$ROOT/compose" && $DOCKER compose up -d --build) || die "docker compose up failed (is port 8000 busy? try: sudo ss -ltnp | grep 8000)"
wait_http http://127.0.0.1:8000/healthz 120 || die "api not healthy: $DOCKER compose -f compose/docker-compose.yml logs api"
ok "$(curl -fsS http://127.0.0.1:8000/healthz)"

# --------------------------------------------------------------------------- Q10
info "Q10: /extract service on :8001"
pkill -f 'uvicorn extract_app:app' 2>/dev/null
sleep 1
(
  cd "$ROOT/local" &&
    OLLAMA_URL=http://127.0.0.1:11434 EXTRACT_MODEL="$MODEL" nohup uv run --quiet \
      --with fastapi --with 'uvicorn[standard]' --with httpx \
      uvicorn extract_app:app --host 127.0.0.1 --port 8001 >"$RUN/extract.log" 2>&1 &
)
wait_http http://127.0.0.1:8001/healthz 180 || die "extractor did not start (see $RUN/extract.log)"
ok "$(curl -fsS http://127.0.0.1:8001/healthz)"

# --------------------------------------------------------------------------- tunnels
info "Starting Cloudflare quick tunnels"
start_tunnel() { # name port [extra cloudflared args]
  local name="$1" port="$2"
  shift 2
  pkill -f "cloudflared tunnel --no-autoupdate --url http://localhost:$port( |$)" 2>/dev/null
  : >"$RUN/tunnel-$name.log"
  nohup cloudflared tunnel --no-autoupdate --url "http://localhost:$port" "$@" >"$RUN/tunnel-$name.log" 2>&1 &
}
tunnel_url() { # name
  local i url
  for ((i = 0; i < 90; i++)); do
    url=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$RUN/tunnel-$1.log" 2>/dev/null | head -1)
    [ -n "$url" ] && { echo "$url"; return 0; }
    sleep 1
  done
  return 1
}
start_tunnel q6 8000
start_tunnel q9 11434 --http-host-header localhost:11434
start_tunnel q10 8001
Q6_URL=$(tunnel_url q6) || die "no tunnel URL for Q6 (see $RUN/tunnel-q6.log)"
Q9_URL=$(tunnel_url q9) || die "no tunnel URL for Q9 (see $RUN/tunnel-q9.log)"
Q10_URL=$(tunnel_url q10) || die "no tunnel URL for Q10 (see $RUN/tunnel-q10.log)"
ok "Q6  $Q6_URL"
ok "Q9  $Q9_URL"
ok "Q10 $Q10_URL"

# --------------------------------------------------------------------------- self-tests via the public URLs
info "Self-testing the public URLs (new tunnels can take ~30s to resolve)"
wait_http "$Q6_URL/healthz" 120 || warn "Q6 tunnel not reachable yet"
FAIL=0
K1="selftest-$RANDOM-a"
K2="selftest-$RANDOM-b"
for _ in 1 2 3; do curl -fsS -m 20 -X POST "$Q6_URL/hit/$K1" >/dev/null || FAIL=1; done
for _ in 1 2; do curl -fsS -m 20 -X POST "$Q6_URL/hit/$K2" >/dev/null || FAIL=1; done
C1=$(curl -fsS -m 20 "$Q6_URL/count/$K1")
C2=$(curl -fsS -m 20 "$Q6_URL/count/$K2")
C0=$(curl -fsS -m 20 "$Q6_URL/count/never-seen-$RANDOM")
H6=$(curl -fsS -m 20 "$Q6_URL/healthz")
echo "  healthz: $H6"
echo "  counts : $C1 | $C2 | $C0"
if [[ "$C1" == *'"count":3'* && "$C2" == *'"count":2'* && "$C0" == *'"count":0'* && "$H6" == *'"redis":"up"'* ]]; then
  ok "Q6 passes"
else
  warn "Q6 self-test FAILED"
  FAIL=1
fi

wait_http "$Q9_URL/api/version" 120 || warn "Q9 tunnel not reachable yet"
R9=$(curl -sS -m 90 -D "$RUN/q9-headers.txt" "$Q9_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' -H 'Origin: https://exam.sanand.workers.dev' \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"What is 37 + 48? Also repeat this token exactly: TKa1b2c3\"}],\"stream\":false}")
echo "  reply  : $(echo "$R9" | head -c 300)"
if [[ "$R9" == *85* && "${R9,,}" == *tka1b2c3* ]] && grep -qi '^access-control-allow-origin' "$RUN/q9-headers.txt"; then
  ok "Q9 passes (answer + echo + CORS header)"
else
  warn "Q9 self-test FAILED"
  FAIL=1
fi

wait_http "$Q10_URL/healthz" 120 || warn "Q10 tunnel not reachable yet"
INVOICE='INVOICE INV-30417\nVendor: Acme-9f2c Industries Ltd.\nBill to: Northwind Traders Inc.\nInvoice date: 2026-03-02\nPayment due: 2026-04-01\nSubtotal: EUR 2,400.00\nVAT (20%): EUR 480.00\nTotal due: EUR 2,880.00'
R10=$(curl -sS -m 90 "$Q10_URL/extract" -H 'Content-Type: application/json' -d "{\"text\": \"$INVOICE\"}")
S10=$(curl -sS -m 30 -o /dev/null -w '%{http_code}' "$Q10_URL/extract" -H 'Content-Type: application/json' -d '{"text": ""}')
echo "  extract: $R10"
echo "  empty input -> HTTP $S10"
if [[ "$R10" == *'Acme-9f2c Industries Ltd'* && "$R10" == *'2880'* && "$R10" == *'"EUR"'* && "$R10" == *'2026-04-01'* && "$S10" != 500 ]]; then
  ok "Q10 passes"
else
  warn "Q10 self-test FAILED"
  FAIL=1
fi

# --------------------------------------------------------------------------- answers
ANS="$ROOT/local-answers.txt"
{
  echo "Q6  : $Q6_URL"
  echo "Q9  : {\"url\": \"$Q9_URL/v1/chat/completions\", \"model\": \"$MODEL\"}"
  echo "Q10 : $Q10_URL/extract"
} >"$ANS"
printf '\n\033[1;35m================ PASTE THESE ================\033[0m\n'
cat "$ANS"
printf '\033[1;35m=============================================\033[0m\n'
[ "$FAIL" = 0 ] && ok "all local self-tests passed" || warn "some self-tests failed - send me the output above"
echo "Saved to $ANS. Keep this WSL window open until you have submitted."
echo "Stop everything later with: bash local/stop_local.sh"
