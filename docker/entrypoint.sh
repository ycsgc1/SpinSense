#!/bin/bash
set -e

echo "🚀 Starting SpinSense Engine..."
# You could add a 'sleep 5' here if you wanted to wait for network
exec python3 core_engine.py