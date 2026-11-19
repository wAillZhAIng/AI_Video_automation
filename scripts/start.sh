#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/docker-common.sh"
compose_file=compose.yaml
open_browser=false
for flag in "$@"; do
  case "$flag" in
    --dev) compose_file=compose.dev.yaml ;;
    --open) open_browser=true ;;
    *) echo 'Usage: bash scripts/start.sh [--dev] [--open]' >&2; exit 2 ;;
  esac
done
cd "$DOCKER_PROJECT_ROOT"
engine_os="$(docker info --format '{{.OSType}}')" || {
  echo 'Start Docker Desktop and wait for its engine to be ready, then retry.' >&2; exit 1;
}
[ "$engine_os" = linux ] || { echo 'Switch Docker Desktop to Linux containers.' >&2; exit 1; }
docker compose -f "$compose_file" up -d --wait --wait-timeout 180
docker compose -f "$compose_file" exec -T frontend wget -q -O /dev/null http://127.0.0.1:8080/api/health
address="$(docker compose -f "$compose_file" port frontend 8080)"
case "$address" in
  0.0.0.0:*) address="127.0.0.1:${address##*:}" ;;
  '[::]:'*) address="[::1]:${address##*:}" ;;
  '') echo 'The frontend did not publish a host port.' >&2; exit 1 ;;
esac
url="http://$address"
printf 'Open %s\n' "$url"
if "$open_browser"; then
  if [ "$(uname -s)" = Darwin ]; then
    open "$url" || echo "Could not open the browser. Visit $url"
  elif command -v xdg-open >/dev/null && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; }; then
    xdg-open "$url" >/dev/null 2>&1 || echo "Could not open the browser. Visit $url"
  fi
fi
