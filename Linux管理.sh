#!/usr/bin/env bash
cd -- "$(dirname "$0")" || exit
exec bash ./scripts/manage.sh "$@"
