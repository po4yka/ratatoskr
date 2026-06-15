from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.architecture._import_rules import collect_forbidden_imports

PEER_ADAPTER_SUBPACKAGES = (
    "academic",
    "digest",
    "git_backup",
    "github",
    "ingestors",
    "rss",
    "telegram",
    "twitter",
    "youtube",
)


def test_academic_adapter_has_no_peer_imports():
    subdomain_root = Path(__file__).resolve().parents[2] / "app" / "adapters" / "academic"
    forbidden_prefixes = tuple(
        "app.adapters." + s for s in PEER_ADAPTER_SUBPACKAGES if s != "academic"
    )
    violations = collect_forbidden_imports(subdomain_root, forbidden_prefixes=forbidden_prefixes)
    assert violations == [], violations


def test_digest_adapter_has_no_peer_imports():
    subdomain_root = Path(__file__).resolve().parents[2] / "app" / "adapters" / "digest"
    forbidden_prefixes = tuple(
        "app.adapters." + s for s in PEER_ADAPTER_SUBPACKAGES if s != "digest"
    )
    violations = collect_forbidden_imports(subdomain_root, forbidden_prefixes=forbidden_prefixes)
    assert violations == [], violations


def test_git_backup_adapter_has_no_peer_imports():
    subdomain_root = Path(__file__).resolve().parents[2] / "app" / "adapters" / "git_backup"
    forbidden_prefixes = tuple(
        "app.adapters." + s for s in PEER_ADAPTER_SUBPACKAGES if s != "git_backup"
    )
    violations = collect_forbidden_imports(subdomain_root, forbidden_prefixes=forbidden_prefixes)
    assert violations == [], violations


def test_github_adapter_has_no_peer_imports():
    subdomain_root = Path(__file__).resolve().parents[2] / "app" / "adapters" / "github"
    forbidden_prefixes = tuple(
        "app.adapters." + s for s in PEER_ADAPTER_SUBPACKAGES if s != "github"
    )
    violations = collect_forbidden_imports(subdomain_root, forbidden_prefixes=forbidden_prefixes)
    assert violations == [], violations


def test_ingestors_adapter_has_no_peer_imports():
    subdomain_root = Path(__file__).resolve().parents[2] / "app" / "adapters" / "ingestors"
    forbidden_prefixes = tuple(
        "app.adapters." + s for s in PEER_ADAPTER_SUBPACKAGES if s != "ingestors"
    )
    violations = collect_forbidden_imports(subdomain_root, forbidden_prefixes=forbidden_prefixes)
    assert violations == [], violations


def test_rss_adapter_has_no_peer_imports():
    subdomain_root = Path(__file__).resolve().parents[2] / "app" / "adapters" / "rss"
    forbidden_prefixes = tuple("app.adapters." + s for s in PEER_ADAPTER_SUBPACKAGES if s != "rss")
    violations = collect_forbidden_imports(subdomain_root, forbidden_prefixes=forbidden_prefixes)
    assert violations == [], violations


def test_telegram_adapter_has_no_peer_imports():
    subdomain_root = Path(__file__).resolve().parents[2] / "app" / "adapters" / "telegram"
    forbidden_prefixes = tuple(
        "app.adapters." + s for s in PEER_ADAPTER_SUBPACKAGES if s != "telegram"
    )
    violations = collect_forbidden_imports(subdomain_root, forbidden_prefixes=forbidden_prefixes)
    assert violations == [], violations


def test_twitter_adapter_has_no_peer_imports():
    subdomain_root = Path(__file__).resolve().parents[2] / "app" / "adapters" / "twitter"
    forbidden_prefixes = tuple(
        "app.adapters." + s for s in PEER_ADAPTER_SUBPACKAGES if s != "twitter"
    )
    violations = collect_forbidden_imports(subdomain_root, forbidden_prefixes=forbidden_prefixes)
    assert violations == [], violations


def test_youtube_adapter_has_no_peer_imports():
    subdomain_root = Path(__file__).resolve().parents[2] / "app" / "adapters" / "youtube"
    forbidden_prefixes = tuple(
        "app.adapters." + s for s in PEER_ADAPTER_SUBPACKAGES if s != "youtube"
    )
    violations = collect_forbidden_imports(subdomain_root, forbidden_prefixes=forbidden_prefixes)
    assert violations == [], violations


@pytest.mark.slow
def test_adapters_dependency_matrix_summary():
    adapters_root = Path(__file__).resolve().parents[2] / "app" / "adapters"

    matrix: dict[str, dict[str, list[str]]] = {s: {} for s in PEER_ADAPTER_SUBPACKAGES}

    for src in PEER_ADAPTER_SUBPACKAGES:
        src_root = adapters_root / src
        if not src_root.exists():
            continue
        for dst in PEER_ADAPTER_SUBPACKAGES:
            if dst == src:
                continue
            hits: list[str] = []
            prefix = "app.adapters." + dst
            for path in sorted(src_root.rglob("*.py")):
                try:
                    tree = ast.parse(path.read_text(), filename=str(path))
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if (
                        isinstance(node, ast.ImportFrom)
                        and node.module
                        and node.module.startswith(prefix)
                    ):
                        hits.append(f"{path.name}:{node.lineno}")
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            if alias.name.startswith(prefix):
                                hits.append(f"{path.name}:{node.lineno}")
            if hits:
                matrix[src][dst] = hits

    col_width = max(len(s) for s in PEER_ADAPTER_SUBPACKAGES)
    header = " " * (col_width + 2) + "  ".join(s[:4] for s in PEER_ADAPTER_SUBPACKAGES)
    print()
    print("Cross-adapter import matrix (X = runtime import detected):")
    print(header)
    for src in PEER_ADAPTER_SUBPACKAGES:
        row_cells = []
        for dst in PEER_ADAPTER_SUBPACKAGES:
            if dst == src:
                row_cells.append(" -- ")
            elif dst in matrix[src]:
                row_cells.append("  X ")
            else:
                row_cells.append("  . ")
        print(f"{src:<{col_width}}  {''.join(row_cells)}")

    for src in PEER_ADAPTER_SUBPACKAGES:
        for dst, hits in matrix[src].items():
            print(f"  {src} -> {dst}:")
            for h in hits:
                print(f"    {h}")
