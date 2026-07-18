#!/usr/bin/env bash
# Check recent CI performance metrics.
# Usage: ./tools/scripts/check_ci_performance.sh

set -euo pipefail

missing_dependencies=()
for dependency in gh jq column; do
    if ! command -v "${dependency}" >/dev/null 2>&1; then
        missing_dependencies+=("${dependency}")
    fi
done

if ((${#missing_dependencies[@]} > 0)); then
    printf '❌ Missing required command(s): %s\n' "${missing_dependencies[*]}"
    exit 1
fi

echo "=== CI Performance Metrics ==="
echo

runs_json="$({
    gh run list \
        --workflow=ci.yml \
        --limit 10 \
        --json conclusion,createdAt,headBranch,startedAt,updatedAt
})"

successful_run_count="$(
    jq '[
        .[]
        | select(
            .conclusion == "success"
            and (.startedAt | type) == "string"
            and (.updatedAt | type) == "string"
        )
    ] | length' <<<"${runs_json}"
)"

echo "📊 Successful runs among the last 10 CI runs:"
echo

if ((successful_run_count == 0)); then
    echo "No successful runs with complete timing data were found."
    average_duration="n/a"
else
    jq -r '
        .[]
        | select(
            .conclusion == "success"
            and (.startedAt | type) == "string"
            and (.updatedAt | type) == "string"
        )
        | ((.updatedAt | fromdateiso8601) - (.startedAt | fromdateiso8601)) as $duration_seconds
        | "\(.createdAt | split("T")[0]) | \(.headBranch) | \($duration_seconds / 60 | floor) min"
    ' <<<"${runs_json}" | column -t -s '|'

    average_duration="$(
        jq -r '[
            .[]
            | select(
                .conclusion == "success"
                and (.startedAt | type) == "string"
                and (.updatedAt | type) == "string"
            )
            | ((.updatedAt | fromdateiso8601) - (.startedAt | fromdateiso8601)) / 60
        ] | add / length | floor' <<<"${runs_json}"
    ) minutes"
fi

echo
echo "📈 Average CI time (successful runs in this sample): ${average_duration}"

echo
echo "🎯 Target metrics:"
echo "  - Warm cache: ≤15 min"
echo "  - Cold cache: ≤20 min"

echo
echo "🔍 Check specific run details:"
echo "  gh run view <run-id> --log | grep 'Cache restored' | wc -l"
echo "  gh run view <run-id> --web"
