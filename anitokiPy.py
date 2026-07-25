import json
import socket
import base64
import subprocess
import argparse
import sys
import shutil
import signal
import time
import re
import os
import logging
import threading
import atexit
from dataclasses import dataclass
from pathlib import Path
import urllib.request
from urllib.parse import urljoin, unquote, urlparse, parse_qs, urlencode
from bs4 import BeautifulSoup, Tag
from curl_cffi import requests as cffi_requests
try:
    import termios
except ImportError:
    termios = None

log_dir = Path.home() / ".local" / "share" / "anitokipy"
log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(log_dir / "anitokipy.log"),
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("anitokipy")

CONFIG_PATH = Path.home() / ".config" / "anitokipy" / "config.json"

def load_config():
    defaults = {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
        "mpv_flags": [
            "--cache=yes",
            "--demuxer-max-bytes=200MiB",
            "--save-position-on-quit",
            "--fullscreen"
        ],
        "download_dir": "."
    }
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r") as f:
                user_conf = json.load(f)
                if isinstance(user_conf, dict):
                    defaults.update(user_conf)
        except Exception as e:
            logger.debug(f"Failed to load config file: {e}")
    return defaults

CONFIG = load_config()
UA = CONFIG["user_agent"]

_alt_screen_active = False

def enter_alt_screen():
    global _alt_screen_active
    if not _alt_screen_active and shutil.which("fzf") and sys.stdin.isatty() and sys.stdout.isatty():
        sys.stdout.write("\033[?1049h\033[H\033[2J")
        sys.stdout.flush()
        _alt_screen_active = True

def exit_alt_screen():
    global _alt_screen_active
    if _alt_screen_active and sys.stdout.isatty():
        sys.stdout.write("\033[?1049l")
        sys.stdout.flush()
        _alt_screen_active = False

atexit.register(exit_alt_screen)

def is_termux():
    return os.environ.get('TERMUX_VERSION') is not None or os.path.isdir('/data/data/com.termux')

base_url = "https://animetoki.com"
search_url = "https://animetoki.com/?s="

session = None
hist_file = Path.home() / ".local" / "state" / "anitokipy" / "ani-hsts"

def _cookie_header():
    if not session or not hasattr(session, "cookies"):
        return ""
    try:
        jar = getattr(session.cookies, "jar", session.cookies)
        cookies_dict = {c.name: c.value for c in jar}
        return "; ".join(f"{name}={value}" for name, value in cookies_dict.items())
    except Exception:
        return ""

def natural_sort_key(s):
    s_norm = re.sub(r'[^\w\s]', ' ', s.lower())
    s_norm = re.sub(r'\s+', ' ', s_norm).strip()
    return [int(text) if text.isdigit() else text for text in re.split('([0-9]+)', s_norm)]

@dataclass
class CloudFile:
    name: str
    id: str
    mime_type: str
    node_index: str
    size: int = 0

@dataclass
class HistoryContext:
    title: str
    anime_url: str
    source_label: str = ""
    source_type: str = ""
    source_url: str = ""
    raw_title: str = ""
    tags: str = ""

class Spinner:
    def __init__(self, message="Fetching..."):
        self.message = message
        self.stop_event = threading.Event()
        self.thread = None

    def _spin(self):
        chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        idx = 0
        use_fzf_box = _alt_screen_active or (shutil.which("fzf") and sys.stdin.isatty() and sys.stdout.isatty())

        while not self.stop_event.is_set():
            char = chars[idx % len(chars)]
            if sys.stdout.isatty():
                if use_fzf_box:
                    cols, rows = shutil.get_terminal_size((80, 24))
                    title = " AnimeToki CLI "
                    pad_top = max(0, (cols - len(title) - 2) // 2)
                    pad_rem = max(0, cols - len(title) - 2 - pad_top)
                    border_top = "╭" + "─" * pad_top + title + "─" * pad_rem + "╮"
                    border_bot = "╰" + "─" * (cols - 2) + "╯"
                    empty_line = "│" + " " * (cols - 2) + "│"
                    
                    msg_str = f"{char} {self.message}"
                    pad_msg = max(0, (cols - 2 - len(msg_str)) // 2)
                    pad_msg_rem = max(0, cols - 2 - pad_msg - len(msg_str))
                    msg_line = "│" + " " * pad_msg + f"\033[1;36m{char}\033[0m {self.message}" + " " * pad_msg_rem + "│"
                    
                    mid_row = rows // 2
                    out = ["\033[H", border_top]
                    for r in range(1, rows - 1):
                        out.append(msg_line if r == mid_row else empty_line)
                    out.append(border_bot)
                    sys.stdout.write("".join(out))
                    sys.stdout.flush()
                else:
                    sys.stdout.write(f"\r\033[K\033[1;36m  > \033[0m\033[36m{char}\033[0m {self.message}")
                    sys.stdout.flush()
            idx += 1
            time.sleep(0.08)

        if not use_fzf_box and sys.stdout.isatty():
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

    def __enter__(self):
        if shutil.which("fzf") and sys.stdin.isatty() and sys.stdout.isatty():
            enter_alt_screen()
        if sys.stdout.isatty():
            self.thread = threading.Thread(target=self._spin, daemon=True)
            self.thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.thread:
            self.stop_event.set()
            self.thread.join()
        if shutil.which("fzf") and sys.stdin.isatty() and sys.stdout.isatty():
            exit_alt_screen()


def is_raw_release_title(t: str) -> bool:
    """Check if title string contains raw release details (resolution, dual audio, etc.)."""
    return bool(re.search(r'\bDual Audio\b|\bSubbed\b|\b1080p\b|\b720p\b|\[.*?\]|Season \d+', str(t), re.I))

def save_history_entry(title, payload):
    hist_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        if hist_file.exists():
            with open(hist_file, "r") as f: hist = json.load(f)
        else: hist = {}
    except Exception as e:
        logger.debug(f"Error reading history file: {e}")
        hist = {}
    if not isinstance(hist, dict): hist = {}

    target_url = payload.get("anime_url", "")
    
    # Deduplicate existing keys with matching anime_url or matching key
    existing = {}
    keys_to_remove = []
    for k, v in hist.items():
        if isinstance(v, dict):
            if (target_url and v.get("anime_url") == target_url) or k == title:
                if not existing:
                    existing = v
                else:
                    w1 = existing.get("watched", [])
                    w2 = v.get("watched", [])
                    existing["watched"] = list(set(w1 + w2))
                keys_to_remove.append(k)

    for k in keys_to_remove:
        del hist[k]

    if isinstance(existing, dict):
        if "watched" in existing and "watched" not in payload:
            payload["watched"] = existing["watched"]
        if "raw_title" in existing and "raw_title" not in payload:
            payload["raw_title"] = existing["raw_title"]

    if "watched" not in payload:
        payload["watched"] = []

    curr_raw = payload.get("raw_title", "")
    prev_raw = existing.get("raw_title", "") if isinstance(existing, dict) else ""
    
    candidates_raw = [curr_raw, prev_raw, title]
    if payload.get("selected_file_name"):
        candidates_raw.append(payload["selected_file_name"])
    if payload.get("source_label"):
        candidates_raw.append(payload["source_label"])
    for ep in payload.get("episodes", []):
        if isinstance(ep, (list, tuple)) and len(ep) > 0:
            candidates_raw.append(ep[0])
        elif isinstance(ep, str):
            candidates_raw.append(ep)

    raw_matches = [c for c in candidates_raw if c and is_raw_release_title(c)]
    if raw_matches:
        best_raw = raw_matches[0]
    else:
        valid_cands = [c for c in (curr_raw, prev_raw, title) if c]
        best_raw = max(valid_cands, key=len) if valid_cands else title

    clean_t, _, tags_str = parse_anime_title(best_raw)
    
    if not tags_str:
        for cand in candidates_raw:
            if cand:
                _, _, t_str = parse_anime_title(cand)
                if t_str:
                    tags_str = t_str
                    break

    payload["raw_title"] = best_raw
    payload["tags"] = tags_str

    hist[clean_t] = payload
    tmp_file = hist_file.with_suffix(".tmp")
    try:
        with open(tmp_file, "w") as f: json.dump(hist, f, indent=2)
        tmp_file.replace(hist_file)
        logger.debug(f"Saved history entry for '{clean_t}'")
    except Exception as e:
        logger.error(f"Failed to save history: {e}")

def load_history():
    if not hist_file.exists():
        return {}
    try:
        with open(hist_file, "r") as f: hist = json.load(f)
        if isinstance(hist, dict):
            dedup = {}
            for k, v in hist.items():
                if not isinstance(v, dict):
                    continue
                url = v.get("anime_url") or k
                curr_raw = v.get("raw_title") or k
                
                if url not in dedup:
                    dedup[url] = (curr_raw, v)
                else:
                    prev_raw, prev_v = dedup[url]
                    if is_raw_release_title(curr_raw) and not is_raw_release_title(prev_raw):
                        best_raw = curr_raw
                    elif is_raw_release_title(prev_raw) and not is_raw_release_title(curr_raw):
                        best_raw = prev_raw
                    else:
                        best_raw = curr_raw if len(curr_raw) > len(prev_raw) else prev_raw
                        
                    t_curr = v.get("last_played", "")
                    t_prev = prev_v.get("last_played", "")
                    best_v = v if t_curr > t_prev else prev_v
                    
                    w1 = prev_v.get("watched", [])
                    w2 = v.get("watched", [])
                    best_v["watched"] = list(set(w1 + w2))
                    
                    dedup[url] = (best_raw, best_v)

            cleaned_hist = {}
            for url, (raw, v) in dedup.items():
                clean_t, _, tags_str = parse_anime_title(raw)
                if not tags_str and isinstance(v, dict):
                    tags_str = v.get("tags", "")
                if not tags_str and isinstance(v, dict):
                    cands = [v.get("selected_file_name"), v.get("source_label")]
                    for ep in v.get("episodes", []):
                        if isinstance(ep, (list, tuple)) and len(ep) > 0: cands.append(ep[0])
                    for cand in cands:
                        if cand:
                            _, _, t_str = parse_anime_title(cand)
                            if t_str:
                                tags_str = t_str
                                break
                v["raw_title"] = raw
                v["tags"] = tags_str
                cleaned_hist[clean_t] = v

            sorted_entries = sorted(
                cleaned_hist.items(),
                key=lambda item: item[1].get("last_played", "") if isinstance(item[1], dict) else "",
                reverse=True
            )
            return dict(sorted_entries)
    except Exception as e:
        logger.debug(f"Failed to load history: {e}")
    return {}

def delete_history_entry(title):
    """Deletes an entry from history state file by clean title or key."""
    if not hist_file.exists():
        return False
    try:
        with open(hist_file, "r") as f:
            hist = json.load(f)
        if not isinstance(hist, dict):
            return False
            
        keys_to_delete = [k for k in hist.keys() if k == title or parse_anime_title(k)[0] == title]
        if keys_to_delete:
            for k in keys_to_delete:
                del hist[k]
            tmp_file = hist_file.with_suffix(".tmp")
            with open(tmp_file, "w") as f:
                json.dump(hist, f, indent=2)
            tmp_file.replace(hist_file)
            logger.debug(f"Deleted history entry for '{title}'")
            return True
    except Exception as e:
        logger.error(f"Failed to delete history entry: {e}")
    return False

def toggle_watched_entry(title, item_name=None):
    """Toggle watched state of item_name (or last played episode) under history title."""
    hist = load_history()
    if not hist or not title:
        return
    entry = hist.get(title)
    if not isinstance(entry, dict):
        return

    watched = list(entry.get("watched", []))
    
    if not item_name:
        source_type = entry.get("source_type", "")
        if source_type == "direct_episodes":
            episodes = entry.get("episodes", [])
            idx = entry.get("selected_idx", 0)
            if episodes and 0 <= idx < len(episodes):
                item_name = episodes[idx][0]
        elif source_type in ("cloud", "worker_folder"):
            item_name = entry.get("selected_file_name", "")

    if not item_name:
        return

    if item_name in watched:
        watched.remove(item_name)
    else:
        watched.append(item_name)

    entry["watched"] = watched
    save_history_entry(title, entry)

def get_watched_list(hist_ctx_or_title):
    """Return list of watched items for given HistoryContext or title string."""
    title = getattr(hist_ctx_or_title, "title", None) if hist_ctx_or_title else hist_ctx_or_title
    if not title:
        return []
    hist = load_history()
    entry = hist.get(title, {})
    return list(entry.get("watched", [])) if isinstance(entry, dict) else []

def update_history(title, url, raw_title=""):
    raw = raw_title or title
    clean_title, _, tags_str = parse_anime_title(raw)
    payload = {
        "anime_url": url,
        "raw_title": raw,
        "tags": tags_str,
        "last_played": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": 1
    }
    save_history_entry(clean_title, payload)

def save_cloud_history(hist_ctx, current_folder_url, folder_stack, selected_file_name, selected_file_id=None, selected_mimetype=None, display_stack=None):
    raw = getattr(hist_ctx, "raw_title", "") or hist_ctx.title
    tags = getattr(hist_ctx, "tags", "") or parse_anime_title(raw)[2]
    payload = {
        "anime_url": hist_ctx.anime_url,
        "raw_title": raw,
        "tags": tags,
        "source_label": hist_ctx.source_label,
        "source_type": "cloud",
        "source_url": hist_ctx.source_url,
        "folder_stack": list(folder_stack),
        "display_stack": list(display_stack) if display_stack else [],
        "current_folder_url": current_folder_url,
        "selected_file_name": selected_file_name,
        "selected_file_id": selected_file_id,
        "selected_mimetype": selected_mimetype,
        "last_played": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": 1
    }
    save_history_entry(hist_ctx.title, payload)

def save_direct_history(hist_ctx, episodes, selected_idx):
    raw = getattr(hist_ctx, "raw_title", "") or hist_ctx.title
    tags = getattr(hist_ctx, "tags", "") or parse_anime_title(raw)[2]
    payload = {
        "anime_url": hist_ctx.anime_url,
        "raw_title": raw,
        "tags": tags,
        "source_label": hist_ctx.source_label,
        "source_type": "direct_episodes",
        "source_url": hist_ctx.source_url,
        "episodes": episodes,
        "selected_idx": selected_idx,
        "last_played": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": 1
    }
    save_history_entry(hist_ctx.title, payload)

def save_worker_history(hist_ctx, current_url, folder_stack, selected_name, display_stack=None):
    raw = getattr(hist_ctx, "raw_title", "") or hist_ctx.title
    tags = getattr(hist_ctx, "tags", "") or parse_anime_title(raw)[2]
    payload = {
        "anime_url": hist_ctx.anime_url,
        "raw_title": raw,
        "tags": tags,
        "source_label": hist_ctx.source_label,
        "source_type": "worker_folder",
        "source_url": hist_ctx.source_url,
        "folder_stack": list(folder_stack),
        "display_stack": list(display_stack) if display_stack else [],
        "current_folder_url": current_url,
        "selected_file_name": selected_name,
        "last_played": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": 1
    }
    save_history_entry(hist_ctx.title, payload)

def format_bytes(size):
    if not size:
        return ""
    try:
        size = float(size)
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"
    except Exception:
        return ""

def truncate_middle(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    half = (max_len - 2) // 2
    remainder = max_len - 2 - half
    return text[:half] + ".." + text[-remainder:]

def parse_anime_title(raw_title: str) -> tuple[str, str, str]:
    """
    Parses a raw anime release title into:
    1. clean_title: Readable display title (preserving named subtitles/spin-offs with '+ ')
    2. path_dir: Root path folder name (/Title)
    3. tags_str: Period-separated right-side tags (e.g. Dual.S1-S3.M.1080p)
    """
    working = raw_title.strip()
    working = re.sub(r'\[(?:AnimeSakura|AnimeToki)\]\s*', '', working, flags=re.IGNORECASE).strip()

    # 1. Extract Resolution & Bracket Info [...]
    brackets = re.findall(r'\[(.*?)\]', working)
    res_tag = ''
    for b in brackets:
        res_matches = re.findall(r'\d{3,4}p|\b4K\b', b, re.I)
        if res_matches:
            if len(res_matches) > 1:
                res_tag = '-'.join(sorted(res_matches, key=lambda x: int(re.sub(r'\D', '', x)) if re.sub(r'\D', '', x) else 0, reverse=True))
            else:
                res_tag = res_matches[0]

    working = re.sub(r'\[.*?\]', '', working).strip()

    # 2. Extract Audio, Sub & Codec Specs
    audio_tag = ''
    audio_patterns = [
        (r'\bDual Audio\b|\bDual\b', 'Dual'),
        (r'\bTri Audio\b|\bTri\b', 'Tri'),
        (r'\bMulti Audio\b|\bMulti\b', 'Multi'),
        (r'\bEnglish Subbed\b|\bSubbed\b', 'Sub'),
        (r'\bEnglish Dubbed\b|\bDubbed\b', 'Dub')
    ]
    for pat, val in audio_patterns:
        if re.search(pat, working, re.I):
            audio_tag = val
            working = re.sub(pat, '', working, flags=re.I).strip()
            break

    codec_tags = []
    for codec in ['HEVC', 'AV1', 'BD', '10bit']:
        if re.search(r'\b' + codec + r'\b', working, re.I):
            codec_tags.append(codec.upper())
            working = re.sub(r'\b' + codec + r'\b', '', working, flags=re.I).strip()

    # 3. Classify Parentheses (...) Contents
    p_match = re.search(r'\((.*?)\)', working)
    generic_tags = []
    named_subtitles = []
    has_seasons = False

    base_title = working
    if p_match:
        p_content = p_match.group(1)
        base_title = working[:p_match.start()].strip()

        items = [i.strip() for i in re.split(r'\+|\bamp\b|,', p_content)]

        for item in items:
            rem_item = item

            # Season detection
            m_s = re.search(r'Seasons?\s*(\d+)(?:\s*[-+to\s]+\s*(\d+))?', rem_item, re.I)
            if m_s:
                has_seasons = True
                s1 = int(m_s.group(1))
                s2 = int(m_s.group(2)) if m_s.group(2) else None
                generic_tags.append(f'S{s1}-S{s2}' if s2 else f'S{s1}')
                rem_item = re.sub(r'Seasons?\s*\d+(?:\s*[-+to\s]+\s*\d+)?', '', rem_item, flags=re.I).strip()
            elif re.search(r'All Seasons?', rem_item, re.I):
                has_seasons = True
                generic_tags.append('Seasons')
                rem_item = re.sub(r'All Seasons?', '', rem_item, flags=re.I).strip()

            if re.search(r'Final Season', rem_item, re.I):
                has_seasons = True
                generic_tags.append('Final')
                rem_item = re.sub(r'Final Season', '', rem_item, flags=re.I).strip()

            # Content Type detection
            if re.search(r'\bMovies?\b|\bThe Movie\b', rem_item, re.I):
                if 'M' not in generic_tags: generic_tags.append('M')
                rem_item = re.sub(r'\bMovies?\b|\bThe Movie\b', '', rem_item, flags=re.I).strip()

            if re.search(r'\bOVAs?\b|\bOAV\b', rem_item, re.I):
                if 'OVA' not in generic_tags: generic_tags.append('OVA')
                rem_item = re.sub(r'\bOVAs?\b|\bOAV\b', '', rem_item, flags=re.I).strip()

            if re.search(r'\bSpecials?\b|\bShorts\b', rem_item, re.I):
                if 'SP' not in generic_tags: generic_tags.append('SP')
                rem_item = re.sub(r'\bSpecials?\b|\bShorts\b', '', rem_item, flags=re.I).strip()

            if re.search(r'Complete Series|Complete Movies Series|Complete', rem_item, re.I):
                if 'Complete' not in generic_tags: generic_tags.append('Complete')
                rem_item = re.sub(r'Complete Series|Complete Movies Series|Complete', '', rem_item, flags=re.I).strip()

            if re.search(r'Directors Cut|DC', rem_item, re.I):
                if 'DC' not in generic_tags: generic_tags.append('DC')
                rem_item = re.sub(r'Directors Cut|DC', '', rem_item, flags=re.I).strip()

            if re.search(r'OST', rem_item, re.I):
                if 'OST' not in generic_tags: generic_tags.append('OST')
                rem_item = re.sub(r'OST', '', rem_item, flags=re.I).strip()

            rem_item = re.sub(r'^\W+|\W+$', '', rem_item)
            if rem_item:
                named_subtitles.append(rem_item)

    # 4. Final Reassembly
    base_title = re.sub(r'\bComplete Series\b|\bComplete\b', '', base_title, flags=re.I).strip()
    base_title = re.sub(r'\s+', ' ', base_title).strip(' -:')

    clean_title = base_title
    if named_subtitles:
        prefix = '+ ' if (has_seasons or 'Complete' in generic_tags or 'M' in generic_tags) else ''
        clean_title += f' ({prefix}{" + ".join(named_subtitles)})'

    path_dir = f'/{base_title}'

    all_tags = []
    if audio_tag: all_tags.append(audio_tag)
    for gt in generic_tags:
        if gt not in all_tags: all_tags.append(gt)
    for ct in codec_tags:
        if ct not in all_tags: all_tags.append(ct)
    if res_tag: all_tags.append(res_tag)

    tags_str = '.'.join(all_tags)

    return clean_title, path_dir, tags_str

def format_anime_title_label(raw_title: str) -> str:
    """Formats an anime title with clean display title on left and right-aligned tags in gray."""
    clean_title, path_dir, tags_str = parse_anime_title(raw_title)
    if not tags_str:
        return clean_title
    cols, _ = shutil.get_terminal_size((80, 24))
    avail_width = max(30, cols - 8)
    pad_count = max(2, avail_width - len(clean_title) - len(tags_str))
    spaces = " " * pad_count
    return f"{clean_title}{spaces}\033[90m{tags_str}\033[0m"

def format_history_label(title: str, entry: dict = None) -> str:
    """Formats a watch history entry with '[History]' prefix, clean display title on left, and right-aligned tags in gray."""
    raw = ""
    saved_tags = ""
    if isinstance(entry, dict):
        raw = entry.get("raw_title", "")
        saved_tags = entry.get("tags", "")

    clean_title, _, tags_str = parse_anime_title(raw if raw else title)
    final_tags = saved_tags or tags_str

    if not final_tags and isinstance(entry, dict):
        cands = [entry.get("selected_file_name"), entry.get("source_label")]
        for ep in entry.get("episodes", []):
            if isinstance(ep, (list, tuple)) and len(ep) > 0:
                cands.append(ep[0])
            elif isinstance(ep, str):
                cands.append(ep)
        for cand in cands:
            if cand:
                _, _, t_str = parse_anime_title(cand)
                if t_str:
                    final_tags = t_str
                    break

    cols, _ = shutil.get_terminal_size((80, 24))
    avail_width = max(30, cols - 8)
    left_ansi = f"\033[1;36m[History]\033[0m {clean_title}"
    left_plain = f"[History] {clean_title}"
    if final_tags:
        pad_count = max(2, avail_width - len(left_plain) - len(final_tags))
        spaces = " " * pad_count
        return f"{left_ansi}{spaces}\033[90m{final_tags}\033[0m"
    return left_ansi

def build_header(path_parts: list, items_info: str = "") -> str:
    """Build a clean header with dynamic breadcrumb path truncated to fit terminal width."""
    cols, _ = shutil.get_terminal_size((80, 24))
    clean_parts = []
    for p in path_parts:
        if p:
            s = re.sub(r'\[(?:AnimeSakura|AnimeToki)\]\s*', '', str(p), flags=re.IGNORECASE).strip('/')
            _, path_dir, _ = parse_anime_title(s)
            s_clean = path_dir.strip('/') if path_dir != '/' else s
            if s_clean:
                clean_parts.append(s_clean)
    
    info_plain = f"  ({items_info})" if items_info else ""
    max_path_len = max(15, cols - len(info_plain) - 6)
    
    if clean_parts:
        full_path = "/" + "/".join(clean_parts) + "/"
        if len(full_path) > max_path_len:
            part_budget = max(8, (max_path_len - len(clean_parts) - 2) // len(clean_parts))
            truncated_parts = [truncate_middle(p, part_budget) if len(p) > part_budget else p for p in clean_parts]
            full_path = "/" + "/".join(truncated_parts) + "/"
            if len(full_path) > max_path_len:
                full_path = truncate_middle(full_path, max_path_len)
        path_str = full_path
    else:
        path_str = "/"

    info_suffix = f"  \033[90m({items_info})\033[0m" if items_info else ""
    return f"{path_str}{info_suffix}"

def build_footer(actions: list) -> str:
    """Build a clean CLI footer string from (key, action_label) tuples or strings."""
    parts = []
    for item in actions:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            parts.append(f" [ {item[0]} ] {item[1]} ")
        else:
            parts.append(str(item))
    return " │ ".join(parts)

def count_items_summary(items_or_files):
    """Return a string like '12 Files, 2 Folders' for header stats."""
    if not items_or_files:
        return ""
    folders = 0
    files = 0
    for item in items_or_files:
        if isinstance(item, CloudFile):
            mime = item.mime_type.lower() if item.mime_type else ''
            if 'folder' in mime or item.mime_type == 'application/vnd.google-apps.folder':
                folders += 1
            else:
                files += 1
        elif isinstance(item, tuple) and len(item) >= 3:
            t = item[2]
            if t in ('folder', 'worker_folder', 'cloud'):
                folders += 1
            else:
                files += 1
        else:
            files += 1

    parts = []
    if files > 0:
        parts.append(f"{files} {'File' if files == 1 else 'Files'}")
    if folders > 0:
        parts.append(f"{folders} {'Folder' if folders == 1 else 'Folders'}")
    return ", ".join(parts)

def format_item_label(name: str, item_type: str, size_str: str = "", is_watched: bool = False) -> str:
    """Format label with type icon, clean name (no extensions/res brackets), right-aligned resolution tag & file size, and ANSI colors."""
    is_folder = item_type in ('folder', 'worker_folder', 'cloud')
    clean_name = re.sub(r'\[(?:AnimeSakura|AnimeToki)\]\s*', '', name, flags=re.IGNORECASE).strip()

    res_tag = ''
    if not is_folder:
        clean_name = re.sub(r'\.(?:mkv|mp4|avi|webm)$', '', clean_name, flags=re.IGNORECASE).strip()

        # Extract resolution tag (1080p, 720p, 480p, 4K) for files only
        res_matches = re.findall(r'\b\d{3,4}p\b|\b4K\b', clean_name, flags=re.IGNORECASE)
        if res_matches:
            if len(res_matches) > 1:
                res_tag = '-'.join(sorted(res_matches, key=lambda x: int(re.sub(r'\D', '', x)) if re.sub(r'\D', '', x) else 0, reverse=True))
            else:
                res_tag = res_matches[0]
                
            clean_name = re.sub(r'\[\s*(?:\d{3,4}p|4K)(?:-\d{3,4}p)?\s*\]', '', clean_name, flags=re.IGNORECASE)
            clean_name = re.sub(r'\b(?:\d{3,4}p|4K)\b', '', clean_name, flags=re.IGNORECASE)

        clean_name = re.sub(r'\[\s*\]', '', clean_name)
        clean_name = re.sub(r'\s+', ' ', clean_name).strip(' .-_')

    # Construct right-side string (resolution tag BEFORE file size, for files only)
    right_parts = []
    if res_tag and not is_folder:
        right_parts.append(res_tag)
    if size_str:
        plain_size = size_str.strip('()')
        if plain_size:
            right_parts.append(plain_size)

    right_str = '  '.join(right_parts)

    cols, _ = shutil.get_terminal_size((80, 24))
    avail_width = max(30, cols - 8)

    if is_folder:
        left_plain = f"🗁  {clean_name}"
        left_ansi = f"\033[1;38;5;215m🗁  {clean_name}\033[0m"
    elif is_watched:
        left_plain = f"✓  {clean_name}"
        left_ansi = f"\033[90m✓  {clean_name}\033[0m"
    elif item_type in ('video', 'direct_video', 'file'):
        left_plain = f"▶  {clean_name}"
        left_ansi = f"\033[1;37m▶  {clean_name}\033[0m"
    else:
        left_plain = f"   {clean_name}"
        left_ansi = f"\033[0m   {clean_name}"

    if right_str:
        pad_count = max(2, avail_width - len(left_plain) - len(right_str))
        spaces = " " * pad_count
        return f"{left_ansi}{spaces}\033[90m{right_str}\033[0m"
    else:
        return left_ansi

def format_cloud_file_label(cf: CloudFile, is_watched: bool = False) -> str:
    """Format CloudFile with type icon, file size, and ANSI colors."""
    mime = cf.mime_type.lower() if cf.mime_type else ''
    s_str = format_bytes(cf.size) if cf.size else ""
    if 'folder' in mime or cf.mime_type == 'application/vnd.google-apps.folder':
        return format_item_label(cf.name, 'folder', is_watched=is_watched)
    elif 'video' in mime:
        return format_item_label(cf.name, 'video', size_str=s_str, is_watched=is_watched)
    else:
        return format_item_label(cf.name, 'file', size_str=s_str, is_watched=is_watched)

def flush_stdin():
    """Flush any unread escape codes or keystrokes from stdin."""
    if sys.stdin.isatty() and termios is not None:
        try:
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except Exception:
            pass

def fzf_select(items, prompt, default_idx=None, header=None, footer=None):
    if not items:
        return None
    flush_stdin()
    if isinstance(footer, list):
        footer_text = build_footer(footer)
    elif isinstance(footer, str):
        footer_text = footer
    else:
        footer_text = build_footer([("Enter / →", "Select"), ("ESC / ←", "Back"), ("Ctrl+W", "Watched"), ("Ctrl+X", "Main Menu"), ("Ctrl+C", "Exit")])

    if shutil.which("fzf") and sys.stdin.isatty() and sys.stdout.isatty():
        cmd = [
            "fzf",
            "--ansi",
            "--border=rounded",
            "--border-label= AnimeToki CLI ",
            "--info=inline: ",
            "--header-border=bottom",
            "--color=prompt:cyan:bold,header:247,footer:247,header-border:247",
            "--reverse",
            "--cycle",
            "--prompt", prompt,
            "--with-nth=2",
            "--delimiter=\t",
            "--expect=left,right,ctrl-c,ctrl-d,ctrl-w,ctrl-x",
            f"--footer={footer_text}"
        ]
        if header:
            cmd.append(f"--header={header}")
        if default_idx is not None and 0 <= default_idx < len(items):
            pos_str = str(default_idx + 1)
            cmd.append(f"--bind=start:pos({pos_str}),load:pos({pos_str})")
            
        text = "\n".join(f"{i}\t{x}" for i, x in enumerate(items))
        p = subprocess.run(cmd, input=text, text=True, capture_output=True)
        if p.returncode == 130:
            return None
        if p.stdout:
            lines = p.stdout.splitlines()
            if lines:
                key_pressed = lines[0].strip().lower()
                selected_idx = None
                if len(lines) > 1 and lines[1].strip():
                    try:
                        selected_idx = int(lines[1].split('\t')[0])
                    except (ValueError, IndexError):
                        pass

                if key_pressed == "ctrl-c":
                    exit_alt_screen()
                    print("\nExiting...")
                    sys.exit(0)
                elif key_pressed == "ctrl-x":
                    return ("main_menu", selected_idx)
                elif key_pressed == "ctrl-d":
                    return ("delete", selected_idx)
                elif key_pressed == "ctrl-w":
                    return ("toggle_watched", selected_idx)
                elif key_pressed in ("left", "esc"):
                    return None
                elif selected_idx is not None:
                    return selected_idx
        return None
    
    if header:
        print(f"\033[37m--- AnimeToki CLI | {header} ---\033[0m")
    for i, item in enumerate(items): print(f"{i+1}. {item}")
    print("0. Back")
    print(f"\033[37m{footer_text}\033[0m\n")
    p_str = f"\033[1;36m{prompt}\033[0m"
    if default_idx is not None and 0 <= default_idx < len(items):
        p_str += f" [{default_idx + 1}]: "
    idx = safe_input(p_str, len(items), default_val=default_idx + 1 if default_idx is not None else None)
    if idx == 0 or idx is None:
        return None
    return idx - 1

def fzf_search_prompt():
    """Run search prompt inside fzf interface when available."""
    flush_stdin()
    if shutil.which("fzf") and sys.stdin.isatty() and sys.stdout.isatty():
        while True:
            hist = load_history()
            hist_titles = list(hist.keys())
            hist_map = {}
            items = []
            for t in hist_titles:
                entry = hist.get(t)
                lbl = format_history_label(t, entry)
                items.append(lbl)
                hist_map[lbl] = t
                plain_lbl = re.sub(r'\033\[[0-9;]*m', '', lbl)
                hist_map[plain_lbl] = t
            
            header = build_header(['search'])
            footer = build_footer([("Enter", "Search / Resume"), ("Ctrl+D", "Delete"), ("Ctrl+W", "Watched"), ("Ctrl+C", "Exit")])
            
            cmd = [
                "fzf",
                "--ansi",
                "--border=rounded",
                "--border-label= AnimeToki CLI ",
                "--info=inline: ",
                "--header-border=bottom",
                "--color=prompt:cyan:bold,header:247,footer:247,header-border:247",
                "--reverse",
                "--cycle",
                "--print-query",
                "--prompt=  > ",
                "--expect=left,ctrl-c,ctrl-d,ctrl-w,ctrl-x",
                f"--header={header}",
                f"--footer={footer}"
            ]
            
            text = "\n".join(items) if items else ""
            p = subprocess.run(cmd, input=text, text=True, capture_output=True)
            if p.returncode == 130:
                # ESC was pressed - re-prompt instead of exiting
                continue
            if p.stdout:
                lines = p.stdout.splitlines()
                typed_query = lines[0].strip() if len(lines) > 0 else ""
                key_pressed = lines[1].strip().lower() if len(lines) > 1 else ""
                selected_item = lines[2].strip() if len(lines) > 2 else ""
                
                if key_pressed == "ctrl-c":
                    exit_alt_screen()
                    print("\nExiting...")
                    sys.exit(0)

                real_title = None
                if selected_item:
                    plain_sel = re.sub(r'\033\[[0-9;]*m', '', selected_item)
                    real_title = hist_map.get(selected_item) or hist_map.get(plain_sel)
                    if not real_title and selected_item.startswith("[History] "):
                        real_title = selected_item[len("[History] "):].strip()

                if key_pressed == "ctrl-d":
                    if real_title and real_title in hist:
                        delete_history_entry(real_title)
                    continue

                if key_pressed == "ctrl-w":
                    if real_title and real_title in hist:
                        toggle_watched_entry(real_title)
                    continue

                if key_pressed in ("left", "ctrl-x"):
                    continue

                if typed_query:
                    if typed_query.lower() in ("exit", "quit"):
                        return ("exit", None)
                    return ("search", typed_query)
                    
                if real_title and real_title in hist:
                    return ("history", (real_title, hist[real_title]))
                        
            # If nothing returned or ESC pressed without output, re-prompt
            continue
    
    # Fallback to standard terminal input
    try:
        query = input("\033[1;36m> \033[0m").strip()
        if not query or query.lower() in ("exit", "quit"):
            return ("exit", None)
        return ("search", query)
    except (EOFError, KeyboardInterrupt):
        return ("exit", None)

def init_session():
    global session
    session = cffi_requests.Session(impersonate="firefox133")
    try:
        session.get("https://animetoki.com", timeout=3)
        session.get("https://cloud.animetoki.com", timeout=3)
        session.get("https://drive.animetoki.com", timeout=3)
    except Exception as e:
        print(f"Warning: Failed to warm up session: {e}")

def check_deps(download_mode):
    if download_mode:
        return
    if is_termux():
        return
    if not shutil.which("mpv"):
        sys.exit("Error: 'mpv' is not installed or not in PATH. Please install mpv to stream.")

def safe_request(method, url, max_retries=3, timeout=3, **kwargs):
    global session
    if session is None:
        init_session()
    with Spinner("Fetching..."):
        for attempt in range(max_retries):
            try:
                resp = getattr(session, method)(url, impersonate="firefox133", timeout=timeout, **kwargs)
                resp.raise_for_status()
                return resp
            except Exception as e:
                if attempt == max_retries - 1:
                    print(f"Failed to fetch {url} after {max_retries} attempts. Error: {e}")
                    return None
                time.sleep(1)
        return None

def safe_input(prompt, max_val=None, allow_zero_back=True, default_val=None):
    while True:
        try:
            val = input(prompt).strip()
            if not val:
                if default_val is not None:
                    return default_val
                continue
            ival = int(val)
            if allow_zero_back and ival == 0:
                return 0
            if max_val is not None:
                if 1 <= ival <= max_val:
                    return ival
                print(f"Please enter a number between 1 and {max_val} (0 to go back).")
            else:
                return ival
        except ValueError:
            print("Invalid input. Please enter a number.")
        except EOFError:
            sys.exit(0)

def stream_in_mpv(download_url, title=None) -> bool:
    """Streams video in MPV and returns True if >= 80% was watched."""
    if is_termux():
        mpv_conf_path = "/storage/emulated/0/mpv/mpv.config.mp4"
        try:
            with open(mpv_conf_path, 'w') as f:
                f.write(f'user-agent={UA}\n')
                f.write(f'http-header-fields=Cookie: {_cookie_header()}\n')
                f.write('cache=yes\n')
                f.write('demuxer-max-bytes=200MiB\n')
        except OSError as e:
            print(f"Warning: Could not write mpv config: {e}")

        am_cmd = [
            'am', 'start',
            '-a', 'android.intent.action.VIEW',
            '-d', download_url,
            '-t', 'video/*',
            '-p', 'is.xyz.mpv'
        ]
        if title:
            am_cmd.extend(['--es', 'title', title])
        logger.info(f"Launching mpv-android: {' '.join(am_cmd)}")
        subprocess.Popen(
            am_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True

    socket_path = f"/tmp/anitokipy_mpv_{os.getpid()}.sock"
    if os.path.exists(socket_path):
        try: os.unlink(socket_path)
        except OSError: pass

    user_flags = CONFIG.get("mpv_flags", [
        '--cache=yes',
        '--demuxer-max-bytes=200MiB',
        '--save-position-on-quit',
        '--fullscreen'
    ])
    if '--save-position-on-quit' not in user_flags and not any(f.startswith('--save-position-on-quit') for f in user_flags):
        user_flags = list(user_flags) + ['--save-position-on-quit']

    mpv_flags = [
        'mpv',
        f'--user-agent={UA}',
        f'--http-header-fields=Cookie: {_cookie_header()}',
        f'--input-ipc-server={socket_path}'
    ] + user_flags + [download_url]

    if title:
        mpv_flags.append(f'--force-media-title={title}')
    logger.info(f"Launching mpv: {download_url}")
    print(f"\033[1;34mPlaying {title or ''}...\033[0m")

    max_percent = 0.0
    stop_event = threading.Event()

    def monitor_mpv():
        nonlocal max_percent
        for _ in range(25):
            if os.path.exists(socket_path): break
            time.sleep(0.2)
        if not os.path.exists(socket_path): return
        
        while not stop_event.is_set():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(1.0)
                    s.connect(socket_path)
                    while not stop_event.is_set():
                        s.sendall(b'{"command": ["get_property", "percent-pos"]}\n')
                        data_bytes = s.recv(1024)
                        if not data_bytes: break
                        res = data_bytes.decode('utf-8', errors='ignore')
                        for line in res.splitlines():
                            if not line.strip(): continue
                            try:
                                obj = json.loads(line)
                                if "data" in obj and isinstance(obj["data"], (int, float)):
                                    if float(obj["data"]) > max_percent:
                                        max_percent = float(obj["data"])
                            except Exception: pass
                        time.sleep(0.5)
            except Exception:
                time.sleep(0.5)

    t = threading.Thread(target=monitor_mpv, daemon=True)
    t.start()

    subprocess.run(mpv_flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stop_event.set()
    t.join(timeout=1.0)

    if os.path.exists(socket_path):
        try: os.unlink(socket_path)
        except OSError: pass

    flush_stdin()
    return max_percent >= 80.0

def download_file(url, output_name):
    download_dir = CONFIG.get("download_dir", ".")
    dest_path = os.path.join(download_dir, output_name)
    if download_dir != ".":
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    print(f"Downloading to {dest_path}...")
    curl_flags = [
        'curl', '-L', '--progress-bar',
        '-A', UA,
        '-H', f'Cookie: {_cookie_header()}',
        '-o', dest_path,
        url
    ]
    try:
        subprocess.run(curl_flags)
    except FileNotFoundError:
        print("curl is not installed. Cannot download.")

def fetch_anime_list(anime_search_url, query=None, download_mode=False):
    res_search_animes = safe_request('get', anime_search_url)
    if not res_search_animes:
        return
    soup_anime_list = BeautifulSoup(res_search_animes.content, 'html.parser')

    anime_list = soup_anime_list.select('.post-item-inner > a:first-child')
    if not anime_list:
        print("No results found.")
        return

    raw_anime_names = [a.get('aria-label', 'Unknown') for a in anime_list]
    display_names = [format_anime_title_label(n) for n in raw_anime_names]
    anime_urls = [urljoin(base_url, a['href']) for a in anime_list]
    
    path_parts = ['search', query] if query else ['search']
    info_str = f"{len(raw_anime_names)} {'Result' if len(raw_anime_names) == 1 else 'Results'}"
    header = build_header(path_parts, info_str)
    footer = build_footer([("Enter", "Select"), ("ESC / ←", "Back"), ("Ctrl+W", "Watched"), ("Ctrl+X", "Main Menu"), ("Ctrl+C", "Exit")])
    
    default_idx = None
    while True:
        res = fzf_select(display_names, "Select anime: ", default_idx=default_idx, header=header, footer=footer)
        if res is None:
            break
        if res == "main_menu" or (isinstance(res, tuple) and res[0] == "main_menu"):
            return "main_menu"
        if isinstance(res, tuple):
            act, idx = res
            if idx is not None and 0 <= idx < len(anime_urls):
                default_idx = idx
            if act == "main_menu":
                return "main_menu"
            elif act == "delete":
                if default_idx is not None:
                    clean_t, _, _ = parse_anime_title(raw_anime_names[default_idx])
                    delete_history_entry(clean_t)
                continue
            elif act == "toggle_watched":
                if default_idx is not None:
                    clean_t, _, _ = parse_anime_title(raw_anime_names[default_idx])
                    toggle_watched_entry(clean_t)
                continue
            idx = default_idx
        else:
            idx = res
            default_idx = idx

        if idx is not None and 0 <= idx < len(anime_urls):
            selected_anime_url = anime_urls[idx]
            raw_anime_name = raw_anime_names[idx] if idx < len(raw_anime_names) else ""
            r = anime_download_link(selected_anime_url, download_mode=download_mode, raw_title=raw_anime_name)
            if r == "main_menu":
                return "main_menu"

def classify_link(url):
    """Classify a link as cloud folder, direct video, or worker folder."""
    video_exts = ('.mkv', '.mp4', '.avi', '.webm')
    parsed = urlparse(url)
    path_lower = parsed.path.lower()
    query_lower = parsed.query.lower()
    
    if 'cloud.animetoki.com' in parsed.netloc or 'drive.animetoki.com' in parsed.netloc:
        return 'cloud'
    
    # Check if it's a direct video file (path ends with video ext, or has ?a=view)
    for ext in video_exts:
        if path_lower.endswith(ext) or f'{ext}?' in path_lower or f'{ext}?' in url.lower():
            return 'direct_video'
    if 'a=view' in query_lower:
        return 'direct_video'
    
    # If path ends with / it's a folder on workers.dev or similar
    if parsed.path.endswith('/'):
        return 'worker_folder'
    
    return 'unknown'

def resolve_stream_url(url):
    """Convert a ?a=view URL to a direct streamable URL by fetching its file ID via POST."""
    parsed = urlparse(url)
    if 'workers.dev' not in parsed.netloc:
        if parsed.query:
            params = parse_qs(parsed.query)
            params.pop('a', None)
            return parsed._replace(query=urlencode(params, doseq=True)).geturl()
        return url

    # For workers.dev links, we must query the parent folder's API
    path = unquote(parsed.path)
    if path.endswith('/'):
        path = path[:-1]
    
    parent_path = "/".join(path.split('/')[:-1]) + "/"
    file_name = path.split('/')[-1]
    
    parent_url = f"{parsed.scheme}://{parsed.netloc}{parent_path}?a=view"
    logger.debug(f"resolve_stream_url: fetching folder API {parent_url}")
    
    res = safe_request('post', parent_url)
    if not res:
        return url
        
    try:
        data = res.json()
        for f in data.get('files', []):
            if f.get('name') == file_name:
                file_id = f.get('id')
                encoded_name = base64.b64encode(unquote(file_name).encode()).decode()
                stream_url = f"{parsed.scheme}://{parsed.netloc}/?a=download&id={file_id}&name={encoded_name}"
                logger.debug(f"resolve_stream_url: resolved to {stream_url}")
                return stream_url
    except Exception as e:
        logger.debug(f"resolve_stream_url error: {e}")
        
    return url

def anime_download_link(selected_anime_url, download_mode=False, raw_title=""):
    res_anime = safe_request('get', selected_anime_url)
    if not res_anime:
        return
    soup_anime_list = BeautifulSoup(res_anime.content, 'html.parser')

    anime_title = soup_anime_list.find('h1', class_="post-title entry-title")
    page_raw_title = anime_title.get_text().strip() if anime_title else "Unknown"
    
    if raw_title and is_raw_release_title(raw_title):
        best_raw = raw_title
    elif is_raw_release_title(page_raw_title):
        best_raw = page_raw_title
    else:
        best_raw = raw_title if len(raw_title) > len(page_raw_title) else page_raw_title

    clean_title, path_dir, tags_str = parse_anime_title(best_raw)
    update_history(clean_title, selected_anime_url, raw_title=best_raw)

    # Find cloud links (completed anime)
    cloud_links = soup_anime_list.css.select('a[href^="//cloud.animetoki.com/"], a[href^="//drive.animetoki.com/"]')
    # Find workers.dev / CDN links (ongoing anime)
    cdn_links = soup_anime_list.select('a.shortc-button[href]')
    # Filter cdn_links to exclude cloud links (already captured) and non-download links
    cdn_links = [a for a in cdn_links 
                 if a.get('href') and not (a['href'].startswith('//cloud.animetoki.com/') or a['href'].startswith('//drive.animetoki.com/'))]
    
    all_links = list(cloud_links) + list(cdn_links)
    
    if not all_links:
        print("No streaming links found for this anime.")
        return

    link_data = []  # list of (label, full_url, link_type)
    for link in all_links:
        label = link.get_text(strip=True)
        href = urljoin(base_url, link['href'])
        link_type = classify_link(href)
        link_data.append((label, href, link_type))

    path_parts = [best_raw]
    info_str = f"{len(link_data)} {'Source' if len(link_data) == 1 else 'Sources'}"
    header = build_header(path_parts, info_str)
    footer = build_footer([("Enter", "Select"), ("ESC / ←", "Back"), ("Ctrl+W", "Watched"), ("Ctrl+X", "Main Menu"), ("Ctrl+C", "Exit")])
    default_idx = None

    while True:
        labels = [format_item_label(l, t) for l, u, t in link_data]
        res = fzf_select(labels, "Select source: ", default_idx=default_idx, header=header, footer=footer)
        if res is None:
            break
        if res == "main_menu" or (isinstance(res, tuple) and res[0] == "main_menu"):
            return "main_menu"
        if isinstance(res, tuple):
            act, idx = res
            if idx is not None and 0 <= idx < len(link_data):
                default_idx = idx
            if act == "main_menu":
                return "main_menu"
            elif act == "delete":
                delete_history_entry(clean_title)
                return "main_menu"
            elif act == "toggle_watched":
                toggle_watched_entry(clean_title)
                continue
            idx = default_idx
        else:
            idx = res
            default_idx = idx

        if idx is not None and 0 <= idx < len(link_data):
            label, selected_url, link_type = link_data[idx]
            stype = link_type if link_type in ('cloud', 'direct_video', 'worker_folder') else 'direct_episodes'
            hist_ctx = HistoryContext(
                title=clean_title,
                anime_url=selected_anime_url,
                source_label=label,
                source_type=stype,
                source_url=selected_url,
                raw_title=best_raw,
                tags=tags_str
            )
            
            if link_type == 'cloud':
                parsed = urlparse(selected_url)
                domain_url = f"https://{parsed.netloc}/"
                segments = [base64.b64encode(unquote(s).encode()).decode() for s in parsed.path.split('/') if s]
                result = {"type": "cloud", "url": domain_url + "/".join(segments) + "/", "hist_ctx": hist_ctx}
            elif link_type == 'direct_video':
                direct_episodes = [(l, u) for l, u, t in link_data if t == 'direct_video']
                direct_episodes.sort(key=lambda e: natural_sort_key(e[0]))
                selected_ep_idx = next((i for i, (l, u) in enumerate(direct_episodes) if u == selected_url), 0)
                result = {"type": "direct_episodes", "episodes": direct_episodes, "selected": selected_ep_idx, "hist_ctx": hist_ctx}
            elif link_type == 'worker_folder':
                result = {"type": "worker_folder", "url": selected_url, "hist_ctx": hist_ctx}
            else:
                result = {"type": "direct_episodes", "episodes": [(label, selected_url)], "selected": 0, "hist_ctx": hist_ctx}
            
            r_disp = _dispatch_result(result, download_mode)
            if r_disp == "main_menu":
                return "main_menu"

def fetch_content(url):    
    """Fetch folder content from cloud API and return list of CloudFile objects and node_index."""
    post_response = safe_request('post', url)
    if not post_response:
        return None, None
        
    try:
        dict_json_ = post_response.json()
    except Exception as e:
        print(f"Error parsing JSON from cloud API: {e}")
        return None, None
        
    initial_file_list = dict_json_.get("files")
    if not initial_file_list:
        print("No files found in this folder.")
        return None, None
        
    initial_node_index = str(dict_json_.get("node_index", ""))

    initial_file_list.sort(key=lambda item: natural_sort_key(item.get("name", "")))
    
    files = [
        CloudFile(
            name=x.get("name", ""),
            id=x.get("id", ""),
            mime_type=x.get("mimeType", ""),
            node_index=initial_node_index,
            size=int(x.get("size", 0) or 0)
        )
        for x in initial_file_list
    ]
        
    return files, initial_node_index

def fetch_worker_folder(url):
    """Fetch a workers.dev folder and return list of (name, url, type) entries."""
    res = safe_request('get', url)
    if not res:
        return None
    soup = BeautifulSoup(res.content, 'html.parser')
    
    folder_parsed = urlparse(url)
    folder_domain = folder_parsed.netloc
    folder_path = folder_parsed.path
    
    entries = []
    for a in soup.select('a[href]'):
        href = a.get('href', '')
        if not href or href in ('.', '..', '../'):
            continue
        full_url = urljoin(url, href)
        link_parsed = urlparse(full_url)
        # Only include links on the same domain and under the same base path
        if link_parsed.netloc != folder_domain:
            continue
        if not link_parsed.path.startswith(folder_path.split('/0:/')[0]):
            continue
        label = a.get_text(strip=True) or unquote(link_parsed.path.split('/')[-1] or link_parsed.path.split('/')[-2])
        link_type = classify_link(full_url)
        entries.append((label, full_url, link_type))
    
    if not entries:
        print("No files found in this folder.")
        return None
    
    entries.sort(key=lambda e: natural_sort_key(e[0]))
    return entries

def browse_worker_folder(url, download_mode=False, hist_ctx=None, resume_from=None):
    """Browse a workers.dev folder, allowing navigation and playback."""
    if resume_from:
        folder_stack = list(resume_from.get("folder_stack", []))
        current_url = resume_from.get("current_folder_url", url)
        default_highlight = resume_from.get("selected_file_name")
        display_stack = list(resume_from.get("display_stack", []))
    else:
        folder_stack = []
        current_url = url
        default_highlight = None
        display_stack = []
    
    if not display_stack:
        anime_title = hist_ctx.title if hist_ctx else "Worker"
        source_label = hist_ctx.source_label if hist_ctx else ""
        display_stack = [anime_title, source_label]

    footer = build_footer([("Enter", "Select"), ("ESC / ←", "Back"), ("Ctrl+W", "Watched"), ("Ctrl+X", "Main Menu"), ("Ctrl+C", "Exit")])

    while True:
        entries = fetch_worker_folder(current_url)
        if not entries:
            while folder_stack and not entries:
                current_url = folder_stack.pop()
                if len(display_stack) > 2:
                    display_stack.pop()
                entries = fetch_worker_folder(current_url)
            if not entries:
                logger.debug("Worker folder unreachable and stack exhausted.")
                return False
        
        header = build_header(display_stack, count_items_summary(entries))
        watched = get_watched_list(hist_ctx)
        labels = [format_item_label(l, t, is_watched=(l in watched)) for l, _, t in entries]
        default_idx = next((i for i, (l, _, _) in enumerate(entries) if l == default_highlight), None) if default_highlight else None
        default_highlight = None

        res = fzf_select(labels, "Select: ", default_idx=default_idx, header=header, footer=footer)
        if res is None:
            if folder_stack:
                current_url = folder_stack.pop()
                if len(display_stack) > 2:
                    display_stack.pop()
                continue
            return True
            
        if res == "main_menu" or (isinstance(res, tuple) and res[0] == "main_menu"):
            return "main_menu"
            
        if isinstance(res, tuple):
            act, sel_idx = res
            if sel_idx is not None and 0 <= sel_idx < len(entries):
                default_idx = sel_idx
            if act == "main_menu":
                return "main_menu"
            elif act == "toggle_watched":
                if hist_ctx and default_idx is not None:
                    toggle_watched_entry(hist_ctx.title, entries[default_idx][0])
                continue
            elif act == "delete":
                if hist_ctx:
                    delete_history_entry(hist_ctx.title)
                return "main_menu"
            idx = default_idx
        else:
            idx = res

        if idx is None or not (0 <= idx < len(entries)):
            continue

        label, selected_url, link_type = entries[idx]
        if hist_ctx:
            save_worker_history(hist_ctx, current_url, folder_stack, label, display_stack=display_stack)
        
        if link_type in ('direct_video', 'unknown'):
            video_entries = [(i, l, u) for i, (l, u, t) in enumerate(entries) if t in ('direct_video', 'unknown')]
            vid_idx = next((j for j, (i, l, u) in enumerate(video_entries) if i == idx), 0)
            
            while True:
                _, label, selected_url = video_entries[vid_idx]
                if hist_ctx:
                    save_worker_history(hist_ctx, current_url, folder_stack, label, display_stack=display_stack)
                stream_url = resolve_stream_url(selected_url)
                if download_mode:
                    download_file(stream_url, unquote(urlparse(selected_url).path.split('/')[-1]))
                    return True
                
                playing_header = build_header(display_stack + [label])
                stream_in_mpv(stream_url, title=label)
                act = fzf_select(["next", "replay", "previous", "select", "quit"], f"Playing {label}... ", header=playing_header)
                if act == "main_menu" or (isinstance(act, tuple) and act[0] == "main_menu"):
                    return "main_menu"
                if isinstance(act, tuple): act = act[0]
                if act == 0 or act == "next": vid_idx = min(vid_idx + 1, len(video_entries) - 1)
                elif act == 1 or act == "replay": pass
                elif act == 2 or act == "previous": vid_idx = max(vid_idx - 1, 0)
                elif act == 3 or act == "select": break
                else: return True
        elif link_type == 'worker_folder':
            folder_stack.append(current_url)
            display_stack.append(label)
            current_url = selected_url
    return True

def play_direct_episodes(episodes, selected_idx, download_mode=False, hist_ctx=None, resume=False):
    """Play from a list of direct video episode links."""
    episodes.sort(key=lambda e: natural_sort_key(e[0]))
    anime_title = hist_ctx.title if hist_ctx else "Episodes"
    source_label = hist_ctx.source_label if hist_ctx else ""
    path_parts = [anime_title, source_label]
    info_str = f"{len(episodes)} {'Episode' if len(episodes) == 1 else 'Episodes'}"
    header = build_header(path_parts, info_str)
    footer = build_footer([("Enter", "Select"), ("ESC / ←", "Back"), ("Ctrl+W", "Watched"), ("Ctrl+X", "Main Menu"), ("Ctrl+C", "Exit")])

    def _select_ep_prompt(cur_idx):
        watched = get_watched_list(hist_ctx)
        items = [format_item_label(e[0], "video", is_watched=(e[0] in watched)) for e in episodes]
        return fzf_select(items, "Select episode: ", default_idx=cur_idx, header=header, footer=footer)

    idx = selected_idx if (selected_idx is not None and 0 <= selected_idx < len(episodes)) else 0

    if resume:
        res = _select_ep_prompt(idx)
        if res is None:
            return True
        if res == "main_menu" or (isinstance(res, tuple) and res[0] == "main_menu"):
            return "main_menu"
        if isinstance(res, tuple):
            act, sel_idx = res
            if sel_idx is not None and 0 <= sel_idx < len(episodes): idx = sel_idx
            if act == "main_menu": return "main_menu"
            elif act == "toggle_watched":
                if hist_ctx: toggle_watched_entry(hist_ctx.title, episodes[idx][0])
            elif act == "delete":
                if hist_ctx: delete_history_entry(hist_ctx.title)
                return "main_menu"
        elif isinstance(res, int):
            idx = res

    while True:
        label, url = episodes[idx]
        if hist_ctx:
            save_direct_history(hist_ctx, episodes, idx)
            
        stream_url = resolve_stream_url(url)
        if download_mode:
            download_file(stream_url, unquote(urlparse(url).path.split('/')[-1]))
            return True
            
        playing_header = build_header(path_parts + [label])
        stream_in_mpv(stream_url, title=label)

        act = fzf_select(["next", "replay", "previous", "select", "quit"], f"Playing {label}... ", header=playing_header)
        if act == "main_menu" or (isinstance(act, tuple) and act[0] == "main_menu"):
            return "main_menu"
        if isinstance(act, tuple): act = act[0]
        
        if act == 0 or act == "next": idx = min(idx + 1, len(episodes) - 1)
        elif act == 1 or act == "replay": pass
        elif act == 2 or act == "previous": idx = max(idx - 1, 0)
        elif act == 3 or act == "select":
            res = _select_ep_prompt(idx)
            if res is None: return True
            if res == "main_menu" or (isinstance(res, tuple) and res[0] == "main_menu"):
                return "main_menu"
            if isinstance(res, tuple):
                a_type, sel_idx = res
                if sel_idx is not None and 0 <= sel_idx < len(episodes): idx = sel_idx
                if a_type == "main_menu": return "main_menu"
                elif a_type == "toggle_watched":
                    if hist_ctx: toggle_watched_entry(hist_ctx.title, episodes[idx][0])
                elif a_type == "delete":
                    if hist_ctx: delete_history_entry(hist_ctx.title)
                    return "main_menu"
            elif isinstance(res, int):
                idx = res
        else: return True
    return True

def play_and_browse(selected_file=None, current_files=None, initial_link_base64=None, download_mode=False, hist_ctx=None, resume_from=None):
    def _cloud_dl_url(cf: CloudFile):
        parsed = urlparse(current_folder_url)
        domain_url = f"https://{parsed.netloc}"
        return f"{domain_url}?a=download&id={cf.id}&name={base64.b64encode(unquote(cf.name).encode()).decode()}&n={cf.node_index}"

    footer = build_footer([("Enter", "Select"), ("ESC / ←", "Back"), ("Ctrl+W", "Watched"), ("Ctrl+X", "Main Menu"), ("Ctrl+C", "Exit")])

    if resume_from:
        current_folder_url = resume_from["current_folder_url"]
        folder_stack = list(resume_from.get("folder_stack", []))
        display_stack = list(resume_from.get("display_stack", []))
        files, _ = fetch_content(current_folder_url)
        while folder_stack and not files:
            current_folder_url = folder_stack.pop()
            if len(display_stack) > 2:
                display_stack.pop()
            files, _ = fetch_content(current_folder_url)
        if not files:
            logger.debug("Resume target cloud folder unreachable and stack exhausted.")
            return False
            
        last_name = resume_from.get("selected_file_name")
        default_idx = next((i for i, f in enumerate(files) if f.name == last_name), None) if (files and last_name) else None
        if not display_stack:
            anime_title = hist_ctx.title if hist_ctx else "Cloud"
            source_label = hist_ctx.source_label if hist_ctx else ""
            display_stack = [anime_title, source_label]
        header = build_header(display_stack, count_items_summary(files))
        watched = get_watched_list(hist_ctx)
        res = fzf_select([format_cloud_file_label(f, is_watched=(f.name in watched)) for f in files], "Select file: ", default_idx=default_idx, header=header, footer=footer)
        if res is None:
            return True
        if res == "main_menu" or (isinstance(res, tuple) and res[0] == "main_menu"):
            return "main_menu"
        if isinstance(res, tuple):
            act, idx = res
            if idx is not None and 0 <= idx < len(files):
                default_idx = idx
            if act == "main_menu":
                return "main_menu"
            elif act == "toggle_watched":
                if hist_ctx and default_idx is not None:
                    toggle_watched_entry(hist_ctx.title, files[default_idx].name)
            elif act == "delete":
                if hist_ctx:
                    delete_history_entry(hist_ctx.title)
                return "main_menu"
            res_idx = default_idx
        else:
            res_idx = res
        if res_idx is not None and 0 <= res_idx < len(files):
            selected_file = files[res_idx]
    else:
        current_folder_url = initial_link_base64
        files = current_files
        folder_stack = []
        display_stack = []
        if files is None and current_folder_url:
            files, _ = fetch_content(current_folder_url)

    if not display_stack:
        anime_title = hist_ctx.title if hist_ctx else "Cloud"
        source_label = hist_ctx.source_label if hist_ctx else ""
        display_stack = [anime_title, source_label]

    while True:
        header = build_header(display_stack, count_items_summary(files))
        if not selected_file:
            if not files:
                if folder_stack:
                    current_folder_url = folder_stack.pop()
                    if len(display_stack) > 2:
                        display_stack.pop()
                    files, _ = fetch_content(current_folder_url)
                    continue
                return True

            watched = get_watched_list(hist_ctx)
            res = fzf_select([format_cloud_file_label(f, is_watched=(f.name in watched)) for f in files], "Select file: ", header=header, footer=footer)
            if res is None:
                if folder_stack:
                    current_folder_url = folder_stack.pop()
                    if len(display_stack) > 2:
                        display_stack.pop()
                    files, _ = fetch_content(current_folder_url)
                    continue
                return True

            if res == "main_menu" or (isinstance(res, tuple) and res[0] == "main_menu"):
                return "main_menu"

            if isinstance(res, tuple):
                act, idx = res
                if idx is not None and 0 <= idx < len(files):
                    selected_file = files[idx]
                if act == "main_menu":
                    return "main_menu"
                elif act == "toggle_watched":
                    if hist_ctx and idx is not None:
                        toggle_watched_entry(hist_ctx.title, files[idx].name)
                    selected_file = None
                    continue
                elif act == "delete":
                    if hist_ctx:
                        delete_history_entry(hist_ctx.title)
                    return "main_menu"
            else:
                if 0 <= res < len(files):
                    selected_file = files[res]

        if hist_ctx and selected_file:
            save_cloud_history(hist_ctx, current_folder_url, folder_stack, selected_file.name, selected_file.id, selected_file.mime_type, display_stack=display_stack)

        is_video = bool(selected_file.mime_type and "video" in selected_file.mime_type.lower())
        if is_video:
            download_url = _cloud_dl_url(selected_file)
            if download_mode:
                download_file(download_url, selected_file.name)
                return True
            
            playing_header = build_header(display_stack + [selected_file.name])
            stream_in_mpv(download_url, title=selected_file.name)
            
            video_siblings = [f for f in files if f.mime_type and "video" in f.mime_type.lower()] if files else []
            vid_idx = next((j for j, f in enumerate(video_siblings) if f.name == selected_file.name), 0)
            
            while True:
                playing_header = build_header(display_stack + [selected_file.name])
                act = fzf_select(["next", "replay", "previous", "select", "quit"], f"Playing {selected_file.name}... ", header=playing_header)
                if act == "main_menu" or (isinstance(act, tuple) and act[0] == "main_menu"):
                    return "main_menu"
                if isinstance(act, tuple): act = act[0]

                if act == 0 or act == "next":
                    vid_idx = min(vid_idx + 1, len(video_siblings) - 1)
                    selected_file = video_siblings[vid_idx]
                    if hist_ctx:
                        save_cloud_history(hist_ctx, current_folder_url, folder_stack, selected_file.name, selected_file.id, selected_file.mime_type, display_stack=display_stack)
                    stream_in_mpv(_cloud_dl_url(selected_file), title=selected_file.name)
                elif act == 1 or act == "replay":
                    stream_in_mpv(_cloud_dl_url(selected_file), title=selected_file.name)
                elif act == 2 or act == "previous":
                    vid_idx = max(vid_idx - 1, 0)
                    selected_file = video_siblings[vid_idx]
                    if hist_ctx:
                        save_cloud_history(hist_ctx, current_folder_url, folder_stack, selected_file.name, selected_file.id, selected_file.mime_type, display_stack=display_stack)
                    stream_in_mpv(_cloud_dl_url(selected_file), title=selected_file.name)
                elif act == 3 or act == "select":
                    selected_file = None
                    break
                else:
                    return True
            selected_file = None
        else:
            folder_stack.append(current_folder_url)
            display_stack.append(selected_file.name)
            current_folder_url += base64.b64encode(unquote(selected_file.name).encode()).decode() + "/"
            files, _ = fetch_content(current_folder_url)
            selected_file = None
    return True

def _dispatch_result(result, download_mode):
    if not result:
        return
    hist_ctx = result.get("hist_ctx")
    title = hist_ctx.title if hist_ctx else None
    if result["type"] == "cloud":
        files, _ = fetch_content(result["url"])
        if not files:
            return
        anime_title = hist_ctx.title if hist_ctx else "Cloud"
        source_label = hist_ctx.source_label if hist_ctx else ""
        display_stack = [anime_title, source_label]
        header = build_header(display_stack, count_items_summary(files))
        watched = get_watched_list(hist_ctx)
        labels = [format_cloud_file_label(f, is_watched=(f.name in watched)) for f in files]
        footer = build_footer([("Enter", "Select"), ("ESC / ←", "Back"), ("Ctrl+W", "Watched"), ("Ctrl+X", "Main Menu"), ("Ctrl+C", "Exit")])
        res = fzf_select(labels, "Select file: ", header=header, footer=footer)
        if res is None: return
        if res == "main_menu" or (isinstance(res, tuple) and res[0] == "main_menu"):
            return "main_menu"
        if isinstance(res, tuple):
            act, idx = res
            if act == "main_menu": return "main_menu"
            elif act == "toggle_watched" and idx is not None and 0 <= idx < len(files):
                toggle_watched_entry(hist_ctx.title, files[idx].name)
                return _dispatch_result(result, download_mode)
            elif act == "delete":
                delete_history_entry(hist_ctx.title)
                return "main_menu"
            res_idx = idx
        else:
            res_idx = res
        if res_idx is not None and 0 <= res_idx < len(files):
            return play_and_browse(selected_file=files[res_idx], current_files=files, initial_link_base64=result["url"], download_mode=download_mode, hist_ctx=hist_ctx)
    elif result["type"] == "direct_episodes":
        return play_direct_episodes(result["episodes"], result["selected"], download_mode, hist_ctx=hist_ctx)
    elif result["type"] == "worker_folder":
        return browse_worker_folder(result["url"], download_mode, hist_ctx=hist_ctx)

def resume_history(title, entry, download_mode=False):
    logger.debug(f"Resuming history for '{title}': {entry}")
    anime_url = entry.get("anime_url")
    source_type = entry.get("source_type")
    
    if not source_type or not anime_url:
        logger.debug(f"Legacy entry or missing source_type for '{title}'. Navigating from home URL.")
        if anime_url:
            return anime_download_link(anime_url, download_mode=download_mode)
        return

    hist_ctx = HistoryContext(
        title=title,
        anime_url=anime_url,
        source_label=entry.get("source_label", ""),
        source_type=source_type,
        source_url=entry.get("source_url", ""),
        raw_title=entry.get("raw_title", ""),
        tags=entry.get("tags", "")
    )

    if source_type == "cloud":
        res = play_and_browse(
            selected_file=None,
            current_files=None,
            initial_link_base64=None,
            download_mode=download_mode,
            hist_ctx=hist_ctx,
            resume_from=entry
        )
        if res == "main_menu": return "main_menu"
        if anime_url:
            return anime_download_link(anime_url, download_mode=download_mode)

    elif source_type == "direct_episodes":
        episodes = entry.get("episodes", [])
        selected_idx = entry.get("selected_idx", 0)
        if episodes:
            res = play_direct_episodes(episodes, selected_idx, download_mode=download_mode, hist_ctx=hist_ctx, resume=True)
            if res == "main_menu": return "main_menu"
        if anime_url:
            return anime_download_link(anime_url, download_mode=download_mode)

    elif source_type == "worker_folder":
        url = entry.get("current_folder_url") or entry.get("source_url")
        if url:
            res = browse_worker_folder(url, download_mode=download_mode, hist_ctx=hist_ctx, resume_from=entry)
            if res == "main_menu": return "main_menu"
        if anime_url:
            return anime_download_link(anime_url, download_mode=download_mode)
    else:
        if anime_url:
            return anime_download_link(anime_url, download_mode=download_mode)

def search(query, download_mode=False):
    anime_search_url = search_url + query
    return fetch_anime_list(anime_search_url, query=query, download_mode=download_mode)

def signal_handler(sig, frame):
    exit_alt_screen()
    print("\nExiting...")
    sys.exit(0)

def main():
    signal.signal(signal.SIGINT, signal_handler)
    
    parser = argparse.ArgumentParser(description="CLI anime player for animetoki.com")
    parser.add_argument("query", nargs="*", help="Search query (if provided, runs in non-interactive mode)")
    parser.add_argument("-d", "--download", action="store_true", help="Download the video instead of playing it")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show debug log output in terminal")
    parser.add_argument("-c", "--continue-watch", action="store_true", help="Continue watching from history")
    parser.add_argument("-C", "--clear-history", action="store_true", help="Clear watch history")
    args = parser.parse_args()

    if args.verbose:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(console_handler)

    logger.info("=== anitokipy session started ===")

    check_deps(args.download)
    init_session()

    initial_query = " ".join(args.query) if args.query else None

    if getattr(args, 'clear_history', False):
        try:
            hist_file.unlink(missing_ok=True)
        except TypeError: # Python < 3.8 compat
            if hist_file.exists(): hist_file.unlink()
        print("History cleared.")
        return

    enter_alt_screen()

    try:
        while True:
            if args.continue_watch:
                hist = load_history()
                if not hist:
                    print("No history found.")
                    return
                else:
                    titles = list(hist.keys())
                    display_titles = [format_history_label(t, hist.get(t)) for t in titles]
                    footer = build_footer([("Enter", "Resume"), ("Ctrl+D", "Delete"), ("Ctrl+W", "Watched"), ("Ctrl+X", "Main Menu"), ("Ctrl+C", "Exit")])
                    res = fzf_select(display_titles, "Select history: ", header="AnimeToki CLI | Watch History", footer=footer)
                    if res is None or res == "main_menu" or (isinstance(res, tuple) and res[0] == "main_menu"):
                        args.continue_watch = False
                        continue
                    if isinstance(res, tuple):
                        act, idx = res
                        if idx is not None and 0 <= idx < len(titles):
                            sel_title = titles[idx]
                            if act == "delete":
                                delete_history_entry(sel_title)
                                continue
                            elif act == "toggle_watched":
                                toggle_watched_entry(sel_title)
                                continue
                    elif isinstance(res, int):
                        selected_title = titles[res]
                        entry = hist[selected_title]
                        r = resume_history(selected_title, entry, args.download)
                        if r == "main_menu":
                            args.continue_watch = False
                            continue
                args.continue_watch = False
                continue

            if initial_query:
                query = initial_query
                initial_query = None
                if query:
                    search(query, args.download)
            else:
                action, payload = fzf_search_prompt()
                if action == "exit":
                    print("\nExiting...")
                    break
                elif action == "reprompt":
                    continue
                elif action == "history":
                    title, entry = payload
                    resume_history(title, entry, args.download)
                elif action == "search":
                    search(payload, args.download)
    except (EOFError, KeyboardInterrupt):
        print("\nExiting...")
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        exit_alt_screen()

if __name__ == "__main__":
    main()