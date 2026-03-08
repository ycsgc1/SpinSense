#!/bin/bash
set -e

# Clean up any old socket files
rm -f /tmp/spinsense.sock

echo "🚀 Starting SpinSense Core Engine (Background)..."
# Run the engine from the root folder
python3 core_engine.py &

echo "🚀 Starting SpinSense Web GUI (Foreground)..."
# Move into the GUI folder so FastAPI can find the static/template folders
cd gui

# Launch FastAPI using Uvicorn, binding to all interfaces on port 8000
exec uvicorn backend_main:app --host 0.0.0.0 --port 8000