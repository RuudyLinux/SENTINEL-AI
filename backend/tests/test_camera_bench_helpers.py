"""Unit coverage for tools/camera_bench.py's pure helper only. The tool's
real work (spinning up N real camera workers with real YOLO+OCR inference)
is deliberately NOT exercised in the regular suite — it is a multi-second,
CPU-heavy real workload meant to be run deliberately (see the tool's own
docstring), not on every test run. Import path mirrors tools/anpr_bench.py's
own pattern of living outside `app/` with its own sys.path insert.
"""
import importlib.util
import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
_spec = importlib.util.spec_from_file_location("camera_bench", _TOOLS_DIR / "camera_bench.py")
camera_bench = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("camera_bench", camera_bench)
_spec.loader.exec_module(camera_bench)  # type: ignore[union-attr]


def test_percentile_of_empty_list_is_zero():
    assert camera_bench._percentile([], 95) == 0.0


def test_percentile_matches_known_values():
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert camera_bench._percentile(values, 0) == 10.0
    assert camera_bench._percentile(values, 100) == 50.0
    assert camera_bench._percentile(values, 50) == 30.0


def test_percentile_single_value():
    assert camera_bench._percentile([42.0], 95) == 42.0
