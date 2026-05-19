#!/bin/bash
# Health check: run check + send alerts to Discord if any
cd "$(dirname "$0")/.."
.venv/bin/python -m portfolio_tracker alert-send 2>&1
