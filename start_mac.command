#!/bin/bash
# Double-click this file to start Symbol Counter.
# (First time: right-click it -> Open, then click "Open" to get past the macOS warning.)
cd "$(dirname "$0")"
echo "============================================"
echo "  Symbol Counter — starting up"
echo "============================================"
echo ""
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python is not installed yet."
  echo "Please install it from https://www.python.org/downloads/ ,"
  echo "then double-click this file again."
  echo ""
  read -p "Press Enter to close."
  exit 1
fi
echo "Installing required parts (only takes a while the first time)..."
python3 -m pip install -r requirements.txt
echo ""
echo "============================================"
echo "  Ready!  Open this in your browser:"
echo "     http://localhost:8000"
echo ""
echo "  To stop the app: close this window,"
echo "  or press Control-C."
echo "============================================"
echo ""
python3 -m uvicorn app:app --port 8000
