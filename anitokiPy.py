import curl_cffi
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
from pathlib import Path
from urllib.parse import urljoin, unquote, urlparse
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

def is_termux():
    return os.environ.get('TERMUX_VERSION') is not None or os.path.isdir('/data/data/com.termux')

base_url = "https://animetoki.com"
search_url = "https://animetoki.com/?s="
base_cloud_url = "https://cloud.animetoki.com/"
session = None
hist_file = Path.home() / ".local" / "state" / "anitokipy" / "ani-hsts"

def update_history(title, url):
    hist_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(hist_file, "r") as f: hist = json.load(f)
    except: hist = {}
    hist[title] = url
    with open(hist_file, "w") as f: json.dump(hist, f)

def fzf_select(items, prompt):
    if not shutil.which("fzf"):
        for i, item in enumerate(items): print(f"{i+1}. {item}")
        print("0. Back")
        idx = safe_input(f"\033[1;36m{prompt}\033[0m", len(items))
        return idx - 1 if idx > 0 else None
    
    text = "\n".join(f"{i}\t{x}" for i, x in enumerate(items))
    p = subprocess.run(["fzf", "--reverse", "--cycle", "--prompt", prompt, "--with-nth=2", "--delimiter=\t"], 
                       input=text, text=True, capture_output=True)
    return int(p.stdout.split('\t')[0]) if p.returncode == 0 else None

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

def safe_input(prompt, max_val=None, allow_zero_back=True):
    while True:
        try:
            val = input(prompt).strip()
            if not val:
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
    global session
    cookie_strings = [f"{name}={value}" for name, value in session.cookies.items()]
    cookie_header = "; ".join(cookie_strings)
    firefox_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"

    if is_termux():
        mpv_conf_path = "/storage/emulated/0/mpv/mpv.config.mp4"
        try:
            with open(mpv_conf_path, 'w') as f:
                f.write(f'user-agent={firefox_ua}\n')
                f.write(f'http-header-fields=Cookie: {cookie_header}\n')
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
        mpv_flags = [
            'mpv',
            f'--user-agent={firefox_ua}',
            f'--http-header-fields=Cookie: {cookie_header}',
            '--cache=yes',
            '--demuxer-max-bytes=200MiB',
            download_url,
            '--fullscreen'
        ]
        if title:
            mpv_flags.append(f'--force-media-title={title}')
        logger.info(f"Launching mpv: {download_url}")
        print(f"\033[1;34mPlaying {title or ''}...\033[0m")
        subprocess.run(mpv_flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def download_file(url, output_name):
    print(f"Downloading to {output_name}...")
    global session
    cookie_strings = [f"{name}={value}" for name, value in session.cookies.items()]
    cookie_header = "; ".join(cookie_strings)
    firefox_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"
    
    curl_flags = [
        'curl', '-L',
        '-A', firefox_ua,
        '-H', f'Cookie: {cookie_header}',
        '-o', output_name,
        url
    ]
    try:
        subprocess.run(curl_flags)
    except FileNotFoundError:
        print("curl is not installed. Cannot download.")

def encode_2_base64(s):
    decoded_url = unquote(s)
    base = decoded_url.encode()
    base = base64.b64encode(base)
    encoded2_64 = base.decode('utf-8')
    return encoded2_64

def split_url(url):
    parse_object = urlparse(url)
    path_segments = [s for s in parse_object.path.split('/') if s]
    return path_segments

def url_2_base64(path_segments, base_cloud_url):
    for i in range(len(path_segments)):
        path_segments[i] = encode_2_base64(path_segments[i])
    url_base64 = base_cloud_url + "/".join(path_segments) + "/"
    return url_base64

def fetch_anime_list(anime_search_url):
    global base_url
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
            from urllib.parse import parse_qs, urlencode
            params = parse_qs(parsed.query)
            params.pop('a', None)
            new_query = urlencode(params, doseq=True)
            return parsed._replace(query=new_query).geturl()
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
                encoded_name = encode_2_base64(file_name)
                stream_url = f"{parsed.scheme}://{parsed.netloc}/?a=download&id={file_id}&name={encoded_name}"
                logger.debug(f"resolve_stream_url: resolved to {stream_url}")
                return stream_url
    except Exception as e:
        logger.debug(f"resolve_stream_url error: {e}")
        
    return url

def anime_download_link(selected_anime_url):
    global base_url
    res_anime = safe_request('get', selected_anime_url)
    if not res_anime:
        return None
    soup_anime_list = BeautifulSoup(res_anime.content, 'html.parser')

    anime_title = soup_anime_list.find('h1', class_="post-title entry-title")
    title_text = anime_title.get_text() if anime_title else "Unknown"
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
    
    if link_type == 'cloud':
        path_segments = split_url(selected_url)
        initial_link_base64 = url_2_base64(path_segments, base_cloud_url)
        return {"type": "cloud", "url": initial_link_base64}
    elif link_type == 'direct_video':
        # Collect all direct video links for episode navigation
        direct_episodes = [(l, u) for l, u, t in link_data if t == 'direct_video']
        selected_ep_idx = next(i for i, (l, u) in enumerate(direct_episodes) if u == selected_url)
        return {"type": "direct_episodes", "episodes": direct_episodes, "selected": selected_ep_idx}
    elif link_type == 'worker_folder':
        return {"type": "worker_folder", "url": selected_url}
    else:
        # Unknown type, try opening as direct URL
        return {"type": "direct_episodes", "episodes": [(label, selected_url)], "selected": 0}

def fetch_content(url):    
    post_response = safe_request('post', url)
    if not post_response:
        return None, None, None, None, None
        
    try:
        dict_json_ = post_response.json()
    except Exception as e:
        print(f"Error parsing JSON from cloud API: {e}")
        return None, None, None, None, None
        
    initial_file_list = dict_json_.get("files")
    if not initial_file_list:
        print("No files found in this folder.")
        return None, None, None, None, None
        
    initial_node_index = str(dict_json_.get("node_index", ""))

    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]

    initial_file_list.sort(key=lambda item: natural_sort_key(item.get("name", "")))

    len_initial_file_list = len(initial_file_list)
    mimetype = [None] * len_initial_file_list
    file_id = [None] * len_initial_file_list
    file_name = [None] * len_initial_file_list
    
    for i, item in enumerate(initial_file_list):
        mimetype[i] = item.get("mimeType")
        file_name[i] = item.get("name")
        file_id[i] = item.get("id")
        
    return mimetype, file_id, file_name, initial_node_index, initial_file_list

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
    
    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]
    entries.sort(key=lambda e: natural_sort_key(e[0]))
    return entries

def browse_worker_folder(url, download_mode=False):
    """Browse a workers.dev folder, allowing navigation and playback."""
    folder_stack = []
    current_url = url
    
    while True:
        entries = fetch_worker_folder(current_url)
        if not entries:
            if folder_stack:
                current_url = folder_stack.pop()
                continue
            return
        
        labels = [f"{l} [{'▶' if t == 'direct_video' else '🗁' if t == 'worker_folder' else ' '}]" for l, _, t in entries]
        idx = fzf_select(labels, "Select: ")
        
        if idx is None:
            if folder_stack:
                current_url = folder_stack.pop()
                continue
            return
        
        label, selected_url, link_type = entries[idx]
        
        if link_type in ('direct_video', 'unknown'):
            # collect sibling videos for next/prev navigation
            video_entries = [(i, l, u) for i, (l, u, t) in enumerate(entries) if t in ('direct_video', 'unknown')]
            vid_idx = next((j for j, (i, l, u) in enumerate(video_entries) if i == idx), 0)
            
            while True:
                _, label, selected_url = video_entries[vid_idx]
                stream_url = resolve_stream_url(selected_url)
                if download_mode:
                    download_file(stream_url, unquote(urlparse(selected_url).path.split('/')[-1]))
                    return
                stream_in_mpv(stream_url, title=label)
                act = fzf_select(["next", "replay", "previous", "select", "quit"], f"Playing {label}... ")
                if act == 0: vid_idx = min(vid_idx + 1, len(video_entries) - 1)
                elif act == 1: pass
                elif act == 2: vid_idx = max(vid_idx - 1, 0)
                elif act == 3: break
                else: return
        elif link_type == 'worker_folder':
            folder_stack.append(current_url)
            current_url = selected_url


def play_direct_episodes(episodes, selected_idx, download_mode=False):
    """Play from a list of direct video episode links."""
    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]
    episodes.sort(key=lambda e: natural_sort_key(e[0]))
    
    idx = selected_idx
    while True:
        label, url = episodes[idx]
        stream_url = resolve_stream_url(url)
        if download_mode:
            download_file(stream_url, unquote(urlparse(url).path.split('/')[-1]))
            return
            
        stream_in_mpv(stream_url, title=label)
        act = fzf_select(["next", "replay", "previous", "select", "quit"], f"Playing {label}... ")
        
        if act == 0: idx = min(idx + 1, len(episodes) - 1)
        elif act == 1: pass
        elif act == 2: idx = max(idx - 1, 0)
        elif act == 3:
            res = fzf_select([e[0] for e in episodes], "Select episode: ")
            if res is None: return
            idx = res
        else: return

def select_content(folder_index, mimetype, file_id, file_name):
    initial_mimetype = mimetype[folder_index-1] 
    initial_file_id = file_id[folder_index-1]
    initial_file_name = file_name[folder_index-1]
    return initial_mimetype, initial_file_id, initial_file_name

def play_and_browse(initial_mimetype, initial_file_name, initial_node_index, initial_file_id, initial_file_list, initial_link_base64, download_mode=False):    
    current_folder_url = initial_link_base64
    selected_mimetype = initial_mimetype
    selected_file_name = initial_file_name
    file_node_index = initial_node_index
    selected_file_id = initial_file_id
    
    folder_stack = []
    # pre-fetch so file_name/file_id/mimetype are bound for sibling navigation
    mimetype, file_id, file_name, file_node_index, file_list = fetch_content(current_folder_url)
    
    while True:
        if "video" in selected_mimetype: 

            file_name_base64 = encode_2_base64(selected_file_name)
            download_url = base_cloud_url + "?a=download&id=" + selected_file_id + "&name=" + file_name_base64 + "&n=" + file_node_index
            if download_mode:
                download_file(download_url, selected_file_name)
                return
            stream_in_mpv(download_url, title=selected_file_name)
            
            # build sibling video list for next/prev
            if mimetype:
                video_siblings = [(i, fn, fi) for i, (fn, fi, mt) in enumerate(zip(file_name, file_id, mimetype)) if mt and "video" in mt]
            else:
                video_siblings = []
            vid_idx = next((j for j, (i, fn, fi) in enumerate(video_siblings) if fn == selected_file_name), 0)
            
            while True:
                act = fzf_select(["next", "replay", "previous", "select", "quit"], f"Playing {selected_file_name}... ")
                if act == 0:
                    vid_idx = min(vid_idx + 1, len(video_siblings) - 1)
                    _, selected_file_name, selected_file_id = video_siblings[vid_idx]
                    file_name_base64 = encode_2_base64(selected_file_name)
                    download_url = base_cloud_url + "?a=download&id=" + selected_file_id + "&name=" + file_name_base64 + "&n=" + file_node_index
                    stream_in_mpv(download_url, title=selected_file_name)
                elif act == 1:
                    stream_in_mpv(download_url, title=selected_file_name)
                elif act == 2:
                    vid_idx = max(vid_idx - 1, 0)
                    _, selected_file_name, selected_file_id = video_siblings[vid_idx]
                    file_name_base64 = encode_2_base64(selected_file_name)
                    download_url = base_cloud_url + "?a=download&id=" + selected_file_id + "&name=" + file_name_base64 + "&n=" + file_node_index
                    stream_in_mpv(download_url, title=selected_file_name)
                elif act == 3: break
                else: return
            mimetype, file_id, file_name, file_node_index, file_list = fetch_content(current_folder_url)
        else:
            folder_name_base64 = encode_2_base64(selected_file_name)
            folder_stack.append(current_folder_url)
            current_folder_url = current_folder_url + folder_name_base64 + "/"
            mimetype, file_id, file_name, file_node_index, file_list = fetch_content(current_folder_url)

        while True:
            if not mimetype:
                if folder_stack:
                    current_folder_url = folder_stack.pop()
                    mimetype, file_id, file_name, file_node_index, file_list = fetch_content(current_folder_url)
                    continue
                return
                
            idx = fzf_select(file_name, "Select file: ")
            if idx is None:
                if folder_stack:
                    current_folder_url = folder_stack.pop()
                    mimetype, file_id, file_name, file_node_index, file_list = fetch_content(current_folder_url)
                    continue
                return
                
            selected_mimetype, selected_file_id, selected_file_name = select_content(idx + 1, mimetype, file_id, file_name)
            break

def search(query, download_mode=False):
    anime_search_url = search_url + query
    selected_anime_url = fetch_anime_list(anime_search_url)
    if not selected_anime_url:
        return
        
    result = anime_download_link(selected_anime_url)
    if not result:
        return
    
    if result["type"] == "cloud":
        mimetype, file_id, file_name, initial_node_index, initial_file_list = fetch_content(result["url"])
        if not mimetype:
            return
        idx = fzf_select(file_name, "Select file: ")
        if idx is None: return
        initial_mimetype, initial_file_id, initial_file_name = select_content(idx + 1, mimetype, file_id, file_name)
        play_and_browse(initial_mimetype, initial_file_name, initial_node_index, initial_file_id, initial_file_list, result["url"], download_mode)
    
    elif result["type"] == "direct_episodes":
        play_direct_episodes(result["episodes"], result["selected"], download_mode)
    
    elif result["type"] == "worker_folder":
        browse_worker_folder(result["url"], download_mode)

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

    while True:
        try:
            if getattr(args, 'continue_watch', False):
                try:
                    with open(hist_file, "r") as f: hist = json.load(f)
                    titles = list(hist.keys())
                    idx = fzf_select(titles, "Select history: ")
                    if idx is not None:
                        result = anime_download_link(hist[titles[idx]])
                        if result and result["type"] == "direct_episodes":
                            play_direct_episodes(result["episodes"], result["selected"], args.download)
                        elif result and result["type"] == "worker_folder":
                            browse_worker_folder(result["url"], args.download)
                        elif result and result["type"] == "cloud":
                            mimetype, file_id, file_name, initial_node_index, initial_file_list = fetch_content(result["url"])
                            if mimetype:
                                f_idx = fzf_select(file_name, "Select file: ")
                                if f_idx is not None:
                                    imime, ifid, iname = select_content(f_idx + 1, mimetype, file_id, file_name)
                                    play_and_browse(imime, iname, initial_node_index, ifid, initial_file_list, result["url"], args.download)
                except Exception as e: print("No history found.")
                args.continue_watch = False
                continue

            if initial_query:
                query = initial_query
                initial_query = None
            else:
                query = input("\033[1;36mSearch anime: \033[0m").strip()
                if query.lower() == "exit": break
            
            if query: search(query, args.download)
        except Exception as e:
            print(f"An error occurred: {e}")
        except EOFError:
            break

if __name__ == "__main__":
    main()