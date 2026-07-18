"""Build a deterministic ratatoskr-web archive for Docker release images."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REVISION_FILE = ROOT / "ops/docker/ratatoskr-web.commit"
DEFAULT_OUTPUT = ROOT / "ops/docker/ratatoskr-web.bundle.tar.gz"


def _run(command: list[str], *, cwd: Path, capture: bool = False) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=capture,
    )
    return result.stdout.strip() if capture else ""


def _normalized_info(
    archive: tarfile.TarFile,
    path: Path,
    arcname: str,
) -> tarfile.TarInfo:
    info = archive.gettarinfo(str(path), arcname=arcname)
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    if info.isdir():
        info.mode = 0o755
    elif info.isfile():
        info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
    return info


def _write_bundle(dist: Path, output: Path, revision: str) -> str:
    output.parent.mkdir(parents=True, exist_ok=True)
    with (
        output.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for path in sorted(dist.rglob("*")):
            relative = path.relative_to(dist).as_posix()
            if relative == ".source-commit":
                continue
            if path.is_symlink():
                raise RuntimeError(f"Frontend dist must not contain symlinks: {relative}")
            info = _normalized_info(archive, path, relative)
            if info.isfile():
                with path.open("rb") as source:
                    archive.addfile(info, source)
            else:
                archive.addfile(info)

        revision_bytes = f"{revision}\n".encode()
        revision_info = tarfile.TarInfo(".source-commit")
        revision_info.size = len(revision_bytes)
        revision_info.mode = 0o644
        revision_info.uid = 0
        revision_info.gid = 0
        revision_info.mtime = 0
        archive.addfile(revision_info, io.BytesIO(revision_bytes))

    return hashlib.sha256(output.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-repo", type=Path, default=ROOT.parent / "ratatoskr-web")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    web_repo = args.web_repo.resolve()
    revision = REVISION_FILE.read_text(encoding="utf-8").strip()
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise RuntimeError(f"Invalid frontend revision in {REVISION_FILE}: {revision!r}")
    if not web_repo.is_dir():
        raise RuntimeError(f"Frontend repository does not exist: {web_repo}")

    with tempfile.TemporaryDirectory(prefix="ratatoskr-web-bundle-") as temp_dir:
        build_repo = Path(temp_dir) / "ratatoskr-web"
        _run(
            [
                "git",
                "clone",
                "--local",
                "--no-hardlinks",
                "--no-checkout",
                str(web_repo),
                str(build_repo),
            ],
            cwd=ROOT,
        )
        _run(["git", "checkout", "--detach", revision], cwd=build_repo)
        actual_revision = _run(["git", "rev-parse", "HEAD"], cwd=build_repo, capture=True)
        if actual_revision != revision:
            raise RuntimeError(
                f"Frontend checkout is {actual_revision}; expected pinned revision {revision}"
            )

        for command in (
            ["npm", "ci", "--no-audit", "--no-fund"],
            ["npm", "run", "check:static"],
            ["npm", "run", "test"],
            ["npm", "run", "build"],
        ):
            _run(command, cwd=build_repo)

        dist = build_repo / "dist"
        if not (dist / "index.html").is_file():
            raise RuntimeError("Frontend build did not produce dist/index.html")
        if not any((dist / "assets").glob("*.js")):
            raise RuntimeError("Frontend build did not produce JavaScript assets")

        digest = _write_bundle(dist, args.output.resolve(), revision)
    print(f"Wrote {args.output} for {revision} (sha256:{digest})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
