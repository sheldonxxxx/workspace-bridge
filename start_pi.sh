#!/usr/bin/env bash
# Pi adapter is managed by launchd (com.workspace-bridge.pi-host-adapter).
# Use kickstart/bootout instead of running node directly: the plist holds the
# current token and a second process would conflict on port 8780.
set -Eeuo pipefail
LABEL="com.workspace-bridge.pi-host-adapter"
case "${1:-status}" in
  status) curl -s http://127.0.0.1:8780/health; echo ;;
  start) launchctl kickstart -k "gui/$(id -u)/${LABEL}" ;;
  stop) launchctl bootout "gui/$(id -u)/${LABEL}" ;;
  *) echo "usage: start_pi.sh [status|start|stop]" >&2; exit 1 ;;
esac
