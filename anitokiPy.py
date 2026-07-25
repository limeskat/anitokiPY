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
from urllib.parse import urljoin, unquote, urlparse, parse_qs, urlencode
from bs4 import BeautifulSoup, Tag
from curl_cffi import requests as cffi_requests

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
UA = CONFIG.get("user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0")

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
base_cloud_url = "https://cloud.animetoki.com/"
session = None
hist_file = Path.home() / ".local" / "state" / "anitokipy" / "ani-hsts"

def _cookie_header():
    return "; ".join(f"{name}={value}" for name, value in session.cookies.items())

def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]

@dataclass
class CloudFile:
    name: str
    id: str
    mime_type: str
    node_index: str

class HistoryContext:
    def __init__(self, title, anime_url, source_label="", source_type="", source_url=""):
        self.title = title
        self.anime_url = anime_url
        self.source_label = source_label
        self.source_type = source_type
        self.source_url = source_url

class Spinner:
    def __init__(self, message="Fetching..."):
        self.message = message
        self.stop_event = threading.Event()
        self.thread = None

    def _spin(self):
        chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        idx = 0
        while not self.stop_event.is_set():
            if sys.stdout.isatty():
                sys.stdout.write(f"\r\033[K\033[1;36m[AnimeToki CLI]\033[0m {chars[idx % len(chars)]} {self.message}")
                sys.stdout.flush()
            idx += 1
            time.sleep(0.08)
        if sys.stdout.isatty():
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

    def __enter__(self):
        if sys.stdout.isatty():
            self.thread = threading.Thread(target=self._spin, daemon=True)
            self.thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.thread:
            self.stop_event.set()
            self.thread.join()

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
    
    existing = hist.get(title, {})
    if isinstance(existing, dict) and "watched" in existing and "watched" not in payload:
        payload["watched"] = existing["watched"]
    if "watched" not in payload:
        payload["watched"] = []

    hist[title] = payload
    tmp_file = hist_file.with_suffix(".tmp")
    try:
        with open(tmp_file, "w") as f: json.dump(hist, f, indent=2)
        tmp_file.replace(hist_file)
        logger.debug(f"Saved history entry for '{title}'")
    except Exception as e:
        logger.error(f"Failed to save history: {e}")

def _migrate_history_entry(data):
    if isinstance(data, str):
        return {
            "anime_url": data,
            "watched": [],
            "version": 1
        }
    if isinstance(data, dict):
        if "watched" not in data or not isinstance(data.get("watched"), list):
            data["watched"] = []
    return data

def load_history():
    if not hist_file.exists():
        return {}
    try:
        with open(hist_file, "r") as f: hist = json.load(f)
        if isinstance(hist, dict):
            return {k: _migrate_history_entry(v) for k, v in hist.items()}
    except Exception as e:
        logger.debug(f"Failed to load history: {e}")
    return {}

def get_watched_set(title):
    if not title: return set()
    hist = load_history()
    entry = hist.get(title, {})
    if isinstance(entry, dict):
        return set(entry.get("watched", []))
    return set()

def get_first_unwatched_idx(items_keys, title, preferred_idx=None):
    """Returns 0-based index of the first unwatched item starting from preferred_idx (or 0)."""
    if not items_keys:
        return 0
    w_set = get_watched_set(title)
    def _is_w(key):
        if isinstance(key, (tuple, list)):
            return any(k in w_set for k in key if k)
        return key in w_set

    start = preferred_idx if (preferred_idx is not None and 0 <= preferred_idx < len(items_keys)) else 0
    for i in range(start, len(items_keys)):
        if not _is_w(items_keys[i]):
            return i
    for i in range(0, start):
        if not _is_w(items_keys[i]):
            return i
    return start

def toggle_watched(title, ep_key):
    if not title or not ep_key: return
    hist = load_history()
    entry = hist.get(title, {})
    if not isinstance(entry, dict): return
    watched = list(entry.get("watched", []))
    if ep_key in watched:
        watched.remove(ep_key)
    else:
        watched.append(ep_key)
    entry["watched"] = watched
    save_history_entry(title, entry)

def mark_watched(title, ep_key):
    if not title or not ep_key: return
    hist = load_history()
    entry = hist.get(title, {})
    if not isinstance(entry, dict): return
    watched = list(entry.get("watched", []))
    if ep_key not in watched:
        watched.append(ep_key)
        entry["watched"] = watched
        save_history_entry(title, entry)

def update_history(title, url):
    payload = {
        "anime_url": url,
        "last_played": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": 1
    }
    save_history_entry(title, payload)

def save_cloud_history(hist_ctx, current_folder_url, folder_stack, selected_file_name, selected_file_id=None, selected_mimetype=None):
    payload = {
        "anime_url": hist_ctx.anime_url,
        "source_label": hist_ctx.source_label,
        "source_type": "cloud",
        "source_url": hist_ctx.source_url,
        "folder_stack": list(folder_stack),
        "current_folder_url": current_folder_url,
        "selected_file_name": selected_file_name,
        "selected_file_id": selected_file_id,
        "selected_mimetype": selected_mimetype,
        "last_played": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": 1
    }
    save_history_entry(hist_ctx.title, payload)

def save_direct_history(hist_ctx, episodes, selected_idx):
    payload = {
        "anime_url": hist_ctx.anime_url,
        "source_label": hist_ctx.source_label,
        "source_type": "direct_episodes",
        "source_url": hist_ctx.source_url,
        "episodes": episodes,
        "selected_idx": selected_idx,
        "last_played": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": 1
    }
    save_history_entry(hist_ctx.title, payload)

def save_worker_history(hist_ctx, current_url, folder_stack, selected_name):
    payload = {
        "anime_url": hist_ctx.anime_url,
        "source_label": hist_ctx.source_label,
        "source_type": "worker_folder",
        "source_url": hist_ctx.source_url,
        "folder_stack": list(folder_stack),
        "current_folder_url": current_url,
        "selected_file_name": selected_name,
        "last_played": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": 1
    }
    save_history_entry(hist_ctx.title, payload)

def format_item_label(name: str, item_type: str, is_watched: bool = False) -> str:
    """Format label with type icon and ANSI colors: Gray for watched, Skinish peach for folders, White for files/videos."""
    if is_watched:
        return f"\033[90m✓  {name}\033[0m"
    if item_type in ('folder', 'worker_folder', 'cloud'):
        return f"\033[1;38;5;215m🗁  {name}\033[0m"
    elif item_type in ('video', 'direct_video', 'file'):
        return f"\033[1;37m▶  {name}\033[0m"
    else:
        return f"\033[0m   {name}"

def format_cloud_file_label(cf: CloudFile, is_watched: bool = False) -> str:
    """Format CloudFile with type icon and ANSI colors."""
    mime = cf.mime_type.lower() if cf.mime_type else ''
    if 'folder' in mime or cf.mime_type == 'application/vnd.google-apps.folder':
        return format_item_label(cf.name, 'folder', is_watched=is_watched)
    elif 'video' in mime:
        return format_item_label(cf.name, 'video', is_watched=is_watched)
    else:
        return format_item_label(cf.name, 'file', is_watched=is_watched)

def flush_stdin():
    """Flush any unread escape codes or keystrokes from stdin."""
    if sys.stdin.isatty():
        try:
            import termios
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except Exception:
            pass

def fzf_select(items, prompt, default_idx=None, header=None, allow_toggle=False, return_key=False):
    if not items:
        return (None, None) if return_key else None
    flush_stdin()
    if allow_toggle:
        footer_text = " [ ← ] Back  |  [ → / Enter ] Select  |  [ Ctrl-W ] Toggle Watched "
        expect_keys = "left,right,ctrl-w"
    else:
        footer_text = " [ ← ] Back  |  [ → / Enter ] Select "
        expect_keys = "left,right"

    if shutil.which("fzf") and sys.stdin.isatty() and sys.stdout.isatty():
        cmd = [
            "fzf",
            "--ansi",
            "--color=prompt:cyan:bold,header:247,footer:247",
            "--reverse",
            "--cycle",
            "--prompt", prompt,
            "--with-nth=2",
            "--delimiter=\t",
            f"--expect={expect_keys}",
            f"--footer={footer_text}\n "
        ]
        if header:
            cmd.append(f"--header={header}")
        if default_idx is not None and 0 <= default_idx < len(items):
            pos_str = str(default_idx + 1)
            cmd.append(f"--bind=start:pos({pos_str}),load:pos({pos_str})")
            
        text = "\n".join(f"{i}\t{x}" for i, x in enumerate(items))
        p = subprocess.run(cmd, input=text, text=True, capture_output=True)
        if p.returncode == 130:
            exit_alt_screen()
            print("\nExiting...")
            sys.exit(0)
        if p.returncode in (0, 1) and p.stdout:
            lines = p.stdout.splitlines()
            if lines:
                key_pressed = lines[0].strip().lower()
                if key_pressed == "left":
                    return (None, "left") if return_key else None
                if len(lines) > 1 and lines[1].strip():
                    try:
                        idx = int(lines[1].split('\t')[0])
                        return (idx, key_pressed) if return_key else idx
                    except (ValueError, IndexError):
                        pass
        return (None, None) if return_key else None
    
    if header:
        print(f"\033[37m--- {header} ---\033[0m")
    for i, item in enumerate(items): print(f"{i+1}. {item}")
    print("0. Back")
    print(f"\033[37m{footer_text}\033[0m\n")
    p_str = f"\033[1;36m{prompt}\033[0m"
    if default_idx is not None and 0 <= default_idx < len(items):
        p_str += f" [{default_idx + 1}]: "
    idx = safe_input(p_str, len(items), default_val=default_idx + 1 if default_idx is not None else None)
    if idx == 0 or idx is None:
        return (None, "left") if return_key else None
    return (idx - 1, "enter") if return_key else (idx - 1)

def fzf_search_prompt():
    """Run search prompt inside fzf interface when available."""
    flush_stdin()
    if shutil.which("fzf") and sys.stdin.isatty() and sys.stdout.isatty():
        hist = load_history()
        hist_titles = list(hist.keys())
        items = [f"[History] {t}" for t in hist_titles]
        
        header = "AnimeToki CLI | Search anime or select history to resume"
        footer = " [ ← / ESC ] Exit  |  [ Enter ] Search / Resume \n "
        
        cmd = [
            "fzf",
            "--color=prompt:cyan:bold,header:247,footer:247",
            "--reverse",
            "--cycle",
            "--print-query",
            "--prompt=Search anime: ",
            "--expect=left",
            f"--header={header}",
            f"--footer={footer}"
        ]
        
        text = "\n".join(items) if items else ""
        p = subprocess.run(cmd, input=text, text=True, capture_output=True)
        if p.returncode == 130:
            exit_alt_screen()
            print("\nExiting...")
            sys.exit(0)
        if p.stdout:
            lines = p.stdout.splitlines()
            typed_query = lines[0].strip() if len(lines) > 0 else ""
            key_pressed = lines[1].strip().lower() if len(lines) > 1 else ""
            selected_item = lines[2].strip() if len(lines) > 2 else ""
            
            if key_pressed == "left":
                return ("exit", None)
                
            if typed_query:
                if typed_query.lower() in ("exit", "quit"):
                    return ("exit", None)
                return ("search", typed_query)
                
            if selected_item and selected_item.startswith("[History] "):
                title = selected_item[len("[History] "):].strip()
                if title in hist:
                    return ("history", (title, hist[title]))
                    
        return ("exit", None)
    
    # Fallback to standard terminal input
    try:
        query = input("\033[1;36mSearch anime: \033[0m").strip()
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

    anime_names = [a.get('aria-label', 'Unknown') for a in anime_list]
    anime_urls = [urljoin(base_url, a['href']) for a in anime_list]
    
    header = f"AnimeToki CLI | Search: '{query}'" if query else "AnimeToki CLI | Search Results"
    
    default_idx = None
    while True:
        idx = fzf_select(anime_names, "Select anime: ", default_idx=default_idx, header=header)
        if idx is None:
            break
        default_idx = idx
        selected_anime_url = anime_urls[idx]
        anime_download_link(selected_anime_url, download_mode=download_mode)

def classify_link(url):
    """Classify a link as cloud folder, direct video, or worker folder."""
    video_exts = ('.mkv', '.mp4', '.avi', '.webm')
    parsed = urlparse(url)
    path_lower = parsed.path.lower()
    query_lower = parsed.query.lower()
    
    if 'cloud.animetoki.com' in parsed.netloc:
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

def anime_download_link(selected_anime_url, download_mode=False):
    res_anime = safe_request('get', selected_anime_url)
    if not res_anime:
        return
    soup_anime_list = BeautifulSoup(res_anime.content, 'html.parser')

    anime_title = soup_anime_list.find('h1', class_="post-title entry-title")
    title_text = anime_title.get_text().strip() if anime_title else "Unknown"
    update_history(title_text, selected_anime_url)

    # Find cloud links (completed anime)
    cloud_links = soup_anime_list.css.select('a[href^="//cloud.animetoki.com/"]')
    # Find workers.dev / CDN links (ongoing anime)
    cdn_links = soup_anime_list.select('a.shortc-button[href]')
    # Filter cdn_links to exclude cloud links (already captured) and non-download links
    cdn_links = [a for a in cdn_links 
                 if a.get('href') and not a['href'].startswith('//cloud.animetoki.com/')]
    
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

    keys = [(l, u) for l, u, t in link_data]
    default_idx = get_first_unwatched_idx(keys, title_text)
    header = f"AnimeToki CLI | {title_text}"

    while True:
        w_set = get_watched_set(title_text)
        labels = [format_item_label(l, t, is_watched=(l in w_set or u in w_set)) for l, u, t in link_data]
        idx = fzf_select(labels, "Select source: ", default_idx=default_idx, header=header)
        if idx is None:
            break
        
        default_idx = idx
        label, selected_url, link_type = link_data[idx]
        stype = link_type if link_type in ('cloud', 'direct_video', 'worker_folder') else 'direct_episodes'
        hist_ctx = HistoryContext(
            title=title_text,
            anime_url=selected_anime_url,
            source_label=label,
            source_type=stype,
            source_url=selected_url
        )
        
        if link_type == 'cloud':
            segments = [base64.b64encode(unquote(s).encode()).decode() for s in urlparse(selected_url).path.split('/') if s]
            result = {"type": "cloud", "url": base_cloud_url + "/".join(segments) + "/", "hist_ctx": hist_ctx}
        elif link_type == 'direct_video':
            # Collect all direct video links for episode navigation
            direct_episodes = [(l, u) for l, u, t in link_data if t == 'direct_video']
            direct_episodes.sort(key=lambda e: natural_sort_key(e[0]))
            selected_ep_idx = next((i for i, (l, u) in enumerate(direct_episodes) if u == selected_url), 0)
            result = {"type": "direct_episodes", "episodes": direct_episodes, "selected": selected_ep_idx, "hist_ctx": hist_ctx}
        elif link_type == 'worker_folder':
            result = {"type": "worker_folder", "url": selected_url, "hist_ctx": hist_ctx}
        else:
            # Unknown type, try opening as direct URL
            result = {"type": "direct_episodes", "episodes": [(label, selected_url)], "selected": 0, "hist_ctx": hist_ctx}
        
        _dispatch_result(result, download_mode)

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
            node_index=initial_node_index
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
    else:
        folder_stack = []
        current_url = url
        default_highlight = None
    
    title = hist_ctx.title if hist_ctx else None
    header = f"AnimeToki CLI | {title if title else 'Worker Folder'}"
    while True:
        entries = fetch_worker_folder(current_url)
        if not entries:
            while folder_stack and not entries:
                current_url = folder_stack.pop()
                entries = fetch_worker_folder(current_url)
            if not entries:
                logger.debug("Worker folder unreachable and stack exhausted.")
                return False
        
        keys = [(l, u) for l, u, t in entries]
        preferred_idx = next((i for i, (l, _, _) in enumerate(entries) if l == default_highlight), None) if default_highlight else None
        default_highlight = None
        default_idx = get_first_unwatched_idx(keys, title, preferred_idx=preferred_idx)

        while True:
            w_set = get_watched_set(title)
            labels = [format_item_label(l, t, is_watched=(l in w_set or u in w_set)) for l, u, t in entries]
            idx, key = fzf_select(labels, "Select: ", default_idx=default_idx, header=header, allow_toggle=True, return_key=True)
            if idx is None:
                if folder_stack:
                    current_url = folder_stack.pop()
                    break
                return True
            if key == "ctrl-w":
                item_key = entries[idx][0]
                toggle_watched(title, item_key)
                default_idx = idx
                continue
            break

        if idx is None:
            continue

        label, selected_url, link_type = entries[idx]
        if hist_ctx:
            save_worker_history(hist_ctx, current_url, folder_stack, label)
        
        if link_type in ('direct_video', 'unknown'):
            video_entries = [(i, l, u) for i, (l, u, t) in enumerate(entries) if t in ('direct_video', 'unknown')]
            vid_idx = next((j for j, (i, l, u) in enumerate(video_entries) if i == idx), 0)
            
            while True:
                _, label, selected_url = video_entries[vid_idx]
                if hist_ctx:
                    save_worker_history(hist_ctx, current_url, folder_stack, label)
                stream_url = resolve_stream_url(selected_url)
                if download_mode:
                    download_file(stream_url, unquote(urlparse(selected_url).path.split('/')[-1]))
                    mark_watched(title, label)
                    return True
                was_watched = stream_in_mpv(stream_url, title=label)
                if was_watched:
                    mark_watched(title, label)

                act = fzf_select(["next", "replay", "previous", "select", "quit"], f"Playing {label}... ", header=header)
                if act == 0: vid_idx = min(vid_idx + 1, len(video_entries) - 1)
                elif act == 1: pass
                elif act == 2: vid_idx = max(vid_idx - 1, 0)
                elif act == 3: break
                else: return True
        elif link_type == 'worker_folder':
            folder_stack.append(current_url)
            current_url = selected_url
    return True

def play_direct_episodes(episodes, selected_idx, download_mode=False, hist_ctx=None, resume=False):
    """Play from a list of direct video episode links."""
    episodes.sort(key=lambda e: natural_sort_key(e[0]))
    title = hist_ctx.title if hist_ctx else None
    keys = [(e[0], e[1]) for e in episodes]

    def _select_ep_prompt(cur_idx):
        default_pos = get_first_unwatched_idx(keys, title, preferred_idx=cur_idx)
        header = f"AnimeToki CLI | {hist_ctx.title if hist_ctx else 'Episodes'}"
        while True:
            w_set = get_watched_set(title)
            items = [format_item_label(e[0], "video", is_watched=(e[1] in w_set or e[0] in w_set)) for e in episodes]
            res, key = fzf_select(items, "Select episode: ", default_idx=default_pos, header=header, allow_toggle=True, return_key=True)
            if res is None:
                return None
            if key == "ctrl-w":
                ep_key = episodes[res][1]
                toggle_watched(title, ep_key)
                default_pos = res
                continue
            return res

    idx = get_first_unwatched_idx(keys, title, preferred_idx=selected_idx)

    if resume:
        res = _select_ep_prompt(idx)
        if res is None:
            return True
        idx = res

    header = f"AnimeToki CLI | {hist_ctx.title if hist_ctx else 'Episodes'}"
    while True:
        label, url = episodes[idx]
        if hist_ctx:
            save_direct_history(hist_ctx, episodes, idx)
            
        stream_url = resolve_stream_url(url)
        if download_mode:
            download_file(stream_url, unquote(urlparse(url).path.split('/')[-1]))
            mark_watched(title, url)
            return True
            
        was_watched = stream_in_mpv(stream_url, title=label)
        if was_watched:
            mark_watched(title, url)

        act = fzf_select(["next", "replay", "previous", "select", "quit"], f"Playing {label}... ", header=header)
        
        if act == 0: idx = min(idx + 1, len(episodes) - 1)
        elif act == 1: pass
        elif act == 2: idx = max(idx - 1, 0)
        elif act == 3:
            res = _select_ep_prompt(idx)
            if res is None: return True
            idx = res
        else: return True
    return True

def play_and_browse(selected_file=None, current_files=None, initial_link_base64=None, download_mode=False, hist_ctx=None, resume_from=None):
    def _cloud_dl_url(cf: CloudFile):
        return f"{base_cloud_url}?a=download&id={cf.id}&name={base64.b64encode(unquote(cf.name).encode()).decode()}&n={cf.node_index}"

    title = hist_ctx.title if hist_ctx else None
    header = f"AnimeToki CLI | {title if title else 'Cloud Folder'}"

    def _select_cloud_file(files, default_name=None):
        keys = [(f.name, f.id) for f in files]
        preferred_idx = next((i for i, f in enumerate(files) if f.name == default_name), None) if default_name else None
        default_idx = get_first_unwatched_idx(keys, title, preferred_idx=preferred_idx)

        while True:
            w_set = get_watched_set(title)
            items = [format_cloud_file_label(f, is_watched=(f.name in w_set or f.id in w_set)) for f in files]
            idx, key = fzf_select(items, "Select file: ", default_idx=default_idx, header=header, allow_toggle=True, return_key=True)
            if idx is None:
                return None
            if key == "ctrl-w":
                f_key = files[idx].name
                toggle_watched(title, f_key)
                default_idx = idx
                continue
            return files[idx]

    if resume_from:
        current_folder_url = resume_from["current_folder_url"]
        folder_stack = list(resume_from.get("folder_stack", []))
        files, _ = fetch_content(current_folder_url)
        while folder_stack and not files:
            current_folder_url = folder_stack.pop()
            files, _ = fetch_content(current_folder_url)
        if not files:
            logger.debug("Resume target cloud folder unreachable and stack exhausted.")
            return False
            
        last_name = resume_from.get("selected_file_name")
        selected_file = _select_cloud_file(files, default_name=last_name)
        if selected_file is None:
            return True
    else:
        current_folder_url = initial_link_base64
        files = current_files
        folder_stack = []
        if files is None and current_folder_url:
            files, _ = fetch_content(current_folder_url)

    while True:
        if not selected_file:
            if not files:
                if folder_stack:
                    current_folder_url = folder_stack.pop()
                    files, _ = fetch_content(current_folder_url)
                    continue
                return True

            selected_file = _select_cloud_file(files)
            if selected_file is None:
                if folder_stack:
                    current_folder_url = folder_stack.pop()
                    files, _ = fetch_content(current_folder_url)
                    continue
                return True

        if hist_ctx and selected_file:
            save_cloud_history(hist_ctx, current_folder_url, folder_stack, selected_file.name, selected_file.id, selected_file.mime_type)

        is_video = bool(selected_file.mime_type and "video" in selected_file.mime_type.lower())
        if is_video:
            download_url = _cloud_dl_url(selected_file)
            if download_mode:
                download_file(download_url, selected_file.name)
                mark_watched(title, selected_file.name)
                return True
            was_watched = stream_in_mpv(download_url, title=selected_file.name)
            if was_watched:
                mark_watched(title, selected_file.name)
            
            video_siblings = [f for f in files if f.mime_type and "video" in f.mime_type.lower()] if files else []
            vid_idx = next((j for j, f in enumerate(video_siblings) if f.name == selected_file.name), 0)
            
            while True:
                act = fzf_select(["next", "replay", "previous", "select", "quit"], f"Playing {selected_file.name}... ", header=header)
                if act == 0:
                    vid_idx = min(vid_idx + 1, len(video_siblings) - 1)
                    selected_file = video_siblings[vid_idx]
                    if hist_ctx:
                        save_cloud_history(hist_ctx, current_folder_url, folder_stack, selected_file.name, selected_file.id, selected_file.mime_type)
                    was_w = stream_in_mpv(_cloud_dl_url(selected_file), title=selected_file.name)
                    if was_w: mark_watched(title, selected_file.name)
                elif act == 1:
                    was_w = stream_in_mpv(_cloud_dl_url(selected_file), title=selected_file.name)
                    if was_w: mark_watched(title, selected_file.name)
                elif act == 2:
                    vid_idx = max(vid_idx - 1, 0)
                    selected_file = video_siblings[vid_idx]
                    if hist_ctx:
                        save_cloud_history(hist_ctx, current_folder_url, folder_stack, selected_file.name, selected_file.id, selected_file.mime_type)
                    was_w = stream_in_mpv(_cloud_dl_url(selected_file), title=selected_file.name)
                    if was_w: mark_watched(title, selected_file.name)
                elif act == 3:
                    selected_file = None
                    break
                else:
                    return True
            selected_file = None
        else:
            folder_stack.append(current_folder_url)
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
        keys = [(f.name, f.id) for f in files]
        default_idx = get_first_unwatched_idx(keys, title)
        header = f"AnimeToki CLI | {hist_ctx.title if hist_ctx else 'Cloud'}"
        while True:
            w_set = get_watched_set(title)
            labels = [format_cloud_file_label(f, is_watched=(f.name in w_set or f.id in w_set)) for f in files]
            idx, key = fzf_select(labels, "Select file: ", default_idx=default_idx, header=header, allow_toggle=True, return_key=True)
            if idx is None: return
            if key == "ctrl-w":
                toggle_watched(title, files[idx].name)
                default_idx = idx
                continue
            play_and_browse(selected_file=files[idx], current_files=files, initial_link_base64=result["url"], download_mode=download_mode, hist_ctx=hist_ctx)
            break
    elif result["type"] == "direct_episodes":
        play_direct_episodes(result["episodes"], result["selected"], download_mode, hist_ctx=hist_ctx)
    elif result["type"] == "worker_folder":
        browse_worker_folder(result["url"], download_mode, hist_ctx=hist_ctx)

def resume_history(title, entry, download_mode=False):
    logger.debug(f"Resuming history for '{title}': {entry}")
    anime_url = entry.get("anime_url")
    source_type = entry.get("source_type")
    
    if not source_type or not anime_url:
        logger.debug(f"Legacy entry or missing source_type for '{title}'. Navigating from home URL.")
        if anime_url:
            anime_download_link(anime_url, download_mode=download_mode)
        return

    hist_ctx = HistoryContext(
        title=title,
        anime_url=anime_url,
        source_label=entry.get("source_label", ""),
        source_type=source_type,
        source_url=entry.get("source_url", "")
    )

    if source_type == "cloud":
        play_and_browse(
            selected_file=None,
            current_files=None,
            initial_link_base64=None,
            download_mode=download_mode,
            hist_ctx=hist_ctx,
            resume_from=entry
        )
        if anime_url:
            anime_download_link(anime_url, download_mode=download_mode)

    elif source_type == "direct_episodes":
        episodes = entry.get("episodes", [])
        selected_idx = entry.get("selected_idx", 0)
        if episodes:
            play_direct_episodes(episodes, selected_idx, download_mode=download_mode, hist_ctx=hist_ctx, resume=True)
        if anime_url:
            anime_download_link(anime_url, download_mode=download_mode)

    elif source_type == "worker_folder":
        url = entry.get("current_folder_url") or entry.get("source_url")
        if url:
            browse_worker_folder(url, download_mode=download_mode, hist_ctx=hist_ctx, resume_from=entry)
        if anime_url:
            anime_download_link(anime_url, download_mode=download_mode)
    else:
        if anime_url:
            anime_download_link(anime_url, download_mode=download_mode)

def search(query, download_mode=False):
    anime_search_url = search_url + query
    fetch_anime_list(anime_search_url, query=query, download_mode=download_mode)

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
                    idx = fzf_select(titles, "Select history: ", header="AnimeToki CLI | Watch History")
                    if idx is not None:
                        selected_title = titles[idx]
                        entry = hist[selected_title]
                        resume_history(selected_title, entry, args.download)
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