#!/usr/bin/env bash
# One command to try the two-tier router locally:
#   ./start.sh
# First run: creates .env with random passwords and an encryption key, asks for
# your OpenRouter key, and picks free host ports. Then it starts the stack,
# waits for setup to finish and opens the chat page.
set -euo pipefail
cd "$(dirname "$0")"

command -v docker >/dev/null || { echo "Docker is required: https://docs.docker.com/get-docker/"; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker is installed but not running. Start Docker Desktop and try again."; exit 1; }

random() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1"; }

port_free() { ! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
free_port() { local p=$1; while ! port_free "$p"; do p=$((p + 1)); done; echo "$p"; }

if [ ! -f .env ]; then
  echo "Creating .env (passwords and the encryption key are generated for you)."
  key="${OPENROUTER_API_KEY:-}"
  if [ -z "$key" ]; then
    read -r -s -p "OpenRouter API key (https://openrouter.ai/settings/keys), or Enter to skip: " key; echo
  fi
  sed -e "s|<choose-a-password>|$(random 20)|" \
      -e "s|<choose-another-password>|$(random 20)|" \
      -e "s|<32+-random-characters>|$(random 48)|" \
      -e "s|<openrouter-api-key>|${key}|" \
      -e "s|^ES_PORT=.*|ES_PORT=$(free_port 9200)|" \
      -e "s|^KIBANA_PORT=.*|KIBANA_PORT=$(free_port 5601)|" \
      -e "s|^APP_PORT=.*|APP_PORT=$(free_port 8080)|" \
      -e "s|^LAYA_PORT=.*|LAYA_PORT=$(free_port 8000)|" \
      .env.example > .env
  [ -z "$key" ] && echo "No OpenRouter key: Laya still routes; LLM steps are skipped. Add it to .env later."
fi

get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }
APP_PORT=$(get APP_PORT); KIBANA_PORT=$(get KIBANA_PORT)

echo "Starting the stack. The first start downloads images and Laya's model (about 10 minutes)."
docker compose up -d --build

echo "Waiting for setup to finish..."
while true; do
  status=$(docker compose ps -a setup --format '{{.State}} {{.ExitCode}}' 2>/dev/null || true)
  case "$status" in
    "exited 0") break ;;
    exited*) echo "Setup failed. Details: docker compose logs setup"; exit 1 ;;
  esac
  sleep 5
done
docker compose logs setup --no-log-prefix | sed -n '/== Smoke test/,$p'

url="http://localhost:${APP_PORT}"
echo
echo "Chat page: ${url}"
echo "Kibana:    http://localhost:${KIBANA_PORT}  (user elastic, password: ELASTIC_PASSWORD in .env)"
echo "Benchmark: docker compose run --rm bench"
(command -v open >/dev/null && open "$url") || (command -v xdg-open >/dev/null && xdg-open "$url") || true
