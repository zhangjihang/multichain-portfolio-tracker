#!/bin/bash
# Weekly report: generate + send to Discord via webhook
cd "$(dirname "$0")/.."
.venv/bin/python -m portfolio_tracker weekly-send 2>&1
