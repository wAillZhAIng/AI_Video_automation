#!/usr/bin/env bash
cd -- "$(dirname "$0")" || exit
bash ./scripts/manage.sh "$@"
result=$?
if [ "$#" -eq 0 ] && [ -t 0 ]; then
  read -r -p '按回车关闭窗口 / Press Enter to close… ' unused
fi
exit "$result"
