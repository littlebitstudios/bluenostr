#!/usr/bin/env python3
"""
bluenostr-convert — Fetch a single Bluesky post by URL and publish it as a
Nostr kind 1 (text note) event.

Usage:
    bluenostr-convert https://bsky.app/profile/<actor>/post/<rkey>

Config is read from the same YAML file / env vars as the main bluenostr tool.
"""

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
import datetime

import requests
import yaml
from nostr.event import Event, EventKind
from nostr.key import PrivateKey
from websockets.sync.client import connect

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_FILE = os.path.expanduser("~/.littlebitstudios/bluenostr/config.yaml")


def get_config() -> dict:
    if os.environ.get("BLUENOSTR_USE_ENV") == "1":
        return {
            "nostr-sec-key": os.environ.get("BLUENOSTR_NSEC_KEY"),
            "bsky-subject": os.environ.get("BLUENOSTR_BSKY_SUBJECT"),
            "nostr-relays": os.environ.get("BLUENOSTR_RELAYS", "").split(","),
            "bsky-stream-endpoint": os.environ.get("BLUENOSTR_JETSTREAM_ENDPOINT"),
            "blossom-server": os.environ.get("BLUENOSTR_BLOSSOM_SERVER"),
        }
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return yaml.safe_load(f) or {}
    # Create empty config file so the user knows where to put their key.
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    open(CONFIG_FILE, "x").close()
    return {}


# ---------------------------------------------------------------------------
# Blossom image re-hosting
# ---------------------------------------------------------------------------


def upload_image_to_blossom(
    image_data: bytes,
    mime_type: str,
    nostr_account: PrivateKey,
    server_url: str = "https://blossom.primal.net",
) -> str | None:
    file_hash = hashlib.sha256(image_data).hexdigest()

    auth_event = Event(
        public_key=nostr_account.public_key.hex(),
        content="Upload image",
        created_at=int(time.time()),
        kind=24242,
        tags=[
            ["t", "upload"],
            ["x", file_hash],
            ["expiration", str(int(time.time()) + 60)],
        ],
    )
    nostr_account.sign_event(auth_event)

    auth_header = (
        "Nostr "
        + base64.urlsafe_b64encode(
            json.dumps(
                {
                    "id": auth_event.id,
                    "pubkey": auth_event.public_key,
                    "created_at": auth_event.created_at,
                    "kind": auth_event.kind,
                    "tags": auth_event.tags,
                    "content": auth_event.content,
                    "sig": auth_event.signature,
                }
            ).encode()
        ).decode()
    )

    ext = mimetypes.guess_extension(mime_type) or ".jpg"
    try:
        resp = requests.put(
            f"{server_url}/upload",
            headers={"Authorization": auth_header, "Content-Type": mime_type},
            data=image_data,
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        return result.get("url") or f"{server_url}/{file_hash}"
    except Exception as e:
        print(f"Blossom upload failed: {e}")
        return None


def download_and_rehost_image(
    img_url: str, nostr_account: PrivateKey, blossom_server: str
) -> str:
    try:
        resp = requests.get(img_url, timeout=15)
        resp.raise_for_status()
        mime_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        new_url = upload_image_to_blossom(
            resp.content, mime_type, nostr_account, blossom_server
        )
        if new_url:
            print(f"  Re-hosted image: {img_url[:50]}... -> {new_url}")
            return new_url
    except Exception as e:
        print(f"  Image re-host failed ({e}), using original URL.")
    return img_url


# ---------------------------------------------------------------------------
# Nostr publishing
# ---------------------------------------------------------------------------


def publish_to_nostr(event: Event, relays: list[str]) -> None:
    event_json = event.to_message()
    for relay_url in relays:
        try:
            with connect(relay_url) as ws:
                print(f"  Publishing to {relay_url}...")
                ws.send(event_json)
                start = time.time()
                while time.time() - start < 5:
                    try:
                        response = ws.recv(timeout=2)
                        if response:
                            print(f"  Relay response: {response}")
                            resp_data = json.loads(response)
                            if resp_data[0] in ("OK", "NOTICE"):
                                break
                    except Exception:
                        continue
        except Exception as e:
            print(f"  Failed to publish to {relay_url}: {e}")


# ---------------------------------------------------------------------------
# Post URL parsing
# ---------------------------------------------------------------------------

_BSKY_URL_RE = re.compile(
    r"https?://bsky\.app/profile/(?P<actor>[^/]+)/post/(?P<rkey>[A-Za-z0-9]+)"
)


def parse_bsky_url(url: str) -> tuple[str, str]:
    """Return (actor, rkey) from a bsky.app post URL, or raise ValueError."""
    m = _BSKY_URL_RE.match(url.strip())
    if not m:
        raise ValueError(
            f"URL does not look like a Bluesky post link: {url!r}\n"
            "Expected format: https://bsky.app/profile/<actor>/post/<rkey>"
        )
    return m.group("actor"), m.group("rkey")


# ---------------------------------------------------------------------------
# Bluesky AppView fetch
# ---------------------------------------------------------------------------


def fetch_bsky_post(actor: str, rkey: str) -> dict:
    """
    Fetch a single post record from the Bluesky public AppView.
    Returns the raw ATProto record dict, plus a synthetic 'did' key.
    """
    # Resolve handle → DID if needed (getProfile works for both)
    profile_resp = requests.get(
        "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile",
        params={"actor": actor},
        timeout=15,
    )
    profile_resp.raise_for_status()
    profile = profile_resp.json()
    did = profile["did"]

    # Fetch the post thread so we get the full record
    at_uri = f"at://{did}/app.bsky.feed.post/{rkey}"
    thread_resp = requests.get(
        "https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread",
        params={"uri": at_uri, "depth": 0, "parentHeight": 0},
        timeout=15,
    )
    thread_resp.raise_for_status()
    thread_data = thread_resp.json()

    post_view = thread_data["thread"]["post"]
    record = post_view["record"]  # the raw ATProto record
    record["__did__"] = did  # stash DID for image URL construction
    record["__rkey__"] = rkey
    return record


# ---------------------------------------------------------------------------
# Core: convert an ATProto record → Nostr content string
# ---------------------------------------------------------------------------


def build_nostr_content(
    record: dict,
    nostr_account: PrivateKey,
    blossom_server: str,
) -> str:
    """
    Turn a raw app.bsky.feed.post record into a Nostr note body.
    Handles: plain text, facet links, images, quote posts, link previews/GIFs.
    Replies are allowed here (unlike the stream version) — caller decides policy.
    """
    did = record.get("__did__", "")
    content = record.get("text", "")

    # --- Facets: collect embedded links and append them ----------------------
    if "facets" in record:
        links = []
        for facet in record["facets"]:
            for feature in facet.get("features", []):
                if feature.get("$type") == "app.bsky.richtext.facet#link":
                    links.append(feature["uri"])
        if links:
            content += "\n\nLinks: " + " ".join(links)

    # --- Embeds --------------------------------------------------------------
    embed = record.get("embed", {})
    if embed:
        etype = embed.get("$type", "")

        if etype == "app.bsky.embed.images":
            content += "\n"
            for img in embed.get("images", []):
                ref_link = img["image"]["ref"]["$link"]
                at_img_url = f"https://cdn.bsky.app/img/feed_fullsize/plain/{did}/{ref_link}@jpeg"
                img_url = download_and_rehost_image(
                    at_img_url, nostr_account, blossom_server
                )
                content += f"\n{img_url}"

        elif etype == "app.bsky.embed.record":
            quoted_uri = embed["record"]["uri"]
            web_link = quoted_uri.replace("at://", "https://bsky.app/profile/").replace(
                "app.bsky.feed.post/", "post/"
            )
            content += f"\n\nQuoted post: {web_link}"

        elif etype == "app.bsky.embed.external":
            external_uri = embed.get("external", {}).get("uri")
            if external_uri:
                content += f"\n\nLink Preview: {external_uri}"

        # recordWithMedia: quote post that also has images
        elif etype == "app.bsky.embed.recordWithMedia":
            media = embed.get("media", {})
            if media.get("$type") == "app.bsky.embed.images":
                content += "\n"
                for img in media.get("images", []):
                    ref_link = img["image"]["ref"]["$link"]
                    at_img_url = f"https://cdn.bsky.app/img/feed_fullsize/plain/{did}/{ref_link}@jpeg"
                    img_url = download_and_rehost_image(
                        at_img_url, nostr_account, blossom_server
                    )
                    content += f"\n{img_url}"
            quoted_uri = embed.get("record", {}).get("record", {}).get("uri")
            if quoted_uri:
                web_link = quoted_uri.replace(
                    "at://", "https://bsky.app/profile/"
                ).replace("app.bsky.feed.post/", "post/")
                content += f"\n\nQuoted post: {web_link}"

        else:
            content += f"\n(Unsupported embed type: {etype})"

    return content


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch a Bluesky post and publish it as a Nostr kind 1 note."
    )
    parser.add_argument(
        "url",
        help="Full bsky.app post URL, e.g. https://bsky.app/profile/alice.bsky.social/post/3abc123",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and print the Nostr event without actually publishing it.",
    )
    parser.add_argument(
        "--no-ref",
        action="store_true",
        help="Skip the trailing reply that links back to the original Bluesky post.",
    )
    parser.add_argument(
        "--no-pings",
        action="store_true",
        help="Skip the reply that lists @-mentioned Bluesky profile links.",
    )
    args = parser.parse_args()

    # --- Config --------------------------------------------------------------
    config = get_config()

    nsec_key = config.get("nostr-sec-key")
    if not nsec_key:
        sys.exit(f"No nsec key found. Add 'nostr-sec-key: nsec1...' to {CONFIG_FILE}")

    nostr_relays: list[str] = config.get("nostr-relays") or ["wss://relay.primal.net"]
    blossom_server: str = config.get("blossom-server") or "https://blossom.primal.net"

    nostr_account = PrivateKey.from_nsec(nsec_key)
    npub = nostr_account.public_key.bech32()
    print(f"Nostr account: {npub[:9]}...{npub[-5:]}")

    # --- Parse URL -----------------------------------------------------------
    try:
        actor, rkey = parse_bsky_url(args.url)
    except ValueError as e:
        sys.exit(str(e))

    print(f"Fetching post: actor={actor!r} rkey={rkey!r}")

    # --- Fetch post ----------------------------------------------------------
    try:
        record = fetch_bsky_post(actor, rkey)
    except requests.HTTPError as e:
        sys.exit(f"Bluesky API error: {e}")
        
    created_time = datetime.datetime.fromisoformat(record.get("createdAt", datetime.datetime.now().isoformat()))

    did = record["__did__"]
    post_weblink = f"https://bsky.app/profile/{did}/post/{rkey}"

    # --- Build content -------------------------------------------------------
    content = build_nostr_content(record, nostr_account, blossom_server)

    if not content.strip():
        sys.exit("Post appears to have no publishable content — aborting.")

    print(
        f"\n--- Nostr content preview ---\n{content}\n-----------------------------\n"
    )

    # --- Collect @mention links (Bluesky-side, added as a reply) -------------
    ping_links: list[str] = []
    if not args.no_pings:
        for facet in record.get("facets", []):
            for feature in facet.get("features", []):
                if feature.get("$type") == "app.bsky.richtext.facet#mention":
                    ping_links.append(f"https://bsky.app/profile/{feature['did']}")

    # --- Build main event ----------------------------------------------------
    main_event = Event(
        public_key=nostr_account.public_key.hex(),
        content=content,
        created_at=int(created_time.timestamp()),
        kind=EventKind.TEXT_NOTE,
    )
    nostr_account.sign_event(main_event)

    if args.dry_run:
        print("Dry-run mode — main event JSON (not published):")
        print("")
        print(json.dumps(json.loads(main_event.to_message())[1], indent=2))
        print("")
    else:
        publish_to_nostr(main_event, nostr_relays)
        print(f"Published main note: {content[:60]}...")

    # --- Optional pings reply ------------------------------------------------
    if ping_links:
        pings_event = Event(
            public_key=nostr_account.public_key.hex(),
            content="Pinged Bluesky users:\n" + "\n".join(ping_links),
            created_at=int(created_time.timestamp()),
            kind=EventKind.TEXT_NOTE,
            tags=[
                ["e", main_event.id, "", "root"],
                ["e", main_event.id, "", "reply"],
                ["p", main_event.public_key],
            ],
        )
        nostr_account.sign_event(pings_event)
        
        if args.dry_run:
            print("Dry-run mode — pings event JSON (not published):")
            print("")
            print(json.dumps(json.loads(pings_event.to_message())[1], indent=2))
            print("")
        else:
            publish_to_nostr(pings_event, nostr_relays)
            print(f"Published pings reply")

    # --- Optional back-reference reply ---------------------------------------
    if not args.no_ref:
        ref_event = Event(
            public_key=nostr_account.public_key.hex(),
            content=(
                f"Post replicated by github.com/littlebitstudios/bluenostr from Bluesky."
                f" View original: {post_weblink}"
            ),
            created_at=int(created_time.timestamp()),
            kind=EventKind.TEXT_NOTE,
            tags=[
                ["e", main_event.id, "", "root"],
                ["e", main_event.id, "", "reply"],
                ["p", main_event.public_key],
            ],
        )
        nostr_account.sign_event(ref_event)
        
        if args.dry_run:
            print("Dry-run mode — back-reference event JSON (not published):")
            print("")
            print(json.dumps(json.loads(ref_event.to_message())[1], indent=2))
            print("")
        else:
            publish_to_nostr(ref_event, nostr_relays)
            print(f"Published back-reference reply")


if __name__ == "__main__":
    main()
