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
    "icons/icon-16.png",
    "icons/icon-32.png",
    "icons/icon-48.png",
    "icons/icon-128.png",
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
    icons = manifest.get("icons", {})
    if icons.get("128") != "icons/icon-128.png":
        raise SystemExit("extension/manifest.json must declare a 128px PNG icon")
    action_icons = manifest.get("action", {}).get("default_icon", {})
    if action_icons.get("128") != "icons/icon-128.png":
        raise SystemExit("extension/manifest.json action must declare a 128px PNG icon")
    if "<all_urls>" in manifest.get("host_permissions", []):
        raise SystemExit("extension/manifest.json must not request persistent <all_urls>")
    # optional_host_permissions may contain "https://*/*" (broad HTTPS access).
    # This is intentional: the extension needs to reach any page the user wants to
    # save, but the permission is user-gated (granted only when the user triggers
    # the action on a given site).  Blocking <all_urls> only in the persistent
    # host_permissions list is therefore sufficient; we explicitly permit the
    # optional variant here.  Reject only if a literal "<all_urls>" token appears,
    # which would be a mistake rather than the intentional broad-optional pattern.
    if "<all_urls>" in manifest.get("optional_host_permissions", []):
        raise SystemExit(
            "extension/manifest.json must not request <all_urls> in optional_host_permissions; "
            "use 'https://*/*' as a user-gated optional permission instead"
        )


# Directories present in the extension source tree that must not be included in
# the published zip artifact. screenshots/ holds store-listing assets, not
# runtime extension files.
_EXCLUDE_DIRS: frozenset[str] = frozenset({"screenshots"})


def build_zip(output: Path) -> Path:
    validate_extension()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(EXTENSION_DIR.rglob("*")):
            if path.is_file():
                relative = path.relative_to(EXTENSION_DIR)
                if relative.parts[0] in _EXCLUDE_DIRS:
                    continue
                archive.write(path, relative.as_posix())
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
