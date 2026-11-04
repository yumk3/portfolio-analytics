"""INPUTS_DIR redirects every per-user input file, so tests never read the real inputs/."""
import importlib.util
import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


def load_local_inputs():
    spec = importlib.util.spec_from_file_location("local_inputs_under_test",
                                                  REPO / "scripts" / "local_inputs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_test_session_uses_an_empty_inputs_folder():
    folder = pathlib.Path(os.environ["INPUTS_DIR"])
    assert folder.resolve() != (REPO / "inputs").resolve()
    assert folder.is_dir() and not any(folder.iterdir())


def test_default_is_the_repo_inputs_folder(monkeypatch):
    monkeypatch.delenv("INPUTS_DIR")
    assert load_local_inputs().INPUTS == REPO / "inputs"


def test_inputs_dir_redirects_every_file(monkeypatch, tmp_path):
    (tmp_path / "accepted_exceptions.local.csv").write_text(
        "symbol,scope,reason\nZZZ,valuation,fabricated\nZZZ,rebuild,fabricated rebuild\n",
        encoding="utf-8")
    (tmp_path / "reviewed_jumps.local.csv").write_text(
        "symbol,date,note\nZZZ,2025-01-02,fabricated\n", encoding="utf-8")
    (tmp_path / "opening_balances.csv").write_text(
        "account_type,symbol,as_of_date,units,source,note\n"
        "INDIVIDUAL,ZZZ,2025-01-01,1.5,test,fabricated\n", encoding="utf-8")
    monkeypatch.setenv("INPUTS_DIR", str(tmp_path))
    li = load_local_inputs()
    assert li.accepted_exceptions("valuation") == {"ZZZ": "fabricated"}
    assert li.accepted_exceptions("rebuild") == {"ZZZ": "fabricated rebuild"}
    assert li.reviewed_jumps() == {("ZZZ", "2025-01-02"): "fabricated"}
    assert li.opening_balances() == [("INDIVIDUAL", "ZZZ", "2025-01-01", 1.5, "test", "fabricated")]


def test_empty_inputs_dir_gives_empty_results():
    li = load_local_inputs()
    assert li.accepted_exceptions("valuation") == {}
    assert li.reviewed_jumps() == {}
    assert li.opening_balances() == []


@pytest.mark.parametrize("script, name", [
    ("validate_valuation.py", "CHECKPOINTS"),
    ("validate_returns.py", "BROKER_CSV"),
    ("validate_returns.py", "STATEMENT_CSV"),
])
def test_validators_read_checkpoints_from_inputs_dir(tmp_path, script, name):
    code = ("import importlib.util, sys; sys.path.insert(0, r'%s'); "
            "s = importlib.util.spec_from_file_location('m', r'%s'); "
            "m = importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "print(m.%s)" % (REPO / "scripts", REPO / "scripts" / script, name))
    env = {k: v for k, v in os.environ.items()
           if k not in ("RETURNS_BROKER_CSV", "RETURNS_STATEMENT_CSV")}
    env["INPUTS_DIR"] = str(tmp_path)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    assert pathlib.Path(r.stdout.strip()).parent == tmp_path
