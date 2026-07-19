# AnitokiPy

### Running [animetoki.com](https://animetoki.com/) in a cli interface.

---
### Dependencies

- **`mpv`**: Required for streaming video.
- **`fzf`**: Recommended for ani-cli like ui/navigation (falls back to numbered lists if not installed).

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

- `-c, --continue-watch`: Open your watch history and jump straight back into the last anime you viewed.
- `-C, --clear-history`: Delete your watch history file.
- `-d, --download`: Download the video file to the current directory instead of streaming it.
- `-v, --verbose`: Show debug log output in the terminal.

### Navigation
- Inside menus, use `fzf` to type and fuzzy-search through anime, episodes, or history.
- After an episode finishes (or you close `mpv`), a post-play menu appears letting you play the `next` episode, `replay`, go to the `previous` episode, `select` a different file, or `quit`.
- Type `exit` in the main search prompt to exit the script.
