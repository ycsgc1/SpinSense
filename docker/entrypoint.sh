#!/bin/bash
set -e

# Clean up any old socket files
rm -f /tmp/spinsense.sock

# Keep the engine running for as long as the container lives.
#
# It used to be a plain background job, so when the engine died the container
# stayed up and served a healthy-looking web UI attached to nothing. That is
# not hypothetical: a USB codec that hadn't finished enumerating raised
# "No input device matching ..." on startup and SpinSense was deaf for 38 hours
# with no sign of it anywhere. The engine now supervises its own loop too; this
# is the outer belt, covering an exit no in-process handler can catch.
supervise_engine() {
  while true; do
    python3 core/core_engine.py
    echo "⚠️  Core engine exited ($?). Restarting in 5s..." >&2
    sleep 5
  done
}

echo "🚀 Starting SpinSense Core Engine (Background)..."
supervise_engine &

echo "🚀 Starting SpinSense Web GUI (Foreground)..."
# Move into the GUI folder so FastAPI can find the static/template folders
cd gui

# Launch FastAPI using Uvicorn, binding to all interfaces on the configured port (SPINSENSE_PORT, default 3313)
exec uvicorn backend_main:app --host 0.0.0.0 --port "${SPINSENSE_PORT:-3313}"
