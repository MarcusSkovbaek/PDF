"""Folio PDF entry point.

    python main.py [file.pdf ...] [--theme light|dark] [--size WxH] [--screenshot out.png]
"""

# The network kill switch must be installed before anything else is imported.
from pdfstudio.security import netguard

netguard.install()

import sys  # noqa: E402


def main() -> int:
    from pdfstudio.ui.app import run

    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
