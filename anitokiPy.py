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
        subprocess.Popen(
            mpv_flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )

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

    len_anime_list = len(anime_list)
    anime_url = [None] * len_anime_list
    
    for i, anime in enumerate(anime_list):
        anime_name = anime.get('aria-label', 'Unknown')
        print(f"{i+1}. {anime_name}")
        if isinstance(anime, Tag):
            anime_url[i] = urljoin(base_url, anime['href'])
            
    print("0. Back")
    selected_index = safe_input("> ", len_anime_list)
    if selected_index == 0:
        return None
    return anime_url[selected_index-1]

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
    if anime_title:
        print(f"> {anime_title.get_text()}")

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

    for i, (label, href, link_type) in enumerate(link_data):
        tag = ""
        if link_type == 'direct_video':
            tag = " [▶ ]"
        elif link_type == 'worker_folder':
            tag = " [🗁 ]"
        print(f"{i+1}. {label}{tag}")

    print("0. Back")
    selected_index = safe_input("> ", len(link_data))
    if selected_index == 0:
        return None
    
    label, selected_url, link_type = link_data[selected_index - 1]
    
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
        print(f"{i+1}. {file_name[i]}")   
        
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
        
        for i, (label, href, link_type) in enumerate(entries):
            tag = " [▶ ]" if link_type == 'direct_video' else " [🗁 ]" if link_type == 'worker_folder' else ""
            print(f"{i+1}. {label}{tag}")
        
        print("0. Back")
        user_input = safe_input("> ", len(entries))
        if user_input == 0:
            if folder_stack:
                current_url = folder_stack.pop()
                continue
            return
        
        label, selected_url, link_type = entries[user_input - 1]
        
        if link_type == 'direct_video':
            stream_url = resolve_stream_url(selected_url)
            if download_mode:
                filename = unquote(urlparse(selected_url).path.split('/')[-1])
                download_file(stream_url, filename)
            else:
                stream_in_mpv(stream_url, title=label)
        elif link_type == 'worker_folder':
            folder_stack.append(current_url)
            current_url = selected_url
        else:
            stream_url = resolve_stream_url(selected_url)
            if download_mode:
                filename = unquote(urlparse(selected_url).path.split('/')[-1])
                download_file(stream_url, filename)
            else:
                stream_in_mpv(stream_url, title=label)

def play_direct_episodes(episodes, selected_idx, download_mode=False):
    """Play from a list of direct video episode links."""
    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]
    episodes.sort(key=lambda e: natural_sort_key(e[0]))
    
    # Play the initially selected episode
    label, url = episodes[selected_idx]
    stream_url = resolve_stream_url(url)
    if download_mode:
        filename = unquote(urlparse(url).path.split('/')[-1])
        download_file(stream_url, filename)
    else:
        stream_in_mpv(stream_url, title=label)
    
    # Show list for further selection
    while True:
        for i, (label, url) in enumerate(episodes):
            print(f"{i+1}. {label}")
        print("0. Back")
        user_input = safe_input("> ", len(episodes))
        if user_input == 0:
            return
        label, url = episodes[user_input - 1]
        stream_url = resolve_stream_url(url)
        if download_mode:
            filename = unquote(urlparse(url).path.split('/')[-1])
            download_file(stream_url, filename)
        else:
            stream_in_mpv(stream_url, title=label)

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
    
    while True:
        if "video" in selected_mimetype: 
            file_name_base64 = encode_2_base64(selected_file_name)
            download_url = base_cloud_url + "?a=download&id=" + selected_file_id + "&name=" + file_name_base64 + "&n=" + file_node_index
            if download_mode:
                download_file(download_url, selected_file_name)
            else:
                stream_in_mpv(download_url, title=selected_file_name)
            
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
                else:
                    return

            print("0. Back")
                
            user_input = safe_input("> ", len(mimetype))
            
            if user_input == 0:
                if folder_stack:
                    current_folder_url = folder_stack.pop()
                    mimetype, file_id, file_name, file_node_index, file_list = fetch_content(current_folder_url)
                    continue
                else:
                    return

            selected_mimetype, selected_file_id, selected_file_name = select_content(
                user_input, mimetype, file_id, file_name
            )
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
        print("0. Back")
        folder_index = safe_input("> ", len(mimetype))
        if folder_index == 0:
            return
        initial_mimetype, initial_file_id, initial_file_name = select_content(folder_index, mimetype, file_id, file_name)
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
            if initial_query:
                query = initial_query
                initial_query = None
            else:
                query = input(">").strip()
                if query.lower() == "exit":
                    break
            
            if query:
                search(query, args.download)
        except Exception as e:
            print(f"An error occurred: {e}")
        except EOFError:
            break

if __name__ == "__main__":
    main()