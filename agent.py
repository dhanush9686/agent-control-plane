"""Thin entry point kept for backwards compatibility: `python agent.py [question]`.

All real logic lives in the `acp` package. With no arguments it runs the demo questions.
"""

import sys

from acp.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["--demo"]))
