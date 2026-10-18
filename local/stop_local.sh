#!/usr/bin/env bash
# Stops the tunnels, the /extract service and the compose stack started by run_local.sh
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pkill -f 'cloudflared tunnel --no-autoupdate --url http://localhost:' && echo "tunnels stopped"
pkill -f 'uvicorn extract_app:app' && echo "extractor stopped"
DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="sudo docker"
(cd "$ROOT/compose" && $DOCKER compose down) && echo "compose stack stopped"
