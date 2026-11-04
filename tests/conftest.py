"""Test-wide setup: no test reads the real inputs/ folder.

INPUTS_DIR is set before any test module imports a script, and subprocesses
inherit it. Tests that need input files point it at their own folder.
"""
import os
import shutil
import tempfile

_EMPTY_INPUTS = tempfile.mkdtemp(prefix="empty_inputs_")
os.environ["INPUTS_DIR"] = _EMPTY_INPUTS


def pytest_unconfigure(config):
    shutil.rmtree(_EMPTY_INPUTS, ignore_errors=True)
