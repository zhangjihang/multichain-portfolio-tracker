#!/bin/bash
# Daily report: generate + send to Discord via webhook
cd "$(dirname "$0")/.."
.venv/bin/python -m portfolio_tracker report-send 2>&1
