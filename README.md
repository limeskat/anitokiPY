# AnitokiPy

### Running [animetoki.com](https://animetoki.com/) in a cli interface.

---

### Dependencies

- **`mpv`**: Required for streaming video (automatically saves and resumes play position).
- **`fzf`**: Recommended for interactive fuzzy selection (gracefully falls back to numbered menus in non-interactive/piped sessions or if `fzf` is uninstalled).
- **`curl`**: Used for downloading videos with progress output.

### Setup

```bash
pip install -e .
```

### Usage

Start the interactive CLI:
```bash
anitokipy
```
Or search directly from the command line:
```bash
anitokipy "anime name"
```

### Flags

- `-c, --continue-watch`: Open your watch history and jump straight back into the last anime you viewed (exits with a clean message if history is empty).
- `-C, --clear-history`: Delete your watch history file.
- `-d, --download`: Download the video file (to `download_dir` or current directory) with a progress bar instead of streaming.
- `-v, --verbose`: Show debug log output in the terminal.

### Configuration

You can customize `anitokipy` by creating a JSON configuration file at `~/.config/anitokipy/config.json`:

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

#### Config Keys:
- **`user_agent`**: Custom HTTP User-Agent string for network requests and streaming.
- **`mpv_flags`**: List of command-line flags passed to `mpv` when streaming (`--save-position-on-quit` is enabled by default to preserve playback timestamps).
- **`download_dir`**: Directory where downloaded files are saved when using `-d` / `--download`.

### Features & Navigation
- **Loading Spinner**: Interactive terminal spinner provides visual feedback during network requests.
- **Fuzzy Search & Arrow Key Navigation**: Use `fzf` to search through anime, episodes, folders, or watch history:
  - **`[← Left]`**: Go back to parent folder or previous menu.
  - **`[→ Right / Enter]`**: Select item or open subfolder.
  Keybindings are prominently displayed in the menu footer at the bottom of the terminal.
- **Post-Play Controls**: After an episode finishes (or `mpv` is closed), options appear to play `next`, `replay`, `previous`, `select` a file, or `quit`.
- **Exit**: Type `exit` in the search prompt or press `Ctrl+D` / `Ctrl+C` to cleanly exit.
