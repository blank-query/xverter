"""PyInstaller entry point for the standalone xverter binary.

Bare double-click opens the TUI (main() maps no-args to the tui
subcommand); command-line use is identical to the installed `xverter`.
"""

import sys

from xverter.cli import main

if __name__ == "__main__":
    sys.exit(main())
