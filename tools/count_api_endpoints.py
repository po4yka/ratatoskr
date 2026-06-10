"""Count HTTP endpoints declared in app/api/routers/ via AST analysis.

Exits 0 always -- intended as an informational CI step, never a gate.
No external dependencies; stdlib only.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_HTTP_METHODS = frozenset({"get", "post", "put", "delete", "patch"})

_ROUTERS_ROOT = Path(__file__).parent.parent / "app" / "api" / "routers"


def _count_endpoints_in_file(path: Path) -> int:
    """Return the number of HTTP-method decorator calls in *path*."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError):
        return 0

    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            # Match: @<anything>.get / .post / .put / .delete / .patch
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in _HTTP_METHODS
            ):
                count += 1
            elif (
                isinstance(decorator, ast.Attribute)
                and decorator.attr in _HTTP_METHODS
            ):
                # Bare attribute reference without call args (rare but valid)
                count += 1
    return count


def main() -> None:
    py_files = sorted(_ROUTERS_ROOT.rglob("*.py"))

    per_file: list[tuple[int, str]] = []
    for path in py_files:
        n = _count_endpoints_in_file(path)
        if n:
            rel = path.relative_to(_ROUTERS_ROOT.parent.parent.parent)
            per_file.append((n, str(rel)))

    total = sum(n for n, _ in per_file)
    per_file.sort(key=lambda t: t[0], reverse=True)

    print(f"API surface: {total} endpoints (single-tenant budget)")
    for n, rel in per_file:
        print(f"  {n:3d}  {rel}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"count_api_endpoints: error (non-fatal): {exc}", file=sys.stderr)
    sys.exit(0)
