#!/usr/bin/env bash
set -euo pipefail

# Simple server manager for FastAPI app
# Usage: ./start_server.sh [start|stop|restart|status|logs [-f]]

APP_MODULE="server.main:app"
HOST="0.0.0.0"
PORT="8000"
WORKERS="1"
RELOAD="1"

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$ROOT_DIR/logs"
RUN_DIR="$ROOT_DIR/.run"
PID_FILE="$RUN_DIR/server.pid"
OUT_LOG="$LOG_DIR/server.out"

mkdir -p "$LOG_DIR" "$RUN_DIR"

have_cmd() { command -v "$1" >/dev/null 2>&1; }

py_bin=""
if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  py_bin="$ROOT_DIR/.venv/bin/python"
elif have_cmd python3; then
  py_bin="$(command -v python3)"
elif have_cmd python; then
  py_bin="$(command -v python)"
else
  echo "python not found" >&2; exit 1
fi

uvicorn_cmd=("$py_bin" -m uvicorn "$APP_MODULE" --host "$HOST" --port "$PORT" --workers "$WORKERS")
if [ -n "$RELOAD" ]; then
  uvicorn_cmd+=(--reload)
fi

is_pid_running() {
  local pid="$1"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

current_pid() {
  if [ -f "$PID_FILE" ]; then
    cat "$PID_FILE"
  else
    echo ""
  fi
}

http_ok() {
  curl -sS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1
}

start() {
  local pid
  pid="$(current_pid)"
  if [ -n "$pid" ] && is_pid_running "$pid"; then
    echo "Server already running (pid $pid)"; return 0
  fi
  if http_ok; then
    echo "Port $PORT is already serving (external process). Stop it first." >&2
    exit 1
  fi
  echo "Starting server on :$PORT ..."
  nohup "${uvicorn_cmd[@]}" >"$OUT_LOG" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 0.3
  if http_ok; then
    echo "Started (pid $(cat "$PID_FILE"))"
  else
    echo "Started, but health check not responding yet (check logs)."
  fi
}

stop() {
  local force="${1:-}"; shift || true
  local pid
  pid="$(current_pid)"
  if [ -n "$pid" ] && is_pid_running "$pid"; then
    echo "Stopping pid $pid ..."
    kill "$pid" 2>/dev/null || true
    for i in {1..20}; do
      if is_pid_running "$pid"; then sleep 0.1; else break; fi
    done
    if is_pid_running "$pid"; then
      echo "Force killing pid $pid ..."
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
  else
    if http_ok; then
      if [ "$force" = "--force" ]; then
        echo "Stopping external server by pattern ..."
        pkill -f "uvicorn .*server\.main:app" 2>/dev/null || true
        pkill -f "python .*server/main\.py" 2>/dev/null || true
      else
        echo "Server seems running but no PID file. Use: $0 stop --force" >&2
        exit 1
      fi
    else
      echo "Server not running"
    fi
  fi
}

status() {
  local pid
  pid="$(current_pid)"
  if [ -n "$pid" ] && is_pid_running "$pid"; then
    echo "running (pid $pid)"
    return 0
  fi
  if http_ok; then
    echo "running (external)"
  else
    echo "stopped"
  fi
}

logs() {
  local follow="${1:-}"
  if [ "$follow" = "-f" ] || [ "$follow" = "--follow" ]; then
    tail -n 120 -f "$OUT_LOG"
  else
    echo "== $OUT_LOG =="; tail -n 400 "$OUT_LOG" || true
  fi
}

case "${1:-}" in
  start) shift; start "$@" ;;
  stop) shift; stop "$@" ;;
  restart) shift; stop "$@" || true; start ;;
  status) shift; status ;;
  logs) shift; logs "$@" ;;
  *) echo "Usage: $0 {start|stop|restart|status|logs [-f]}" ; exit 1 ;;
esac
