#!/usr/bin/env bash
# Idempotent dockerd control for the Cursor Cloud Agent VM.
#
# The VM has no init system, so `service docker start` is not available:
# the daemon has to be launched directly and waited on. Shared by
# cursor-install.sh (prefetching images) and cursor-start.sh (per boot).
#
# Usage: cursor-dockerd.sh start|stop
set -euo pipefail

DOCKERD_LOG=/var/log/cursor-dockerd.log
DOCKERD_PID=/var/run/docker.pid
WAIT_SECONDS=90

dockerd_up() { sudo docker info >/dev/null 2>&1; }

start_dockerd() {
  if dockerd_up; then
    echo "cursor-dockerd: already running"
    return 0
  fi

  if ! command -v dockerd >/dev/null 2>&1; then
    echo "cursor-dockerd: dockerd not installed" >&2
    return 1
  fi

  # A pid file left behind by the build snapshot points at a process that
  # does not exist in this pod; dockerd refuses to start until it is gone.
  if [ -f "$DOCKERD_PID" ] && ! sudo kill -0 "$(sudo cat "$DOCKERD_PID")" 2>/dev/null; then
    sudo rm -f "$DOCKERD_PID"
  fi

  echo "cursor-dockerd: starting"
  sudo touch "$DOCKERD_LOG"
  sudo sh -c "nohup dockerd --group docker >>'$DOCKERD_LOG' 2>&1 &"

  for _ in $(seq "$WAIT_SECONDS"); do
    if dockerd_up; then
      echo "cursor-dockerd: ready"
      # Let the agent run docker without sudo in the shell it already has.
      sudo chgrp docker /var/run/docker.sock 2>/dev/null || true
      sudo chmod g+rw /var/run/docker.sock 2>/dev/null || true
      return 0
    fi
    sleep 1
  done

  echo "cursor-dockerd: not ready after ${WAIT_SECONDS}s; last log lines:" >&2
  sudo tail -n 40 "$DOCKERD_LOG" >&2 || true
  return 1
}

stop_dockerd() {
  if [ -f "$DOCKERD_PID" ]; then
    sudo kill "$(sudo cat "$DOCKERD_PID")" 2>/dev/null || true
  fi
  for _ in $(seq 30); do
    dockerd_up || break
    sleep 1
  done
  sudo rm -f "$DOCKERD_PID"
  echo "cursor-dockerd: stopped"
}

case "${1:-}" in
start) start_dockerd ;;
stop) stop_dockerd ;;
*)
  echo "usage: $0 start|stop" >&2
  exit 2
  ;;
esac
