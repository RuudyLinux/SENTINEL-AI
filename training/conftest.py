"""Make the training tooling importable as top-level modules.

training/ isn't a package and can't be imported from backend/: dataset
tooling must never pull in torch/OpenCV/ultralytics, and the backend image
must never carry training code. Two separate roots keep it that way.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
