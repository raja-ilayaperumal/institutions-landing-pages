"""Upload all institution JSON files to a Cloudflare Workers KV namespace.

Each file in `data/json/*.json` becomes one KV pair:
  key   = institution slug (e.g. "massachusetts-institute-of-technology")
  value = JSON contents (full document as a string)

The frontend can then read `https://<account>.workers.dev/?slug=<slug>`
(or whichever Worker route) and pull the JSON directly from KV.

Uses Cloudflare's bulk-write endpoint so all 101 files go up in a single
authenticated request (max 10k pairs / 100 MiB per call — we use 101 / ~2 MiB).

Env vars required (in .env):
  CLOUDFLARE_KV_API_TOKEN                          — token w/ "Workers KV Storage:Edit"
  CLOUDFLARE_KV_INSTITUTIONS_LANDING_PAGES_NS_ID   — KV namespace ID
  CLOUDFLARE_ACCOUNT_ID                            — optional; auto-discovered from token if absent

Usage:
  python scripts/upload_to_cloudflare_kv.py            # upload all
  python scripts/upload_to_cloudflare_kv.py --dry-run  # show what would upload
  python scripts/upload_to_cloudflare_kv.py --verify   # list keys after upload
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click
import httpx
from dotenv import load_dotenv


load_dotenv()

CF_API = "https://api.cloudflare.com/client/v4"


def _require(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        click.echo(f"❌ Missing env var: {name}", err=True)
        click.echo(
            "   Add it to .env (see scripts/upload_to_cloudflare_kv.py header).",
            err=True,
        )
        sys.exit(2)
    return v


def _discover_account_id(api_token: str) -> str:
    """If CLOUDFLARE_ACCOUNT_ID isn't set, ask the API which account this
    token is scoped to. Returns the only account ID, or fails if the
    token has access to multiple (in which case caller must set it
    explicitly to remove ambiguity).
    """
    headers = {"Authorization": f"Bearer {api_token}",
               "Content-Type": "application/json"}
    with httpx.Client(timeout=15.0) as c:
        r = c.get(f"{CF_API}/accounts", headers=headers)
    if r.status_code != 200:
        click.echo(
            f"❌ Account auto-discovery failed (HTTP {r.status_code}): "
            f"{r.text[:200]}\n"
            "   Set CLOUDFLARE_ACCOUNT_ID in .env to skip this lookup.",
            err=True,
        )
        sys.exit(2)
    accounts = r.json().get("result") or []
    if not accounts:
        click.echo(
            "❌ Token has no account access. Verify the token's permissions.",
            err=True,
        )
        sys.exit(2)
    if len(accounts) > 1:
        names = ", ".join(a["name"] for a in accounts)
        click.echo(
            f"❌ Token has access to multiple accounts ({names}). "
            f"Set CLOUDFLARE_ACCOUNT_ID in .env explicitly.",
            err=True,
        )
        sys.exit(2)
    a = accounts[0]
    click.echo(f"✓ Auto-discovered account: {a['name']} ({a['id']})")
    return a["id"]


def _load_pairs(json_dir: Path) -> list[dict]:
    """Build the KV bulk-write payload from every *.json in json_dir."""
    pairs = []
    for fp in sorted(json_dir.glob("*.json")):
        # Use the filename stem as the key — that's the institution slug
        # (matches what the dynamic renderer uses for ?slug=… lookups).
        try:
            content = fp.read_text(encoding="utf-8")
            # Validate it parses so we don't upload corrupt JSON
            json.loads(content)
        except Exception as e:
            click.echo(f"  ✗ skipping {fp.name}: {e}", err=True)
            continue
        pairs.append({"key": fp.stem, "value": content})
    return pairs


@click.command()
@click.option("--json-dir", default="data/json", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--dry-run", is_flag=True, help="Show what would upload, don't call CF")
@click.option("--verify", is_flag=True, help="After upload, list KV keys and counts")
def main(json_dir: Path, dry_run: bool, verify: bool) -> None:
    api_token = _require("CLOUDFLARE_KV_API_TOKEN")
    namespace_id = _require("CLOUDFLARE_KV_INSTITUTIONS_LANDING_PAGES_NS_ID")
    account_id = (
        os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        or _discover_account_id(api_token)
    )

    pairs = _load_pairs(json_dir)
    total_size = sum(len(p["value"]) for p in pairs)
    click.echo(
        f"Found {len(pairs)} JSON files in {json_dir} "
        f"({total_size / 1024:.0f} KB total).",
    )

    if dry_run:
        click.echo("\nDry run — first 5 keys that would be written:")
        for p in pairs[:5]:
            click.echo(f"  • {p['key']:<55} {len(p['value']) // 1024:>4} KB")
        click.echo(f"  ... and {len(pairs) - 5} more")
        return

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    url = f"{CF_API}/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/bulk"

    click.echo(f"\nPUT {url}")
    click.echo(f"Uploading {len(pairs)} KV pairs ...")

    # CF bulk-write is one shot — no streaming needed for 101 pairs.
    # The endpoint accepts up to 10,000 pairs and 100 MiB per call.
    with httpx.Client(timeout=120.0) as client:
        resp = client.put(url, headers=headers, json=pairs)
    if resp.status_code != 200:
        click.echo(f"❌ HTTP {resp.status_code}: {resp.text[:500]}", err=True)
        sys.exit(1)

    result = resp.json()
    if not result.get("success"):
        click.echo(f"❌ Cloudflare rejected the write:", err=True)
        for err in result.get("errors", []):
            click.echo(f"   • {err}", err=True)
        sys.exit(1)

    click.echo(f"✅ Uploaded {len(pairs)} KV pairs to namespace {namespace_id}")

    if verify:
        click.echo("\nVerifying by listing keys...")
        list_url = (
            f"{CF_API}/accounts/{account_id}/storage/kv/namespaces/"
            f"{namespace_id}/keys?limit=1000"
        )
        with httpx.Client(timeout=60.0) as client:
            r = client.get(list_url, headers=headers)
        if r.status_code != 200:
            click.echo(f"  list failed: HTTP {r.status_code}: {r.text[:200]}", err=True)
            return
        data = r.json()
        keys = data.get("result", [])
        click.echo(f"  ✅ Namespace contains {len(keys)} keys")
        for k in keys[:5]:
            click.echo(f"     • {k['name']}")
        if len(keys) > 5:
            click.echo(f"     ... and {len(keys) - 5} more")


if __name__ == "__main__":
    main()
