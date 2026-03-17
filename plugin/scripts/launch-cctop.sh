#!/bin/bash
# Launch cctop — Claude Code Sessions dashboard with the background poller
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

# Handle --reset: wipe session data before starting
if [[ " $* " == *" --reset "* ]]; then
    rm -rf ~/.cctop
    mkdir -p ~/.cctop
    echo "cctop: session data cleared"
fi

# Start the local poller in the background
uv run --script "$SCRIPT_DIR/cctop-poller.py" &
POLLER_PID=$!

# Start the remote poller in the background (only if machines.json exists)
REMOTE_POLLER_PID=""
if [ -f "$HOME/.cctop/machines.json" ]; then
    uv run --script "$SCRIPT_DIR/cctop-remote-poller.py" &
    REMOTE_POLLER_PID=$!
fi

# Kill all pollers when this script exits (dashboard quit, ctrl-c, etc.)
trap "kill $POLLER_PID 2>/dev/null; wait $POLLER_PID 2>/dev/null; \
      [ -n \"$REMOTE_POLLER_PID\" ] && kill $REMOTE_POLLER_PID 2>/dev/null; wait $REMOTE_POLLER_PID 2>/dev/null" EXIT

# Run the dashboard in the foreground
uv run --script "$SCRIPT_DIR/cctop_dashboard.py" "$@"
