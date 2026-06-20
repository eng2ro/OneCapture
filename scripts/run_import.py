"""Thin wrapper so the importer can be run without installing the console script:

    python scripts/run_import.py --config config/clients/abc_manufacturing.yaml LISTING
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from erpsync.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
