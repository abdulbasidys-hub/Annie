"""Copy Annie's memory from the running deployment down to your machine.

    python -m tools.memory_pull                      # -> ./memory-pulled/
    python -m tools.memory_pull --out ~/annie-memory
    python -m tools.memory_pull --section core --section playbook
    python -m tools.memory_pull --search 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU

Memory lives on the server's volume, which you cannot open directly. This
walks the memory API and writes the same markdown files into a local folder,
preserving the section layout — so `core/market-model.md` on the server
becomes `core/market-model.md` on your disk, and you can read the whole
notebook in any editor.

Reads only. It never writes to the deployment.

Configuration comes from the same ``.env`` the app uses:

    VITE_API_BASE_URL   the deployment's URL (or pass --url)
    AUTH_USERNAME       the login you use on the website
    AUTH_PASSWORD

The `--search` mode is the quick one: pass a contract address, a creator
wallet or a phrase and it prints matching excerpts to the terminal instead of
downloading anything.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover - a clearer message than a traceback
    print("This needs httpx: pip install httpx", file=sys.stderr)
    raise SystemExit(2)


def _load_env(path: Path) -> dict[str, str]:
    """Minimal .env reader — no dependency on pydantic-settings for a CLI."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _login(client: httpx.Client, base: str, username: str, password: str) -> str:
    response = client.post(
        f"{base}/api/auth/login", json={"username": username, "password": password}, timeout=30
    )
    if response.status_code != 200:
        raise SystemExit(
            f"Login failed ({response.status_code}). Check AUTH_USERNAME / AUTH_PASSWORD "
            f"against the deployment you are pointing at ({base})."
        )
    token = (response.json() or {}).get("token")
    if not token:
        raise SystemExit("Login succeeded but returned no token.")
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull Annie's memory files locally.")
    parser.add_argument("--url", help="Deployment base URL. Defaults to VITE_API_BASE_URL.")
    parser.add_argument("--out", default="memory-pulled", help="Local directory to write into.")
    parser.add_argument(
        "--section",
        action="append",
        help="Only these sections (repeatable). Default: all.",
    )
    parser.add_argument(
        "--search",
        help="Print matching excerpts instead of downloading. Takes a CA, wallet, or phrase.",
    )
    parser.add_argument("--env", default=".env", help="Path to the .env file to read.")
    args = parser.parse_args()

    env = {**_load_env(Path(args.env)), **os.environ}
    base = (args.url or env.get("VITE_API_BASE_URL") or "").rstrip("/")
    if not base:
        raise SystemExit("No deployment URL. Pass --url or set VITE_API_BASE_URL in .env.")

    username = env.get("AUTH_USERNAME", "")
    password = env.get("AUTH_PASSWORD", "")
    if not username or not password:
        raise SystemExit("AUTH_USERNAME and AUTH_PASSWORD must be set in .env (or the environment).")

    with httpx.Client(timeout=60) as client:
        token = _login(client, base, username, password)
        headers = {"Authorization": f"Bearer {token}"}

        if args.search:
            found = client.get(
                f"{base}/api/memory/search",
                params={"q": args.search, "limit": 15},
                headers=headers,
            ).json()
            print(f"\n{found['count']} result(s) — matched by {found['matched_by']}\n")
            for hit in found["hits"]:
                print(f"  {hit['path']}  ({hit['section']})")
                print(f"    {hit['title']}")
                print(f"    {hit['snippet']}\n")
            if not found["count"]:
                print("  Nothing in memory covers this yet.\n")
            return 0

        tree = client.get(f"{base}/api/memory", headers=headers).json()
        out_root = Path(args.out).expanduser()
        wanted = set(args.section) if args.section else None

        written = 0
        for section in tree["sections"]:
            if wanted and section["name"] not in wanted:
                continue
            for entry in section["files"]:
                # Ask for the raw form so what lands on disk is byte-identical
                # to what the server holds, frontmatter and all — a pulled copy
                # that quietly differs from the original is worse than none.
                response = client.get(
                    f"{base}/api/memory/file",
                    params={"path": entry["path"], "raw": "true"},
                    headers=headers,
                )
                if response.status_code != 200:
                    print(f"  ! skipped {entry['path']} ({response.status_code})", file=sys.stderr)
                    continue
                target = out_root / entry["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(response.text, encoding="utf-8", newline="\n")
                written += 1
                print(f"  {entry['path']}")

        index = tree.get("index") or {}
        print(
            f"\n{written} file(s) written to {out_root.resolve()}\n"
            f"Server holds {tree['total_files']} file(s), {index.get('keys', '?')} lookup keys."
        )
        durability = tree.get("durability") or {}
        if durability and not durability.get("looks_like_volume"):
            print(f"\nNote: {durability.get('note')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
