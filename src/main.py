from websockets.sync.client import connect
import os
import yaml
from nostr.event import Event, EventKind
from nostr.key import PrivateKey
import requests
import json
import time
import hashlib
import mimetypes

configFileLocation = os.path.expanduser("~/.littlebitstudios/bluenostr/config.yaml")

def upload_image_to_blossom(image_data: bytes, mime_type: str, nostr_account: PrivateKey, server_url: str = "https://blossom.primal.net") -> str | None:
    """Upload image bytes to a Blossom server (BUD-01/02) and return the URL."""
    # Compute SHA-256 hash of the file (required by Blossom)
    file_hash = hashlib.sha256(image_data).hexdigest()
    
    # Build a Blossom auth event (kind 24242) for the upload
    auth_event = Event(
        public_key=nostr_account.public_key.hex(),
        content="Upload image",
        created_at=int(time.time()),
        kind=24242,  # Blossom auth
        tags=[
            ["t", "upload"],
            ["x", file_hash],
            ["expiration", str(int(time.time()) + 60)]
        ]
    )
    nostr_account.sign_event(auth_event)
    
    # Base64-encode the auth event for the Authorization header
    import base64
    auth_header = "Nostr " + base64.urlsafe_b64encode(json.dumps({
        "id": auth_event.id,
        "pubkey": auth_event.public_key,
        "created_at": auth_event.created_at,
        "kind": auth_event.kind,
        "tags": auth_event.tags,
        "content": auth_event.content,
        "sig": auth_event.signature
    }).encode()).decode()
    
    ext = mimetypes.guess_extension(mime_type) or ".jpg"
    filename = f"{file_hash}{ext}"
    
    try:
        resp = requests.put(
            f"{server_url}/upload",
            headers={
                "Authorization": auth_header,
                "Content-Type": mime_type,
            },
            data=image_data,
            timeout=30
        )
        resp.raise_for_status()
        result = resp.json()
        # Blossom returns { url, sha256, size, type, ... }
        return result.get("url") or f"{server_url}/{file_hash}"
    except Exception as e:
        print(f"Blossom upload failed: {e}")
        return None


def download_and_rehost_image(img_url: str, nostr_account: PrivateKey, blossom_server: str) -> str:
    """Download an image from Bluesky CDN and reupload to Blossom. Returns the new URL or original on failure."""
    try:
        resp = requests.get(img_url, timeout=15)
        resp.raise_for_status()
        mime_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        new_url = upload_image_to_blossom(resp.content, mime_type, nostr_account, blossom_server)
        if new_url:
            print(f"  Re-hosted image: {img_url[:40]}... -> {new_url}")
            return new_url
    except Exception as e:
        print(f"Image re-host failed ({e}), falling back to original URL.")
    return img_url

def publish_to_nostr(event: Event, relays: list[str]):
    """Publish a signed Nostr event to each relay directly over WebSocket."""
    message = event.to_message()
    for relay_url in relays:
        try:
            with connect(relay_url) as ws:
                ws.send(message)
                # Wait for an OK or NOTICE response (with a short timeout)
                try:
                    ws.settimeout(5)
                    response = ws.recv()
                    print(f"Relay response [{relay_url}]: {response[:80]}")
                except Exception:
                    pass  # timeout is fine — message was sent
        except Exception as e:
            print(f"Failed to publish to {relay_url}: {e}")

def get_config() -> dict:
    if os.environ.get("BLUENOSTR_USE_ENV") == "1":
        return {
            "nostr-sec-key": os.environ.get("BLUENOSTR_NSEC_KEY"),
            "bsky-subject": os.environ.get("BLUENOSTR_BSKY_SUBJECT"),
            "nostr-relays": os.environ.get("BLUENOSTR_RELAYS").split(","),
            "bsky-stream-endpoint": os.environ.get("BLUENOSTR_JETSTREAM_ENDPOINT"),
            "blossom-server": os.environ.get("BLUENOSTR_BLOSSOM_SERVER")
        }
    elif os.path.exists(configFileLocation):
        with open(configFileLocation) as f:
            return yaml.safe_load(f)
    else:
        with open(configFileLocation, "x") as f:
            pass
        return {}

def main():
    print("BlueNostr by LittleBit - send posts you make on Bluesky to Nostr")
    print("Initializing...")
    
    # loading config
    
    print("Gathering configuration data...")
    config = get_config()
    
    nsec_key = ""
    if not config.get("nostr-sec-key"):
        print(f"No nsec key provided. Open {configFileLocation} and add the line \"nostr-sec-key: [nsec...]\" before starting again.")
        exit(1)
    else:
        nsec_key = config.get("nostr-sec-key")
    
    bsky_subject = ""
    if not config.get("bsky-subject"):
        print(f"No Bluesky subject provided. Open {configFileLocation} and add the line \"bsky-subject: [handle or did:...]\" before trying again.")
        exit(1)
    else:
        bsky_subject = config.get("bsky-subject")
        
    nostr_relays = []
    if not config.get("nostr-relays"):
        print("No Nostr relays defined. Using the default (relay.primal.net).")
        nostr_relays = ["wss://relay.primal.net"]
    else:
        nostr_relays = config.get("nostr-relays")
        
    bsky_stream_endpoint = ""
    if not config.get("bsky-stream-endpoint"):
        print("No ATProto Jetstream endpoint defined. Using the default (jetstream1.us-east.fire.hose.cam).")
        bsky_stream_endpoint = "wss://jetstream1.us-east.fire.hose.cam/subscribe"
    else:
        bsky_stream_endpoint = config.get("bsky-stream-endpoint")
        
    blossom_server = ""
    if not config.get("blossom-server"):
        print("No Blossom server defined. Using the default (blossom.primal.net).")
        blossom_server = "https://blossom.primal.net"
    else:
        blossom_server = config.get("blossom-server")
    
    # nostr initialization
    nostr_account = PrivateKey.from_nsec(nsec_key)
    nostr_npub = nostr_account.public_key.bech32()
    print(f"Signed into Nostr as {nostr_npub[:9]}...{nostr_npub[-5:]}")
    
    # showing the selected bsky user
    bsky_did = ""
    try:
        bsky_profile_request = requests.get(f"https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor={bsky_subject}")
        bsky_profile_request.raise_for_status()
        bsky_profile_data = bsky_profile_request.json()
        bsky_did = bsky_profile_data.get("did")
        print(f"Will be searching for posts from Bluesky user {bsky_profile_data.get("displayName")} (@{bsky_profile_data.get("handle")}, {bsky_profile_data.get("did")[:13]}...{bsky_profile_data.get("did")[-5:]})")
    except:
        print("The Bluesky user provided is possibly invalid.")
        exit(1)
        
    print("Connecting to the provided Jetstream endpoint...")
    with connect(f"{bsky_stream_endpoint}?wantedDids={bsky_did}", ping_interval=20) as stream:
        print("Connected to Jetstream. Beginning to parse.")
        while True:
            message = stream.recv()
            data = json.loads(message)

            if data.get("commit", {}).get("operation") == "create" and data["commit"].get("collection") == "app.bsky.feed.post":
                post_aturi = f"at://{data.get("did")}/app.bsky.feed.post/{data.get("commit").get("rkey")}"
                post_weblink = f"https://bsky.app/profile/{data.get("did")}/post/{data.get("commit").get("rkey")}"
                record = data.get("commit", {}).get("record", {})
                if "reply" in record: continue

                content = record.get("text", "")
                    
                # 2. Handle Rich Text (Links/Pings) via Facets
                # Note: Facets use byte offsets, so we handle them carefully
                if "facets" in record:
                    # We'll append links to the end for simplicity, 
                    # or you can use a library like 'atproto' to inject them.
                    links = []
                    for facet in record["facets"]:
                        for feature in facet.get("features", []):
                            if feature["$type"] == "app.bsky.richtext.facet#link":
                                links.append(feature["uri"])
                    if links:
                        content += "\n\nLinks: " + " ".join(links)

                # 3. Handle Embeds
                embed = record.get("embed", {})
                if embed:
                    if embed.get("$type") == "app.bsky.embed.images":
                        content += "\n"
                        for img in embed.get("images", []):
                            # Construct public CDN link for the image
                            at_img_url = f"https://cdn.bsky.app/img/feed_fullsize/plain/{bsky_did}/{img['image']['ref']['$link']}@jpeg"
                            img_url = download_and_rehost_image(at_img_url, nostr_account, blossom_server)
                            content += f"\n{img_url}"
                    # 4. Handle Quote Posts
                    elif embed.get("$type") == "app.bsky.embed.record":
                        # Add the URI of the quoted post
                        quoted_uri = embed["record"]["uri"]
                        # Convert at:// to a web link for better compatibility
                        web_link = quoted_uri.replace("at://", "https://bsky.app/profile/").replace("app.bsky.feed.post/", "post/")
                        content += f"\n\nQuoted post: {web_link}"    
                    # Handle Link Previews / GIFs
                    elif embed.get("$type") == "app.bsky.embed.external":
                        external = embed.get("external", {})
                        external_uri = external.get("uri")
                        if external_uri:
                            content += f"\n\nLink Preview: {external_uri}"
                    else:
                        content += f"(Original Bluesky post contains unsupported embed of type {embed.get("$type")})"
                        
                facets:list[dict] = record.get("facets")
                pingLinks = []
                if facets:
                    for facet in facets:
                        features:list[dict] = facet.get("features")
                        for feature in features:
                            if feature.get("$type") == "app.bsky.richtext.facet#mention":
                                pingLinks.append(f"https://bsky.app/profile/{feature.get("did")}")

                # 5. Send to Nostr
                if content:
                    event = Event(
                        public_key=nostr_account.public_key.hex(),
                        content=content,
                        created_at=int(time.time()),
                        kind=EventKind.TEXT_NOTE
                    )
                    nostr_account.sign_event(event)
                    publish_to_nostr(event, nostr_relays)
                    print(f"Published to Nostr: {content[:30]}...")
                    
                    if pingLinks:
                        og_ref_event = Event(
                            public_key=nostr_account.public_key.hex(),
                            content=f"Pinged Bluesky users:\n{"\n".join(pingLinks)}",
                            created_at=int(time.time()),
                            kind=EventKind.TEXT_NOTE,
                            tags=[
                                ['e', event.id, '', 'root'],
                                ['e', event.id, '', 'reply'],
                                ['p', event.public_key]
                            ]
                        )
                        nostr_account.sign_event(og_ref_event)
                        publish_to_nostr(og_ref_event, nostr_relays)
                        print(f"Published pings reply to Nostr")
                        
                    
                    og_ref_event = Event(
                        public_key=nostr_account.public_key.hex(),
                        content=f"Post replicated by github.com/littlebitstudios/bluenostr from Bluesky. View original: {post_weblink}",
                        created_at=int(time.time()),
                        kind=EventKind.TEXT_NOTE,
                        tags=[
                            ['e', event.id, '', 'root'],
                            ['e', event.id, '', 'reply'],
                            ['p', event.public_key]
                        ]
                    )
                    nostr_account.sign_event(og_ref_event)
                    publish_to_nostr(og_ref_event, nostr_relays)
                    print(f"Published original reference reply to Nostr")

if __name__ == "__main__":
    main()
