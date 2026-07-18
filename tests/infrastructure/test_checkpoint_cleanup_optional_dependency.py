"""The checkpoint cleanup module must not eagerly require the graph extra."""

from __future__ import annotations

import subprocess
import sys


def test_cleanup_module_imports_without_psycopg() -> None:
    script = """
import sys

class BlockPsycopg:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "psycopg" or fullname.startswith("psycopg."):
            raise ImportError("blocked optional dependency")
        return None

sys.meta_path.insert(0, BlockPsycopg())
import app.infrastructure.checkpointing.cleanup
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
