from __future__ import annotations

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[3] / "services" / "api"
sys.path.insert(0, str(API_ROOT))
