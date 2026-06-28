"""
Operator session-capture helper for AI account backup.

This script launches (or connects to) a real browser via Playwright and saves
the resulting storage_state JSON so the operator can POST it to
/v1/ai-backups/{service}/session.

WHY a real browser is required
--------------------------------
The critical session cookies — ChatGPT's ``__Secure-next-auth.session-token``
and Claude's ``sessionKey`` — are marked **httpOnly**.  The browser never
exposes them to JavaScript, so a bookmarklet (``document.cookie``) cannot read
them.  Only the browser process itself (via the CDP ``Network.getCookies``
domain) can export them, and that is exactly what Playwright's
``context.storage_state()`` uses under the hood.

cf_clearance and fingerprint/IP binding
-----------------------------------------
Cloudflare's ``cf_clearance`` cookie is bound to a combination of the
**browser fingerprint** (TLS JA3/JA4, HTTP/2 settings, browser version) and
the **originating IP**.  A blob captured in the operator's desktop Chrome and
then replayed by the Ratatoskr sidecar container will frequently be
re-challenged because the fingerprint and/or IP differs.

If you are targeting a deployment that runs a CloakBrowser sidecar, prefer
**Mode B** (``--cdp``): point this script at the sidecar's CDP endpoint so the
session capture happens *inside* the sidecar process.  That guarantees the
``cf_clearance`` fingerprint and IP already match the environment that will
replay the cookies, dramatically reducing re-challenge rate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVICE_CONFIG: dict[str, dict[str, str]] = {
    "chatgpt": {
        "login_url": "https://chatgpt.com/",
        "session_cookie": "__Secure-next-auth.session-token",
        "display_name": "ChatGPT",
    },
    "claude": {
        "login_url": "https://claude.ai/",
        "session_cookie": "sessionKey",
        "display_name": "Claude",
    },
}

CF_CLEARANCE_COOKIE = "cf_clearance"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Capture a browser storage_state JSON for an AI service session. "
            "The JSON is suitable for POSTing to /v1/ai-backups/{service}/session."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--service",
        required=True,
        choices=list(SERVICE_CONFIG.keys()),
        help="Which AI service to capture a session for.",
    )
    parser.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help=(
            "Output path for the storage_state JSON. Defaults to ./<service>-storage_state.json."
        ),
    )
    parser.add_argument(
        "--cdp",
        default=None,
        metavar="URL",
        help=(
            "CDP WebSocket endpoint to attach to an existing browser "
            "(e.g. http://localhost:9222 or a CloakBrowser sidecar URL). "
            "When omitted a local headful Chromium is launched."
        ),
    )
    return parser


def _print_summary(out_path: Path, session_cookie_name: str) -> bool:
    """Re-read the saved file, print cookie names, return True if session cookie present."""
    try:
        data = json.loads(out_path.read_text())
    except Exception as exc:
        print(f"[ERROR] Could not re-read {out_path}: {exc}", file=sys.stderr)
        return False

    cookies: list[dict] = data.get("cookies", [])
    names = [c.get("name", "") for c in cookies]

    print()
    print(f"Saved {len(cookies)} cookie(s) to: {out_path}")
    print("Cookie names found:")
    for name in sorted(names):
        print(f"  {name}")

    session_present = session_cookie_name in names
    cf_present = CF_CLEARANCE_COOKIE in names

    print()
    print(
        f"Expected session cookie ({session_cookie_name!r}): "
        + ("PRESENT" if session_present else "MISSING")
    )
    print(
        f"Cloudflare clearance ({CF_CLEARANCE_COOKIE!r}): "
        + ("present" if cf_present else "not found")
    )

    print()
    print("=" * 72)
    print("SECURITY REMINDER")
    print("=" * 72)
    print(f"  {out_path} contains live session cookies.")
    print("  1. POST it to the /v1/ai-backups/{service}/session endpoint over HTTPS.")
    print("  2. Delete the file immediately afterwards.")
    print("  3. NEVER commit it to version control or share it.")
    print("=" * 72)

    return session_present


# ---------------------------------------------------------------------------
# Main async entrypoint
# ---------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    cfg = SERVICE_CONFIG[args.service]
    out_path = Path(args.out) if args.out else Path(f"{args.service}-storage_state.json")
    login_url: str = cfg["login_url"]
    session_cookie_name: str = cfg["session_cookie"]
    display_name: str = cfg["display_name"]

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = None
        context = None

        if args.cdp:
            # Mode B: connect to an existing browser over CDP
            print(f"[Mode B] Connecting to CDP endpoint: {args.cdp}")
            print(
                "NOTE: Mode B requires you to SEE the browser "
                "(noVNC / display attached to the sidecar) to log in."
            )
            browser = await p.chromium.connect_over_cdp(args.cdp)
            existing_contexts = browser.contexts
            if existing_contexts:
                context = existing_contexts[0]
                print(f"Reusing existing browser context ({len(existing_contexts)} found).")
            else:
                context = await browser.new_context()
                print("Created a new browser context.")
        else:
            # Mode A: launch a local headful Chromium
            print("[Mode A] Launching a local headful Chromium browser.")
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context()

        page = await context.new_page()

        print(f"\nNavigating to {display_name} login page: {login_url}")
        await page.goto(login_url)

        print()
        print("=" * 72)
        print(f"ACTION REQUIRED — {display_name}")
        print("=" * 72)
        print(
            "  Log in fully (including any 2FA) in the opened browser window,"
            "\n  then return here and press Enter to capture the session."
        )
        print("=" * 72)

        try:
            await asyncio.to_thread(input, "\n  [Press Enter when you have fully logged in] ")
        except EOFError:
            # Non-interactive environment; proceed anyway
            pass

        print("\nCapturing storage state ...")
        await context.storage_state(path=str(out_path))
        print(f"Storage state written to: {out_path}")

        if browser is not None and not args.cdp:
            await browser.close()

    session_present = _print_summary(out_path, session_cookie_name)
    return 0 if session_present else 1


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
