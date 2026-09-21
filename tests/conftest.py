"""Test suite bootstrap.

Makes the repository root importable when pytest is invoked as a bare
console script (which does not add the CWD to sys.path, unlike
``python -m pytest``).
"""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
