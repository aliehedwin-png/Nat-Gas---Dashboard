#!/bin/bash
cd "$(dirname "$0")"
python3 -c "import ib_async" 2>/dev/null || { echo "Installing ib_async, one time only..."; python3 -m pip install ib_async; }
echo "Starting the dashboard. It also starts the read-only IBKR feed. Leave this window open."
OPEN_BROWSER=1 python3 server.py
echo; read -p "The dashboard stopped. Read any message above, then press Enter to close."
