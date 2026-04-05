# BlueNostr

A Bluesky to Nostr bridge in Python.

Posts AND images work now; the images get rebroadcasted to a Blossom server :D

## Usage

For local usage, create a file on your machine at `[HOME FOLDER]/.littlebitstudios/bluenostr/config.yaml. Use example-config.yaml as a reference. Then install "bluenostr" from pip or pipx and run it. You might want to set it up as an autorunning background process on your PC or server so it's always running to rebroadcast your Bluesky posts.

A Docker image is also available, with an example Compose file at example-compose.yml. No separate config is needed; the configuration is done with environment variables in the Compose file.
