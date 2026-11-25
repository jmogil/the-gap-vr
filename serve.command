#!/bin/sh
# Double-click (macOS) or run ./serve.command to view the model at http://localhost:8777
cd "$(dirname "$0")" || exit 1
(sleep 1 && open "http://localhost:8777") &
exec python3 serve.py 8777
