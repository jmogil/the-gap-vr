#!/bin/sh
# Double-click (macOS) or run ./serve.command to view the model at http://localhost:8777
cd "$(dirname "$0")/web" || exit 1
(sleep 1 && open "http://localhost:8777") &
exec python3 -m http.server 8777 --bind 127.0.0.1
