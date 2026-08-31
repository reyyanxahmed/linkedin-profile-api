#!/usr/bin/env python3
"""Refresh the LinkedIn session everywhere, from one cookie export.

LinkedIn sessions expire, get challenged, and rotate. When the live API starts
returning PROFILE_NOT_FOUND, the fix is almost always "the cookies are stale" —
and doing that by hand means editing .env, re-encoding JSON, updating the host's
environment, and redeploying, with four chances to paste something wrong.

Usage:
    # From a file containing the browser cookie export (a JSON array):
    python scripts/update_session.py cookies.json

    # Or from stdin:
    pbpaste | python scripts/update_session.py -

    # Update .env only, skip the deployment:
    python scripts/update_session.py cookies.json --local-only

It writes .env, clears the stale rotated-cookie cache, verifies the session
against LinkedIn with a single request, and only then pushes to Vercel and
redeploys. If verification fails it stops and says so, rather than deploying
credentials that are already dead.

Never prints a cookie value.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

# Cookies LinkedIn's edge needs beyond the two auth ones. Missing lidc/bcookie is
# what makes every identity endpoint self-redirect until curl gives up.
REQUIRED = {"li_at", "JSESSIONID"}
RECOMMENDED = {"lidc", "bcookie", "liap"}


def load_cookies(source: str) -> list[dict]:
    raw = sys.stdin.read() if source == "-" else Path(source).read_text()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"error: input is not valid JSON ({e}). Export cookies as a JSON array.")
    if not isinstance(data, list):
        sys.exit("error: expected a JSON array of cookie objects.")

    cookies = [
        {"name": str(c["name"]), "value": str(c["value"]), "domain": str(c.get("domain", ""))}
        for c in data
        if isinstance(c, dict) and "name" in c and "value" in c
    ]
    names = {c["name"] for c in cookies}

    missing = REQUIRED - names
    if missing:
        sys.exit(f"error: cookie export is missing {sorted(missing)}. Re-export while logged in.")
    thin = RECOMMENDED - names
    if thin:
        print(f"  warning: no {sorted(thin)} — identity endpoints may redirect-loop.")
    return cookies


def write_env(cookies: list[dict]) -> None:
    value = json.dumps([{"cookies": cookies}], separators=(",", ":"))
    text = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    line = "LI_SESSIONS=" + value
    if re.search(r"^LI_SESSIONS=.*$", text, re.M):
        text = re.sub(r"^LI_SESSIONS=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    ENV_PATH.write_text(text)
    print(f"  .env updated ({len(cookies)} cookies)")


def clear_rotated_state() -> None:
    """Drop the persisted rotation state; it belongs to the session just replaced."""
    for path in (ROOT / ".cookie_state.json",):
        if path.exists():
            path.unlink()
            print(f"  cleared {path.name}")


def verify() -> bool:
    """Spend exactly one request confirming the session authenticates."""
    import asyncio

    sys.path.insert(0, str(ROOT))
    from app.config import Settings
    from app.linkedin.client import LinkedInClient
    from app.linkedin.session import SessionPool

    settings = Settings()
    pool = SessionPool.from_raw(settings.sessions)
    client = LinkedInClient(settings=settings, pool=pool)

    async def check() -> bool:
        resp = await client.fetch("https://www.linkedin.com/voyager/api/me")
        print(f"  GET /voyager/api/me -> {resp.status} ({resp.outcome.value})")
        return resp.is_ok()

    return asyncio.run(check())


def deploy() -> None:
    value = json.dumps(
        [{"cookies": json.loads(re.search(r"^LI_SESSIONS=(.*)$", ENV_PATH.read_text(), re.M).group(1))[0]["cookies"]}],
        separators=(",", ":"),
    )
    print("  pushing LI_SESSIONS to Vercel...")
    subprocess.run(
        ["vercel", "env", "rm", "LI_SESSIONS", "production", "--yes"],
        cwd=ROOT, capture_output=True, check=False,
    )
    add = subprocess.run(
        ["vercel", "env", "add", "LI_SESSIONS", "production"],
        cwd=ROOT, input=value, text=True, capture_output=True, check=False,
    )
    if add.returncode != 0:
        sys.exit(f"error: vercel env add failed:\n{add.stderr[:400]}")

    print("  redeploying...")
    dep = subprocess.run(
        ["vercel", "deploy", "--prod", "--yes"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    if dep.returncode != 0:
        sys.exit(f"error: deploy failed:\n{dep.stderr[-600:]}")
    for line in dep.stdout.splitlines():
        if "Aliased:" in line or "Production:" in line:
            print("  " + line.strip())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", help="path to the cookie JSON export, or - for stdin")
    ap.add_argument("--local-only", action="store_true", help="update .env but do not deploy")
    ap.add_argument("--skip-verify", action="store_true", help="do not spend a request verifying")
    args = ap.parse_args()

    print("Refreshing LinkedIn session")
    cookies = load_cookies(args.source)
    write_env(cookies)
    clear_rotated_state()

    if not args.skip_verify:
        if not verify():
            sys.exit(
                "\nThe new cookies do not authenticate.\n"
                "  401 -> the session is expired; log in again and re-export.\n"
                "  999 -> the account is challenged; open linkedin.com in the browser,\n"
                "         clear any checkpoint, then re-export.\n"
                "Nothing was deployed."
            )
        print("  session verified")

    if args.local_only:
        print("\nDone (local only).")
        return

    deploy()
    print("\nDone. Live: https://linkedin-profile-api-green.vercel.app/v1/health")


if __name__ == "__main__":
    main()
