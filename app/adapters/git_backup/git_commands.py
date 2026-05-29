"""Construction of the ``git`` argv for clone/update operations.

Port of the command builder in ``Engine.kt`` (the ``buildGitCommand`` helper around
line 950). This is the heart of the tool, so the exact argv is captured as fixtures.
``build_git_command`` is implemented in Phase 1.

Argv shape (in order):

    git
      -c safe.directory=*
      [-c http.sslVerify=false]                 # when not verify_certificates
      [-c http.version=HTTP/1.1]                # when force_http1 or http_version == HTTP/1.1
      -c http.postBuffer=<post_buffer_size>
      [-c http.lowSpeedLimit=<n> -c http.lowSpeedTime=<n>]   # when low_speed_limit > 0
      [-c credential.helper=store --file=<credentials_path>] # when credentials_path
      <operation...>

    operation when repo does NOT exist:
      shallow:        clone --depth=1 --single-branch [--progress] -- <url> <repo_name>
      single_branch:  clone --bare --single-branch [--branch <default_branch>]
                            [--progress] -- <url> <repo_name>
      mirror:         clone --mirror [--progress] -- <url> <repo_name>

    operation when repo exists:
      single_branch_only: fetch --prune origin
      mirror (default):   remote update --prune
"""

from __future__ import annotations

GIT_EXECUTABLE = "git"


def build_git_command(
    *,
    repo_exists: bool,
    url: str | None = None,
    repo_name: str | None = None,
    git_executable: str = GIT_EXECUTABLE,
    verify_certificates: bool = True,
    http_version: str = "HTTP/1.1",
    post_buffer_size: int = 524_288_000,
    low_speed_limit: int = 1000,
    low_speed_time: int = 60,
    credentials_path: str | None = None,
    force_http1: bool = False,
    use_shallow_clone: bool = False,
    show_progress: bool = False,
    single_branch_only: bool = False,
    default_branch: str | None = None,
) -> list[str]:
    """Build the full ``git`` argv for a clone or update of a single repository."""
    command = [git_executable]

    # Prevent "dubious ownership" errors when the repo dir has a different owner.
    command += ["-c", "safe.directory=*"]

    if not verify_certificates:
        command += ["-c", "http.sslVerify=false"]

    if force_http1 or http_version == "HTTP/1.1":
        command += ["-c", "http.version=HTTP/1.1"]

    command += ["-c", f"http.postBuffer={post_buffer_size}"]

    if low_speed_limit > 0:
        command += [
            "-c",
            f"http.lowSpeedLimit={low_speed_limit}",
            "-c",
            f"http.lowSpeedTime={low_speed_time}",
        ]

    if credentials_path is not None:
        command += ["-c", f"credential.helper=store --file={credentials_path}"]

    if not repo_exists:
        if url is None or repo_name is None:
            raise ValueError("url and repo_name are required to clone a repository")
        if use_shallow_clone:
            command += ["clone", "--depth=1", "--single-branch"]
            if show_progress:
                command.append("--progress")
        elif single_branch_only:
            command += ["clone", "--bare", "--single-branch"]
            if default_branch is not None:
                command += ["--branch", default_branch]
            if show_progress:
                command.append("--progress")
        else:
            command += ["clone", "--mirror"]
            if show_progress:
                command.append("--progress")
        command += ["--", url, repo_name]
    elif single_branch_only:
        command += ["fetch", "--prune", "origin"]
    else:
        command += ["remote", "update", "--prune"]

    return command
