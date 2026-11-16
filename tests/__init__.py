"""Tests never touch your real settings or data: everything points at a temporary folder."""
import os
import tempfile

_sandbox = tempfile.mkdtemp(prefix="nudgy-tests-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_sandbox, "config")
os.environ["XDG_DATA_HOME"] = os.path.join(_sandbox, "data")
os.environ.pop("NUDGY_NOTES", None)
