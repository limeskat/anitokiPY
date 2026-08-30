# Animetoki CLI

Stream/Download anime from [animetoki.com](https://animetoki.com/) on terminal.

## Dependencies

- **`mpv`**: Video playback
- **`fzf`**: Interactive selection (optional, version >= 0.74.3)
- **`curl`**: File downloads

## Installation & Usage

```bash
pip install -e .

animetoki-cli               # Interactive CLI
animetoki-cli "anime title" # Direct search
```

## Options

- `-c, --continue-watch`: Resume last viewed anime
- `-d, --download`: Download video instead of streaming
- `-C, --clear-history`: Clear watch history
- `-v, --verbose`: Verbose debug logging

## Config File

`~/.config/animetoki-cli/config.json`:

```json
{
  "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
  "mpv_flags": [
    "--cache=yes",
    "--demuxer-max-bytes=200MiB",
    "--save-position-on-quit",
    "--fullscreen"
  ],
  "download_dir": "."
}
```
