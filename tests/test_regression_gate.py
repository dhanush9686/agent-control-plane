import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from check_regression import check  # noqa: E402

THR = {"pass_rate": {"min": 0.8}, "hallucination_rate": {"max": 0.1}}


def test_gate_passes_when_within_thresholds():
    assert check({"pass_rate": 0.9, "hallucination_rate": 0.05}, THR) == []


def test_gate_fails_below_min_and_above_max():
    problems = check({"pass_rate": 0.5, "hallucination_rate": 0.3}, THR)
    assert len(problems) == 2


def test_gate_fails_on_missing_metric():
    assert check({"pass_rate": 0.9}, THR)


def test_gate_script_exit_codes(tmp_path):
    root = Path(__file__).resolve().parent.parent
    rep, thr = tmp_path / "r.json", tmp_path / "t.json"
    thr.write_text(json.dumps(THR))
    script = str(root / "scripts" / "check_regression.py")
    rep.write_text(json.dumps({"summary": {"pass_rate": 0.9, "hallucination_rate": 0.0}}))
    assert subprocess.run([sys.executable, script, str(rep), str(thr)]).returncode == 0
    rep.write_text(json.dumps({"summary": {"pass_rate": 0.1, "hallucination_rate": 0.0}}))
    assert subprocess.run([sys.executable, script, str(rep), str(thr)]).returncode == 1
