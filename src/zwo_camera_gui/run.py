#!/usr/bin/env python3
"""
Launcher — run this from the repo root without installing.

    python run.py --sdk C:\path\to\ASICamera2.dll
"""
import sys
from pathlib import Path

# Add src/ to the import path so the package resolves
sys.path.insert(0, str(Path(__file__).parent / "src"))

from zwo_camera_gui.__main__ import main
main()
