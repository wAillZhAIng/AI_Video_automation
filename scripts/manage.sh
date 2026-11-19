#!/usr/bin/env bash
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/docker-common.sh"
cd "$DOCKER_PROJECT_ROOT"
action="${1:-menu}"
option="${2:-}"
if [ "$action" = menu ]; then
  if [ "$(uname -s)" = Darwin ]; then
    action="$(osascript <<'APPLESCRIPT'
set actionName to button returned of (display dialog "管理此项目的 Docker 应用" with title "AI 视频自动化" buttons {"一键清理", "一键关闭", "一键启动"} default button "一键启动")
if actionName is "一键启动" then return "start"
if actionName is "一键关闭" then return "stop"
return "clean"
APPLESCRIPT
    )" || exit 0
  elif command -v zenity >/dev/null && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; }; then
    action="$(zenity --list --title='AI 视频自动化' --text='选择要执行的操作' \
      --hide-column=1 --print-column=1 --column='Action' --column='操作' \
      start '一键启动' stop '一键关闭' clean '一键清理')" || exit 0
  else
    printf '1. 一键启动 / Start\n2. 一键关闭 / Stop\n3. 一键清理 / Reset\n'
    read -r -p '> ' action || exit 0
    case "$action" in 1) action=start ;; 2) action=stop ;; 3) action=clean ;; *) exit 0 ;; esac
  fi
fi

compose() { docker compose -f compose.yaml "$@"; }

[ "$#" -le 2 ] || exit 2
case "$action:$option" in
  start:) exec bash ./scripts/start.sh --open ;;
  start:--no-browser) exec bash ./scripts/start.sh ;;
  stop:|clean:|clean:--yes) ;;
  *) echo 'Usage: bash scripts/manage.sh [start [--no-browser]|stop|clean [--yes]]' >&2; exit 2 ;;
esac
if ! command -v docker >/dev/null || ! engine="$(docker info --format '{{.OSType}}')" || [ "$engine" != linux ]; then
  echo 'Start Docker Desktop or the Docker Engine and verify access to its Linux containers.' >&2
  exit 1
fi
project="$(compose config | awk '/^name:[[:space:]]/ {print $2}')"
if ! [[ "$project" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
  echo 'Could not identify the current Compose project.' >&2
  exit 1
fi
if [ "$action" = stop ]; then
  compose stop --timeout 5
  containers="$(compose ps --quiet)"
  [ -z "$containers" ] || { echo "Some containers in $project are still running." >&2; exit 1; }
  echo "Stopped $project. Its data volumes are retained."
  exit 0
fi
if [ "$option" != '--yes' ]; then
  printf '永久清除 %s 的项目记录、服务档案与密钥、素材、成片和运行日志。\n' "$project"
  if [ "$(uname -s)" = Darwin ] && [ "$#" -eq 0 ]; then
    osascript - "$project" <<'APPLESCRIPT' >/dev/null || exit 0
on run argv
  display dialog ("永久清除 " & item 1 of argv & " 的项目记录、服务档案与密钥、素材、成片和运行日志？") with title "确认恢复全新状态" buttons {"取消", "永久清理"} default button "取消" cancel button "取消" with icon caution
end run
APPLESCRIPT
  elif [ "$#" -eq 0 ] && command -v zenity >/dev/null && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; }; then
    zenity --question --title='确认恢复全新状态' --text="永久清除 $project 的项目记录、服务档案与密钥、素材、成片和运行日志？" --default-cancel || exit 0
  else
    read -r -p 'Type the Compose project name to confirm: ' answer || exit 0
    [ "$answer" = "$project" ] || exit 0
  fi
fi
compose down --timeout 5 --volumes
remaining="$(docker volume ls --quiet --filter "label=com.docker.compose.project=$project")"
containers="$(compose ps --all --quiet)"
if [ -n "$remaining" ] || [ -n "$containers" ]; then
  echo "Reset incomplete for $project. Check for a container using its volumes and retry." >&2
  exit 1
fi
echo "Reset complete for $project. Start again to create empty volumes."
