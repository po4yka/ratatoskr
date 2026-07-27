from __future__ import annotations

import json
import os
import subprocess
import sys

import yaml

from app.api.models.responses.common import API_CONTRACT_VERSION
from tools.scripts.generate_openapi import (
    JSON_PATH,
    YAML_PATH,
    generate_spec,
)


def test_generated_openapi_version_matches_contract_version() -> None:
    spec = generate_spec()

    assert spec["info"]["version"] == API_CONTRACT_VERSION


def test_generated_422_responses_use_project_error_envelope() -> None:
    spec = generate_spec()
    expected = {"$ref": "#/components/responses/ValidationError"}

    mismatches = []
    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if method.upper() not in {"DELETE", "GET", "HEAD", "PATCH", "POST", "PUT"}:
                continue
            if operation["responses"]["422"] != expected:
                mismatches.append(f"{method.upper()} {path}")

    assert mismatches == []
    assert "HTTPValidationError" not in spec["components"]["schemas"]
    assert "ValidationError" not in spec["components"]["schemas"]


def test_committed_openapi_docs_match_generator() -> None:
    env = {
        **os.environ,
        "ALLOWED_ORIGINS": "http://localhost",
        "JWT_SECRET_KEY": "x" * 40,
        "SECRET_KEY": "x" * 40,
        "REDIS_ENABLED": "0",
    }
    result = subprocess.run(
        [sys.executable, "tools/scripts/generate_openapi.py", "--check"],
        cwd=YAML_PATH.parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_committed_openapi_yaml_and_json_are_equivalent() -> None:
    yaml_spec = yaml.safe_load(YAML_PATH.read_text())
    json_spec = json.loads(JSON_PATH.read_text())

    assert yaml_spec == json_spec
