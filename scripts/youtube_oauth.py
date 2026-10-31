#!/usr/bin/env python3
"""One-time YouTube OAuth for Synergy — run this on the bot server.

Prints an authorize URL, you approve it once in a browser, paste back the code
(or the whole redirected URL), and it writes a refresh token file you can feed
to GitHub Actions or to a cron job on this box.

Only uses the Python standard library — nothing to install.

  python3 youtube_oauth.py --out /var/lib/synergy-bot/youtube.json

You'll need your own OAuth client first (Google Cloud → Credentials → Create
credentials → OAuth client ID → Application type: Desktop app). If you haven't
got one yet, the script will explain that and stop.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import urllib.parse
import urllib.request

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_ME = "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true"
SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"

# Desktop clients accept a loopback redirect on any port. We never actually
# listen on it — after consent the browser shows "can't reach this page" and the
# code sits in the address bar, which is what you paste back.
REDIRECT_URI = "http://localhost"


def ask(prompt: str, secret: bool = False) -> str:
    if secret:
        try:
            import getpass

            return getpass.getpass(prompt).strip()
        except Exception:
            pass
    return input(prompt).strip()


def build_auth_url(client_id: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        # prompt=consent forces a refresh_token even if this account approved before
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def extract_code(raw: str) -> str:
    """Accept either the bare code or the entire redirected URL."""
    raw = raw.strip()
    if "code=" in raw:
        query = urllib.parse.urlparse(raw).query or raw.split("?", 1)[-1]
        params = urllib.parse.parse_qs(query)
        if "code" in params:
            return params["code"][0]
    return raw


def post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        try:
            parsed = json.loads(detail)
            msg = parsed.get("error_description") or parsed.get("error") or detail
        except Exception:
            msg = detail
        raise SystemExit(f"❌ Google rejected the exchange: {msg}")


def whoami(access_token: str) -> str | None:
    req = urllib.request.Request(
        YOUTUBE_ME, headers={"Authorization": "Bearer " + access_token}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            data = json.loads(res.read().decode())
        items = data.get("items") or []
        if not items:
            return None
        snippet = items[0].get("snippet") or {}
        return f"{snippet.get('title')} ({snippet.get('customUrl') or items[0].get('id')})"
    except Exception:
        return None


def write_private(path: str, payload: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description="One-time YouTube OAuth for Synergy")
    ap.add_argument(
        "--out",
        default="/var/lib/synergy-bot/youtube.json",
        help="where to write the token file (default: /var/lib/synergy-bot/youtube.json)",
    )
    ap.add_argument("--client-id", help="OAuth client ID (otherwise you'll be asked)")
    ap.add_argument("--client-secret", help="OAuth client secret (otherwise you'll be asked)")
    ap.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the 'which channel did I authorize' check",
    )
    args = ap.parse_args()

    print("Synergy — YouTube OAuth setup")
    print("=" * 40)

    client_id = (args.client_id or os.getenv("YT_CLIENT_ID") or "").strip()
    if not client_id:
        client_id = ask("Client ID: ")
    client_secret = (args.client_secret or os.getenv("YT_CLIENT_SECRET") or "").strip()
    if not client_secret:
        client_secret = ask("Client secret (hidden): ", secret=True)

    if not client_id.endswith(".apps.googleusercontent.com"):
        print(
            "\n⚠️  That doesn't look like an OAuth client ID — it normally ends with "
            ".apps.googleusercontent.com.\n"
            "    Create one: console.cloud.google.com → Credentials → Create credentials →\n"
            "    OAuth client ID → Application type: Desktop app."
        )
        if ask("Continue anyway? [y/N]: ").lower() not in ("y", "yes"):
            return 1

    url = build_auth_url(client_id)

    print("\n1. Open this URL in any browser (it can be on another machine):\n")
    print(url)
    print(
        "\n2. Sign in as the account that owns the YouTube channel you want comments from."
        "\n   If Google offers a brand/channel picker, choose that channel."
        '\n   On "Google hasn\'t verified this app" → Advanced → Go to … (unsafe) → Allow.'
        "\n   You'll land on a page that can't load (localhost) — that's expected."
        "\n3. Copy the address bar, or just the code= value, and paste it below.\n"
    )

    raw = ask("Code (or full redirected URL): ")
    code = extract_code(raw)
    if not code:
        print("❌ No code supplied.")
        return 1

    print("\nExchanging the code for tokens…")
    tokens = post_form(
        TOKEN_URL,
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
    )

    refresh = tokens.get("refresh_token")
    access = tokens.get("access_token")
    if not refresh:
        print(
            "❌ Google didn't return a refresh token.\n"
            "   Usually this means the account already granted access to this client.\n"
            "   Fix: revoke it at https://myaccount.google.com/permissions and run this again."
        )
        return 1

    if not args.no_verify and access:
        channel = whoami(access)
        if channel:
            print(f"✅ Authorized channel: {channel}")
        else:
            print("⚠️  Could not read the channel name — the token may lack the YouTube scope.")

    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh,
        "scope": SCOPE,
        "obtained_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
    }
    write_private(args.out, payload)

    print(f"\n✅ Wrote {args.out} (mode 600)")
    print(
        "\nNext:\n"
        "  • GitHub Actions: copy these three values into repository secrets —\n"
        f"      YT_CLIENT_ID      = {client_id}\n"
        "      YT_CLIENT_SECRET  = (the secret you just entered)\n"
        "      YT_REFRESH_TOKEN  = (refresh_token inside the file above)\n"
        "  • Or run the commenter on this box with a cron/systemd timer."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
