"""
Compatibility wrapper for the original root-level demo entrypoint.

The full showcase now lives in `demos/runtime_demo.py`, but keeping this file
around preserves the earlier `python3 demo.py` workflow.
"""

import os
import sys


DEMO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demos")
if DEMO_DIR not in sys.path:
    sys.path.insert(0, DEMO_DIR)


from runtime_demo import main


if __name__ == "__main__":
    main()
