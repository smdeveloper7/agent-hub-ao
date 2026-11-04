#!/usr/bin/env python3
"""aohub 진입점: python scripts/aohub.py <명령> ... (도움말: --help)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aohub.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
