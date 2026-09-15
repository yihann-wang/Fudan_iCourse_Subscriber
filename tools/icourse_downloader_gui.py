"""Compatibility entry point for the current GUI (credentials use Keychain)."""
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mac_gui import main

if __name__ == "__main__":
    raise SystemExit(main())
