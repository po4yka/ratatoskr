#!/bin/sh
set -eu

# CloakBrowser 0.3.30 correctly parses --headless=false, but also forwards the
# same flag to Chromium. Chromium treats the mere presence of --headless as
# enabling headless mode, so the headed process exits with "Multiple targets
# are not supported in headless mode". Patch only that pinned, verified parser
# defect in a temporary copy; fail closed if the upstream source changes.
python - <<'PY'
from pathlib import Path

source = Path("/usr/local/bin/cloakserve")
target = Path("/tmp/ratatoskr-cloakserve-headed")
text = source.read_text(encoding="utf-8")
bug = '''        elif arg == "--headless=false" or arg == "--headless=False":
            config["headless"] = False
            passthrough.append(arg)
'''
fixed = '''        elif arg == "--headless=false" or arg == "--headless=False":
            config["headless"] = False
'''
if text.count(bug) != 1:
    raise SystemExit("unsupported CloakBrowser cloakserve parser; refusing to start")
target.write_text(text.replace(bug, fixed), encoding="utf-8")
PY

exec python /tmp/ratatoskr-cloakserve-headed "$@"
