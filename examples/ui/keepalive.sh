#!/usr/bin/env bash
# Coriqo local keep-alive — starts (or restarts) the three live surfaces:
#   1. MCP connector gateway        :8800/mcp  (+ Tailscale outside)
#   2. Desktop capture proxy        :8080      (system HTTPS proxy target)
#   3. Coriqo shield analyzer + UI  :17831      (ledger → verdicts → seal → UI)
# Designed to run under launchd KeepAlive: if a process dies, running this
# again resyncs everything.
set -u
SERVICES_ICON="🛡"
if [ -n "${CORE_ROOT:-}" ]; then ROOT="$CORE_ROOT"; else
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." 2>/dev/null && pwd)"
fi
: "${ROOT:=/Users/abdulrabiu/Documents/GitHub/byoai-runtime}"
PY="$ROOT/.venv/bin/python"
LOGDIR="${HOME}/.byoai_shell"
mkdir -p "$LOGDIR"

start() { # name, pattern, command...
  local name="$1" pattern="$2"; shift 2
  if pgrep -f "$pattern" >/dev/null; then
    echo "[keepalive] $name already running"
    return
  fi
  echo "[keepalive] starting $name"
  nohup "$@" >>"$LOGDIR/$name.log" 2>&1 &
}

start mcp-server   "mcp_capture/server.py --http" "$PY" "$ROOT/examples/mcp_capture/server.py" --http
start desktop-proxy "mitmdump --listen-port 8080" "$ROOT/.venv/bin/mitmdump" --listen-port 8080 -s "$ROOT/examples/desktop_proxy_capture.py"
start shield       "byoai.integrations.shield" "$PY" -m byoai.integrations.shield "$ROOT/examples/mcp_capture/captures.jsonl" --port 17831

echo "[keepalive] all surfaces synced → MCP :8800 · proxy :8080 · shield :17831"

# Stay in the foreground babysitting; if any surface dies, exit 1 so launchd
# (KeepAlive SuccessfulExit=false) re-runs this script and restarts what's
# missing.
while true; do
  sleep 10
  pgrep -f "mcp_capture/server.py --http" >/dev/null || exit 1
  pgrep -f "mitmdump --listen-port 8080" >/dev/null || exit 1
  pgrep -f "byoai.integrations.shield" >/dev/null || exit 1
done
