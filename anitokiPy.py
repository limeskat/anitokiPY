import json
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
    def __init__(self, message="Loading..."):
        self.message = message
        self.stop_event = threading.Event()
        self.thread = None

    def _spin(self):
        chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        idx = 0
        while not self.stop_event.is_set():
            if sys.stdout.isatty():
                sys.stdout.write(f"\r\033[K{chars[idx % len(chars)]} {self.message}")
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
            "version": 1
        }
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

def fzf_select(items, prompt, default_idx=None):
    if not items:
        return None
    if shutil.which("fzf") and sys.stdin.isatty() and sys.stdout.isatty():
        cmd = ["fzf", "--reverse", "--cycle", "--prompt", prompt, "--with-nth=2", "--delimiter=\t"]
        if default_idx is not None and 0 <= default_idx < len(items):
            cmd.append(f"--bind=start:pos({default_idx + 1})")
            
        text = "\n".join(f"{i}\t{x}" for i, x in enumerate(items))
        p = subprocess.run(cmd, input=text, text=True, capture_output=True)
        if p.returncode == 0 and p.stdout.strip():
            try:
                return int(p.stdout.split('\t')[0])
            except (ValueError, IndexError):
                pass
    
    for i, item in enumerate(items): print(f"{i+1}. {item}")
    print("0. Back")
    p_str = f"\033[1;36m{prompt}\033[0m"
    if default_idx is not None and 0 <= default_idx < len(items):
        p_str += f" [{default_idx + 1}]: "
    idx = safe_input(p_str, len(items), default_val=default_idx + 1 if default_idx is not None else None)
    return idx - 1 if idx > 0 else None

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

def stream_in_mpv(download_url, title=None):
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
    else:
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
        ] + user_flags + [download_url]

        if title:
            mpv_flags.append(f'--force-media-title={title}')
        logger.info(f"Launching mpv: {download_url}")
        print(f"\033[1;34mPlaying {title or ''}...\033[0m")
        subprocess.run(mpv_flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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

def fetch_anime_list(anime_search_url):
    res_search_animes = safe_request('get', anime_search_url)
    if not res_search_animes:
        return None
    soup_anime_list = BeautifulSoup(res_search_animes.content, 'html.parser')

    anime_list = soup_anime_list.select('.post-item-inner > a:first-child')
    if not anime_list:
        print("No results found.")
        return None

    anime_names = [a.get('aria-label', 'Unknown') for a in anime_list]
    anime_urls = [urljoin(base_url, a['href']) for a in anime_list]
    
    idx = fzf_select(anime_names, "Select anime: ")
    return anime_urls[idx] if idx is not None else None

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

def anime_download_link(selected_anime_url):
    res_anime = safe_request('get', selected_anime_url)
    if not res_anime:
        return None
    soup_anime_list = BeautifulSoup(res_anime.content, 'html.parser')

    anime_title = soup_anime_list.find('h1', class_="post-title entry-title")
    title_text = anime_title.get_text().strip() if anime_title else "Unknown"
    update_history(title_text, selected_anime_url)
    print(f"> {title_text}")

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
        return None

    link_data = []  # list of (label, full_url, link_type)
    for link in all_links:
        label = link.get_text(strip=True)
        href = urljoin(base_url, link['href'])
        link_type = classify_link(href)
        link_data.append((label, href, link_type))

    labels = [f"{l} [{'▶' if t == 'direct_video' else '🗁' if t == 'worker_folder' else ' '}]" for l, _, t in link_data]
    idx = fzf_select(labels, "Select source: ")
    if idx is None: return None
    
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
        return {"type": "cloud", "url": base_cloud_url + "/".join(segments) + "/", "hist_ctx": hist_ctx}
    elif link_type == 'direct_video':
        # Collect all direct video links for episode navigation
        direct_episodes = [(l, u) for l, u, t in link_data if t == 'direct_video']
        selected_ep_idx = next(i for i, (l, u) in enumerate(direct_episodes) if u == selected_url)
        return {"type": "direct_episodes", "episodes": direct_episodes, "selected": selected_ep_idx, "hist_ctx": hist_ctx}
    elif link_type == 'worker_folder':
        return {"type": "worker_folder", "url": selected_url, "hist_ctx": hist_ctx}
    else:
        # Unknown type, try opening as direct URL
        return {"type": "direct_episodes", "episodes": [(label, selected_url)], "selected": 0, "hist_ctx": hist_ctx}

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
    
    while True:
        entries = fetch_worker_folder(current_url)
        if not entries:
            while folder_stack and not entries:
                current_url = folder_stack.pop()
                entries = fetch_worker_folder(current_url)
            if not entries:
                logger.debug("Worker folder unreachable and stack exhausted.")
                return False
        
        labels = [f"{l} [{'▶' if t == 'direct_video' else '🗁' if t == 'worker_folder' else ' '}]" for l, _, t in entries]
        default_idx = None
        if default_highlight:
            default_idx = next((i for i, (l, _, _) in enumerate(entries) if l == default_highlight), None)
            default_highlight = None
            
        idx = fzf_select(labels, "Select: ", default_idx=default_idx)
        
        if idx is None:
            if folder_stack:
                current_url = folder_stack.pop()
                continue
            return True
        
        label, selected_url, link_type = entries[idx]
        if hist_ctx:
            save_worker_history(hist_ctx, current_url, folder_stack, label)
        
        if link_type in ('direct_video', 'unknown'):
            # collect sibling videos for next/prev navigation
            video_entries = [(i, l, u) for i, (l, u, t) in enumerate(entries) if t in ('direct_video', 'unknown')]
            vid_idx = next((j for j, (i, l, u) in enumerate(video_entries) if i == idx), 0)
            
            while True:
                _, label, selected_url = video_entries[vid_idx]
                if hist_ctx:
                    save_worker_history(hist_ctx, current_url, folder_stack, label)
                stream_url = resolve_stream_url(selected_url)
                if download_mode:
                    download_file(stream_url, unquote(urlparse(selected_url).path.split('/')[-1]))
                    return True
                stream_in_mpv(stream_url, title=label)
                act = fzf_select(["next", "replay", "previous", "select", "quit"], f"Playing {label}... ")
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
    
    idx = selected_idx
    if idx < 0 or idx >= len(episodes):
        idx = 0

    if resume:
        res = fzf_select([e[0] for e in episodes], "Select episode: ", default_idx=idx)
        if res is None:
            return True
        idx = res

    while True:
        label, url = episodes[idx]
        if hist_ctx:
            save_direct_history(hist_ctx, episodes, idx)
            
        stream_url = resolve_stream_url(url)
        if download_mode:
            download_file(stream_url, unquote(urlparse(url).path.split('/')[-1]))
            return True
            
        stream_in_mpv(stream_url, title=label)
        act = fzf_select(["next", "replay", "previous", "select", "quit"], f"Playing {label}... ")
        
        if act == 0: idx = min(idx + 1, len(episodes) - 1)
        elif act == 1: pass
        elif act == 2: idx = max(idx - 1, 0)
        elif act == 3:
            res = fzf_select([e[0] for e in episodes], "Select episode: ", default_idx=idx)
            if res is None: return True
            idx = res
        else: return True
    return True

def play_and_browse(selected_file=None, current_files=None, initial_link_base64=None, download_mode=False, hist_ctx=None, resume_from=None):
    def _cloud_dl_url(cf: CloudFile):
        return f"{base_cloud_url}?a=download&id={cf.id}&name={base64.b64encode(unquote(cf.name).encode()).decode()}&n={cf.node_index}"

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
        default_idx = next((i for i, f in enumerate(files) if f.name == last_name), None) if (files and last_name) else None
        idx = fzf_select([f.name for f in files], "Select file: ", default_idx=default_idx)
        if idx is None:
            return True
        selected_file = files[idx]
    else:
        current_folder_url = initial_link_base64
        files = current_files
        folder_stack = []
        if files is None and current_folder_url:
            files, _ = fetch_content(current_folder_url)

    if hist_ctx and selected_file:
        save_cloud_history(hist_ctx, current_folder_url, folder_stack, selected_file.name, selected_file.id, selected_file.mime_type)

    while True:
        if selected_file and "video" in selected_file.mime_type: 
            download_url = _cloud_dl_url(selected_file)
            if download_mode:
                download_file(download_url, selected_file.name)
                return True
            stream_in_mpv(download_url, title=selected_file.name)
            
            # build sibling video list for next/prev
            video_siblings = [f for f in files if f.mime_type and "video" in f.mime_type] if files else []
            vid_idx = next((j for j, f in enumerate(video_siblings) if f.name == selected_file.name), 0)
            
            while True:
                act = fzf_select(["next", "replay", "previous", "select", "quit"], f"Playing {selected_file.name}... ")
                if act == 0:
                    vid_idx = min(vid_idx + 1, len(video_siblings) - 1)
                    selected_file = video_siblings[vid_idx]
                    if hist_ctx:
                        save_cloud_history(hist_ctx, current_folder_url, folder_stack, selected_file.name, selected_file.id, selected_file.mime_type)
                    stream_in_mpv(_cloud_dl_url(selected_file), title=selected_file.name)
                elif act == 1:
                    stream_in_mpv(_cloud_dl_url(selected_file), title=selected_file.name)
                elif act == 2:
                    vid_idx = max(vid_idx - 1, 0)
                    selected_file = video_siblings[vid_idx]
                    if hist_ctx:
                        save_cloud_history(hist_ctx, current_folder_url, folder_stack, selected_file.name, selected_file.id, selected_file.mime_type)
                    stream_in_mpv(_cloud_dl_url(selected_file), title=selected_file.name)
                elif act == 3: break
                else: return True
            files, _ = fetch_content(current_folder_url)
        else:
            folder_stack.append(current_folder_url)
            current_folder_url += base64.b64encode(unquote(selected_file.name).encode()).decode() + "/"
            files, _ = fetch_content(current_folder_url)

        while True:
            if not files:
                if folder_stack:
                    current_folder_url = folder_stack.pop()
                    files, _ = fetch_content(current_folder_url)
                    continue
                return True
                
            idx = fzf_select([f.name for f in files], "Select file: ")
            if idx is None:
                if folder_stack:
                    current_folder_url = folder_stack.pop()
                    files, _ = fetch_content(current_folder_url)
                    continue
                return True
                
            selected_file = files[idx]
            if hist_ctx:
                save_cloud_history(hist_ctx, current_folder_url, folder_stack, selected_file.name, selected_file.id, selected_file.mime_type)
            break
    return True

def _dispatch_result(result, download_mode):
    if not result:
        return
    hist_ctx = result.get("hist_ctx")
    if result["type"] == "cloud":
        files, _ = fetch_content(result["url"])
        if not files:
            return
        idx = fzf_select([f.name for f in files], "Select file: ")
        if idx is None: return
        play_and_browse(selected_file=files[idx], current_files=files, initial_link_base64=result["url"], download_mode=download_mode, hist_ctx=hist_ctx)
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
            res = anime_download_link(anime_url)
            _dispatch_result(res, download_mode)
        return

    hist_ctx = HistoryContext(
        title=title,
        anime_url=anime_url,
        source_label=entry.get("source_label", ""),
        source_type=source_type,
        source_url=entry.get("source_url", "")
    )

    if source_type == "cloud":
        ok = play_and_browse(
            selected_file=None,
            current_files=None,
            initial_link_base64=None,
            download_mode=download_mode,
            hist_ctx=hist_ctx,
            resume_from=entry
        )
        if not ok:
            print("Failed to reach saved cloud folder. Falling back to anime home page.")
            res = anime_download_link(anime_url)
            _dispatch_result(res, download_mode)

    elif source_type == "direct_episodes":
        episodes = entry.get("episodes", [])
        selected_idx = entry.get("selected_idx", 0)
        if not episodes:
            print("No episodes recorded. Falling back to anime home page.")
            res = anime_download_link(anime_url)
            _dispatch_result(res, download_mode)
        else:
            play_direct_episodes(episodes, selected_idx, download_mode=download_mode, hist_ctx=hist_ctx, resume=True)

    elif source_type == "worker_folder":
        url = entry.get("current_folder_url") or entry.get("source_url")
        if not url:
            res = anime_download_link(anime_url)
            _dispatch_result(res, download_mode)
        else:
            ok = browse_worker_folder(url, download_mode=download_mode, hist_ctx=hist_ctx, resume_from=entry)
            if not ok:
                print("Failed to reach saved worker folder. Falling back to anime home page.")
                res = anime_download_link(anime_url)
                _dispatch_result(res, download_mode)
    else:
        res = anime_download_link(anime_url)
        _dispatch_result(res, download_mode)

def search(query, download_mode=False):
    anime_search_url = search_url + query
    selected_anime_url = fetch_anime_list(anime_search_url)
    if not selected_anime_url:
        return
        
    result = anime_download_link(selected_anime_url)
    _dispatch_result(result, download_mode)

def signal_handler(sig, frame):
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

    while True:
        try:
            if args.continue_watch:
                hist = load_history()
                if not hist:
                    print("No history found.")
                    return
                else:
                    titles = list(hist.keys())
                    idx = fzf_select(titles, "Select history: ")
                    if idx is not None:
                        selected_title = titles[idx]
                        entry = hist[selected_title]
                        resume_history(selected_title, entry, args.download)
                args.continue_watch = False
                continue

            if initial_query:
                query = initial_query
                initial_query = None
            else:
                query = input("\033[1;36mSearch anime: \033[0m").strip()
                if query.lower() == "exit": break
            
            if query: search(query, args.download)
        except (EOFError, KeyboardInterrupt):
            print("\nExiting...")
            break
        except Exception as e:
            print(f"An error occurred: {e}")

if __name__ == "__main__":
    main()