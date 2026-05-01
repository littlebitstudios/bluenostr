# BlueNostr

A Bluesky to Nostr bridge in Python.

Posts AND images work now; the images get rebroadcasted to a Blossom server :D

## Usage

For local usage, create a file on your machine at `[HOME FOLDER]/.littlebitstudios/bluenostr/config.yaml. Use example-config.yaml as a reference. Then install "bluenostr" using pip or pipx and run it. You might want to set it up as an autorunning background process on your PC or server so it's always running to rebroadcast your Bluesky posts.

A Docker image is also available, with an example Compose file at example-compose.yml. No separate config is needed; the configuration is done with environment variables in the Compose file.

## BlueNostr Convert

A second command-line tool is available within the `bluenostr` pip package and Docker image called `bluenostr-convert`.

It takes a Bluesky post URL (`bsky.app/profile/[actor]/post/[rkey]`) as an argument, and runs the same conversion steps as the main BlueNostr real-time crosspost tool. It also uses the same configuration options.

You can run this by:
- Installing `bluenostr` using pip or pipx. You'll need to use the `~/.littlebitstudios/bluenostr/config.yaml` file to add your key, relays, and a Blossom server to upload media to. Run with `bluenostr-convert (bluesky post url)`
- Using docker/podman and the bluenostr container image (`ghcr.io/littlebitstudios/bluenostr`). You'll need to make a file called `.env` (or `something.env`) with the content of `.env.example` and edit the variables with your key, relays, and a Blossom server. Run with the command `docker run --env-file (your env file) --rm ghcr.io/littlebitstudios/bluenostr bluenostr-convert (bluesky post url)`. If you want to use podman, replace `docker` with `podman` in that command.