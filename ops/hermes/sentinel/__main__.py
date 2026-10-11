"""`python -m sentinel --once` (run from the directory that CONTAINS the sentinel package)."""

import sys

from .detect import main

if __name__ == "__main__":
    sys.exit(main())
