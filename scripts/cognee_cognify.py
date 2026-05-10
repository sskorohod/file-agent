"""Trigger cognee cognify on `main_dataset` without the asyncio + httpx
event-loop-closure dance that kills `cognee_reingest_all.py`.

Used after `cognee_reingest_all.py` has added all 207 sources. Cognify
walks them, extracts entities / relations, builds the graph + LanceDB
vectors. Long-running (30-90 min for 200 sources) — we kick it off
``run_in_background=true`` and exit immediately. Sidecar log
(`infra/cognee/logs/cognee.log`) shows progress.

Usage:
    .venv/bin/python scripts/cognee_cognify.py
    .venv/bin/python scripts/cognee_cognify.py --foreground   # block + print result
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

import dotenv
dotenv.load_dotenv("/Users/vskorokhod/fag/.env", override=True)

DEFAULT_USER_EMAIL = "default_user@example.com"
DEFAULT_USER_PASSWORD = "default_password"


def _login(base_url: str) -> str:
    """Acquire bearer token by logging in as the default user."""
    body = (
        f"username={DEFAULT_USER_EMAIL}&password={DEFAULT_USER_PASSWORD}"
        "&grant_type=password"
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/v1/auth/login",
        data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())["access_token"]


def _post(base_url: str, path: str, token: str, payload: dict, timeout: int):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=body, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8765")
    ap.add_argument("--dataset", default="main_dataset")
    ap.add_argument(
        "--foreground", action="store_true",
        help="block until cognify returns (default: kick off and exit)",
    )
    args = ap.parse_args()

    try:
        token = _login(args.base_url)
    except urllib.error.URLError as exc:
        print(f"✗ login failed: {exc}", file=sys.stderr)
        return 2

    payload = {
        # cognee 1.0.x rejects datasetName-singular, expects an array
        "datasets": [args.dataset],
        "runInBackground": not args.foreground,
    }
    try:
        result = _post(
            args.base_url, "/api/v1/cognify",
            token, payload,
            timeout=20 if not args.foreground else 1800,
        )
    except urllib.error.HTTPError as exc:
        print(f"✗ cognify HTTP {exc.code}: {exc.read()[:500].decode()}",
              file=sys.stderr)
        return 3
    except urllib.error.URLError as exc:
        print(f"✗ cognify failed: {exc}", file=sys.stderr)
        return 3

    print(json.dumps(result, ensure_ascii=False, indent=2)[:1000])
    if args.foreground:
        print("\n✓ cognify finished (foreground).")
    else:
        print("\n✓ cognify dispatched in background.")
        print("  watch: tail -f infra/cognee/logs/cognee.log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
