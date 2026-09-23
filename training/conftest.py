"""Make the training tooling importable as top-level modules.

`training/` is deliberately NOT a package and NOT importable from `backend/`:
the dataset tooling must never pull in the inference stack (torch, OpenCV,
ultralytics), and the backend image must never carry the training code. Keeping
them as two independent roots is what enforces that separation.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
