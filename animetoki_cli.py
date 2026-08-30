import sys
import json
import os
import shutil
import time
import re
import base64
import socket
import subprocess
import argparse
import signal
import threading
import shlex
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, unquote, quote, urlparse, parse_qs, urlencode

try:
    import fcntl
except ImportError:
    fcntl = None

try:
    import termios
except ImportError:
    termios = None

# Set up logging early so all components can log warnings/errors
log_dir = Path.home() / ".local" / "share" / "animetoki-cli"
try:
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_dir / "animetoki-cli.log"),
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
except Exception:
    logging.basicConfig(level=logging.ERROR)

logger = logging.getLogger("animetoki-cli")

base_url = "https://animetoki.com"
search_url = "https://animetoki.com/?s="

hist_dir = Path.home() / ".local" / "state" / "animetoki-cli"
hist_dir.mkdir(parents=True, exist_ok=True)
old_hist_file = Path.home() / ".local" / "state" / "anitokipy" / "ani-hsts"

hist_file = hist_dir / "ani-hsts"
if not hist_file.exists() and old_hist_file.exists():
    try:
        import shutil
        shutil.copy2(old_hist_file, hist_file)
    except Exception:
        hist_file = old_hist_file

COOKIE_PATH = hist_dir / "cookies.json"
CACHE_FILE = hist_dir / "cache.json"

session = None

# --- Pre-compiled Regular Expressions for Performance ---
RE_RELEASE_PREFIX = re.compile(r'\[(?:AnimeSakura|AnimeToki)\]\s*', re.IGNORECASE)
RE_BRACKETS = re.compile(r'\[(.*?)\]')
RE_RESOLUTION = re.compile(r'\d{3,4}p|\b4K\b', re.IGNORECASE)
RE_DIGITS_ONLY = re.compile(r'\D')
RE_PAREN_CONTENT = re.compile(r'\((.*?)\)')
RE_SEASONS = re.compile(r'Seasons?\s*(\d+)(?:\s*[-+to\s]+\s*(\d+))?', re.IGNORECASE)
RE_ALL_SEASONS = re.compile(r'All Seasons?', re.IGNORECASE)
RE_FINAL_SEASON = re.compile(r'Final Season', re.IGNORECASE)
RE_MOVIES = re.compile(r'\bMovies?\b|\bThe Movie\b', re.IGNORECASE)
RE_OVAS = re.compile(r'\bOVAs?\b|\bOAV\b', re.IGNORECASE)
RE_SPECIALS = re.compile(r'\bSpecials?\b|\bShorts\b', re.IGNORECASE)
RE_COMPLETE = re.compile(r'Complete Series|Complete Movies Series|Complete', re.IGNORECASE)
RE_DIRECTORS_CUT = re.compile(r'Directors Cut|DC', re.IGNORECASE)
RE_OST = re.compile(r'OST', re.IGNORECASE)
RE_TRIM_PUNCT = re.compile(r'^\W+|\W+$')
RE_NATURAL_SORT_NORM = re.compile(r'[^\w\s]')
RE_WHITESPACE = re.compile(r'\s+')
RE_DIGIT_SPLIT = re.compile(r'([0-9]+)')
RE_RAW_RELEASE = re.compile(r'\bDual Audio\b|\bSubbed\b|\b1080p\b|\b720p\b|\[.*?\]|Season \d+', re.IGNORECASE)
RE_BASE64_INVALID = re.compile(r"[%\s\[\]\(\)\\]")
RE_BASE64_FULL = re.compile(r"[A-Za-z0-9+/=_~-]+")
RE_VIDEO_EXT = re.compile(r'\.(?:mkv|mp4|avi|webm)$', re.IGNORECASE)
RE_TAG_WORDS = re.compile(
    r'HEVC|HVEC|x265|x264|AVC|10bit|8bit|Dual[- ]Audio|Tri[- ]Audio|Multi[- ]Audio|'
    r'Multi[- ]Subs?|Eng[- ]Subs?|Softsubs?|Hardsubs?|Subbed|Dubbed|BD|BDRip|WEBRip|WEB-DL|AAC|OPUS|FLAC|AC3',
    re.IGNORECASE
)
RE_ANSI_ESCAPE = re.compile(r'\033\[[0-9;]*m')
RE_CODEC_HEVC = re.compile(r'\bHEVC\b', re.IGNORECASE)
RE_CODEC_AV1 = re.compile(r'\bAV1\b', re.IGNORECASE)
RE_CODEC_BD = re.compile(r'\bBD\b', re.IGNORECASE)
RE_CODEC_10BIT = re.compile(r'\b10bit\b', re.IGNORECASE)

AUDIO_PATTERNS = [
    (re.compile(r'\bDual Audio\b|\bDual\b', re.IGNORECASE), 'Dual'),
    (re.compile(r'\bTri Audio\b|\bTri\b', re.IGNORECASE), 'Tri'),
    (re.compile(r'\bMulti Audio\b|\bMulti\b', re.IGNORECASE), 'Multi'),
    (re.compile(r'\bEnglish Subbed\b|\bSubbed\b', re.IGNORECASE), 'Sub'),
    (re.compile(r'\bEnglish Dubbed\b|\bDubbed\b', re.IGNORECASE), 'Dub')
]

CODEC_PATTERNS = [
    ('HEVC', RE_CODEC_HEVC),
    ('AV1', RE_CODEC_AV1),
    ('BD', RE_CODEC_BD),
    ('10bit', RE_CODEC_10BIT)
]

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

# --- Thread-Safe & Atomic In-Memory Storage Classes ---
class FetchCacheStore:
    def __init__(self, file_path: Path):
        self.file_path = file_path
        self._cache: dict | None = None
        self._lock = threading.Lock()

    def get(self, key: str, ttl: int = 300):
        with self._lock:
            if self._cache is None:
                self._cache = self._load()
            entry = self._cache.get(key)
            if isinstance(entry, dict) and "time" in entry and "data" in entry:
                if time.time() - entry["time"] < ttl:
                    return entry["data"]
            return None

    def set(self, key: str, data) -> None:
        with self._lock:
            if self._cache is None:
                self._cache = self._load()
            self._cache[key] = {"time": time.time(), "data": data}
            self._save(self._cache)

    def _load(self) -> dict:
        if not self.file_path.exists():
            return {}
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read fetch cache from {self.file_path}: {e}")
            return {}

    def _save(self, data: dict) -> None:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self.file_path)
        except OSError as e:
            logger.error(f"Failed to save fetch cache to {self.file_path}: {e}")

fetch_cache_store = FetchCacheStore(CACHE_FILE)

def load_fetch_cache():
    return fetch_cache_store._load()

def save_fetch_cache(cache):
    fetch_cache_store._save(cache)

def get_cached_fetch(key, ttl=300):
    return fetch_cache_store.get(key, ttl=ttl)

def set_cached_fetch(key, data):
    fetch_cache_store.set(key, data)

class HistoryStore:
    def __init__(self, file_path: Path):
        self.file_path = file_path
        self._cache: dict | None = None
        self._lock = threading.Lock()

    def get_all(self, force_reload: bool = False) -> dict:
        with self._lock:
            if self._cache is not None and not force_reload:
                return self._cache
            self._cache = self._load_from_disk()
            return self._cache

    def _load_from_disk(self) -> dict:
        if not self.file_path.exists():
            return {}
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                if fcntl:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                    except OSError:
                        pass
                hist = json.load(f)
                if isinstance(hist, dict):
                    return self._clean_and_dedup(hist)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load history from {self.file_path}: {e}")
        return {}

    def _clean_and_dedup(self, hist: dict) -> dict:
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
            
            if clean_t in cleaned_hist:
                existing_v = cleaned_hist[clean_t]
                w1 = existing_v.get("watched", [])
                w2 = v.get("watched", [])
                v["watched"] = list(set(w1 + w2))

            cleaned_hist[clean_t] = v

        sorted_entries = sorted(
            cleaned_hist.items(),
            key=lambda item: item[1].get("last_played", "") if isinstance(item[1], dict) else "",
            reverse=True
        )
        return dict(sorted_entries)

    def save_entry(self, title: str, payload: dict) -> None:
        with self._lock:
            hist = self._load_from_disk()
            target_url = payload.get("anime_url", "")
            clean_target = parse_anime_title(title)[0]
            
            existing = {}
            existing_watched = []
            keys_to_remove = []
            
            for k, v in hist.items():
                if isinstance(v, dict):
                    k_clean = parse_anime_title(k)[0]
                    v_url = v.get("anime_url", "")
                    if (target_url and v_url == target_url) or k == title or k_clean == clean_target:
                        if not existing:
                            existing = v
                        if "watched" in v and isinstance(v["watched"], list):
                            existing_watched.extend(v["watched"])
                        if "raw_title" in v and "raw_title" not in payload:
                            payload["raw_title"] = v["raw_title"]
                        keys_to_remove.append(k)

            for k in keys_to_remove:
                del hist[k]

            if "watched" in payload and isinstance(payload["watched"], list):
                payload["watched"] = list(set(payload["watched"]))
            else:
                payload["watched"] = list(set(existing_watched))

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
            self._cache = hist
            self._flush_to_disk(hist)

    def delete_entry(self, title: str) -> bool:
        with self._lock:
            hist = self._load_from_disk()
            clean_target = parse_anime_title(title)[0]
            keys_to_delete = [k for k in hist.keys() if k == title or parse_anime_title(k)[0] == clean_target]
            if keys_to_delete:
                for k in keys_to_delete:
                    del hist[k]
                self._cache = hist
                self._flush_to_disk(hist)
                return True
            return False

    def _flush_to_disk(self, hist: dict) -> None:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self.file_path.with_suffix(".tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                if fcntl:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    except OSError:
                        pass
                json.dump(hist, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            tmp_file.replace(self.file_path)
        except OSError as e:
            logger.error(f"Failed writing history to {self.file_path}: {e}")

    def clear(self) -> None:
        with self._lock:
            self._cache = {}
            try:
                self.file_path.unlink(missing_ok=True)
            except (OSError, TypeError):
                if self.file_path.exists():
                    self.file_path.unlink()

history_store = HistoryStore(hist_file)

def save_cookies():
    if not session or not hasattr(session, "cookies"):
        return
    try:
        COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
        jar = getattr(session.cookies, "jar", session.cookies)
        cookies_data = []
        for c in jar:
            cookies_data.append({
                "name": c.name,
                "value": c.value,
                "domain": getattr(c, "domain", ""),
                "path": getattr(c, "path", "/")
            })
        tmp_file = COOKIE_PATH.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(cookies_data, f)
            f.flush()
            os.fsync(f.fileno())
        tmp_file.replace(COOKIE_PATH)
        try:
            COOKIE_PATH.chmod(0o600)
        except OSError:
            pass
    except OSError as e:
        logger.error(f"Failed to save cookies: {e}")

def load_cookies():
    if not session or not COOKIE_PATH.exists():
        return
    try:
        with open(COOKIE_PATH, "r", encoding="utf-8") as f:
            cookies_data = json.load(f)
        if isinstance(cookies_data, list):
            for c in cookies_data:
                if isinstance(c, dict) and "name" in c and "value" in c:
                    kwargs = {}
                    if c.get("domain"): kwargs["domain"] = c["domain"]
                    if c.get("path"): kwargs["path"] = c["path"]
                    session.cookies.set(c["name"], c["value"], **kwargs)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load cookies from {COOKIE_PATH}: {e}")

def _cookie_header():
    if not session or not hasattr(session, "cookies"):
        return ""
    try:
        jar = getattr(session.cookies, "jar", session.cookies)
        cookies_dict = {c.name: c.value for c in jar}
        return "; ".join(f"{name}={value}" for name, value in cookies_dict.items())
    except Exception as e:
        logger.debug(f"Failed generating cookie header: {e}")
        return ""

def init_session(force_refresh=False):
    global session
    if session is not None and not force_refresh:
        return
    from curl_cffi import requests as cffi_requests
    session = cffi_requests.Session(impersonate="firefox133")
    if not force_refresh:
        load_cookies()
    for endpoint in ("https://animetoki.com", "https://cloud.animetoki.com", "https://drive.animetoki.com"):
        try:
            session.get(endpoint, timeout=4)
        except Exception as e:
            logger.debug(f"Session ping to {endpoint} failed: {e}")
    save_cookies()

def safe_request(method, url, max_retries=3, timeout=4, **kwargs):
    global session
    if session is None:
        init_session()
    for attempt in range(max_retries):
        try:
            resp = getattr(session, method)(url, impersonate="firefox133", timeout=timeout, **kwargs)
            if resp.status_code == 403 or (resp.text and "Session Expired" in resp.text):
                init_session(force_refresh=True)
                resp = getattr(session, method)(url, impersonate="firefox133", timeout=timeout, **kwargs)
            resp.raise_for_status()
            save_cookies()
            return resp
        except Exception as e:
            if logger and logger.handlers:
                logger.debug(f"Request failed ({method} {url}, attempt {attempt+1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                return None
            time.sleep(0.5)
    return None

def is_base64_segment(s: str) -> bool:
    if not s:
        return False
    if RE_BASE64_INVALID.search(s):
        return False
    if not RE_BASE64_FULL.fullmatch(s):
        return False
    try:
        padded = s + "=" * (-len(s) % 4)
        decoded_bytes = base64.b64decode(padded.encode("ascii"))
        decoded_str = decoded_bytes.decode("utf-8")
        return all(ord(c) >= 32 or c in "\n\r\t" for c in decoded_str)
    except Exception:
        return False

def encode_cloud_url(url: str) -> str:
    parsed = urlparse(url)
    if "cloud.animetoki.com" not in parsed.netloc and "drive.animetoki.com" not in parsed.netloc:
        return url
        
    domain_url = f"https://{parsed.netloc}/"
    segments = []
    for s in parsed.path.split("/"):
        if not s:
            continue
        unquoted = unquote(s)
        if is_base64_segment(unquoted):
            segments.append(unquoted)
        else:
            encoded_seg = base64.b64encode(unquoted.encode("utf-8")).decode("utf-8")
            segments.append(encoded_seg)
            
    return domain_url + "/".join(segments) + "/" if segments else domain_url

def natural_sort_key(s):
    s_norm = RE_NATURAL_SORT_NORM.sub(' ', s.lower())
    s_norm = RE_WHITESPACE.sub(' ', s_norm).strip()
    return [int(text) if text.isdigit() else text for text in RE_DIGIT_SPLIT.split(s_norm)]

def is_raw_release_title(t: str) -> bool:
    return bool(RE_RAW_RELEASE.search(str(t)))

def parse_anime_title(raw_title: str) -> tuple[str, str, str]:
    working = str(raw_title).strip()
    working = RE_RELEASE_PREFIX.sub('', working).strip()

    brackets = RE_BRACKETS.findall(working)
    res_tag = ''
    for b in brackets:
        res_matches = RE_RESOLUTION.findall(b)
        if res_matches:
            if len(res_matches) > 1:
                res_tag = '-'.join(sorted(res_matches, key=lambda x: int(RE_DIGITS_ONLY.sub('', x)) if RE_DIGITS_ONLY.sub('', x) else 0, reverse=True))
            else:
                res_tag = res_matches[0]

    working = RE_BRACKETS.sub('', working).strip()

    audio_tag = ''
    for pat, val in AUDIO_PATTERNS:
        if pat.search(working):
            audio_tag = val
            working = pat.sub('', working).strip()
            break

    codec_tags = []
    for codec, pat in CODEC_PATTERNS:
        if pat.search(working):
            codec_tags.append(codec)
            working = pat.sub('', working).strip()

    p_match = RE_PAREN_CONTENT.search(working)
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

            m_s = RE_SEASONS.search(rem_item)
            if m_s:
                has_seasons = True
                s1 = int(m_s.group(1))
                s2 = int(m_s.group(2)) if m_s.group(2) else None
                generic_tags.append(f'S{s1}-S{s2}' if s2 else f'S{s1}')
                rem_item = RE_SEASONS.sub('', rem_item).strip()
            elif RE_ALL_SEASONS.search(rem_item):
                has_seasons = True
                generic_tags.append('Seasons')
                rem_item = RE_ALL_SEASONS.sub('', rem_item).strip()

            if RE_FINAL_SEASON.search(rem_item):
                has_seasons = True
                generic_tags.append('Final')
                rem_item = RE_FINAL_SEASON.sub('', rem_item).strip()

            if RE_MOVIES.search(rem_item):
                if 'M' not in generic_tags: generic_tags.append('M')
                rem_item = RE_MOVIES.sub('', rem_item).strip()

            if RE_OVAS.search(rem_item):
                if 'OVA' not in generic_tags: generic_tags.append('OVA')
                rem_item = RE_OVAS.sub('', rem_item).strip()

            if RE_SPECIALS.search(rem_item):
                if 'SP' not in generic_tags: generic_tags.append('SP')
                rem_item = RE_SPECIALS.sub('', rem_item).strip()

            if RE_COMPLETE.search(rem_item):
                if 'Complete' not in generic_tags: generic_tags.append('Complete')
                rem_item = RE_COMPLETE.sub('', rem_item).strip()

            if RE_DIRECTORS_CUT.search(rem_item):
                if 'DC' not in generic_tags: generic_tags.append('DC')
                rem_item = RE_DIRECTORS_CUT.sub('', rem_item).strip()

            if RE_OST.search(rem_item):
                if 'OST' not in generic_tags: generic_tags.append('OST')
                rem_item = RE_OST.sub('', rem_item).strip()

            rem_item = RE_TRIM_PUNCT.sub('', rem_item)
            if rem_item:
                named_subtitles.append(rem_item)

    base_title = RE_COMPLETE.sub('', base_title).strip()
    base_title = RE_WHITESPACE.sub(' ', base_title).strip(' -:')

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

def save_history_entry(title, payload):
    history_store.save_entry(title, payload)

def load_history():
    return history_store.get_all(force_reload=True)

def delete_history_entry(title):
    return history_store.delete_entry(title)


def toggle_watched_entry(title, item_name=None):
    hist = load_history()
    if not title:
        return
    
    clean_t = parse_anime_title(title)[0]
    entry = None
    if hist:
        entry = hist.get(title)
        if not isinstance(entry, dict):
            for k, v in hist.items():
                if k == title or parse_anime_title(k)[0] == clean_t:
                    entry = v
                    break

    if not isinstance(entry, dict):
        url = ""
        cached_search = get_cached_fetch("search:" + clean_t.lower()) or get_cached_fetch("search:" + clean_t)
        if cached_search and cached_search.get("urls"):
            url = cached_search["urls"][0]
        entry = {
            "anime_url": url,
            "raw_title": title,
            "tags": parse_anime_title(title)[2],
            "last_played": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "watched": [],
            "version": 1
        }

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
    save_history_entry(clean_t, entry)

def get_watched_list(hist_ctx_or_title):
    if not hist_ctx_or_title:
        return []
    if isinstance(hist_ctx_or_title, str):
        title = hist_ctx_or_title
    else:
        title = getattr(hist_ctx_or_title, "title", "")
        if callable(title):
            title = str(hist_ctx_or_title)
    if not title or not isinstance(title, str):
        return []

    hist = load_history()
    entry = hist.get(title)
    if not isinstance(entry, dict):
        clean_t = parse_anime_title(title)[0]
        for k, v in hist.items():
            if k == title or parse_anime_title(k)[0] == clean_t:
                entry = v
                break
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

def classify_link(url):
    video_exts = ('.mkv', '.mp4', '.avi', '.webm')
    parsed = urlparse(url)
    path_lower = parsed.path.lower()
    query_lower = parsed.query.lower()
    
    if 'cloud.animetoki.com' in parsed.netloc or 'drive.animetoki.com' in parsed.netloc:
        return 'cloud'
    
    for ext in video_exts:
        if path_lower.endswith(ext) or f'{ext}?' in path_lower or f'{ext}?' in url.lower():
            return 'direct_video'
    if 'a=view' in query_lower:
        return 'direct_video'
    
    if parsed.path.endswith('/'):
        return 'worker_folder'
    
    return 'unknown'

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

def format_anime_title_label(raw_title: str) -> str:
    clean_title, path_dir, tags_str = parse_anime_title(raw_title)
    if not tags_str:
        return clean_title
    cols, _ = shutil.get_terminal_size((80, 24))
    avail_width = max(30, cols - 8)
    pad_count = max(2, avail_width - len(clean_title) - len(tags_str))
    spaces = " " * pad_count
    return f"{clean_title}{spaces}\033[90m{tags_str}\033[0m"

def format_item_label(name: str, item_type: str, size_str: str = "", is_watched: bool = False) -> str:
    is_folder = item_type in ('folder', 'worker_folder', 'cloud')
    clean_name = re.sub(r'\[(?:AnimeSakura|AnimeToki)\]\s*', '', name, flags=re.IGNORECASE).strip()

    res_tag = ''
    if not is_folder:
        clean_name = re.sub(r'\.(?:mkv|mp4|avi|webm)$', '', clean_name, flags=re.IGNORECASE).strip()

        res_matches = re.findall(r'\b\d{3,4}p\b|\b4K\b', clean_name, flags=re.IGNORECASE)
        if res_matches:
            if len(res_matches) > 1:
                res_tag = '-'.join(sorted(res_matches, key=lambda x: int(re.sub(r'\D', '', x)) if re.sub(r'\D', '', x) else 0, reverse=True))
            else:
                res_tag = res_matches[0]
                
        clean_name = re.sub(r'\[\s*(?:\d{3,4}p|4K)\b[^\]]*\]', '', clean_name, flags=re.IGNORECASE)
        clean_name = re.sub(r'\(\s*(?:\d{3,4}p|4K)\b[^\)]*\)', '', clean_name, flags=re.IGNORECASE)
        clean_name = re.sub(r'\b(?:\d{3,4}p|4K)\b', '', clean_name, flags=re.IGNORECASE)

        tag_words = r'HEVC|HVEC|x265|x264|AVC|10bit|8bit|Dual[- ]Audio|Tri[- ]Audio|Multi[- ]Audio|Multi[- ]Subs?|Eng[- ]Subs?|Softsubs?|Hardsubs?|Subbed|Dubbed|BD|BDRip|WEBRip|WEB-DL|AAC|OPUS|FLAC|AC3'
        clean_name = re.sub(rf'\[\s*(?:{tag_words})\b[^\]]*\]', '', clean_name, flags=re.IGNORECASE)
        clean_name = re.sub(rf'\(\s*(?:{tag_words})\b[^\)]*\)', '', clean_name, flags=re.IGNORECASE)

        clean_name = re.sub(r'\[\s*\]|\(\s*\)', '', clean_name)
        clean_name = re.sub(r'\s+', ' ', clean_name).strip(' .-_')

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
    mime = cf.mime_type.lower() if cf.mime_type else ''
    s_str = format_bytes(cf.size) if cf.size else ""
    if 'folder' in mime or cf.mime_type == 'application/vnd.google-apps.folder':
        return format_item_label(cf.name, 'folder', is_watched=is_watched)
    elif 'video' in mime:
        return format_item_label(cf.name, 'video', size_str=s_str, is_watched=is_watched)
    else:
        return format_item_label(cf.name, 'file', size_str=s_str, is_watched=is_watched)

def fetch_content(url):
    url = encode_cloud_url(url)
    cached = get_cached_fetch("cloud:" + url)
    if cached:
        files_data = cached.get("files", [])
        node_idx = cached.get("node_index", "")
        files = [CloudFile(**x) for x in files_data]
        return files, node_idx

    post_response = safe_request('post', url)
    if not post_response:
        return None, None
        
    try:
        dict_json_ = post_response.json()
    except Exception:
        return None, None
        
    initial_file_list = dict_json_.get("files")
    if not initial_file_list:
        return None, None
        
    initial_node_index = str(dict_json_.get("node_index", ""))
    initial_file_list.sort(key=lambda item: natural_sort_key(item.get("name", "")))
    
    files_data = [
        {
            "name": x.get("name", ""),
            "id": x.get("id", ""),
            "mime_type": x.get("mimeType", ""),
            "node_index": initial_node_index,
            "size": int(x.get("size", 0) or 0)
        }
        for x in initial_file_list
    ]
    set_cached_fetch("cloud:" + url, {"files": files_data, "node_index": initial_node_index})
    files = [CloudFile(**x) for x in files_data]
    return files, initial_node_index

def fetch_worker_folder(url):
    cached = get_cached_fetch("worker:" + url)
    if cached:
        cached_entries = [tuple(x) for x in cached]
        valid_cached = [e for e in cached_entries if len(e) > 1 and e[1] and not e[1].endswith('#') and e[0] not in ('Download', 'Report Issue', 'Home', 'Back')]
        if valid_cached:
            return valid_cached

    res = safe_request('get', url)
    if not res:
        return None
    from bs4 import BeautifulSoup
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
        if link_parsed.netloc != folder_domain:
            continue
        if not link_parsed.path.startswith(folder_path.split('/0:/')[0]):
            continue
        label = a.get_text(strip=True) or unquote(link_parsed.path.split('/')[-1] or link_parsed.path.split('/')[-2])
        link_type = classify_link(full_url)
        entries.append((label, full_url, link_type))
    
    valid_entries = [e for e in entries if e[1] and not e[1].endswith('#') and e[0] not in ('Download', 'Report Issue', 'Home', 'Back')]
    
    if not valid_entries:
        files, _ = fetch_content(url)
        if files:
            entries = []
            base_u = url if url.endswith('/') else url + '/'
            for cf in files:
                f_url = base_u + quote(cf.name)
                mime = cf.mime_type.lower() if cf.mime_type else ''
                ltype = 'worker_folder' if ('folder' in mime or cf.mime_type == 'application/vnd.google-apps.folder') else classify_link(cf.name)
                entries.append((cf.name, f_url, ltype))
        else:
            return None
    else:
        entries = valid_entries
    
    entries.sort(key=lambda e: natural_sort_key(e[0]))
    set_cached_fetch("worker:" + url, entries)
    return entries

def run_internal_fetch(args):
    if not args:
        sys.exit(1)
    action = args[0]
    action_args = list(args[1:])
    
    toggle_item = None
    if "--toggle" in action_args:
        t_idx = action_args.index("--toggle")
        if t_idx + 1 < len(action_args):
            toggle_item = action_args[t_idx + 1]
        action_args = action_args[:t_idx]

    try:
        if action == "toggle_watched":
            anime_title = action_args[0] if len(action_args) > 0 else ""
            item_name = action_args[1] if len(action_args) > 1 else ""
            if anime_title and item_name:
                toggle_watched_entry(anime_title, item_name)
            sys.exit(0)

        elif action == "cloud_folder":
            url = encode_cloud_url(action_args[0])
            anime_title = action_args[1] if len(action_args) > 1 else ""
            if toggle_item and anime_title:
                toggle_watched_entry(anime_title, toggle_item)
            watched = get_watched_list(anime_title)
            cached = get_cached_fetch("cloud:" + url)
            if cached:
                files_data = cached.get("files", [])
                files = [CloudFile(**x) for x in files_data]
            else:
                init_session()
                files, _ = fetch_content(url)
            if not files:
                print("ERROR: No files found in cloud folder.")
                sys.exit(1)
            for i, cf in enumerate(files):
                lbl = format_cloud_file_label(cf, is_watched=(cf.name in watched))
                print(f"{i}\t{lbl}\t{cf.name}\t{cf.id}\t{cf.mime_type}\t{cf.node_index}\t{cf.size}")
            sys.exit(0)

        elif action == "worker_folder":
            url = action_args[0]
            anime_title = action_args[1] if len(action_args) > 1 else ""
            if toggle_item and anime_title:
                toggle_watched_entry(anime_title, toggle_item)
            watched = get_watched_list(anime_title)
            cached = get_cached_fetch("worker:" + url)
            if cached:
                entries = [tuple(x) for x in cached]
            else:
                init_session()
                entries = fetch_worker_folder(url)
            if not entries:
                print("ERROR: No entries found in worker folder.")
                sys.exit(1)
            for i, (label, full_url, link_type) in enumerate(entries):
                lbl = format_item_label(label, link_type, is_watched=(label in watched))
                print(f"{i}\t{lbl}\t{label}\t{full_url}\t{link_type}")
            sys.exit(0)

        elif action == "search":
            init_session()
            from bs4 import BeautifulSoup
            query = " ".join(action_args)
            cached = get_cached_fetch("search:" + query)
            if cached:
                raw_names, urls = cached["raw_names"], cached["urls"]
            else:
                res = safe_request('get', search_url + query)
                if not res:
                    print(f"ERROR: Failed to connect for query '{query}'")
                    sys.exit(1)
                soup = BeautifulSoup(res.content, 'html.parser')
                anime_list = soup.select('.post-item-inner > a:first-child')
                if not anime_list:
                    print("ERROR: No results found.")
                    sys.exit(1)
                raw_names = [a.get('aria-label', 'Unknown') for a in anime_list]
                urls = [urljoin(base_url, a['href']) for a in anime_list]
                set_cached_fetch("search:" + query, {"raw_names": raw_names, "urls": urls})

            for i, (name, url) in enumerate(zip(raw_names, urls)):
                lbl = format_anime_title_label(name)
                print(f"{i}\t{lbl}\t{url}\t{name}")
            sys.exit(0)

        elif action == "anime_sources":
            init_session()
            from bs4 import BeautifulSoup
            selected_anime_url = action_args[0]
            raw_title = action_args[1] if len(action_args) > 1 else ""
            cached = get_cached_fetch("sources:" + selected_anime_url)
            if cached:
                items_data, best_raw = cached["items_data"], cached["best_raw"]
            else:
                res_anime = safe_request('get', selected_anime_url)
                if not res_anime:
                    print("ERROR: Failed to fetch anime details.")
                    sys.exit(1)
                soup = BeautifulSoup(res_anime.content, 'html.parser')
                anime_title = soup.find('h1', class_="post-title entry-title")
                page_raw_title = anime_title.get_text().strip() if anime_title else "Unknown"
                best_raw = raw_title if (raw_title and is_raw_release_title(raw_title)) else (page_raw_title if is_raw_release_title(page_raw_title) else (raw_title if len(raw_title) > len(page_raw_title) else page_raw_title))

                cloud_links = soup.css.select('a[href^="//cloud.animetoki.com/"], a[href^="//drive.animetoki.com/"]')
                cdn_links = [a for a in soup.select('a.shortc-button[href]') if a.get('href') and not (a['href'].startswith('//cloud.animetoki.com/') or a['href'].startswith('//drive.animetoki.com/'))]
                all_links = list(cloud_links) + list(cdn_links)
                if not all_links:
                    print("ERROR: No streaming links found for this anime.")
                    sys.exit(1)
                items_data = []
                for link in all_links:
                    label = link.get_text(strip=True)
                    href = urljoin(base_url, link['href'])
                    link_type = classify_link(href)
                    if link_type == 'cloud':
                        href = encode_cloud_url(href)
                    items_data.append((label, href, link_type))
                set_cached_fetch("sources:" + selected_anime_url, {"items_data": items_data, "best_raw": best_raw})

            for i, (label, href, link_type) in enumerate(items_data):
                lbl = format_item_label(label, link_type)
                print(f"{i}\t{lbl}\t{label}\t{href}\t{link_type}\t{best_raw}")
            sys.exit(0)
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

# FAST PATH ROUTING FOR INTERNAL FETCH (MUST BE BEFORE HEAVY INITS)
if len(sys.argv) > 1 and sys.argv[1] == "--internal-fetch":
    run_internal_fetch(sys.argv[2:])
    sys.exit(0)

# HEAVY INITIALIZATIONS ONLY RUN IN INTERACTIVE / MPV MODE
import logging
log_dir = Path.home() / ".local" / "share" / "animetoki-cli"
log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(log_dir / "animetoki-cli.log"),
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("animetoki-cli")

CONFIG_PATH = Path.home() / ".config" / "animetoki-cli" / "config.json"

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
        except Exception:
            pass
    return defaults

CONFIG = load_config()
UA = CONFIG["user_agent"]

def is_termux():
    return os.environ.get('TERMUX_VERSION') is not None or os.path.isdir('/data/data/com.termux')

def format_history_label(title: str, entry: dict = None) -> str:
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
    parts = []
    for item in actions:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            parts.append(f" [ {item[0]} ] {item[1]} ")
        else:
            parts.append(str(item))
    return " │ ".join(parts)

def count_items_summary(items_or_files):
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

def truncate_middle(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    half = (max_len - 2) // 2
    remainder = max_len - 2 - half
    return text[:half] + ".." + text[-remainder:]

def flush_stdin():
    if sys.stdin.isatty() and termios is not None:
        try:
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except Exception:
            pass

MIN_FZF_VERSION = (0, 74, 3)
_fzf_checked = False
_fzf_supported = False

def is_fzf_supported() -> bool:
    global _fzf_checked, _fzf_supported
    if _fzf_checked:
        return _fzf_supported

    _fzf_checked = True
    if not (sys.stdin.isatty() and sys.stdout.isatty() and shutil.which("fzf")):
        _fzf_supported = False
        return False

    try:
        p = subprocess.run(["fzf", "--version"], capture_output=True, text=True, timeout=2)
        if p.returncode == 0 and p.stdout:
            m = re.search(r'(\d+)\.(\d+)(?:\.(\d+))?', p.stdout)
            if m:
                ver = (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
                if ver >= MIN_FZF_VERSION:
                    _fzf_supported = True
                    return True
                else:
                    ver_str = f"{ver[0]}.{ver[1]}.{ver[2]}"
                    min_ver_str = f"{MIN_FZF_VERSION[0]}.{MIN_FZF_VERSION[1]}.{MIN_FZF_VERSION[2]}"
                    print(f"\033[33mWarning: Detected fzf v{ver_str} (< v{min_ver_str}). Please update fzf to >= v{min_ver_str} for interactive UI support. Falling back to non-fzf mode.\033[0m\n")
    except Exception as e:
        logger.debug(f"Error checking fzf version: {e}")

    _fzf_supported = False
    return False

def fzf_select(items=None, prompt="Select: ", default_idx=None, header=None, footer=None, reload_cmd=None, anime_title=None):
    flush_stdin()
    if isinstance(footer, list):
        footer_text = build_footer(footer)
    elif isinstance(footer, str):
        footer_text = footer
    else:
        footer_text = build_footer([("Enter / →", "Select"), ("ESC / ←", "Back"), ("Ctrl+W", "Watched"), ("Ctrl+X", "Main Menu"), ("Ctrl+C", "Exit")])

    if is_fzf_supported():
        cmd = [
            "fzf",
            "--ansi",
            "--border=rounded",
            "--border-label= AnimeToki CLI ",
            "--info=inline: ",
            "--height=100%",
            "--header-border=bottom",
            "--color=prompt:cyan:bold,header:247,footer:247,header-border:247",
            "--reverse",
            "--cycle",
            "--prompt", prompt,
            "--with-nth=2",
            "--delimiter=\t",
            "--expect=left,right,ctrl-c,ctrl-d,ctrl-x,ctrl-w",
            f"--footer={footer_text}"
        ]
        if reload_cmd:
            if anime_title:
                script_path = os.path.abspath(__file__)
                toggle_cmd = (
                    f"{shlex.quote(sys.executable)} {shlex.quote(script_path)} "
                    f"--internal-fetch toggle_watched {shlex.quote(anime_title)} {{3}}"
                )
                cmd.append(f"--bind=ctrl-w:execute({toggle_cmd})+reload({reload_cmd})+down")
            else:
                cmd.append(f"--bind=ctrl-w:reload({reload_cmd})+down")

        if header:
            cmd.append(f"--header={header}")
        if default_idx is not None and default_idx >= 0:
            pos_str = str(default_idx + 1)
            if reload_cmd:
                cmd.append(f"--bind=start:reload({reload_cmd}),load:pos({pos_str})+unbind(load)")
            else:
                cmd.append(f"--bind=start:pos({pos_str})")
        elif reload_cmd:
            cmd.append(f"--bind=start:reload({reload_cmd})")

        text = "\n".join(f"{i}\t{x}" for i, x in enumerate(items)) if items else ""
        p = subprocess.run(cmd, input=text if not reload_cmd else None, text=True, capture_output=True)
        if p.returncode == 130:
            return None
        if p.stdout:
            lines = p.stdout.splitlines()
            if lines:
                key_pressed = lines[0].strip().lower()
                selected_idx = None
                raw_parts = []
                if len(lines) > 1 and lines[1].strip():
                    raw_parts = lines[1].split('\t')
                    try:
                        selected_idx = int(raw_parts[0])
                    except (ValueError, IndexError):
                        pass

                if len(raw_parts) > 1 and raw_parts[1].startswith("ERROR:"):
                    return ("error", raw_parts[1])

                if key_pressed == "ctrl-c":
                    print("\nExiting...")
                    sys.exit(0)
                elif key_pressed == "ctrl-x":
                    return ("main_menu", selected_idx, raw_parts)
                elif key_pressed == "ctrl-d":
                    return ("delete", selected_idx, raw_parts)
                elif key_pressed == "ctrl-w":
                    return ("toggle_watched", selected_idx, raw_parts)
                elif key_pressed in ("left", "esc"):
                    return None
                elif selected_idx is not None or raw_parts:
                    return (selected_idx, raw_parts) if raw_parts else selected_idx
        return None

    if not items:
        return None
    if header:
        print(f"\033[37m--- AnimeToki CLI | {header} ---\033[0m")
    for i, item in enumerate(items): print(f"{i+1}. {item}")
    print("0. Back")
    print(f"\033[37m{footer_text}\033[0m\n")
    p_str = f"\033[1;36m{prompt}\033[0m"
    if default_idx is not None and 0 <= default_idx < len(items):
        p_str += f" [{default_idx + 1}]: "
    idx = safe_input(p_str, len(items), allow_zero_back=True, default_val=default_idx + 1 if default_idx is not None else None)
    if idx == 0 or idx is None:
        return None
    return idx - 1

def parse_fzf_result(res):
    if res is None:
        return None, None, []
    if isinstance(res, str):
        return res, None, []
    if isinstance(res, tuple):
        if isinstance(res[0], str):
            act = res[0]
            idx = res[1] if len(res) > 1 else None
            raw = res[2] if len(res) > 2 else []
            return act, idx, raw
        elif isinstance(res[0], int):
            return None, res[0], res[1] if len(res) > 1 and isinstance(res[1], list) else []
        elif res[0] is None and len(res) > 1:
            return None, None, res[1] if isinstance(res[1], list) else []
    elif isinstance(res, int):
        return None, res, []
    return None, None, []


def fzf_search_prompt():
    flush_stdin()
    if is_fzf_supported():
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
                plain_lbl = RE_ANSI_ESCAPE.sub('', lbl)
                hist_map[plain_lbl] = t
            
            header = build_header(['search'])
            footer = build_footer([("Enter", "Search / Resume"), ("Ctrl+D", "Delete"), ("Ctrl+W", "Watched"), ("Ctrl+C", "Exit")])
            
            cmd = [
                "fzf",
                "--ansi",
                "--border=rounded",
                "--border-label= AnimeToki CLI ",
                "--info=inline: ",
                "--height=100%",
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
            if p.returncode not in (0, 1, 130):
                logger.debug(f"fzf search prompt error (code {p.returncode}): {p.stderr}")
                break
            if p.returncode == 130:
                return ("exit", None)
            if p.stdout:
                lines = p.stdout.splitlines()
                typed_query = lines[0].strip() if len(lines) > 0 else ""
                key_pressed = lines[1].strip().lower() if len(lines) > 1 else ""
                selected_item = lines[2].strip() if len(lines) > 2 else ""
                
                if key_pressed == "ctrl-c":
                    print("\nExiting...")
                    sys.exit(0)

                real_title = None
                if selected_item:
                    plain_sel = RE_ANSI_ESCAPE.sub('', selected_item)
                    real_title = hist_map.get(selected_item) or hist_map.get(plain_sel)
                    if not real_title and selected_item.startswith("[History] "):
                        real_title = selected_item[len("[History] "):].strip()

                if key_pressed == "ctrl-d":
                    if real_title:
                        delete_history_entry(real_title)
                    continue

                if key_pressed == "ctrl-w":
                    if real_title:
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
                
                if p.returncode == 1 or (not typed_query and not selected_item):
                    return ("exit", None)
                        
            if p.returncode == 1:
                return ("exit", None)
            continue
    
    try:
        query = input("\033[1;36m> \033[0m").strip()
        if not query or query.lower() in ("exit", "quit"):
            return ("exit", None)
        return ("search", query)
    except (EOFError, KeyboardInterrupt):
        return ("exit", None)

def check_deps(download_mode):
    if download_mode:
        return
    if is_termux():
        return
    if not shutil.which("mpv"):
        sys.exit("Error: 'mpv' is not installed or not in PATH. Please install mpv to stream.")

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
        subprocess.Popen(
            am_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True

    socket_path = f"/tmp/animetoki_cli_mpv_{os.getpid()}.sock"
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
    download_dir = Path(CONFIG.get("download_dir", ".")).resolve()
    safe_name = Path(output_name).name
    dest_path = download_dir / safe_name
    download_dir.mkdir(parents=True, exist_ok=True)
    print(f"Downloading to {dest_path}...")
    curl_flags = [
        'curl', '-L', '--progress-bar',
        '-A', UA,
        '-H', f'Cookie: {_cookie_header()}',
        '-o', str(dest_path),
        url
    ]
    try:
        subprocess.run(curl_flags)
    except FileNotFoundError:
        print("curl is not installed. Cannot download.")

def fetch_anime_list(anime_search_url, query=None, download_mode=False):
    use_fzf = is_fzf_supported()
    path_parts = ['search', query] if query else ['search']
    header_title = build_header(path_parts)
    footer = build_footer([("Enter", "Select"), ("ESC / ←", "Back"), ("Ctrl+W", "Watched"), ("Ctrl+X", "Main Menu"), ("Ctrl+C", "Exit")])

    if use_fzf:
        script_path = os.path.abspath(__file__)
        reload_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(script_path)} --internal-fetch search {shlex.quote(query or '')}"
        default_idx = None
        while True:
            res = fzf_select(prompt="Select anime: ", default_idx=default_idx, header=header_title, footer=footer, reload_cmd=reload_cmd)
            if res is None:
                break
            if isinstance(res, tuple):
                act = res[0]
                if act == "main_menu":
                    return "main_menu"
                elif act == "error":
                    print(f"\033[31m{res[1]}\033[0m")
                    break
                elif act in ("delete", "toggle_watched"):
                    raw_parts = res[2] if len(res) > 2 else []
                    if len(raw_parts) > 3:
                        clean_t, _, _ = parse_anime_title(raw_parts[3])
                        if act == "delete": delete_history_entry(clean_t)
                        else: toggle_watched_entry(clean_t)
                    continue
                elif isinstance(act, int):
                    idx, raw_parts = res
                    if len(raw_parts) > 3:
                        anime_url = raw_parts[2]
                        raw_anime_name = raw_parts[3]
                        r = anime_download_link(anime_url, download_mode=download_mode, raw_title=raw_anime_name)
                        if r == "main_menu":
                            return "main_menu"
            break
        return

    res_search_animes = safe_request('get', anime_search_url)
    if not res_search_animes:
        print("\033[31mError: Connection failed or request returned an error. Please check your network or proxy settings.\033[0m")
        return
    from bs4 import BeautifulSoup
    soup_anime_list = BeautifulSoup(res_search_animes.content, 'html.parser')
    anime_list = soup_anime_list.select('.post-item-inner > a:first-child')
    if not anime_list:
        print("No results found.")
        return

    raw_anime_names = [a.get('aria-label', 'Unknown') for a in anime_list]
    display_names = [format_anime_title_label(n) for n in raw_anime_names]
    anime_urls = [urljoin(base_url, a['href']) for a in anime_list]

    info_str = f"{len(raw_anime_names)} {'Result' if len(raw_anime_names) == 1 else 'Results'}"
    header = build_header(path_parts, info_str)
    default_idx = None
    while True:
        res = fzf_select(display_names, "Select anime: ", default_idx=default_idx, header=header, footer=footer)
        act, idx, raw = parse_fzf_result(res)
        if res is None:
            break
        if act == "main_menu":
            return "main_menu"
        if idx is not None and 0 <= idx < len(anime_urls):
            default_idx = idx
            if act == "delete":
                clean_t, _, _ = parse_anime_title(raw_anime_names[idx])
                delete_history_entry(clean_t)
                continue
            elif act == "toggle_watched":
                clean_t, _, _ = parse_anime_title(raw_anime_names[idx])
                toggle_watched_entry(clean_t)
                continue
            else:
                selected_anime_url = anime_urls[idx]
                raw_anime_name = raw_anime_names[idx] if idx < len(raw_anime_names) else ""
                r = anime_download_link(selected_anime_url, download_mode=download_mode, raw_title=raw_anime_name)
                if r == "main_menu":
                    return "main_menu"

def resolve_stream_url(url):
    parsed = urlparse(url)
    if 'workers.dev' not in parsed.netloc:
        if parsed.query:
            params = parse_qs(parsed.query)
            params.pop('a', None)
            return parsed._replace(query=urlencode(params, doseq=True)).geturl()
        return url

    path = unquote(parsed.path)
    if path.endswith('/'):
        path = path[:-1]
    
    parent_path = "/".join(path.split('/')[:-1]) + "/"
    file_name = path.split('/')[-1]
    
    parent_url = f"{parsed.scheme}://{parsed.netloc}{parent_path}?a=view"
    
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
                return stream_url
    except Exception:
        pass
        
    return url

def anime_download_link(selected_anime_url, download_mode=False, raw_title=""):
    use_fzf = is_fzf_supported()
    footer = build_footer([("Enter", "Select"), ("ESC / ←", "Back"), ("Ctrl+W", "Watched"), ("Ctrl+X", "Main Menu"), ("Ctrl+C", "Exit")])
    path_parts = [raw_title or selected_anime_url]
    header = build_header(path_parts)

    if use_fzf:
        script_path = os.path.abspath(__file__)
        reload_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(script_path)} --internal-fetch anime_sources {shlex.quote(selected_anime_url)} {shlex.quote(raw_title or '')}"
        default_idx = None
        while True:
            res = fzf_select(prompt="Select source: ", default_idx=default_idx, header=header, footer=footer, reload_cmd=reload_cmd)
            if res is None:
                break
            if isinstance(res, tuple):
                act = res[0]
                if act == "main_menu":
                    return "main_menu"
                elif act == "error":
                    print(f"\033[31m{res[1]}\033[0m")
                    break
                elif act in ("delete", "toggle_watched"):
                    raw_parts = res[2] if len(res) > 2 else []
                    best_raw = raw_parts[5] if len(raw_parts) > 5 else raw_title
                    clean_t, _, _ = parse_anime_title(best_raw)
                    if act == "delete":
                        delete_history_entry(clean_t)
                        return "main_menu"
                    else:
                        toggle_watched_entry(clean_t)
                    continue
                elif isinstance(act, int):
                    idx, raw_parts = res
                    if len(raw_parts) > 4:
                        label = raw_parts[2]
                        selected_url = raw_parts[3]
                        link_type = raw_parts[4]
                        best_raw = raw_parts[5] if len(raw_parts) > 5 else raw_title
                        clean_title, _, tags_str = parse_anime_title(best_raw)
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
                            result = {"type": "cloud", "url": encode_cloud_url(selected_url), "hist_ctx": hist_ctx}
                        elif link_type in ('direct_video', 'direct_episodes', 'video'):
                            cached = get_cached_fetch("sources:" + selected_anime_url)
                            items_data = cached.get("items_data", []) if cached else []
                            direct_eps = [(l, h) for l, h, t in items_data if t in ('direct_video', 'direct_episodes', 'video')]
                            if direct_eps:
                                direct_eps.sort(key=lambda e: natural_sort_key(e[0]))
                                selected_ep_idx = next((i for i, (l, u) in enumerate(direct_eps) if u == selected_url), 0)
                                result = {"type": "direct_episodes", "episodes": direct_eps, "selected": selected_ep_idx, "hist_ctx": hist_ctx}
                            else:
                                result = {"type": "direct_episodes", "episodes": [(label, selected_url)], "selected": 0, "hist_ctx": hist_ctx}
                        elif link_type == 'worker_folder':
                            result = {"type": "worker_folder", "url": selected_url, "hist_ctx": hist_ctx}
                        else:
                            result = {"type": "direct_episodes", "episodes": [(label, selected_url)], "selected": 0, "hist_ctx": hist_ctx}
                        
                        r_disp = _dispatch_result(result, download_mode)
                        if r_disp == "main_menu":
                            return "main_menu"
            break
        return

    res_anime = safe_request('get', selected_anime_url)
    if not res_anime:
        return
    from bs4 import BeautifulSoup
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

    cloud_links = soup_anime_list.css.select('a[href^="//cloud.animetoki.com/"], a[href^="//drive.animetoki.com/"]')
    cdn_links = soup_anime_list.select('a.shortc-button[href]')
    cdn_links = [a for a in cdn_links 
                 if a.get('href') and not (a['href'].startswith('//cloud.animetoki.com/') or a['href'].startswith('//drive.animetoki.com/'))]
    all_links = list(cloud_links) + list(cdn_links)
    if not all_links:
        print("No streaming links found for this anime.")
        return

    link_data = []
    for link in all_links:
        label = link.get_text(strip=True)
        href = urljoin(base_url, link['href'])
        link_type = classify_link(href)
        if link_type == 'cloud':
            href = encode_cloud_url(href)
        link_data.append((label, href, link_type))

    path_parts = [best_raw]
    info_str = f"{len(link_data)} {'Source' if len(link_data) == 1 else 'Sources'}"
    header = build_header(path_parts, info_str)
    default_idx = None

    while True:
        labels = [format_item_label(l, t) for l, u, t in link_data]
        res = fzf_select(labels, "Select source: ", default_idx=default_idx, header=header, footer=footer)
        act, idx, raw = parse_fzf_result(res)
        if res is None:
            break
        if act == "main_menu":
            return "main_menu"
        if idx is not None and 0 <= idx < len(link_data):
            default_idx = idx
            if act == "delete":
                delete_history_entry(clean_title)
                return "main_menu"
            elif act == "toggle_watched":
                toggle_watched_entry(clean_title)
                continue
            else:
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
                result = {"type": "cloud", "url": encode_cloud_url(selected_url), "hist_ctx": hist_ctx}
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

def browse_worker_folder(url, download_mode=False, hist_ctx=None, resume_from=None):
    if resume_from:
        folder_stack = list(resume_from.get("folder_stack", []))
        current_url = resume_from.get("current_folder_url", url)
        display_stack = list(resume_from.get("display_stack", []))
    else:
        folder_stack = []
        current_url = url
        display_stack = []

    if not display_stack:
        anime_title = hist_ctx.title if hist_ctx else "Worker"
        source_label = hist_ctx.source_label if hist_ctx else ""
        display_stack = [anime_title, source_label]

    footer = build_footer([("Enter", "Select"), ("ESC / ←", "Back"), ("Ctrl+W", "Watched"), ("Ctrl+X", "Main Menu"), ("Ctrl+C", "Exit")])
    use_fzf = is_fzf_supported()

    while True:
        header = build_header(display_stack)
        entries = fetch_worker_folder(current_url)
        watched = get_watched_list(hist_ctx)
        first_unwatched_idx = None
        if entries:
            for i, (label, link, link_type) in enumerate(entries):
                if link_type in ('video', 'direct_video', 'file') and label not in watched:
                    first_unwatched_idx = i
                    break

        if use_fzf:
            script_path = os.path.abspath(__file__)
            anime_t = hist_ctx.title if hist_ctx else ""
            reload_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(script_path)} --internal-fetch worker_folder {shlex.quote(current_url)} {shlex.quote(anime_t)}"
            res = fzf_select(prompt="Select: ", default_idx=first_unwatched_idx, header=header, footer=footer, reload_cmd=reload_cmd, anime_title=anime_t)
            if res is None:
                if folder_stack:
                    current_url = folder_stack.pop()
                    if len(display_stack) > 2: display_stack.pop()
                    continue
                return True
            if isinstance(res, tuple):
                act = res[0]
                if act == "main_menu":
                    return "main_menu"
                elif act == "error":
                    if folder_stack:
                        current_url = folder_stack.pop()
                        if len(display_stack) > 2: display_stack.pop()
                        continue
                    return True
                elif act == "toggle_watched":
                    raw_parts = res[2] if len(res) > 2 else []
                    if len(raw_parts) > 2 and hist_ctx:
                        toggle_watched_entry(hist_ctx.title, raw_parts[2])
                    continue
                elif act == "delete":
                    if hist_ctx:
                        delete_history_entry(hist_ctx.title)
                    return "main_menu"
                elif isinstance(act, int):
                    raw_parts = res[1]
                    if len(raw_parts) > 4:
                        label = raw_parts[2]
                        selected_url = raw_parts[3]
                        link_type = raw_parts[4]
        else:
            if not entries:
                while folder_stack and not entries:
                    current_url = folder_stack.pop()
                    if len(display_stack) > 2: display_stack.pop()
                    entries = fetch_worker_folder(current_url)
                if not entries:
                    return False
            header_with_stats = build_header(display_stack, count_items_summary(entries))
            labels = [format_item_label(l, t, is_watched=(l in watched)) for l, _, t in entries]
            res = fzf_select(labels, "Select: ", default_idx=first_unwatched_idx, header=header_with_stats, footer=footer)
            act, idx, raw = parse_fzf_result(res)
            if res is None:
                if folder_stack:
                    current_url = folder_stack.pop()
                    if len(display_stack) > 2: display_stack.pop()
                    continue
                return True
            if act == "main_menu":
                return "main_menu"
            if idx is None or not (0 <= idx < len(entries)):
                continue
            if act == "toggle_watched":
                if hist_ctx:
                    toggle_watched_entry(hist_ctx.title, entries[idx][0])
                    first_unwatched_idx = min(idx + 1, len(entries) - 1)
                continue
            elif act == "delete":
                if hist_ctx:
                    delete_history_entry(hist_ctx.title)
                return "main_menu"
            label, selected_url, link_type = entries[idx]

        if hist_ctx:
            save_worker_history(hist_ctx, current_url, folder_stack, label, display_stack=display_stack)
        
        if link_type in ('direct_video', 'unknown'):
            stream_url = resolve_stream_url(selected_url)
            if download_mode:
                download_file(stream_url, unquote(urlparse(selected_url).path.split('/')[-1]))
                return True
            
            stream_in_mpv(stream_url, title=label)
            playing_header = build_header(display_stack + [label])
            act = fzf_select(["next", "replay", "previous", "select", "quit"], f"Playing {label}... ", header=playing_header)
            if act == "main_menu" or (isinstance(act, tuple) and act[0] == "main_menu"):
                return "main_menu"
            return True
        elif link_type == 'worker_folder':
            folder_stack.append(current_url)
            display_stack.append(label)
            current_url = selected_url
    return True

def play_direct_episodes(episodes, selected_idx, download_mode=False, hist_ctx=None, resume=False):
    episodes.sort(key=lambda e: natural_sort_key(e[0]))
    anime_title = hist_ctx.title if hist_ctx else "Episodes"
    source_label = hist_ctx.source_label if hist_ctx else ""
    path_parts = [anime_title, source_label]
    info_str = f"{len(episodes)} {'Episode' if len(episodes) == 1 else 'Episodes'}"
    header = build_header(path_parts, info_str)
    footer = build_footer([("Enter", "Select"), ("ESC / ←", "Back"), ("Ctrl+W", "Watched"), ("Ctrl+X", "Main Menu"), ("Ctrl+C", "Exit")])

    watched = get_watched_list(hist_ctx)
    first_unwatched_idx = None
    if episodes:
        for i, (ep_label, ep_url) in enumerate(episodes):
            if ep_label not in watched:
                first_unwatched_idx = i
                break

    def _select_ep_prompt(cur_idx):
        w_list = get_watched_list(hist_ctx)
        items = [format_item_label(e[0], "video", is_watched=(e[0] in w_list)) for e in episodes]
        return fzf_select(items, "Select episode: ", default_idx=cur_idx, header=header, footer=footer)

    idx = selected_idx if (selected_idx is not None and 0 <= selected_idx < len(episodes)) else (first_unwatched_idx if first_unwatched_idx is not None else 0)

    if resume:
        while True:
            res = _select_ep_prompt(idx)
            act, sel_idx, raw = parse_fzf_result(res)
            if res is None:
                return True
            if act == "main_menu":
                return "main_menu"
            if sel_idx is not None and 0 <= sel_idx < len(episodes):
                idx = sel_idx
            if act == "toggle_watched":
                if hist_ctx: toggle_watched_entry(hist_ctx.title, episodes[idx][0])
                idx = min(idx + 1, len(episodes) - 1)
                continue
            elif act == "delete":
                if hist_ctx: delete_history_entry(hist_ctx.title)
                return "main_menu"
            break

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
            while True:
                res = _select_ep_prompt(idx)
                act_sel, sel_idx, raw = parse_fzf_result(res)
                if res is None: break
                if act_sel == "main_menu":
                    return "main_menu"
                if sel_idx is not None and 0 <= sel_idx < len(episodes):
                    idx = sel_idx
                if act_sel == "toggle_watched":
                    if hist_ctx: toggle_watched_entry(hist_ctx.title, episodes[idx][0])
                    idx = min(idx + 1, len(episodes) - 1)
                    continue
                elif act_sel == "delete":
                    if hist_ctx: delete_history_entry(hist_ctx.title)
                    return "main_menu"
                break
        else: return True
    return True

def play_and_browse(selected_file=None, current_files=None, initial_link_base64=None, download_mode=False, hist_ctx=None, resume_from=None):
    def _cloud_dl_url(cf: CloudFile, folder_url: str):
        parsed = urlparse(folder_url)
        domain_url = f"https://{parsed.netloc}"
        return f"{domain_url}?a=download&id={cf.id}&name={base64.b64encode(unquote(cf.name).encode()).decode()}&n={cf.node_index}"

    footer = build_footer([("Enter", "Select"), ("ESC / ←", "Back"), ("Ctrl+W", "Watched"), ("Ctrl+X", "Main Menu"), ("Ctrl+C", "Exit")])
    use_fzf = is_fzf_supported()

    if resume_from:
        current_folder_url = resume_from["current_folder_url"]
        folder_stack = list(resume_from.get("folder_stack", []))
        display_stack = list(resume_from.get("display_stack", []))
    else:
        current_folder_url = initial_link_base64
        folder_stack = []
        display_stack = []

    if not display_stack:
        anime_title = hist_ctx.title if hist_ctx else "Cloud"
        source_label = hist_ctx.source_label if hist_ctx else ""
        display_stack = [anime_title, source_label]

    while True:
        header = build_header(display_stack)
        if not selected_file:
            files, _ = fetch_content(current_folder_url)
            watched = get_watched_list(hist_ctx)
            first_unwatched_idx = None
            if files:
                for i, cf in enumerate(files):
                    mime = cf.mime_type.lower() if cf.mime_type else ''
                    is_folder = 'folder' in mime or cf.mime_type == 'application/vnd.google-apps.folder'
                    if not is_folder and cf.name not in watched:
                        first_unwatched_idx = i
                        break

            if use_fzf:
                script_path = os.path.abspath(__file__)
                anime_t = hist_ctx.title if hist_ctx else ""
                reload_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(script_path)} --internal-fetch cloud_folder {shlex.quote(current_folder_url)} {shlex.quote(anime_t)}"
                res = fzf_select(prompt="Select file: ", default_idx=first_unwatched_idx, header=header, footer=footer, reload_cmd=reload_cmd, anime_title=anime_t)
                if res is None:
                    if folder_stack:
                        current_folder_url = folder_stack.pop()
                        if len(display_stack) > 2:
                            display_stack.pop()
                        continue
                    return True
                if isinstance(res, tuple):
                    act = res[0]
                    if act == "main_menu":
                        return "main_menu"
                    elif act == "error":
                        if folder_stack:
                            current_folder_url = folder_stack.pop()
                            if len(display_stack) > 2: display_stack.pop()
                            continue
                        return True
                    elif act == "toggle_watched":
                        raw_parts = res[2] if len(res) > 2 else []
                        if len(raw_parts) > 2 and hist_ctx:
                            toggle_watched_entry(hist_ctx.title, raw_parts[2])
                        continue
                    elif act == "delete":
                        if hist_ctx:
                            delete_history_entry(hist_ctx.title)
                        return "main_menu"
                    elif isinstance(act, int):
                        raw_parts = res[1]
                        if len(raw_parts) > 6:
                            selected_file = CloudFile(
                                name=raw_parts[2],
                                id=raw_parts[3],
                                mime_type=raw_parts[4],
                                node_index=raw_parts[5],
                                size=int(raw_parts[6]) if raw_parts[6].isdigit() else 0
                            )
            else:
                files, _ = fetch_content(current_folder_url)
                if not files:
                    if folder_stack:
                        current_folder_url = folder_stack.pop()
                        if len(display_stack) > 2:
                            display_stack.pop()
                        continue
                    return True
                header_with_stats = build_header(display_stack, count_items_summary(files))
                watched = get_watched_list(hist_ctx)
                res = fzf_select([format_cloud_file_label(f, is_watched=(f.name in watched)) for f in files], "Select file: ", header=header_with_stats, footer=footer)
                act, idx, raw = parse_fzf_result(res)
                if res is None:
                    if folder_stack:
                        current_folder_url = folder_stack.pop()
                        if len(display_stack) > 2:
                            display_stack.pop()
                        continue
                    return True
                if act == "main_menu":
                    return "main_menu"
                if idx is not None and 0 <= idx < len(files):
                    if act == "toggle_watched":
                        if hist_ctx:
                            toggle_watched_entry(hist_ctx.title, files[idx].name)
                            first_unwatched_idx = min(idx + 1, len(files) - 1)
                        continue
                    elif act == "delete":
                        if hist_ctx:
                            delete_history_entry(hist_ctx.title)
                        return "main_menu"
                    selected_file = files[idx]

        if hist_ctx and selected_file:
            save_cloud_history(hist_ctx, current_folder_url, folder_stack, selected_file.name, selected_file.id, selected_file.mime_type, display_stack=display_stack)

        is_video = bool(selected_file.mime_type and "video" in selected_file.mime_type.lower())
        if is_video:
            download_url = _cloud_dl_url(selected_file, current_folder_url)
            if download_mode:
                download_file(download_url, selected_file.name)
                return True
            
            playing_header = build_header(display_stack + [selected_file.name])
            stream_in_mpv(download_url, title=selected_file.name)
            
            files_in_folder, _ = fetch_content(current_folder_url)
            video_siblings = [f for f in files_in_folder if f.mime_type and "video" in f.mime_type.lower()] if files_in_folder else []
            vid_idx = next((j for j, f in enumerate(video_siblings) if f.name == selected_file.name), 0)
            
            while True:
                playing_header = build_header(display_stack + [selected_file.name])
                act = fzf_select(["next", "replay", "previous", "select", "quit"], f"Playing {selected_file.name}... ", header=playing_header)
                if act == "main_menu" or (isinstance(act, tuple) and act[0] == "main_menu"):
                    return "main_menu"
                if isinstance(act, tuple): act = act[0]

                if act == 0 or act == "next":
                    if video_siblings:
                        vid_idx = min(vid_idx + 1, len(video_siblings) - 1)
                        selected_file = video_siblings[vid_idx]
                        if hist_ctx:
                            save_cloud_history(hist_ctx, current_folder_url, folder_stack, selected_file.name, selected_file.id, selected_file.mime_type, display_stack=display_stack)
                        stream_in_mpv(_cloud_dl_url(selected_file, current_folder_url), title=selected_file.name)
                elif act == 1 or act == "replay":
                    stream_in_mpv(_cloud_dl_url(selected_file, current_folder_url), title=selected_file.name)
                elif act == 2 or act == "previous":
                    if video_siblings:
                        vid_idx = max(vid_idx - 1, 0)
                        selected_file = video_siblings[vid_idx]
                        if hist_ctx:
                            save_cloud_history(hist_ctx, current_folder_url, folder_stack, selected_file.name, selected_file.id, selected_file.mime_type, display_stack=display_stack)
                        stream_in_mpv(_cloud_dl_url(selected_file, current_folder_url), title=selected_file.name)
                elif act == 3 or act == "select":
                    selected_file = None
                    break
                else:
                    return True
            selected_file = None
        else:
            folder_stack.append(current_folder_url)
            display_stack.append(selected_file.name)
            sub_seg = base64.b64encode(unquote(selected_file.name).encode()).decode()
            current_folder_url = encode_cloud_url(current_folder_url + sub_seg + "/")
            selected_file = None
    return True

def _dispatch_result(result, download_mode):
    if not result:
        return
    hist_ctx = result.get("hist_ctx")
    title = hist_ctx.title if hist_ctx else None
    if result["type"] == "cloud":
        return play_and_browse(initial_link_base64=result["url"], download_mode=download_mode, hist_ctx=hist_ctx)
    elif result["type"] == "direct_episodes":
        return play_direct_episodes(result["episodes"], result["selected"], download_mode, hist_ctx=hist_ctx)
    elif result["type"] == "worker_folder":
        return browse_worker_folder(result["url"], download_mode, hist_ctx=hist_ctx)

def resume_history(title, entry, download_mode=False):
    anime_url = entry.get("anime_url")
    source_type = entry.get("source_type")
    
    if not source_type or not anime_url:
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

    res = None
    if source_type == "cloud":
        res = play_and_browse(
            selected_file=None,
            current_files=None,
            initial_link_base64=None,
            download_mode=download_mode,
            hist_ctx=hist_ctx,
            resume_from=entry
        )
    elif source_type == "direct_episodes":
        if anime_url:
            return anime_download_link(anime_url, download_mode=download_mode)
        episodes = entry.get("episodes", [])
        selected_idx = entry.get("selected_idx", 0)
        if episodes:
            res = play_direct_episodes(episodes, selected_idx, download_mode=download_mode, hist_ctx=hist_ctx, resume=True)
    elif source_type == "worker_folder":
        url = entry.get("current_folder_url") or entry.get("source_url")
        if url:
            res = browse_worker_folder(url, download_mode=download_mode, hist_ctx=hist_ctx, resume_from=entry)

    if res == "main_menu":
        return "main_menu"
    elif anime_url:
        return anime_download_link(anime_url, download_mode=download_mode)
    return True

def search(query, download_mode=False):
    anime_search_url = search_url + query
    return fetch_anime_list(anime_search_url, query=query, download_mode=download_mode)

def signal_handler(sig, frame):
    print("\nExiting...")
    os._exit(0)

def main():
    signal.signal(signal.SIGINT, signal_handler)
    
    parser = argparse.ArgumentParser(description="CLI anime player for animetoki.com")
    parser.add_argument("query", nargs="*", help="Search query (if provided, runs in non-interactive mode)")
    parser.add_argument("-d", "--download", action="store_true", help="Download the video instead of playing it")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show debug log output in terminal")
    parser.add_argument("-c", "--continue-watch", action="store_true", help="Continue watching from history")
    parser.add_argument("-C", "--clear-history", action="store_true", help="Clear watch history (and exit)")
    parser.add_argument("--version", action="version", version="animetoki-cli 2.5")
    parser.add_argument("--internal-fetch", nargs="+", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.internal_fetch:
        run_internal_fetch(args.internal_fetch)
        return

    if args.verbose:
        import logging
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(console_handler)

    check_deps(args.download)
    init_session()

    raw_query = " ".join(args.query).strip() if args.query else ""
    initial_query = raw_query if raw_query else None

    if getattr(args, 'clear_history', False):
        history_store.clear()
        print("History cleared.")
        return

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
                    act, idx, raw = parse_fzf_result(res)
                    if res is None or act == "main_menu":
                        args.continue_watch = False
                        continue
                    if idx is not None and 0 <= idx < len(titles):
                        sel_title = titles[idx]
                        if act == "delete":
                            delete_history_entry(sel_title)
                            continue
                        elif act == "toggle_watched":
                            toggle_watched_entry(sel_title)
                            continue
                        else:
                            entry = hist[sel_title]
                            r = resume_history(sel_title, entry, args.download)
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

if __name__ == "__main__":
    main()