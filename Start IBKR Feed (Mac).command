#!/bin/bash
cd "$(dirname "$0")"
python3 -c "import ib_async" 2>/dev/null || { echo "Installing ib_async, one time only..."; python3 -m pip install ib_async; }
echo "Reading bars from TWS / IB Gateway (read-only). Leave this window open."
python3 ibkr_feed.py
echo; read -p "The feed stopped. Read any message above, then press Enter to close."
