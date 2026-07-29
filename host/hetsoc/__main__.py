# =============================================================================
# `python -m hetsoc` entry point.
#
# Copyright (C) 2026, SoC Labs (www.soclabs.org)
# =============================================================================
"""Run the hetsoc CLI as a module."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
