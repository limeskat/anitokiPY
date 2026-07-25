# AnitokiPy

CLI for [animetoki.com](https://animetoki.com/). Stream or download anime from terminal.

## Dependencies

- **`mpv`**: Video playback
- **`fzf`**: Interactive selection (optional)
- **`curl`**: File downloads

## Installation & Usage

```bash
pip install -e .

anitokipy               # Interactive CLI
anitokipy "anime title" # Direct search
```

## Options

- `-c, --continue-watch`: Resume last viewed anime
- `-d, --download`: Download video instead of streaming
- `-C, --clear-history`: Clear watch history
- `-v, --verbose`: Verbose debug logging

## Config File

`~/.config/anitokipy/config.json`:

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
