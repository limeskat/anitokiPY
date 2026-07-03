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
from urllib.parse import urljoin, unquote, urlparse
from bs4 import BeautifulSoup, Tag
from curl_cffi import requests as cffi_requests

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

def stream_in_mpv(download_url):
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

def anime_download_link(selected_anime_url):
    global base_url
    res_anime = safe_request('get', selected_anime_url)
    if not res_anime:
        return None
    soup_anime_list = BeautifulSoup(res_anime.content, 'html.parser')

    anime_title = soup_anime_list.find('h1', class_="post-title entry-title")
    if anime_title:
        print(f"> {anime_title.get_text()}")

    links_content = soup_anime_list.css.select('a[href^="//cloud.animetoki.com/"]')
    if not links_content:
        print("No streaming links found for this anime.")
        return None
        
    len_links = len(links_content)
    links = [None] * len_links

    for i, link in enumerate(links_content):
        link_name = link.get_text()
        links[i] = urljoin(base_url, link['href'])
        print(f"{i+1}. {link_name}")

    print("0. Back")
    selected_index = safe_input("> ", len_links)
    if selected_index == 0:
        return None
    selected_link = links[selected_index-1]
    path_segments = split_url(selected_link)
    initial_link_base64 = url_2_base64(path_segments, base_cloud_url)
    return initial_link_base64

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
                stream_in_mpv(download_url)
            
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
        
    initial_link_base64 = anime_download_link(selected_anime_url)
    if not initial_link_base64:
        return
        
    mimetype, file_id, file_name, initial_node_index, initial_file_list = fetch_content(initial_link_base64)
    if not mimetype:
        return
        
    print("0. Back")
    folder_index = safe_input("> ", len(mimetype))
    if folder_index == 0:
        return
    initial_mimetype, initial_file_id, initial_file_name = select_content(folder_index, mimetype, file_id, file_name)
    play_and_browse(initial_mimetype, initial_file_name, initial_node_index, initial_file_id, initial_file_list, initial_link_base64, download_mode)

def signal_handler(sig, frame):
    print("\nExiting...")
    sys.exit(0)

def main():
    signal.signal(signal.SIGINT, signal_handler)
    
    parser = argparse.ArgumentParser(description="CLI anime player for animetoki.com")
    parser.add_argument("query", nargs="*", help="Search query (if provided, runs in non-interactive mode)")
    parser.add_argument("-d", "--download", action="store_true", help="Download the video instead of playing it")
    args = parser.parse_args()

    check_deps(args.download)
    init_session()

    initial_query = " ".join(args.query) if args.query else None

    while True:
        try:
            if initial_query:
                query = initial_query
                initial_query = None  # Clear it so we prompt next time
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