"""Build the Ratatoskr browser extension zip artifact."""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXTENSION_DIR = ROOT / "extension"
DEFAULT_OUTPUT = ROOT / "dist" / "ratatoskr-quick-save-extension.zip"
REQUIRED_FILES = (
    "manifest.json",
    "popup.html",
    "popup.css",
    "popup.js",
    "background.js",
    "icon.svg",
    "README.md",
)


def validate_extension() -> None:
    missing = [name for name in REQUIRED_FILES if not (EXTENSION_DIR / name).is_file()]
    if missing:
        msg = f"Missing extension file(s): {', '.join(missing)}"
        raise SystemExit(msg)
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("manifest_version") != 3:
        raise SystemExit("extension/manifest.json must use Manifest V3")
    if "storage" not in manifest.get("permissions", []):
        raise SystemExit("extension/manifest.json must request storage permission")


def build_zip(output: Path) -> Path:
    validate_extension()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(EXTENSION_DIR.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(EXTENSION_DIR).as_posix())
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = build_zip(args.output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
