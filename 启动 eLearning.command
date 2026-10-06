#!/bin/zsh
set -e
cd -- "${0:A:h}"
exec .venv/bin/python -m src.elearning_panel
