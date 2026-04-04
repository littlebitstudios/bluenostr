from websockets.sync.client import connect
import os
import yaml
from nostr.event import Event, EventKind
from nostr.relay_manager import RelayManager
from nostr.key import PrivateKey
import requests
import json
import time

configFileLocation = os.path.expanduser("~/.littlebitstudios/bluenostr/config.yaml")

def get_config() -> dict:
    if os.environ.get("BLUENOSTR_USE_ENV") == 1:
        return {
            "nostr-sec-key": os.environ.get("BLUENOSTR_NSEC_KEY"),
            "bsky-subject": os.environ.get("BLUENOSTR_BSKY_SUBJECT"),
            "nostr-relays": os.environ.get("BLUENOSTR_RELAYS"),
            "bsky-stream-endpoint": os.environ.get("BLUENOSTR_JETSTREAM_ENDPOINT")
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
    
    # nostr initialization
    
    print("Setting up Nostr...")
    nostr_relaymgr = RelayManager()
    for relay in nostr_relays:
        nostr_relaymgr.add_relay(relay)
    nostr_relaymgr.open_connections()
    print("Sleeping to allow relays to connect...")
    time.sleep(1.25)
    
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
    with connect(f"{bsky_stream_endpoint}?wantedDids={bsky_did}") as stream:
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

                # 3. Handle Embeds (Images)
                embed = record.get("embed", {})
                if embed:
                    if embed.get("$type") == "app.bsky.embed.images":
                        content += "\n"
                        for img in embed.get("images", []):
                            # Construct public CDN link for the image
                            img_url = f"https://cdn.bsky.app/img/feed_fullsize/plain/{bsky_did}/{img['image']['ref']['$link']}@jpeg"
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
                        content += f"(Original Bluesky post contains unsupported embed of type {embed.get("$type")}. View on Bluesky: {post_weblink})"

                # 5. Send to Nostr
                if content:
                    # Construct and Sign Note
                    event = Event(
                        public_key=nostr_account.public_key.hex(),
                        content=content,
                        created_at=int(time.time()),
                        kind=EventKind.TEXT_NOTE
                    )
                    nostr_account.sign_event(event)
                    
                    # Publish to the RelayManager's queue
                    nostr_relaymgr.publish_event(event)
                    print(f"Event queued for relays: {content[:30]}...")

                    # THE FIX: Give the background relay thread time to send
                    # before the main loop blocks again on stream.recv()
                    time.sleep(1) 
                    
                # Correct method to check for Relay Notices/Errors
                while nostr_relaymgr.message_pool.has_notices():
                    notice = nostr_relaymgr.message_pool.get_notice()
                    print(f"Relay Notice [{notice.url}]: {notice.content}")
            


if __name__ == "__main__":
    main()
