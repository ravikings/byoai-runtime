#!/usr/bin/env bash
# Installs byoai-runtime (editable, with the fastapi + recorder extras) and
# launches the Agent Showcase demo. Run from anywhere; paths are resolved
# relative to this script.
#
#   ./start.sh
#
# Env vars (all optional, see README.md for the full list):
#   ANTHROPIC_API_KEY, OPENAI_API_KEY   live model calls (falls back to
#                                       cached transcripts if unset)
#   BYOAI_RECORDER_ENABLED=1           seal every run to the Coriqo ledger
#                                       (on by default here — set to 0 to
#                                       disable and confirm /verify reports
#                                       the recorder as disabled)
#   BYOAI_RECORDER_DIR                 ledger dir, defaults to ~/.byoai/recorder
#   BYOAI_DEMO_LIVE_CALL_STATE         file tracking each agent's last live
#                                       call, defaults to
#                                       ~/.byoai/agent_showcase_live_calls.json
#                                       (persists the 24h live-call TTL across
#                                       restarts)
#   DEMO_TAMPER=1                      enable the /api/demo/tamper endpoint
#   BYOAI_DEMO_AUTOPILOT=1             background traffic every 90-240s
#   BYOAI_DEMO_STEP_DELAY_MS           pace fallback-transcript replay so the
#                                       UI's heartbeat/active-card/workflow
#                                       indicators are visible (400ms here;
#                                       fallback replay has no real network
#                                       latency of its own, so without this
#                                       every step fires in milliseconds)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

for ENV_FILE in "$REPO_ROOT/.env" "$SCRIPT_DIR/.env"; do
  if [ -f "$ENV_FILE" ]; then
    echo "==> Loading $ENV_FILE"
    set -a
    # shellcheck disable=SC1091
    source "$ENV_FILE"
    set +a
  fi
done

cd "$REPO_ROOT"

echo "==> Installing byoai-runtime (editable) with fastapi + recorder extras"
pip install --pre -e ".[fastapi,recorder]"

export BYOAI_RECORDER_ENABLED="${BYOAI_RECORDER_ENABLED:-1}"
export BYOAI_RECORDER_DIR="${BYOAI_RECORDER_DIR:-$HOME/.byoai/recorder}"
export BYOAI_DEMO_STEP_DELAY_MS="${BYOAI_DEMO_STEP_DELAY_MS:-400}"

echo "==> Recorder: BYOAI_RECORDER_ENABLED=$BYOAI_RECORDER_ENABLED  BYOAI_RECORDER_DIR=$BYOAI_RECORDER_DIR"
if [ "$BYOAI_RECORDER_ENABLED" = "1" ]; then
  echo "    Runs will be sealed to the ledger above. Check with:"
  echo "      curl -s localhost:8000/api/runs/<run_id>/verify | python -m json.tool"
else
  echo "    Recorder disabled — runs will NOT be sealed; /verify will report it as disabled."
fi

echo "==> Starting uvicorn on http://localhost:8000"
exec uvicorn examples.agent_showcase.app:app --reload
