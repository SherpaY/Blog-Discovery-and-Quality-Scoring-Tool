#!/bin/bash
# Start the local web interface at http://localhost:8501
set -euo pipefail
cd "$(dirname "$0")/.."
exec ./.venv/bin/python -m streamlit run app.py --browser.gatherUsageStats false
