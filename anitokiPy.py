import curl_cffi
import json
import base64
import subprocess
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from bs4 import Tag
from curl_cffi import requests as cffi_requests
from curl_cffi.requests import Session
from urllib.parse import unquote
from urllib.parse import urlparse


base_url = "https://animetoki.com"
search_url = "https://animetoki.com/?s="
base_cloud_url = "https://cloud.animetoki.com/"

session = cffi_requests.Session(impersonate="firefox133")
session.get("https://animetoki.com")
session.get("https://cloud.animetoki.com")

def stream_in_mpv(session, download_url):
    cookie_strings = [f"{name}={value}" for name, value in session.cookies.items()]
    print(cookie_strings)
    cookie_header = "; ".join(cookie_strings)
    print(cookie_header)
    firefox_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"
    
    mpv_flags = [
        'mpv',
        f'--user-agent={firefox_ua}',
        f'--http-header-fields=Cookie: {cookie_header}',
        '--cache=yes',
        '--demuxer-max-bytes=200MiB',
        download_url ,
        '--fullscreen'
    ]
    subprocess.run(mpv_flags)
   
def encode_2_base64(str):
    encoded_url = str
    decoded_url = unquote(encoded_url)
    base = decoded_url.encode()
    base = base64.b64encode(base)
    encoded2_64 = (base.decode('utf-8'))
    return encoded2_64

def split_url(url):
    parse_object = urlparse(url)
    path_segments = [s for s in parse_object.path.split('/') if s]
    return path_segments

def url_2_base64(path_segments,base_cloud_url):
    for i in range(len(path_segments)):
        path_segments[i] = encode_2_base64(path_segments[i])
    url_base64 = base_cloud_url + "/".join(path_segments) + "/"
    return url_base64

def fetch_anime_list(anime_search_url):
    global base_url
    res_search_animes = session.get(anime_search_url,impersonate="firefox133")
    soup_anime_list = BeautifulSoup(res_search_animes.content, 'html.parser')

    anime_list = soup_anime_list.select('.post-item-inner > a:first-child')

    len_anime_list = len(anime_list)
    anime_name_ = [0] * len_anime_list
    anime_url = [0] * len_anime_list
    i = 0
    for anime in anime_list:
        anime_name_[i] = anime['aria-label']
        print(i+1,". ",anime_name_[i])
        if isinstance(anime, Tag):
            anime_url[i] = urljoin(base_url, anime['href'])
        i = i + 1
    print("0. Exit")
    selected_index = int(input("> "))
    selected_anime_url = anime_url[selected_index-1]
    return selected_anime_url

def anime_download_link(selected_anime_url):
  global base_url
  res_anime = session.get(selected_anime_url,impersonate="firefox133")
  soup_anime_list = BeautifulSoup(res_anime.content, 'html.parser')

  anime_title = soup_anime_list.find('h1',class_="post-title entry-title")
  print(f"> {anime_title.get_text()}")

  links_content = soup_anime_list.css.select('a[href^="//cloud.animetoki.com/"]')
  len_links = len(links_content)
  link_name_ = [0] * len_links
  links = [0] * len_links
  i = 0

  for link in links_content:
      link_name_[i] = link.get_text()
      links[i] = urljoin(base_url, link['href'])
      print(i+1,".",link_name_[i])
      i =  i + 1

  selected_index = int(input("> "))
  selected_link = links[selected_index-1]
  path_segments = split_url(selected_link)
  initial_link_base64 = url_2_base64(path_segments,base_cloud_url)
  return initial_link_base64

def fetch_content(url):    
    max_retries = 3
    for attempt in range(max_retries):
        post_response = session.post(url)
        dict_json_ = post_response.json()
        initial_file_list = dict_json_.get("files")
        if initial_file_list:
            break
    
    initial_node_index = dict_json_.get("node_index")
    initial_node_index = str(initial_node_index)

    len_initial_file_list = len(dict_json_.get("files","name"))
    mimetype = [0] * len_initial_file_list
    file_id = [0] * len_initial_file_list
    file_name = [0] * len_initial_file_list
    i = 0
    for item in initial_file_list:
        mimetype[i] = item.get("mimeType")
        file_name[i] = item.get("name")
        file_id[i] = item.get("id")
        print(f"{i+1}. {file_name[i]}")   
        i += 1
    return mimetype,file_id,file_name,initial_node_index,initial_file_list

def select_content(folder_index,mimetype,file_id,file_name):
    initial_mimetype = mimetype[folder_index-1] 
    initial_file_id = file_id[folder_index-1]
    initial_file_name =  file_name[folder_index-1]
    return initial_mimetype,initial_file_id,initial_file_name


def play_and_browse(initial_mimetype,initial_file_name,initial_node_index,initial_file_id,initial_file_list,initial_link_base64):    
    current_folder_url = initial_link_base64
    selected_mimetype = initial_mimetype
    selected_file_name = initial_file_name
    file_node_index = initial_node_index
    selected_file_id = initial_file_id
    file_list = initial_file_list
    selected_link_base64 = initial_link_base64
    
    while True:
        if "video" in selected_mimetype: 
            file_name_base64 = encode_2_base64(initial_file_name)
            download_url = base_cloud_url + "?a=download&id=" + initial_file_id + "&name=" + file_name_base64 + "&n=" + initial_node_index
            print(f"Opening {download_url} in mpv")
            stream_in_mpv(session,download_url)
            mimetype, file_id, file_name, initial_node_index, initial_file_list = fetch_content(current_folder_url)

        else:
            folder_name_base64 = encode_2_base64(initial_file_name)
            current_folder_url = initial_link_base64 + folder_name_base64 + "/"
            mimetype, file_id, file_name, initial_node_index, initial_file_list = fetch_content(current_folder_url)
            selected_link_base64 = current_folder_url

        print("0. Exit")
        
        folder_index = int(input("> "))
        if folder_index == 0:
            break
        
        selected_mimetype, selected_file_id, selected_file_name = select_content(
            folder_index, mimetype, file_id, file_name
        )
        
        
def search(query):
    anime_search = query
    anime_search_url = search_url + anime_search
    selected_anime_url = fetch_anime_list(anime_search_url)
    initial_link_base64 = anime_download_link(selected_anime_url)
    mimetype, file_id, file_name, initial_node_index, initial_file_list= fetch_content(initial_link_base64)
    folder_index = int(input("> "))
    initial_mimetype, initial_file_id, initial_file_name = select_content(folder_index, mimetype, file_id, file_name)
    play_and_browse(initial_mimetype, initial_file_name, initial_node_index,initial_file_id,initial_file_list,initial_link_base64)

def main():
    query = " "
    while query != "exit":
        query = input("> ")
        if query != "exit":
            try:
                search(query)
            except Exception as e:
              print("An error occured",e)

if __name__ == "__main__":
    main()