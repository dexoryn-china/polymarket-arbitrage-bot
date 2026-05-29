#!/bin/bash
cd "$(dirname "$0")"
exec .venv/bin/python -m src.cli dashboard
