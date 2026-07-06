import itertools
import json
import math
import time
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
import requests
import streamlit as st

APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "worship_songs.db"
SOURCES_PATH = APP_DIR / "worship_sources.csv"

DEFAULT_EXCLUDE_KEYWORDS = [
    "커버", "cover", "COVER", "1시간", "2시간",
    "광고없는 플레이리스트", "MR", "mr", "반주", "악보", "강의", "tutorial", "drum", "guitar",
    "piano", "피아노", "드럼", "기타", "베이스", "lyrics only", "광고", "광고없는",
]

SPEED_OPTIONS = ["미확인", "느림", "중간", "빠름"]
KEY_OPTIONS = ["미확인", "C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
THEME_OPTIONS = [
    "미확인", "은혜", "회복", "십자가", "보혈", "성령", "찬양", "감사", "결단", "선포", "예배", "소망", "사랑", "기도", "부흥", "승리", "임재", "경배", "기쁨", "묵상"
]

DEFAULT_SECTION_THEME_PRESETS = {
    "느린 시작": ["은혜", "임재", "회복", "경배", "묵상", "십자가"],
    "빠른 찬양": ["찬양", "기쁨", "감사", "선포", "승리", "부흥"],
    "마무리": ["결단", "기도", "임재", "회복", "은혜", "경배"],
}

NVIDIA_DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_DEFAULT_MODEL = "minimaxai/minimax-m3"

# -----------------------------
# Secrets / API key helpers
# -----------------------------

def _get_nested_secret(section: str, key: str, default: str = "") -> str:
    """Read nested Streamlit secrets safely, e.g. [youtube] api_key = "..."."""
    try:
        section_obj = st.secrets.get(section, {})
        value = section_obj.get(key, default) if hasattr(section_obj, "get") else default
        return str(value) if value else default
    except Exception:
        return default


def get_app_secret(root_key: str, nested_section: str, nested_key: str, env_key: Optional[str] = None, default: str = "") -> str:
    """
    Priority:
    1) Streamlit root-level secrets: YOUTUBE_API_KEY = "..."
    2) Streamlit nested secrets: [youtube] api_key = "..."
    3) Environment variable fallback
    4) Default
    """
    try:
        value = st.secrets.get(root_key, "")
        if value:
            return str(value)
    except Exception:
        pass

    nested_value = _get_nested_secret(nested_section, nested_key, "")
    if nested_value:
        return nested_value

    if env_key:
        env_value = os.getenv(env_key, "")
        if env_value:
            return env_value

    return default


def secret_status_label(value: str) -> str:
    return "✅ secrets.toml에서 불러옴" if value else "⚠️ secrets.toml에 없음"


def sanitize_error_message(error: Exception | str, api_key: str = "") -> str:
    """Hide API keys from error messages before showing them in Streamlit logs."""
    text = str(error)
    if api_key:
        text = text.replace(api_key, "[API_KEY_HIDDEN]")
    text = re.sub(r"([?&]key=)[^&\s]+", r"\1[API_KEY_HIDDEN]", text)
    text = re.sub(r"(Bearer\s+)[A-Za-z0-9._\-]+", r"\1[API_KEY_HIDDEN]", text)
    return text


def is_rate_limit_error(error: Exception | str) -> bool:
    text = str(error).lower()
    return "429" in text or "too many requests" in text or "quota" in text or "rate limit" in text

# -----------------------------
# DB
# -----------------------------

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS songs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT UNIQUE,
            source_video_id TEXT,
            song_index INTEGER DEFAULT 1,
            segment_label TEXT DEFAULT '',
            segment_start_seconds INTEGER,
            raw_title TEXT,
            clean_title TEXT,
            team TEXT,
            channel_title TEXT,
            video_url TEXT,
            published_at TEXT,
            duration_iso TEXT,
            duration_seconds INTEGER,
            description TEXT,
            speed TEXT DEFAULT '미확인',
            bpm INTEGER,
            song_key TEXT DEFAULT '미확인',
            available_keys TEXT DEFAULT '',
            first_chord TEXT DEFAULT '',
            last_chord TEXT DEFAULT '',
            theme TEXT DEFAULT '미확인',
            energy INTEGER DEFAULT 3,
            sheet_url TEXT DEFAULT '',
            memo TEXT DEFAULT '',
            is_medley INTEGER DEFAULT 0,
            medley_song_count INTEGER DEFAULT 1,
            medley_songs TEXT DEFAULT '',
            ai_analyzed INTEGER DEFAULT 0,
            ai_note TEXT DEFAULT '',
            checked INTEGER DEFAULT 0,
            usable INTEGER DEFAULT 1,
            source_query TEXT DEFAULT '',
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    # Upgrade DBs created by older versions without deleting user data.
    existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(songs)").fetchall()}
    migrations = {
        "source_video_id": "ALTER TABLE songs ADD COLUMN source_video_id TEXT",
        "song_index": "ALTER TABLE songs ADD COLUMN song_index INTEGER DEFAULT 1",
        "segment_label": "ALTER TABLE songs ADD COLUMN segment_label TEXT DEFAULT ''",
        "segment_start_seconds": "ALTER TABLE songs ADD COLUMN segment_start_seconds INTEGER",
        "is_medley": "ALTER TABLE songs ADD COLUMN is_medley INTEGER DEFAULT 0",
        "medley_song_count": "ALTER TABLE songs ADD COLUMN medley_song_count INTEGER DEFAULT 1",
        "medley_songs": "ALTER TABLE songs ADD COLUMN medley_songs TEXT DEFAULT ''",
        "ai_analyzed": "ALTER TABLE songs ADD COLUMN ai_analyzed INTEGER DEFAULT 0",
        "ai_note": "ALTER TABLE songs ADD COLUMN ai_note TEXT DEFAULT ''",
    }
    for col, ddl in migrations.items():
        if col not in existing_cols:
            cur.execute(ddl)
    cur.execute("UPDATE songs SET source_video_id = video_id WHERE source_video_id IS NULL OR source_video_id = ''")
    cur.execute("UPDATE songs SET medley_song_count = 1 WHERE medley_song_count IS NULL OR medley_song_count < 1")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS collection_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team TEXT NOT NULL,
            source_query TEXT NOT NULL,
            next_page_token TEXT DEFAULT '',
            completed INTEGER DEFAULT 0,
            pages_fetched INTEGER DEFAULT 0,
            videos_seen INTEGER DEFAULT 0,
            songs_inserted INTEGER DEFAULT 0,
            songs_skipped INTEGER DEFAULT 0,
            last_error TEXT DEFAULT '',
            started_at TEXT,
            updated_at TEXT,
            UNIQUE(team, source_query)
        )
        """
    )
    conn.commit()
    conn.close()


def get_collection_state(team: str, source_query: str) -> Dict:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM collection_state WHERE team = ? AND source_query = ?",
        (team, source_query),
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def reset_collection_state(team: str, source_query: str) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM collection_state WHERE team = ? AND source_query = ?", (team, source_query))
    conn.commit()
    conn.close()


def update_collection_state(
    team: str,
    source_query: str,
    *,
    next_page_token: str = '',
    completed: int = 0,
    page_delta: int = 0,
    videos_delta: int = 0,
    inserted_delta: int = 0,
    skipped_delta: int = 0,
    last_error: str = '',
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    existing = get_collection_state(team, source_query)
    conn = get_conn()
    if existing:
        conn.execute(
            """
            UPDATE collection_state
            SET next_page_token = ?, completed = ?, pages_fetched = pages_fetched + ?,
                videos_seen = videos_seen + ?, songs_inserted = songs_inserted + ?,
                songs_skipped = songs_skipped + ?, last_error = ?, updated_at = ?
            WHERE team = ? AND source_query = ?
            """,
            (
                next_page_token or '',
                int(completed),
                int(page_delta),
                int(videos_delta),
                int(inserted_delta),
                int(skipped_delta),
                last_error or '',
                now,
                team,
                source_query,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO collection_state (
                team, source_query, next_page_token, completed, pages_fetched, videos_seen,
                songs_inserted, songs_skipped, last_error, started_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                team,
                source_query,
                next_page_token or '',
                int(completed),
                int(page_delta),
                int(videos_delta),
                int(inserted_delta),
                int(skipped_delta),
                last_error or '',
                now,
                now,
            ),
        )
    conn.commit()
    conn.close()


def upsert_song(song: Dict) -> bool:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO songs (
                video_id, source_video_id, song_index, segment_label, segment_start_seconds,
                raw_title, clean_title, team, channel_title, video_url,
                published_at, duration_iso, duration_seconds, description, speed, theme,
                is_medley, medley_song_count, medley_songs, ai_analyzed, ai_note,
                source_query, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                song.get("video_id"),
                song.get("source_video_id") or song.get("video_id"),
                int(song.get("song_index") or 1),
                song.get("segment_label", ""),
                song.get("segment_start_seconds"),
                song.get("raw_title"),
                song.get("clean_title"),
                song.get("team"),
                song.get("channel_title"),
                song.get("video_url"),
                song.get("published_at"),
                song.get("duration_iso"),
                song.get("duration_seconds"),
                song.get("description", ""),
                song.get("speed", "미확인"),
                song.get("theme", "미확인"),
                int(song.get("is_medley") or 0),
                max(1, int(song.get("medley_song_count") or 1)),
                song.get("medley_songs", ""),
                int(song.get("ai_analyzed") or 0),
                song.get("ai_note", ""),
                song.get("source_query", ""),
                now,
                now,
            ),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def query_df(sql: str, params: Tuple = ()) -> pd.DataFrame:
    conn = get_conn()
    df = pd.read_sql_query(sql, conn, params=params)
    conn.close()
    return df


def update_song(song_id: int, values: Dict) -> None:
    if not values:
        return
    values = dict(values)
    values["updated_at"] = datetime.now().isoformat(timespec="seconds")
    set_clause = ", ".join([f"{k} = ?" for k in values.keys()])
    params = list(values.values()) + [song_id]
    conn = get_conn()
    conn.execute(f"UPDATE songs SET {set_clause} WHERE id = ?", params)
    conn.commit()
    conn.close()

# -----------------------------
# YouTube API helpers
# -----------------------------

def iso8601_duration_to_seconds(duration: str) -> int:
    # Example: PT5M31S, PT1H2M3S
    pattern = re.compile(r"P(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?")
    match = pattern.fullmatch(duration or "")
    if not match:
        return 0
    parts = {name: int(value) if value else 0 for name, value in match.groupdict().items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


def youtube_search(api_key: str, query: str, max_results: int = 10, page_token: Optional[str] = None) -> Dict:
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "key": api_key,
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": min(max_results, 50),
        "order": "relevance",
        "safeSearch": "none",
        "relevanceLanguage": "ko",
    }
    if page_token:
        params["pageToken"] = page_token
    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def youtube_videos(api_key: str, video_ids: Sequence[str]) -> Dict:
    if not video_ids:
        return {"items": []}
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "key": api_key,
        "part": "snippet,contentDetails,statistics",
        "id": ",".join(video_ids),
    }
    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def youtube_channel_search(api_key: str, query: str, max_results: int = 8) -> List[Dict]:
    """Find YouTube channel candidates by team/name.

    This uses YouTube Data API search.list with type=channel. It is meant for
    one-time channel discovery when the user adds a new worship team.
    """
    if not api_key:
        return []
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "key": api_key,
        "part": "snippet",
        "q": query,
        "type": "channel",
        "maxResults": min(max_results, 50),
        "order": "relevance",
        "safeSearch": "none",
        "relevanceLanguage": "ko",
    }
    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()
    candidates: List[Dict] = []
    for item in response.json().get("items", []):
        raw_id = item.get("id", {})
        channel_id = raw_id.get("channelId") if isinstance(raw_id, dict) else ""
        snippet = item.get("snippet", {})
        if channel_id:
            candidates.append({
                "channel_id": channel_id,
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "published_at": snippet.get("publishedAt", ""),
                "channel_url": f"https://www.youtube.com/channel/{channel_id}",
                "rss_url": f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
            })
    return candidates


def youtube_channel_uploads_playlist(api_key: str, channel_id: str) -> str:
    """Return a channel's uploads playlist id using YouTube Data API."""
    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {
        "key": api_key,
        "part": "contentDetails",
        "id": channel_id,
        "maxResults": 1,
    }
    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()
    items = response.json().get("items", [])
    if not items:
        return ""
    return (
        items[0]
        .get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads", "")
    )


def youtube_playlist_items(api_key: str, playlist_id: str, page_token: Optional[str] = None, max_results: int = 50) -> Dict:
    """Fetch items from a YouTube playlist, used for channel uploads collection."""
    url = "https://www.googleapis.com/youtube/v3/playlistItems"
    params = {
        "key": api_key,
        "part": "snippet,contentDetails",
        "playlistId": playlist_id,
        "maxResults": min(max_results, 50),
    }
    if page_token:
        params["pageToken"] = page_token
    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def extract_youtube_video_id(value: str) -> str:
    """Extract a YouTube video id from youtu.be / watch?v= / shorts URLs or raw id."""
    value = normalize_text(value)
    if not value:
        return ""
    patterns = [
        r"youtu\.be/([A-Za-z0-9_-]{6,})",
        r"[?&]v=([A-Za-z0-9_-]{6,})",
        r"youtube\.com/shorts/([A-Za-z0-9_-]{6,})",
        r"^([A-Za-z0-9_-]{6,})$",
    ]
    for pat in patterns:
        m = re.search(pat, value)
        if m:
            return m.group(1)[:32]
    return ""

def extract_youtube_channel_id(value: str) -> str:
    """Extract a YouTube channel id from a channel id, RSS URL, /channel/ URL, or @handle page.

    Important: a handle URL like https://www.youtube.com/@FIAWORSHIP is not itself an RSS feed.
    The app fetches that page, finds the channel_id/RSS link, and then uses
    https://www.youtube.com/feeds/videos.xml?channel_id=CHANNEL_ID.
    """
    value = normalize_text(value)
    if not value:
        return ""

    # Raw channel id or URL containing channel_id.
    direct_patterns = [
        r"(?:channel_id=)(UC[A-Za-z0-9_-]{20,})",
        r"youtube\.com/channel/(UC[A-Za-z0-9_-]{20,})",
        r"^(UC[A-Za-z0-9_-]{20,})$",
    ]
    for pat in direct_patterns:
        m = re.search(pat, value)
        if m:
            return m.group(1)

    # Handle/custom URLs require one page fetch. This does not use YouTube Data API quota.
    if "youtube.com/" in value:
        url = value
    elif value.startswith("@"):
        url = f"https://www.youtube.com/{value}"
    else:
        url = f"https://www.youtube.com/@{value}"

    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    html = response.text

    # Most reliable: RSS alternate link in the page source.
    m = re.search(r"https://www\.youtube\.com/feeds/videos\.xml\?channel_id=(UC[A-Za-z0-9_-]{20,})", html)
    if m:
        return m.group(1)

    # Fallbacks commonly present in YouTube page source.
    for pat in [r'"channelId":"(UC[A-Za-z0-9_-]{20,})"', r'"browseId":"(UC[A-Za-z0-9_-]{20,})"']:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return ""


def youtube_rss_url_from_channel_input(channel_value: str) -> Tuple[str, str]:
    """Return (rss_url, channel_id_or_user) from channel id/handle/channel URL/RSS URL.

    YouTube exposes RSS in two useful forms:
    - https://www.youtube.com/feeds/videos.xml?channel_id=UC...
    - https://www.youtube.com/feeds/videos.xml?user=legacyUserName

    A /channel/UC... URL can be converted without any network request.
    A legacy /user/... URL can also be converted without any network request.
    @handle and /c/custom URLs still require resolving the page, so default sources
    prefer direct /channel/UC... IDs whenever possible.
    """
    channel_value = normalize_text(channel_value)
    if not channel_value:
        return "", ""

    # Already an RSS URL. Accept both channel_id and legacy user feeds.
    if "feeds/videos.xml" in channel_value:
        if "channel_id=" in channel_value:
            channel_id = extract_youtube_channel_id(channel_value)
            return channel_value, channel_id
        m_user = re.search(r"[?&]user=([^&\s]+)", channel_value)
        if m_user:
            return channel_value, m_user.group(1)

    # Legacy /user/ URLs can use RSS directly. This avoids fragile @handle page parsing.
    m_user = re.search(r"youtube\.com/user/([^/?&#]+)", channel_value)
    if m_user:
        user_name = m_user.group(1)
        return f"https://www.youtube.com/feeds/videos.xml?user={user_name}", user_name

    channel_id = extract_youtube_channel_id(channel_value)
    if not channel_id:
        return "", ""
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}", channel_id


def fetch_youtube_rss_entries(channel_value: str) -> Tuple[List[Dict], str, str]:
    """Fetch a YouTube channel RSS feed without YouTube Data API quota.

    RSS generally returns recent uploads only. It is useful as the quota-free/default collector,
    not as a guaranteed full historical archive.
    """
    rss_url, channel_id = youtube_rss_url_from_channel_input(channel_value)
    if not rss_url:
        raise ValueError("채널 ID/RSS URL을 찾지 못했습니다. 예: https://www.youtube.com/@FIAWORSHIP 또는 UC... 형식")

    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(rss_url, headers=headers, timeout=20)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }
    entries: List[Dict] = []
    for entry in root.findall("atom:entry", ns):
        video_id = (entry.findtext("yt:videoId", default="", namespaces=ns) or "").strip()
        title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
        published = (entry.findtext("atom:published", default="", namespaces=ns) or "").strip()
        author_name = ""
        author = entry.find("atom:author", ns)
        if author is not None:
            author_name = (author.findtext("atom:name", default="", namespaces=ns) or "").strip()
        description = ""
        media_group = entry.find("media:group", ns)
        if media_group is not None:
            description = (media_group.findtext("media:description", default="", namespaces=ns) or "").strip()
        if video_id:
            entries.append(
                {
                    "video_id": video_id,
                    "title": title,
                    "published_at": published,
                    "channel_title": author_name,
                    "description": description,
                    "rss_url": rss_url,
                    "channel_id": channel_id,
                }
            )
    return entries, rss_url, channel_id


def build_video_row_from_rss_entry(entry: Dict, team: str, source_query: str) -> Dict:
    """Store one RSS entry as one DB item. Only URL + metadata are stored."""
    source_video_id = entry.get("video_id", "")
    raw_title = entry.get("title", "")
    return {
        "video_id": source_video_id,
        "source_video_id": source_video_id,
        "song_index": 1,
        "segment_label": "",
        "segment_start_seconds": None,
        "raw_title": raw_title,
        "clean_title": clean_video_title(raw_title, team),
        "team": team,
        "channel_title": entry.get("channel_title", ""),
        "video_url": video_url_with_start(source_video_id, None),
        "published_at": entry.get("published_at", ""),
        "duration_iso": "",
        "duration_seconds": 0,
        "description": entry.get("description", ""),
        "speed": "미확인",
        "theme": "미확인",
        "is_medley": 0,
        "medley_song_count": 1,
        "medley_songs": "",
        "ai_analyzed": 0,
        "ai_note": "YouTube RSS 수집 단계에서 저장됨. NVIDIA 분석 전.",
        "source_query": source_query,
    }

# -----------------------------
# Text cleaning / guesses
# -----------------------------

def normalize_text(text: str) -> str:
    text = text or ""
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_video_title(raw_title: str, team: str = "") -> str:
    title = normalize_text(raw_title)
    # Remove common bracketed parts but keep Korean song title portions.
    title = re.sub(r"\[[^\]]{0,50}\]", " ", title)
    title = re.sub(r"\([^\)]{0,50}\)", " ", title)
    title = re.sub(r"\s+", " ", title).strip()

    remove_terms = [team, "LIVE", "Live", "라이브", "Official", "OFFICIAL", "MV", "워십", "찬양", "예배", "가사", "lyrics", "Lyrics"]
    for term in remove_terms:
        if term:
            title = title.replace(term, " ")

    # Split on common separators and choose a plausible title segment.
    parts = re.split(r"[|/｜\-–—:]", title)
    parts = [p.strip() for p in parts if p.strip()]
    if parts:
        # Usually the shortest meaningful segment is the song title.
        candidates = [p for p in parts if 2 <= len(p) <= 40]
        title = min(candidates, key=len) if candidates else parts[0]

    title = re.sub(r"\s+", " ", title).strip()
    return title or normalize_text(raw_title)


def guess_speed(title: str, duration_seconds: int) -> str:
    text = title.lower()
    slow_words = ["기도", "묵상", "임재", "은혜", "십자가", "보혈", "주 품", "주품", "눈물", "회복"]
    fast_words = ["기뻐", "춤", "찬양하", "승리", "선포", "할렐루야", "뛰", "호산나", "주를 찬양", "celebrate"]
    if any(w in text for w in fast_words):
        return "빠름"
    if any(w in text for w in slow_words):
        return "느림"
    if duration_seconds >= 540:
        return "느림"
    return "미확인"


def guess_theme(title: str) -> str:
    text = title.lower()
    mapping = {
        "십자가": ["십자가", "cross"],
        "보혈": ["보혈", "피", "blood"],
        "은혜": ["은혜", "grace"],
        "성령": ["성령", "holy spirit"],
        "감사": ["감사", "thank"],
        "회복": ["회복", "restore"],
        "승리": ["승리", "victory"],
        "소망": ["소망", "hope"],
        "사랑": ["사랑", "love"],
        "기도": ["기도", "pray"],
        "부흥": ["부흥", "revival"],
        "예배": ["예배", "worship"],
        "찬양": ["찬양", "praise"],
        "결단": ["나아가", "따르", "헌신", "결단"],
    }
    for theme, words in mapping.items():
        if any(w in text for w in words):
            return theme
    return "미확인"


def should_exclude(title: str, exclude_keywords: Sequence[str], min_sec: int, max_sec: int, duration_seconds: int) -> bool:
    lower_title = title.lower()
    if any(k.lower() in lower_title for k in exclude_keywords if k.strip()):
        return True
    if duration_seconds and duration_seconds < min_sec:
        return True
    if duration_seconds and duration_seconds > max_sec:
        return True
    return False


def load_sources() -> pd.DataFrame:
    if not SOURCES_PATH.exists():
        return pd.DataFrame(columns=["team", "search_queries", "channel_urls", "notes"])
    df = pd.read_csv(SOURCES_PATH)
    df = df.dropna(how="all")
    # Older versions had only team/search_queries/notes. Channel URLs are optional.
    for col in ["team", "search_queries", "channel_urls", "notes"]:
        if col not in df.columns:
            df[col] = ""
    return df[["team", "search_queries", "channel_urls", "notes"]].copy()


def save_sources(df: pd.DataFrame) -> None:
    for col in ["team", "search_queries", "channel_urls", "notes"]:
        if col not in df.columns:
            df[col] = ""
    df = df[["team", "search_queries", "channel_urls", "notes"]].copy()
    df = df.dropna(how="all")
    df.to_csv(SOURCES_PATH, index=False, encoding="utf-8-sig")


def add_or_update_source(team: str, search_queries: str, channel_urls: str = "", notes: str = "사용자 추가") -> None:
    team = normalize_text(team)
    search_queries = normalize_text(search_queries)
    channel_urls = normalize_text(channel_urls)
    if not team:
        raise ValueError("찬양팀 이름을 입력해야 합니다.")
    # 사용자가 팀 이름만 넣어도 일단 저장되게 기본 검색어를 자동 생성합니다.
    # 채널 ID/URL은 YouTube API 채널 자동 찾기에서 나중에 보강할 수 있습니다.
    if not search_queries:
        search_queries = f"{team} 찬양|{team} worship|{team} 라이브|{team} 예배"
    df = load_sources()
    new_row = {"team": team, "search_queries": search_queries, "channel_urls": channel_urls, "notes": notes}
    if not df.empty and team in df["team"].astype(str).tolist():
        df.loc[df["team"].astype(str) == team, ["search_queries", "channel_urls", "notes"]] = [search_queries, channel_urls, notes]
    else:
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    save_sources(df)


def extract_json_from_text(text: str) -> Dict:
    text = text or ""
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def nvidia_chat_completion(api_key: str, base_url: str, model: str, messages: List[Dict], max_tokens: int = 700, temperature: float = 0.2) -> str:
    if not api_key:
        return ""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": 0.9,
        "stream": False,
    }
    response = requests.post(url, headers=headers, json=payload, timeout=45)
    response.raise_for_status()
    data = response.json()
    return data.get("choices", [{}])[0].get("message", {}).get("content", "")


def analyze_song_with_nvidia(raw_title: str, team: str, channel_title: str, description: str, api_key: str, base_url: str, model: str) -> Dict:
    prompt = f"""
유튜브 찬양 영상 정보를 보고 찬양 DB에 넣을 값을 추정해줘.
반드시 JSON만 출력해. 모르면 미확인으로 써.

허용 speed: 미확인, 느림, 중간, 빠름
허용 theme: 미확인, 은혜, 회복, 십자가, 보혈, 성령, 찬양, 감사, 결단, 선포, 예배, 소망, 사랑, 기도, 부흥, 승리, 임재, 경배, 기쁨, 묵상

입력:
- 찬양팀 후보: {team}
- 채널명: {channel_title}
- 원본 제목: {raw_title}
- 설명 일부: {(description or '')[:500]}

출력 JSON 형식:
{{
  "clean_title": "곡명만",
  "speed": "느림/중간/빠름/미확인",
  "theme": "위 허용 theme 중 하나",
  "confidence": 0.0
}}
"""
    content = nvidia_chat_completion(
        api_key=api_key,
        base_url=base_url,
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
        temperature=0.1,
    )
    data = extract_json_from_text(content)
    if data.get("speed") not in SPEED_OPTIONS:
        data["speed"] = "미확인"
    if data.get("theme") not in THEME_OPTIONS:
        data["theme"] = "미확인"
    clean = normalize_text(str(data.get("clean_title", "")))
    if not clean or len(clean) > 60:
        data["clean_title"] = ""
    return data


def timestamp_to_seconds(value: str) -> Optional[int]:
    """Convert YouTube-style timestamps such as 1:23 or 01:02:03 to seconds."""
    value = normalize_text(value)
    if not value:
        return None
    match = re.search(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})", value)
    if not match:
        return None
    h = int(match.group(1) or 0)
    m = int(match.group(2) or 0)
    s = int(match.group(3) or 0)
    return h * 3600 + m * 60 + s


def video_url_with_start(source_video_id: str, start_seconds: Optional[int]) -> str:
    base = f"https://www.youtube.com/watch?v={source_video_id}"
    if start_seconds and start_seconds > 0:
        return f"{base}&t={int(start_seconds)}s"
    return base


def normalize_ai_song_item(item: Dict, fallback_title: str, fallback_speed: str, fallback_theme: str) -> Optional[Dict]:
    clean_title = normalize_text(str(item.get("clean_title") or item.get("title") or ""))
    if not clean_title or clean_title in {"미확인", "unknown", "Unknown"}:
        clean_title = fallback_title
    if not clean_title or len(clean_title) > 80:
        return None

    speed = normalize_text(str(item.get("speed") or fallback_speed or "미확인"))
    if speed not in SPEED_OPTIONS:
        speed = "미확인"

    theme = normalize_text(str(item.get("theme") or fallback_theme or "미확인"))
    if theme not in THEME_OPTIONS:
        theme = "미확인"

    segment_start = normalize_text(str(item.get("segment_start") or item.get("start") or ""))
    segment_label = normalize_text(str(item.get("segment_label") or item.get("section") or ""))
    start_seconds = timestamp_to_seconds(segment_start)
    if not segment_label and segment_start:
        segment_label = segment_start

    return {
        "clean_title": clean_title,
        "speed": speed,
        "theme": theme,
        "segment_start": segment_start,
        "segment_label": segment_label,
        "segment_start_seconds": start_seconds,
        "confidence": item.get("confidence", ""),
    }


def split_song_titles_from_raw_title(raw_title: str, team: str, duration_seconds: int, max_songs: int = 12) -> List[Dict]:
    """Fallback splitter for titles like '곡A+곡B+곡C - 피아워십'.
    It does not invent codes/lyrics; it only splits likely title text.
    """
    title = normalize_text(raw_title)
    # Remove team/channel and bracketed metadata first.
    title = re.sub(r"\[[^\]]{0,80}\]", " ", title)
    title = re.sub(r"\([^\)]{0,80}\)", " ", title)
    # Keep only the part before common credit separators if it contains song separators.
    parts = re.split(r"\s[-–—|｜:]\s", title)
    candidate = parts[0] if parts else title
    for term in [team, "피아워십", "F.I.A", "FIA", "워십", "찬양", "LIVE", "Live", "라이브"]:
        if term:
            candidate = candidate.replace(term, " ")
    # Split common medley separators.
    pieces = re.split(r"\s*(?:\+|/|,|，|ㆍ|·|→|>)\s*", candidate)
    pieces = [normalize_text(p) for p in pieces if 2 <= len(normalize_text(p)) <= 60]
    # Avoid false splitting when nothing really looks like a medley.
    if len(pieces) <= 1:
        return [
            {
                "clean_title": clean_video_title(raw_title, team),
                "speed": guess_speed(raw_title, duration_seconds),
                "theme": guess_theme(raw_title),
                "segment_start": "",
                "segment_label": "",
                "segment_start_seconds": None,
                "confidence": "fallback_single",
            }
        ]
    out = []
    seen = set()
    for p in pieces[:max_songs]:
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "clean_title": p,
                "speed": guess_speed(p, duration_seconds),
                "theme": guess_theme(p),
                "segment_start": "",
                "segment_label": "",
                "segment_start_seconds": None,
                "confidence": "fallback_title_split",
            }
        )
    return out


def build_song_rows_from_video_item(
    item: Dict,
    team: str,
    source_query: str,
    use_nvidia_enrichment: bool,
    split_multi_song_videos: bool,
    nvidia_api_key: str,
    nvidia_base_url: str,
    nvidia_model: str,
    max_songs_per_video: int,
) -> Tuple[List[Dict], List[str]]:
    """Convert one YouTube video resource into one or more DB rows."""
    logs: List[str] = []
    source_video_id = item.get("id")
    snippet = item.get("snippet", {})
    content = item.get("contentDetails", {})
    duration_iso = content.get("duration", "")
    duration_sec = iso8601_duration_to_seconds(duration_iso)
    raw_title = snippet.get("title", "")
    description = snippet.get("description", "")
    channel_title = snippet.get("channelTitle", "")
    song_items: List[Dict]
    if split_multi_song_videos and use_nvidia_enrichment and nvidia_api_key:
        try:
            song_items = analyze_video_songs_with_nvidia(
                raw_title=raw_title,
                team=team,
                channel_title=channel_title,
                description=description,
                duration_seconds=duration_sec,
                api_key=nvidia_api_key,
                base_url=nvidia_base_url,
                model=nvidia_model,
                max_songs=int(max_songs_per_video),
            )
        except Exception as ai_e:
            logs.append(f"⚠️ NVIDIA 곡 분리 실패: {raw_title[:35]}... / {ai_e}")
            song_items = split_song_titles_from_raw_title(raw_title, team, duration_sec, int(max_songs_per_video))
    elif split_multi_song_videos:
        song_items = split_song_titles_from_raw_title(raw_title, team, duration_sec, int(max_songs_per_video))
    elif use_nvidia_enrichment and nvidia_api_key:
        try:
            ai_guess = analyze_song_with_nvidia(
                raw_title=raw_title,
                team=team,
                channel_title=channel_title,
                description=description,
                api_key=nvidia_api_key,
                base_url=nvidia_base_url,
                model=nvidia_model,
            )
            song_items = [
                {
                    "clean_title": ai_guess.get("clean_title") or clean_video_title(raw_title, team),
                    "speed": ai_guess.get("speed") or guess_speed(raw_title, duration_sec),
                    "theme": ai_guess.get("theme") or guess_theme(raw_title),
                    "segment_start": "",
                    "segment_label": "",
                    "segment_start_seconds": None,
                    "confidence": ai_guess.get("confidence", ""),
                }
            ]
        except Exception as ai_e:
            logs.append(f"⚠️ NVIDIA 분석 실패: {raw_title[:35]}... / {ai_e}")
            song_items = split_song_titles_from_raw_title(raw_title, team, duration_sec, 1)
    else:
        song_items = split_song_titles_from_raw_title(raw_title, team, duration_sec, 1)

    rows: List[Dict] = []
    for idx, song_item in enumerate(song_items[: int(max_songs_per_video)], start=1):
        start_seconds = song_item.get("segment_start_seconds")
        multi = len(song_items) > 1
        row_video_id = f"{source_video_id}__song_{idx:02d}" if multi else source_video_id
        rows.append(
            {
                "video_id": row_video_id,
                "source_video_id": source_video_id,
                "song_index": idx,
                "segment_label": song_item.get("segment_label", ""),
                "segment_start_seconds": start_seconds,
                "raw_title": raw_title,
                "clean_title": song_item.get("clean_title") or clean_video_title(raw_title, team),
                "team": team,
                "channel_title": channel_title,
                "video_url": video_url_with_start(source_video_id, start_seconds),
                "published_at": snippet.get("publishedAt", ""),
                "duration_iso": duration_iso,
                "duration_seconds": duration_sec,
                "description": description,
                "speed": song_item.get("speed") or guess_speed(raw_title, duration_sec),
                "theme": song_item.get("theme") or guess_theme(raw_title),
                "source_query": source_query,
            }
        )
    return rows, logs


def analyze_video_songs_with_nvidia(
    raw_title: str,
    team: str,
    channel_title: str,
    description: str,
    duration_seconds: int,
    api_key: str,
    base_url: str,
    model: str,
    max_songs: int = 12,
) -> List[Dict]:
    """
    Use NVIDIA-compatible chat completion to turn one YouTube video into one or more song DB rows.
    This is useful for worship sets where one live video contains multiple songs.
    """
    fallback_title = clean_video_title(raw_title, team)
    fallback_speed = guess_speed(raw_title, duration_seconds)
    fallback_theme = guess_theme(raw_title)
    prompt = f"""
유튜브 찬양 영상 하나를 찬양곡 DB에 넣기 위해 분석해줘.
영상 하나가 한 곡이면 songs 배열에 1개만 넣고, 예배실황/메들리/콘티 영상처럼 여러 곡이 들어있으면 곡별로 나눠서 여러 개를 넣어줘.
설명란에 타임스탬프가 있으면 segment_start에 00:00 또는 12:34 형식으로 넣어줘.
모르면 미확인으로 쓰고, 코드/악보/가사는 추정하거나 만들지 마.
반드시 JSON만 출력해.

허용 speed: 미확인, 느림, 중간, 빠름
허용 theme: 미확인, 은혜, 회복, 십자가, 보혈, 성령, 찬양, 감사, 결단, 선포, 예배, 소망, 사랑, 기도, 부흥, 승리, 임재, 경배, 기쁨, 묵상

입력:
- 찬양팀 후보: {team}
- 채널명: {channel_title}
- 원본 제목: {raw_title}
- 영상 길이: {duration_seconds}초
- 설명 일부:
{(description or '')[:1800]}

출력 JSON 형식:
{{
  "is_multi_song_video": true,
  "songs": [
    {{
      "clean_title": "곡명만",
      "speed": "느림/중간/빠름/미확인",
      "theme": "허용 theme 중 하나",
      "segment_start": "00:00",
      "segment_label": "선택: 00:00 곡명",
      "confidence": 0.0
    }}
  ]
}}
"""
    content = nvidia_chat_completion(
        api_key=api_key,
        base_url=base_url,
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1400,
        temperature=0.1,
    )
    data = extract_json_from_text(content)
    raw_songs = data.get("songs") if isinstance(data, dict) else []
    if not isinstance(raw_songs, list) or not raw_songs:
        return [
            {
                "clean_title": fallback_title,
                "speed": fallback_speed,
                "theme": fallback_theme,
                "segment_start": "",
                "segment_label": "",
                "segment_start_seconds": None,
                "confidence": "",
            }
        ]

    normalized: List[Dict] = []
    seen = set()
    for item in raw_songs[:max_songs]:
        if not isinstance(item, dict):
            continue
        n = normalize_ai_song_item(item, fallback_title, fallback_speed, fallback_theme)
        if not n:
            continue
        dedupe_key = (n["clean_title"].lower(), n.get("segment_start_seconds"))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append(n)

    return normalized or [
        {
            "clean_title": fallback_title,
            "speed": fallback_speed,
            "theme": fallback_theme,
            "segment_start": "",
            "segment_label": "",
            "segment_start_seconds": None,
            "confidence": "",
        }
    ]



def build_video_row_from_item(item: Dict, team: str, source_query: str) -> Dict:
    """Store one YouTube video as one DB item.
    The app saves only the YouTube URL and metadata, never the actual video file.
    AI analysis is intentionally handled later so full collection can run without NVIDIA 429 errors.
    """
    source_video_id = item.get("id")
    snippet = item.get("snippet", {})
    content = item.get("contentDetails", {})
    duration_iso = content.get("duration", "")
    duration_sec = iso8601_duration_to_seconds(duration_iso)
    raw_title = snippet.get("title", "")
    return {
        "video_id": source_video_id,
        "source_video_id": source_video_id,
        "song_index": 1,
        "segment_label": "",
        "segment_start_seconds": None,
        "raw_title": raw_title,
        "clean_title": clean_video_title(raw_title, team),
        "team": team,
        "channel_title": snippet.get("channelTitle", ""),
        "video_url": video_url_with_start(source_video_id, None),
        "published_at": snippet.get("publishedAt", ""),
        "duration_iso": duration_iso,
        "duration_seconds": duration_sec,
        "description": snippet.get("description", ""),
        "speed": "미확인",
        "theme": "미확인",
        "is_medley": 0,
        "medley_song_count": 1,
        "medley_songs": "",
        "ai_analyzed": 0,
        "ai_note": "YouTube 전체 수집 단계에서 저장됨. NVIDIA 분석 전.",
        "source_query": source_query,
    }


def build_video_row_from_search_item(search_item: Dict, team: str, source_query: str) -> Optional[Dict]:
    """Fallback storage from search.list results when videos.list is unavailable or rate-limited.

    This still stores only the YouTube URL and basic metadata. Duration remains 0 until a later refresh/analyze step.
    """
    raw_id = search_item.get("id", {})
    source_video_id = raw_id.get("videoId") if isinstance(raw_id, dict) else raw_id
    if not source_video_id:
        return None
    snippet = search_item.get("snippet", {})
    raw_title = snippet.get("title", "")
    return {
        "video_id": source_video_id,
        "source_video_id": source_video_id,
        "song_index": 1,
        "segment_label": "",
        "segment_start_seconds": None,
        "raw_title": raw_title,
        "clean_title": clean_video_title(raw_title, team),
        "team": team,
        "channel_title": snippet.get("channelTitle", ""),
        "video_url": video_url_with_start(source_video_id, None),
        "published_at": snippet.get("publishedAt", ""),
        "duration_iso": "",
        "duration_seconds": 0,
        "description": snippet.get("description", ""),
        "speed": "미확인",
        "theme": "미확인",
        "is_medley": 0,
        "medley_song_count": 1,
        "medley_songs": "",
        "ai_analyzed": 0,
        "ai_note": "YouTube search 결과에서 우선 저장됨. 상세 길이 정보는 나중에 보강 가능.",
        "source_query": source_query,
    }


def build_video_row_from_playlist_item(playlist_item: Dict, team: str, source_query: str) -> Optional[Dict]:
    """Store one channel uploads playlist item as one DB row.

    This saves only the YouTube URL and basic metadata. Duration remains 0
    until a later detail refresh/analyze step.
    """
    snippet = playlist_item.get("snippet", {})
    content_details = playlist_item.get("contentDetails", {})
    source_video_id = content_details.get("videoId") or snippet.get("resourceId", {}).get("videoId", "")
    if not source_video_id:
        return None
    raw_title = snippet.get("title", "")
    return {
        "video_id": source_video_id,
        "source_video_id": source_video_id,
        "song_index": 1,
        "segment_label": "",
        "segment_start_seconds": None,
        "raw_title": raw_title,
        "clean_title": clean_video_title(raw_title, team),
        "team": team,
        "channel_title": snippet.get("channelTitle", ""),
        "video_url": video_url_with_start(source_video_id, None),
        "published_at": snippet.get("publishedAt", ""),
        "duration_iso": "",
        "duration_seconds": 0,
        "description": snippet.get("description", ""),
        "speed": "미확인",
        "theme": "미확인",
        "is_medley": 0,
        "medley_song_count": 1,
        "medley_songs": "",
        "ai_analyzed": 0,
        "ai_note": "YouTube 채널 업로드 목록 수집 단계에서 저장됨. NVIDIA 분석 전.",
        "source_query": source_query,
    }


def normalize_medley_songs(value) -> List[str]:
    """Return a clean list of song titles from AI output or existing DB text."""
    if value is None:
        return []
    if isinstance(value, list):
        raw = value
    else:
        text = normalize_text(str(value))
        if not text:
            return []
        try:
            parsed = json.loads(text)
            raw = parsed if isinstance(parsed, list) else [text]
        except Exception:
            raw = re.split(r"\s*(?:\+|/|,|，|ㆍ|·|→|>|\n)\s*", text)
    out = []
    seen = set()
    for item in raw:
        if isinstance(item, dict):
            title = normalize_text(str(item.get("clean_title") or item.get("title") or item.get("song") or ""))
        else:
            title = normalize_text(str(item))
        title = re.sub(r"^[0-9]+[.)]\s*", "", title).strip()
        if not title or title in {"미확인", "unknown", "Unknown"}:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(title[:80])
    return out


def analyze_video_as_medley_item_with_nvidia(
    raw_title: str,
    team: str,
    channel_title: str,
    description: str,
    duration_seconds: int,
    api_key: str,
    base_url: str,
    model: str,
) -> Dict:
    """Analyze one YouTube video as ONE DB item.
    If it contains several songs, keep it as a medley item and store the included titles/count.
    """
    fallback_title = clean_video_title(raw_title, team)
    prompt = f"""
유튜브 찬양 영상 정보를 보고 DB 검수에 필요한 값을 추정해줘.
중요: 영상 하나에 여러 곡이 들어있는 예배실황/메들리/콘티 영상이어도 DB 행은 1개로 저장할 거야.
따라서 여러 곡이면 is_medley=true, medley_song_count=포함된 실제 곡 수, medley_songs=곡명 배열로만 정리해.
메들리라고 해서 곡별 DB 행으로 쪼개지 마.
코드/악보/가사는 추정하거나 만들지 마. 모르면 미확인으로 써.
반드시 JSON만 출력해.

허용 speed: 미확인, 느림, 중간, 빠름
허용 theme: 미확인, 은혜, 회복, 십자가, 보혈, 성령, 찬양, 감사, 결단, 선포, 예배, 소망, 사랑, 기도, 부흥, 승리, 임재, 경배, 기쁨, 묵상

입력:
- 찬양팀 후보: {team}
- 채널명: {channel_title}
- 원본 제목: {raw_title}
- 영상 길이: {duration_seconds}초
- 설명 일부:
{(description or '')[:1800]}

출력 JSON 형식:
{{
  "clean_title": "DB에 표시할 제목. 메들리면 대표 제목 또는 원제목 요약",
  "speed": "느림/중간/빠름/미확인",
  "theme": "허용 theme 중 하나",
  "is_medley": true,
  "medley_song_count": 3,
  "medley_songs": ["곡명1", "곡명2", "곡명3"],
  "ai_note": "짧은 분석 메모",
  "confidence": 0.0
}}
"""
    content = nvidia_chat_completion(
        api_key=api_key,
        base_url=base_url,
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1100,
        temperature=0.1,
    )
    data = extract_json_from_text(content)
    if not isinstance(data, dict):
        data = {}

    speed = normalize_text(str(data.get("speed") or "미확인"))
    if speed not in SPEED_OPTIONS:
        speed = "미확인"
    theme = normalize_text(str(data.get("theme") or "미확인"))
    if theme not in THEME_OPTIONS:
        theme = "미확인"

    medley_songs = normalize_medley_songs(data.get("medley_songs"))
    is_medley = bool(data.get("is_medley")) or len(medley_songs) > 1
    medley_count = int(data.get("medley_song_count") or len(medley_songs) or 1)
    if medley_count < 1:
        medley_count = 1
    if medley_songs and medley_count < len(medley_songs):
        medley_count = len(medley_songs)
    if not is_medley:
        medley_count = 1
        medley_songs = []

    clean_title = normalize_text(str(data.get("clean_title") or ""))
    if not clean_title or clean_title == "미확인" or len(clean_title) > 100:
        if is_medley and medley_songs:
            clean_title = " + ".join(medley_songs[:4]) + (" + ..." if len(medley_songs) > 4 else "")
        else:
            clean_title = fallback_title

    return {
        "clean_title": clean_title,
        "speed": speed,
        "theme": theme,
        "is_medley": 1 if is_medley else 0,
        "medley_song_count": medley_count,
        "medley_songs": json.dumps(medley_songs, ensure_ascii=False) if medley_songs else "",
        "ai_analyzed": 1,
        "ai_note": normalize_text(str(data.get("ai_note") or data.get("note") or ""))[:500],
    }


def song_count_value(row: Dict) -> int:
    """How many actual worship songs this DB item represents.

    A normal single-song video counts as 1.
    A medley/set video can count as 2, 3, 5... depending on `medley_song_count`.
    """
    try:
        return max(1, int(row.get("medley_song_count") or 1))
    except Exception:
        return 1


def selected_song_count_value(row: Dict) -> int:
    """How many songs from this DB item are selected for the current conti section."""
    try:
        return max(1, int(row.get("_selected_song_count") or song_count_value(row)))
    except Exception:
        return song_count_value(row)


def medley_songs_display(value: str) -> str:
    songs = normalize_medley_songs(value)
    return ", ".join(songs)


def make_section_item(row: Dict, selected_count: int) -> Dict:
    """Copy a DB row and attach section-specific selection info.

    Example: a 5-song fast medley can be used as exactly 3 songs
    in a section that asks for 3 fast songs. The full DB item remains
    unchanged, but the recommendation output clearly says only 3 songs
    from that medley are being used.
    """
    out = dict(row)
    full_count = song_count_value(out)
    selected_count = max(1, min(int(selected_count), full_count))
    medley_titles = normalize_medley_songs(out.get("medley_songs", ""))

    out["_selected_song_count"] = selected_count
    out["_full_song_count"] = full_count
    out["_partial_medley"] = 1 if full_count > selected_count else 0
    if full_count > 1:
        out["_selected_medley_songs"] = medley_titles[:selected_count] if medley_titles else []
    else:
        out["_selected_medley_songs"] = []
    return out


def conti_title_for_item(row: Dict) -> str:
    """Readable title for output rows."""
    selected_count = selected_song_count_value(row)
    full_count = song_count_value(row)
    title = row.get("clean_title") or row.get("raw_title") or "미확인"
    selected_titles = row.get("_selected_medley_songs") or []
    if full_count > 1 and selected_titles:
        return " + ".join(selected_titles)
    if full_count > 1 and selected_count < full_count:
        return f"{title} 중 {selected_count}곡"
    return title

# -----------------------------
# Recommendation
# -----------------------------

def parse_available_keys(value: str) -> List[str]:
    if not value:
        return []
    return [x.strip() for x in re.split(r"[,/| ]+", value) if x.strip()]


def key_compatible(a: Dict, b: Dict) -> int:
    ka = a.get("song_key") or "미확인"
    kb = b.get("song_key") or "미확인"
    if ka == "미확인" or kb == "미확인":
        return 5
    if ka == kb:
        return 40
    if kb in parse_available_keys(a.get("available_keys", "")) or ka in parse_available_keys(b.get("available_keys", "")):
        return 28
    # Very simple related-key comfort score.
    fifths = ["C", "G", "D", "A", "E", "B", "Gb", "Db", "Ab", "Eb", "Bb", "F"]
    try:
        gap = abs(fifths.index(ka) - fifths.index(kb))
        gap = min(gap, 12 - gap)
        if gap == 1:
            return 20
        if gap == 2:
            return 12
    except ValueError:
        pass
    return 0


def transition_score(a: Dict, b: Dict) -> Tuple[int, List[str]]:
    score = 0
    reasons = []

    ks = key_compatible(a, b)
    score += ks
    if ks >= 40:
        reasons.append("같은 키라 바로 연결 쉬움")
    elif ks >= 20:
        reasons.append("가까운 키라 전조/패드 연결 가능")
    elif ks <= 5:
        reasons.append("키 연결은 검토 필요")

    bpm_a = a.get("bpm") or 0
    bpm_b = b.get("bpm") or 0
    if bpm_a and bpm_b:
        gap = abs(int(bpm_a) - int(bpm_b))
        if gap <= 5:
            score += 25
            reasons.append(f"BPM 차이 {gap}로 매우 비슷함")
        elif gap <= 10:
            score += 16
            reasons.append(f"BPM 차이 {gap}로 연결 가능")
        elif gap <= 18:
            score += 8
            reasons.append(f"BPM 차이 {gap}라 드럼/멘트 연결 권장")
        else:
            score -= 8
            reasons.append(f"BPM 차이 {gap}가 커서 연결 주의")
    else:
        score += 5
        reasons.append("BPM 미입력 곡 포함")

    first_b = (b.get("first_chord") or "").strip()
    last_a = (a.get("last_chord") or "").strip()
    if first_b and last_a:
        if first_b == last_a:
            score += 18
            reasons.append("이전 곡 마지막 코드와 다음 곡 첫 코드가 같음")
        else:
            # Same root letter or common dominant-to-tonic relation gets small score.
            if first_b[0] == last_a[0]:
                score += 10
                reasons.append("첫 코드/마지막 코드 루트가 비슷함")
            else:
                score += 2
                reasons.append("코드 연결은 인도자가 확인 필요")
    else:
        score += 4
        reasons.append("첫 코드/마지막 코드 미입력 포함")

    theme_a = a.get("theme") or "미확인"
    theme_b = b.get("theme") or "미확인"
    if theme_a != "미확인" and theme_a == theme_b:
        score += 12
        reasons.append("주제가 이어짐")
    elif theme_a != "미확인" and theme_b != "미확인":
        score += 4
        reasons.append("주제 전환 가능")

    return score, reasons


def sequence_score(seq: List[Dict], connected: bool) -> Tuple[int, str]:
    if len(seq) <= 1:
        return 20, "단독 곡이라 연결 제약 없음"
    total = 0
    reason_lines = []
    for i in range(len(seq) - 1):
        sc, rs = transition_score(seq[i], seq[i + 1]) if connected else (10, ["끊어 부르는 구간"])
        total += sc
        reason_lines.append(f"{i+1}→{i+2}: " + "; ".join(rs[:3]))
    return total, "\n".join(reason_lines)


def generate_section_sequences(candidates: List[Dict], count: int, connected: bool, max_candidates: int = 45) -> List[Tuple[int, List[Dict], str]]:
    """Generate section sequences whose actual worship-song count equals `count`.

    Important medley rule:
    - DB stores a medley as ONE row.
    - Recommendation counts it by `medley_song_count`.
    - If a medley has more songs than the requested remaining count, the app can recommend only the needed subset.
      Example: requested fast section = 3 songs, candidate medley = 5 songs -> recommend that medley item as "use 3 of 5 songs".
    """
    candidates = candidates[:max_candidates]
    if count <= 0 or not candidates:
        return []

    results: List[Tuple[int, List[Dict], str]] = []
    max_results_to_score = 9000

    def completeness_bonus(seq: List[Dict]) -> int:
        bonus = 0
        for c in seq:
            for field in ["song_key", "bpm", "theme"]:
                if c.get(field) and c.get(field) != "미확인":
                    bonus += 2
            if c.get("first_chord") and c.get("last_chord"):
                bonus += 2
            # Small bonus when a medley naturally fills several requested slots.
            if selected_song_count_value(c) > 1:
                bonus += min(selected_song_count_value(c), 4)
        return bonus

    def add_result(seq: List[Dict]) -> None:
        if not seq:
            return
        actual_count = sum(selected_song_count_value(x) for x in seq)
        if actual_count != count:
            return
        sc, reason = sequence_score(seq, connected)
        unique_teams = len(set(s.get("team", "") for s in seq))
        sc += min(unique_teams * 3, 9)
        sc += completeness_bonus(seq)
        partial_notes = []
        for item in seq:
            full_count = song_count_value(item)
            selected_count = selected_song_count_value(item)
            if full_count > selected_count:
                partial_notes.append(f"{item.get('clean_title')} 메들리 {full_count}곡 중 {selected_count}곡만 사용")
            elif full_count > 1:
                partial_notes.append(f"{item.get('clean_title')} 메들리 {full_count}곡 사용")
        if partial_notes:
            reason = reason + "\n" + "\n".join(partial_notes)
        results.append((sc, seq, reason))

    def dfs(path: List[Dict], remaining: int, used_ids: set) -> None:
        if len(results) >= max_results_to_score:
            return
        if remaining == 0:
            add_result(path)
            return
        for c in candidates:
            cid = c.get("id")
            if cid in used_ids:
                continue
            full_count = song_count_value(c)
            selected_count = min(full_count, remaining)
            item = make_section_item(c, selected_count)
            next_remaining = remaining - selected_count
            dfs(path + [item], next_remaining, used_ids | {cid})
            if len(results) >= max_results_to_score:
                return

    dfs([], int(count), set())

    # De-duplicate sequences that can arise from equivalent medley selections.
    unique = {}
    for score, seq, reason in results:
        key = tuple((x.get("id"), selected_song_count_value(x)) for x in seq)
        if key not in unique or score > unique[key][0]:
            unique[key] = (score, seq, reason)

    return sorted(unique.values(), key=lambda x: x[0], reverse=True)[:20]

def build_contis(section_results: List[List[Tuple[int, List[Dict], str]]], top_n: int = 3) -> List[Dict]:
    if any(not r for r in section_results):
        return []
    contis = []
    for combo in itertools.product(*section_results):
        used_ids = []
        songs = []
        score = 0
        reasons = []
        duplicate = False
        for idx, (section_score, seq, reason) in enumerate(combo, start=1):
            score += section_score
            reasons.append(f"[구간 {idx}]\n{reason}")
            for s in seq:
                if s["id"] in used_ids:
                    duplicate = True
                    break
                used_ids.append(s["id"])
                songs.append(s)
            if duplicate:
                break
        if duplicate:
            continue
        contis.append({"score": score, "songs": songs, "reasons": "\n\n".join(reasons)})
        if len(contis) > 500:
            break
    return sorted(contis, key=lambda x: x["score"], reverse=True)[:top_n]

# -----------------------------
# UI
# -----------------------------

st.set_page_config(page_title="찬양 콘티 자동 추천기", page_icon="🎵", layout="wide")
init_db()

st.title("🎵 찬양곡 DB 자동 수집 + 콘티 추천기")
st.caption("YouTube 기반 전체 링크 수집 → NVIDIA AI 일괄 분석 → 사람이 키/BPM/코드 검수 → 검수 완료 곡으로 콘티 추천")

with st.sidebar:
    st.header("설정")

    youtube_secret_key = get_app_secret("YOUTUBE_API_KEY", "youtube", "api_key", env_key="YOUTUBE_API_KEY")
    nvidia_secret_key = get_app_secret("NVIDIA_API_KEY", "nvidia", "api_key", env_key="NVIDIA_API_KEY")
    nvidia_secret_base_url = get_app_secret("NVIDIA_BASE_URL", "nvidia", "base_url", env_key="NVIDIA_BASE_URL", default=NVIDIA_DEFAULT_BASE_URL)
    nvidia_secret_model = get_app_secret("NVIDIA_MODEL", "nvidia", "model", env_key="NVIDIA_MODEL", default=NVIDIA_DEFAULT_MODEL)

    st.write(f"YouTube API: {secret_status_label(youtube_secret_key)}")
    st.write(f"NVIDIA API: {secret_status_label(nvidia_secret_key)}")

    with st.expander("로컬 테스트용 직접 입력", expanded=False):
        st.caption("Streamlit Cloud/운영 환경에서는 secrets.toml 값을 우선 사용합니다. 여기는 임시 테스트용입니다.")
        youtube_override = st.text_input("YouTube Data API Key 직접 입력", type="password")
        nvidia_override = st.text_input("NVIDIA API Key 직접 입력", type="password")

    api_key = youtube_secret_key or youtube_override
    nvidia_api_key = nvidia_secret_key or nvidia_override

    st.divider()
    st.subheader("NVIDIA AI 보조")
    nvidia_base_url = st.text_input("NVIDIA Base URL", value=nvidia_secret_base_url or NVIDIA_DEFAULT_BASE_URL)
    nvidia_model = st.text_input("NVIDIA Model", value=nvidia_secret_model or NVIDIA_DEFAULT_MODEL, help="예: minimaxai/minimax-m3")

    menu = st.radio("메뉴", ["1. DB 자동 수집", "2. NVIDIA AI 분석", "3. 곡 검수", "4. 콘티 추천", "5. DB 관리"])

if menu == "1. DB 자동 수집":
    st.subheader("1. 유튜브 기반 찬양곡 후보 자동 수집")
    st.write("수집 방식을 선택할 수 있습니다. 기본 추천은 YouTube API 키 없이 가능한 채널 RSS 수집입니다. 영상 파일은 저장하지 않고 유튜브 링크와 메타데이터만 저장합니다.")

    sources = load_sources()
    if sources.empty:
        st.error("worship_sources.csv 파일이 없습니다.")
        st.stop()

    with st.expander("➕ 자동 수집 찬양팀 직접 추가/수정", expanded=False):
        st.write("팀 이름만 입력해도 저장됩니다. YouTube API 키가 있으면 채널 후보를 자동으로 찾아 채널 ID/RSS 주소까지 저장할 수 있습니다.")
        if "new_source_channel_urls" not in st.session_state:
            st.session_state["new_source_channel_urls"] = ""
        if "channel_search_candidates" not in st.session_state:
            st.session_state["channel_search_candidates"] = []

        add_c1, add_c2 = st.columns([1, 2])
        with add_c1:
            new_team = st.text_input("추가할 찬양팀 이름", placeholder="예: 피아워십")
            auto_query_text = f"{new_team} 찬양|{new_team} worship|{new_team} 라이브|{new_team} 예배" if new_team else ""
            st.caption("검색어를 비워두면 팀 이름 기준으로 자동 생성됩니다.")
        with add_c2:
            new_queries = st.text_input("검색어 묶음", value=auto_query_text, placeholder="검색어를 | 로 구분해서 입력")
            new_channel_urls = st.text_input(
                "채널/RSS URL 묶음",
                placeholder="자동 찾기로 채널 ID를 넣거나, https://www.youtube.com/@FIAWORSHIP 를 입력",
                key="new_source_channel_urls",
            )
            new_notes = st.text_input("메모", value="사용자 추가")

        st.markdown("#### 🔎 새 팀 채널 자동 찾기")
        st.caption("YouTube API 키가 있을 때 팀 이름으로 채널 후보를 검색합니다. 한 번 찾은 채널 ID/RSS 주소를 저장하면 이후 RSS 수집은 API 키 없이 가능합니다.")
        search_c1, search_c2 = st.columns([2, 1])
        with search_c1:
            channel_search_query = st.text_input("채널 검색어", value=new_team or "", placeholder="예: 피아워십, FIA WORSHIP")
        with search_c2:
            channel_candidate_count = st.number_input("후보 개수", min_value=1, max_value=20, value=8, step=1)
        if st.button("YouTube API로 채널 후보 찾기"):
            if not api_key:
                st.warning("채널 자동 찾기는 YouTube Data API 키가 필요합니다. 키가 없으면 채널 핸들 URL이나 채널 ID를 직접 넣어주세요.")
            elif not normalize_text(channel_search_query):
                st.warning("채널 검색어를 입력해주세요.")
            else:
                try:
                    st.session_state["channel_search_candidates"] = youtube_channel_search(api_key, channel_search_query, int(channel_candidate_count))
                    if not st.session_state["channel_search_candidates"]:
                        st.warning("채널 후보를 찾지 못했습니다. 검색어를 더 구체적으로 입력해보세요.")
                except Exception as e:
                    st.error(f"채널 후보 검색 실패: {sanitize_error_message(e, api_key)}")

        candidates = st.session_state.get("channel_search_candidates", [])
        if candidates:
            candidate_labels = [
                f"{i+1}. {c.get('title','미확인')} / {c.get('channel_id','')}"
                for i, c in enumerate(candidates)
            ]
            selected_label = st.selectbox("저장할 채널 후보 선택", candidate_labels)
            selected_idx = candidate_labels.index(selected_label)
            selected_candidate = candidates[selected_idx]
            st.write("선택 후보 설명:", selected_candidate.get("description", "")[:300])
            st.code(selected_candidate.get("rss_url", ""))
            if st.button("선택한 채널을 채널/RSS URL 칸에 넣기"):
                current_urls = normalize_text(st.session_state.get("new_source_channel_urls", ""))
                new_url = selected_candidate.get("channel_url", "")
                if current_urls and new_url not in current_urls:
                    st.session_state["new_source_channel_urls"] = current_urls + "|" + new_url
                elif not current_urls:
                    st.session_state["new_source_channel_urls"] = new_url
                st.success("선택한 채널 URL을 입력칸에 반영했습니다. 필요하면 찬양팀 목록에 저장을 눌러주세요.")
                st.rerun()

        if st.button("찬양팀 목록에 저장"):
            try:
                add_or_update_source(new_team, new_queries, st.session_state.get("new_source_channel_urls", ""), new_notes)
                st.success("찬양팀 목록에 저장했습니다. 화면을 새로고침하면 선택 목록에 보입니다.")
                sources = load_sources()
            except Exception as e:
                st.error(str(e))

    collection_method = st.radio(
        "수집 방식 선택",
        ["채널 RSS 수집(API 키 없음, 추천)", "YouTube API 채널 수집(쿼터 절약)", "YouTube API 검색 수집(넓게 수집, 쿼터 많이 사용)", "직접 URL 저장", "CSV 가져오기"],
        horizontal=False,
        help="RSS는 API 키/쿼터 없이 최근 업로드 중심으로 수집합니다. 채널 수집은 공식 채널 업로드 목록 중심이라 검색보다 쿼터를 아낍니다.",
    )
    st.info("찬양팀 우선순위는 쓰지 않습니다. 선택한 모든 찬양팀/채널/검색어를 동일하게 수집합니다.")
    col_a, col_b = st.columns([2, 1])
    with col_a:
        selected_teams = st.multiselect(
            "수집할 찬양팀 선택",
            options=sources["team"].tolist(),
            default=sources["team"].tolist(),
            help="기본값은 전체 선택입니다. 모든 팀을 DB에 넣고 싶으면 그대로 두면 됩니다.",
        )
    with col_b:
        st.metric("검색 페이지 크기", "50개")
        st.caption("YouTube API의 검색 1페이지 최대값인 50개로 고정합니다. 앱이 nextPageToken을 따라 끝까지 가져옵니다.")
        resume_collection = st.checkbox("이전 중단 지점부터 이어서 수집", value=True, help="quota 초과/네트워크 오류로 멈췄던 검색어는 저장된 다음 페이지 토큰부터 재개합니다.")
        reset_before_collect = st.checkbox("선택한 검색어의 수집 진행상태 초기화", value=False, help="처음 페이지부터 다시 훑고 싶을 때만 체크하세요. 이미 DB에 있는 영상은 중복 저장되지 않습니다.")
        use_optional_filter = st.checkbox("제외 키워드/길이 필터 적용", value=False, help="기본은 모든 영상 링크와 메타데이터를 저장합니다. 커버, MR, 너무 긴 영상 등을 제외하고 싶을 때만 켜세요.")
        if use_optional_filter:
            min_sec = st.number_input("최소 영상 길이(초)", min_value=0, max_value=600, value=0, step=30)
            max_sec = st.number_input("최대 영상 길이(초)", min_value=180, max_value=28800, value=14400, step=60)
        else:
            min_sec = 0
            max_sec = 10**9
        st.caption("이 단계에서는 NVIDIA AI를 쓰지 않습니다. 유튜브 링크/제목/채널/길이/설명만 전체 수집하고, AI 분석은 2번 메뉴에서 별도로 실행합니다.")

    exclude_keywords = []
    if use_optional_filter:
        exclude_text = st.text_area("제외 키워드", value=", ".join(DEFAULT_EXCLUDE_KEYWORDS), height=90)
        exclude_keywords = [x.strip() for x in exclude_text.split(",") if x.strip()]

    st.dataframe(sources[sources["team"].isin(selected_teams)], use_container_width=True)

    if collection_method == "채널 RSS 수집(API 키 없음, 추천)":
        st.markdown("### 📡 채널 RSS 수집")
        st.caption("`https://www.youtube.com/@FIAWORSHIP` 같은 핸들 URL은 RSS가 아닙니다. 앱이 채널 ID를 찾아 `feeds/videos.xml?channel_id=...` RSS 주소로 변환합니다. RSS는 보통 최근 업로드 중심으로 제공됩니다.")
        extra_rss_urls = st.text_area(
            "추가로 수집할 채널/RSS URL",
            placeholder="https://www.youtube.com/@FIAWORSHIP\nhttps://www.youtube.com/feeds/videos.xml?channel_id=UC...",
            height=90,
        )
        if st.button("선택한 찬양팀 채널 RSS 수집 시작", type="primary"):
            selected = sources[sources["team"].isin(selected_teams)]
            tasks = []
            for _, row in selected.iterrows():
                for raw_url in str(row.get("channel_urls", "")).split("|"):
                    raw_url = raw_url.strip()
                    if raw_url:
                        tasks.append((row["team"], raw_url))
            for raw_url in [x.strip() for x in extra_rss_urls.splitlines() if x.strip()]:
                tasks.append(("직접 추가 RSS", raw_url))

            if not tasks:
                st.warning("선택한 찬양팀에 channel_urls가 없습니다. 찬양팀 추가/수정에서 채널 URL을 넣거나, 추가 URL 칸에 직접 입력해주세요.")
            else:
                total_inserted = 0
                total_skipped = 0
                total_seen = 0
                progress = st.progress(0)
                log_box = st.empty()
                logs = []
                for idx, (team, channel_value) in enumerate(tasks, start=1):
                    try:
                        entries, rss_url, channel_id = fetch_youtube_rss_entries(channel_value)
                        inserted = 0
                        skipped = 0
                        for entry in entries:
                            song = build_video_row_from_rss_entry(entry, team=team, source_query=f"RSS:{rss_url}")
                            if upsert_song(song):
                                inserted += 1
                            else:
                                skipped += 1
                        update_collection_state(
                            team,
                            f"RSS:{rss_url}",
                            next_page_token="",
                            completed=1,
                            page_delta=1,
                            videos_delta=len(entries),
                            inserted_delta=inserted,
                            skipped_delta=skipped,
                        )
                        total_inserted += inserted
                        total_skipped += skipped
                        total_seen += len(entries)
                        logs.append(f"✅ {team}: RSS {len(entries)}개 확인, 신규 {inserted}개, 중복 {skipped}개 / channel_id={channel_id}")
                    except Exception as e:
                        safe_error = sanitize_error_message(e, api_key)
                        update_collection_state(team, f"RSS:{channel_value}", completed=0, last_error=safe_error)
                        logs.append(f"⚠️ {team}: RSS 수집 실패 - {safe_error}")
                    progress.progress(idx / max(len(tasks), 1))
                    log_box.text("\n".join(logs[-18:]))

                db_total = int(query_df("SELECT COUNT(*) AS cnt FROM songs").iloc[0]["cnt"])
                st.success(f"RSS 수집 완료: 영상 {total_seen}개 확인, 신규 저장 {total_inserted}개, 중복 {total_skipped}개, 현재 DB 총 {db_total}개")

    if collection_method == "YouTube API 채널 수집(쿼터 절약)":
        st.markdown("### 📺 YouTube API 채널 수집")
        st.caption("검색어로 넓게 찾는 대신, 지정된 채널의 업로드 목록을 가져옵니다. 검색 수집보다 쿼터를 훨씬 아끼고 공식 채널 중심 DB를 만들 때 좋습니다.")
        auto_find_missing = st.checkbox("채널 URL이 없는 팀은 팀 이름으로 채널 1순위 자동 탐색", value=True)
        save_auto_found = st.checkbox("자동 탐색한 채널 URL을 찬양팀 목록에 저장", value=True)
        extra_channel_values = st.text_area(
            "추가로 수집할 채널 ID/핸들/URL",
            placeholder="UCmDCtLeqOzF7_uf_UXoNiYA\nhttps://www.youtube.com/@FIAWORSHIP",
            height=90,
        )

        if st.button("선택한 찬양팀 YouTube API 채널 수집 시작", type="primary"):
            if not api_key:
                st.error("YouTube API 채널 수집은 YouTube Data API Key가 필요합니다. API 키 없이 하려면 채널 RSS 수집을 사용하세요.")
                st.stop()

            selected = sources[sources["team"].isin(selected_teams)]
            tasks = []
            task_source_rows = {}
            for _, row in selected.iterrows():
                team = str(row.get("team", ""))
                urls = [x.strip() for x in str(row.get("channel_urls", "")).split("|") if x.strip()]
                if urls:
                    for channel_value in urls:
                        tasks.append((team, channel_value, row))
                elif auto_find_missing:
                    tasks.append((team, f"AUTO_SEARCH::{team}", row))
            for raw_value in [x.strip() for x in extra_channel_values.splitlines() if x.strip()]:
                tasks.append(("직접 추가 채널", raw_value, {}))

            if not tasks:
                st.warning("수집할 채널이 없습니다. 채널 URL을 추가하거나 자동 탐색을 켜주세요.")
            else:
                total_inserted = 0
                total_skipped = 0
                total_videos = 0
                total_pages = 0
                progress = st.progress(0)
                log_box = st.empty()
                logs = []
                stop_all_collection = False

                for idx, (team, channel_value, source_row) in enumerate(tasks, start=1):
                    try:
                        found_by_search = False
                        if channel_value.startswith("AUTO_SEARCH::"):
                            candidates = youtube_channel_search(api_key, team, max_results=1)
                            if not candidates:
                                logs.append(f"⚠️ {team}: 채널 자동 탐색 실패 - 후보 없음")
                                progress.progress(idx / max(len(tasks), 1))
                                log_box.text("\n".join(logs[-18:]))
                                continue
                            channel_id = candidates[0]["channel_id"]
                            channel_value = candidates[0]["channel_url"]
                            found_by_search = True
                            logs.append(f"🔎 {team}: 자동 발견 채널 - {candidates[0].get('title','')} / {channel_id}")
                        else:
                            channel_id = extract_youtube_channel_id(channel_value)

                        if not channel_id:
                            logs.append(f"⚠️ {team}: 채널 ID를 찾지 못했습니다 - {channel_value}")
                            progress.progress(idx / max(len(tasks), 1))
                            log_box.text("\n".join(logs[-18:]))
                            continue

                        if found_by_search and save_auto_found and team != "직접 추가 채널":
                            existing_queries = str(source_row.get("search_queries", "")) if hasattr(source_row, "get") else ""
                            existing_urls = str(source_row.get("channel_urls", "")) if hasattr(source_row, "get") else ""
                            new_channel_url = f"https://www.youtube.com/channel/{channel_id}"
                            merged_urls = existing_urls
                            if new_channel_url not in merged_urls:
                                merged_urls = (merged_urls + "|" + new_channel_url).strip("|") if merged_urls else new_channel_url
                            try:
                                add_or_update_source(team, existing_queries, merged_urls, str(source_row.get("notes", "자동 발견 채널 저장")) if hasattr(source_row, "get") else "자동 발견 채널 저장")
                            except Exception:
                                pass

                        source_query = f"CHANNEL_API:{channel_id}"
                        if reset_before_collect:
                            reset_collection_state(team, source_query)
                        state = get_collection_state(team, source_query) if resume_collection else {}
                        if state.get("completed") and resume_collection:
                            logs.append(f"⏭️ {team}: 채널 업로드 목록 이미 끝까지 수집 완료 / {channel_id}")
                            progress.progress(idx / max(len(tasks), 1))
                            log_box.text("\n".join(logs[-18:]))
                            continue

                        uploads_playlist_id = youtube_channel_uploads_playlist(api_key, channel_id)
                        if not uploads_playlist_id:
                            logs.append(f"⚠️ {team}: 업로드 플레이리스트를 찾지 못했습니다 / {channel_id}")
                            continue

                        page_token = state.get("next_page_token", "") if resume_collection else ""
                        query_inserted = 0
                        query_skipped = 0
                        query_videos = 0
                        query_pages = 0

                        while True:
                            page_json = youtube_playlist_items(api_key, uploads_playlist_id, page_token=page_token or None, max_results=50)
                            query_pages += 1
                            total_pages += 1
                            items = page_json.get("items", [])
                            page_inserted = 0
                            page_skipped = 0
                            for item in items:
                                song = build_video_row_from_playlist_item(item, team=team, source_query=source_query)
                                if not song:
                                    page_skipped += 1
                                    continue
                                raw_title = song.get("raw_title", "")
                                duration_sec = int(song.get("duration_seconds", 0) or 0)
                                if should_exclude(raw_title, exclude_keywords, int(min_sec), int(max_sec), duration_sec):
                                    page_skipped += 1
                                    continue
                                if upsert_song(song):
                                    page_inserted += 1
                                else:
                                    page_skipped += 1

                            query_videos += len(items)
                            total_videos += len(items)
                            query_inserted += page_inserted
                            query_skipped += page_skipped
                            total_inserted += page_inserted
                            total_skipped += page_skipped

                            next_page_token = page_json.get("nextPageToken", "") or ""
                            completed = 0 if next_page_token else 1
                            update_collection_state(
                                team,
                                source_query,
                                next_page_token=next_page_token,
                                completed=completed,
                                page_delta=1,
                                videos_delta=len(items),
                                inserted_delta=page_inserted,
                                skipped_delta=page_skipped,
                            )

                            logs.append(f"✅ {team}: 채널 {query_pages}페이지, 영상 {query_videos}개 확인, 신규 {query_inserted}개, 중복/제외 {query_skipped}개 / {channel_id}")
                            progress.progress(min((idx - 1 + 0.5) / max(len(tasks), 1), 1.0))
                            log_box.text("\n".join(logs[-18:]))

                            if not next_page_token:
                                break
                            page_token = next_page_token

                    except Exception as e:
                        safe_error = sanitize_error_message(e, api_key)
                        logs.append(f"⚠️ {team}: 채널 수집 중단 - {safe_error}")
                        logs.append("   다음에 다시 실행하면 저장된 지점부터 이어서 수집할 수 있습니다.")
                        if is_rate_limit_error(e):
                            logs.append("   429/쿼터 제한으로 판단되어 남은 채널 수집도 여기서 멈춥니다.")
                            stop_all_collection = True
                    progress.progress(idx / max(len(tasks), 1))
                    log_box.text("\n".join(logs[-18:]))
                    if stop_all_collection:
                        break

                db_total = int(query_df("SELECT COUNT(*) AS cnt FROM songs").iloc[0]["cnt"])
                st.success(f"채널 수집 완료: 채널 페이지 {total_pages}개, 영상 {total_videos}개 확인, 신규 저장 {total_inserted}개, 중복/제외 {total_skipped}개, 현재 DB 총 {db_total}개")

    if collection_method == "CSV 가져오기":
        st.markdown("### 📄 CSV 가져오기")
        st.caption("컬럼 예시: clean_title, team, video_url, raw_title, channel_title, published_at, description. 영상 파일은 저장하지 않습니다.")
        uploaded_csv = st.file_uploader("CSV 파일 업로드", type=["csv"])
        if uploaded_csv is not None:
            try:
                import_df = pd.read_csv(uploaded_csv)
                st.dataframe(import_df.head(50), use_container_width=True)
                if st.button("CSV 내용을 DB에 저장"):
                    inserted = 0
                    skipped = 0
                    for _, row in import_df.iterrows():
                        url = str(row.get("video_url", "") or row.get("url", ""))
                        video_id = extract_youtube_video_id(url)
                        if not video_id:
                            video_id = normalize_text(str(row.get("video_id", "")))
                        if not video_id:
                            skipped += 1
                            continue
                        raw_title = str(row.get("raw_title", "") or row.get("clean_title", "") or row.get("title", ""))
                        team = str(row.get("team", "미확인") or "미확인")
                        song = {
                            "video_id": video_id,
                            "source_video_id": video_id,
                            "raw_title": raw_title,
                            "clean_title": str(row.get("clean_title", "") or clean_video_title(raw_title, team)),
                            "team": team,
                            "channel_title": str(row.get("channel_title", "")),
                            "video_url": url or video_url_with_start(video_id, None),
                            "published_at": str(row.get("published_at", "")),
                            "duration_iso": "",
                            "duration_seconds": int(row.get("duration_seconds", 0) or 0),
                            "description": str(row.get("description", "")),
                            "speed": str(row.get("speed", "미확인") or "미확인"),
                            "theme": str(row.get("theme", "미확인") or "미확인"),
                            "source_query": "CSV 가져오기",
                        }
                        if upsert_song(song):
                            inserted += 1
                        else:
                            skipped += 1
                    st.success(f"CSV 저장 완료: 신규 {inserted}개, 중복/실패 {skipped}개")
            except Exception as e:
                st.error(f"CSV 처리 실패: {e}")

    st.divider()
    if collection_method == "직접 URL 저장":
        st.subheader("🔗 유튜브 URL 직접 저장")
    st.caption("유튜브 영상 파일은 저장하지 않고 링크와 메타데이터만 저장합니다. 메들리 여부/포함 곡 수는 2번 NVIDIA AI 분석에서 처리합니다.")
    url_c1, url_c2 = st.columns([2, 1])
    with url_c1:
        direct_youtube_url = st.text_input("유튜브 영상 URL", placeholder="https://youtu.be/...")
    with url_c2:
        direct_team = st.text_input("찬양팀/출처", value="피아워십")
    if st.button("이 유튜브 URL을 DB에 저장"):
        if not api_key:
            st.error("YouTube Data API Key를 먼저 설정해주세요.")
            st.stop()
        video_id = extract_youtube_video_id(direct_youtube_url)
        if not video_id:
            st.error("유튜브 영상 ID를 찾지 못했습니다. URL을 다시 확인해주세요.")
            st.stop()
        try:
            details_json = youtube_videos(api_key, [video_id])
            if not details_json.get("items"):
                st.error("YouTube API에서 영상을 찾지 못했습니다.")
                st.stop()
            item = details_json["items"][0]
            song = build_video_row_from_item(item=item, team=direct_team or "미확인", source_query="직접 URL")
            inserted = 1 if upsert_song(song) else 0
            skipped = 0 if inserted else 1
            st.success(f"저장 완료: 신규 {inserted}개, 중복 {skipped}개")
            st.dataframe(pd.DataFrame([song])[["clean_title", "team", "video_url", "duration_seconds"]], use_container_width=True)
        except Exception as e:
            st.error(f"URL 저장 실패: {sanitize_error_message(e, api_key)}")

    st.divider()

    if collection_method == "YouTube API 검색 수집(넓게 수집, 쿼터 많이 사용)":
        st.markdown("### 🔎 YouTube API 검색 수집")
        st.caption("검색어로 넓게 수집합니다. 가장 많은 후보를 찾을 수 있지만 search.list 쿼터를 많이 사용합니다.")
        if st.button("선택한 찬양팀 전체 자동 수집 시작", type="primary"):
            if not api_key:
                st.error("YouTube Data API Key를 입력해주세요.")
                st.stop()

            selected = sources[sources["team"].isin(selected_teams)]
            total_inserted = 0
            total_skipped = 0
            total_videos = 0
            total_pages = 0
            progress = st.progress(0)
            log_box = st.empty()
            logs = []
            tasks = []
            for _, row in selected.iterrows():
                for q in str(row["search_queries"]).split("|"):
                    q = q.strip()
                    if q:
                        tasks.append((row["team"], q))

            stop_all_collection = False
            for idx, (team, query) in enumerate(tasks, start=1):
                if reset_before_collect:
                    reset_collection_state(team, query)

                state = get_collection_state(team, query) if resume_collection else {}
                if state.get("completed") and resume_collection:
                    logs.append(f"⏭️ {team} / {query}: 이미 끝까지 수집 완료")
                    progress.progress(idx / max(len(tasks), 1))
                    log_box.text("\n".join(logs[-18:]))
                    continue

                page_token = state.get("next_page_token", "") if resume_collection else ""
                query_inserted = 0
                query_skipped = 0
                query_videos = 0
                query_pages = 0

                while True:
                    try:
                        search_json = youtube_search(api_key, query, max_results=50, page_token=page_token or None)
                        query_pages += 1
                        total_pages += 1
                        search_items = search_json.get("items", [])
                        video_ids = [item.get("id", {}).get("videoId") for item in search_items if item.get("id", {}).get("videoId")]
                        query_videos += len(video_ids)
                        total_videos += len(video_ids)

                        details_by_id = {}
                        details_warning = ""
                        try:
                            details_json = youtube_videos(api_key, video_ids)
                            details_by_id = {item.get("id"): item for item in details_json.get("items", []) if item.get("id")}
                        except Exception as detail_error:
                            # If videos.list hits quota/rate limits after search.list succeeded, keep the search results.
                            # This prevents the confusing case: "영상 N개 확인, 신규 저장 0개".
                            details_warning = sanitize_error_message(detail_error, api_key)

                        page_inserted = 0
                        page_skipped = 0
                        for search_item in search_items:
                            video_id = search_item.get("id", {}).get("videoId") if isinstance(search_item.get("id", {}), dict) else ""
                            if not video_id:
                                page_skipped += 1
                                continue

                            detail_item = details_by_id.get(video_id)
                            if detail_item:
                                snippet = detail_item.get("snippet", {})
                                content = detail_item.get("contentDetails", {})
                                duration_sec = iso8601_duration_to_seconds(content.get("duration", ""))
                                raw_title = snippet.get("title", "")
                                song = build_video_row_from_item(item=detail_item, team=team, source_query=query)
                            else:
                                snippet = search_item.get("snippet", {})
                                duration_sec = 0
                                raw_title = snippet.get("title", "")
                                song = build_video_row_from_search_item(search_item=search_item, team=team, source_query=query)

                            if not song:
                                page_skipped += 1
                                continue
                            if should_exclude(raw_title, exclude_keywords, int(min_sec), int(max_sec), duration_sec):
                                page_skipped += 1
                                continue

                            if upsert_song(song):
                                page_inserted += 1
                            else:
                                page_skipped += 1

                        if details_warning:
                            logs.append(f"⚠️ {team} / {query}: 영상 상세조회 실패, 검색 결과 기본정보만 저장 - {details_warning}")

                        total_inserted += page_inserted
                        total_skipped += page_skipped
                        query_inserted += page_inserted
                        query_skipped += page_skipped

                        next_page_token = search_json.get("nextPageToken", "") or ""
                        completed = 0 if next_page_token else 1
                        update_collection_state(
                            team,
                            query,
                            next_page_token=next_page_token,
                            completed=completed,
                            page_delta=1,
                            videos_delta=len(video_ids),
                            inserted_delta=page_inserted,
                            skipped_delta=page_skipped,
                        )

                        logs.append(
                            f"✅ {team} / {query}: {query_pages}페이지, 영상 {query_videos}개 확인, 신규 {query_inserted}개, 중복/제외 {query_skipped}개"
                        )
                        progress.progress(min((idx - 1 + 0.5) / max(len(tasks), 1), 1.0))
                        log_box.text("\n".join(logs[-18:]))

                        if not next_page_token:
                            break
                        page_token = next_page_token

                    except Exception as e:
                        safe_error = sanitize_error_message(e, api_key)
                        update_collection_state(
                            team,
                            query,
                            next_page_token=page_token or "",
                            completed=0,
                            last_error=safe_error,
                        )
                        logs.append(f"⚠️ {team} / {query}: 중단 - {safe_error}")
                        logs.append("   다음에 다시 실행하면 저장된 지점부터 이어서 수집할 수 있습니다.")
                        if is_rate_limit_error(e):
                            logs.append("   429/쿼터 제한으로 판단되어 남은 검색어 수집도 여기서 멈춥니다.")
                            stop_all_collection = True
                        break

                progress.progress(idx / max(len(tasks), 1))
                log_box.text("\n".join(logs[-18:]))
                if 'stop_all_collection' in locals() and stop_all_collection:
                    break

            db_total = int(query_df("SELECT COUNT(*) AS cnt FROM songs").iloc[0]["cnt"])
            st.success(f"전체 수집 실행 완료: 검색 페이지 {total_pages}개, 영상 {total_videos}개 확인, 신규 저장 {total_inserted}개, 중복/제외 {total_skipped}개, 현재 DB 총 {db_total}개")

    st.divider()
    st.subheader("최근 수집된 곡")
    recent = query_df(
        """
        SELECT id, clean_title, team, channel_title, speed, theme, duration_seconds, checked, video_url, created_at
        FROM songs ORDER BY id DESC LIMIT 50
        """
    )
    st.dataframe(recent, use_container_width=True)


elif menu == "2. NVIDIA AI 분석":
    st.subheader("2. NVIDIA AI 자동 분석")
    st.write("YouTube 전체 수집이 끝난 뒤, DB에 저장된 미분석 영상 링크를 NVIDIA API로 분석합니다. 메들리 영상은 한 행으로 유지하고 포함 곡 수만 저장합니다.")

    if not nvidia_api_key:
        st.warning("NVIDIA API Key가 없습니다. secrets.toml 또는 사이드바 직접 입력에 API 키를 넣어주세요.")

    col1, col2, col3 = st.columns(3)
    with col1:
        target_mode = st.selectbox("분석 대상", ["미분석만", "전체 재분석"])
    with col2:
        batch_size = st.number_input("이번 실행 분석 개수", min_value=1, max_value=5000, value=200, step=50)
    with col3:
        only_team = st.text_input("특정 찬양팀만 분석", placeholder="비우면 전체")

    where = []
    params = []
    if target_mode == "미분석만":
        where.append("(ai_analyzed = 0 OR ai_analyzed IS NULL)")
    if only_team.strip():
        where.append("team LIKE ?")
        params.append(f"%{only_team.strip()}%")
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    pending = query_df(
        f"""
        SELECT id, clean_title, raw_title, team, channel_title, video_url, duration_seconds,
               is_medley, medley_song_count, medley_songs, ai_analyzed, created_at
        FROM songs {where_sql}
        ORDER BY ai_analyzed ASC, id ASC
        LIMIT ?
        """,
        tuple(params + [int(batch_size)]),
    )

    total_pending = query_df(
        f"SELECT COUNT(*) AS cnt FROM songs {where_sql}",
        tuple(params),
    )
    st.metric("조건에 맞는 분석 대상", int(total_pending.iloc[0]["cnt"]) if not total_pending.empty else 0)
    st.dataframe(pending, use_container_width=True)

    st.info("429 오류가 나면 현재까지 분석한 내용은 DB에 저장된 상태로 멈춥니다. 나중에 다시 실행하면 미분석 항목부터 이어서 분석할 수 있습니다.")

    if st.button("NVIDIA AI 분석 시작", type="primary"):
        if not nvidia_api_key:
            st.error("NVIDIA API Key를 먼저 설정해주세요.")
            st.stop()
        if pending.empty:
            st.info("분석할 항목이 없습니다.")
            st.stop()

        progress = st.progress(0)
        log_box = st.empty()
        logs = []
        analyzed = 0
        stopped = False
        rows = pending.to_dict("records")
        for i, row in enumerate(rows, start=1):
            try:
                result = analyze_video_as_medley_item_with_nvidia(
                    raw_title=row.get("raw_title") or row.get("clean_title") or "",
                    team=row.get("team") or "미확인",
                    channel_title=row.get("channel_title") or "",
                    description=query_df("SELECT description FROM songs WHERE id = ?", (int(row["id"]),)).iloc[0]["description"] or "",
                    duration_seconds=int(row.get("duration_seconds") or 0),
                    api_key=nvidia_api_key,
                    base_url=nvidia_base_url,
                    model=nvidia_model,
                )
                update_song(int(row["id"]), result)
                analyzed += 1
                medley_label = f"메들리 {result.get('medley_song_count')}곡" if result.get("is_medley") else "단일곡"
                logs.append(f"✅ {row['id']} / {result.get('clean_title')} / {medley_label}")
            except Exception as e:
                logs.append(f"⚠️ {row.get('id')} 분석 중단: {sanitize_error_message(e, nvidia_api_key)}")
                logs.append("   지금까지 완료된 항목은 저장됐습니다. 나중에 다시 실행하면 이어서 분석할 수 있습니다.")
                stopped = True
                break
            progress.progress(i / max(len(rows), 1))
            log_box.text("\n".join(logs[-18:]))

        if stopped:
            st.warning(f"분석 중단: 완료 {analyzed}개. 429/쿼터 제한이면 잠시 뒤 또는 다음 날 다시 실행하세요.")
        else:
            st.success(f"AI 분석 완료: {analyzed}개")

    st.divider()
    st.subheader("최근 AI 분석 결과")
    analyzed_df = query_df(
        """
        SELECT id, clean_title, team, speed, theme, is_medley, medley_song_count, medley_songs, ai_analyzed, video_url, updated_at
        FROM songs
        ORDER BY updated_at DESC, id DESC
        LIMIT 80
        """
    )
    if not analyzed_df.empty:
        analyzed_df["포함 곡"] = analyzed_df["medley_songs"].apply(medley_songs_display)
        st.dataframe(analyzed_df.drop(columns=["medley_songs"]), use_container_width=True)

elif menu == "3. 곡 검수":
    st.subheader("3. 곡 검수")
    st.write("자동 수집된 곡은 미확인 상태입니다. 예배에서 실제 사용할 키, BPM, 첫 코드, 마지막 코드, 악보 링크를 입력하고 검수 완료로 저장하세요.")

    filter_col1, filter_col2, filter_col3 = st.columns(3)
    with filter_col1:
        status = st.selectbox("검수 상태", ["미검수", "검수 완료", "전체"])
    with filter_col2:
        team_filter = st.text_input("찬양팀 검색")
    with filter_col3:
        title_filter = st.text_input("곡명 검색")

    where = []
    params = []
    if status == "미검수":
        where.append("checked = 0")
    elif status == "검수 완료":
        where.append("checked = 1")
    if team_filter:
        where.append("team LIKE ?")
        params.append(f"%{team_filter}%")
    if title_filter:
        where.append("clean_title LIKE ?")
        params.append(f"%{title_filter}%")

    where_sql = "WHERE " + " AND ".join(where) if where else ""
    songs = query_df(
        f"""
        SELECT id, clean_title, raw_title, team, channel_title, speed, bpm, song_key, first_chord,
               last_chord, theme, energy, sheet_url, checked, usable, video_url, duration_seconds,
               is_medley, medley_song_count, medley_songs, memo
        FROM songs {where_sql}
        ORDER BY checked ASC, id DESC
        LIMIT 200
        """,
        tuple(params),
    )

    if songs.empty:
        st.info("표시할 곡이 없습니다. 먼저 DB 자동 수집을 실행하세요.")
    else:
        preview = songs[["id", "clean_title", "team", "speed", "bpm", "song_key", "theme", "is_medley", "medley_song_count", "checked", "usable", "video_url"]].copy()
        preview["포함 곡"] = songs["medley_songs"].apply(medley_songs_display)
        st.dataframe(preview, use_container_width=True)
        selected_id = st.selectbox("수정할 곡 ID 선택", songs["id"].tolist())
        row = songs[songs["id"] == selected_id].iloc[0].to_dict()

        st.markdown(f"### {row['clean_title']}")
        st.write(f"원본 제목: {row['raw_title']}")
        st.write(f"유튜브: {row['video_url']}")

        col1, col2, col3 = st.columns(3)
        with col1:
            clean_title = st.text_input("곡명", value=row.get("clean_title") or "")
            team = st.text_input("찬양팀", value=row.get("team") or "")
            speed = st.selectbox("빠르기", SPEED_OPTIONS, index=SPEED_OPTIONS.index(row.get("speed")) if row.get("speed") in SPEED_OPTIONS else 0)
            theme = st.selectbox("주제", THEME_OPTIONS, index=THEME_OPTIONS.index(row.get("theme")) if row.get("theme") in THEME_OPTIONS else 0)
        with col2:
            bpm_default = int(row["bpm"]) if not pd.isna(row.get("bpm")) and row.get("bpm") else 0
            bpm = st.number_input("BPM", min_value=0, max_value=240, value=bpm_default, step=1)
            song_key = st.selectbox("원키/사용키", KEY_OPTIONS, index=KEY_OPTIONS.index(row.get("song_key")) if row.get("song_key") in KEY_OPTIONS else 0)
            available_keys = st.text_input("가능한 조옮김 키", value=row.get("available_keys") or "", placeholder="예: D,E,F,G")
            energy = st.slider("에너지", 1, 5, int(row.get("energy") or 3))
        with col3:
            first_chord = st.text_input("첫 코드", value=row.get("first_chord") or "", placeholder="예: D")
            last_chord = st.text_input("마지막 코드", value=row.get("last_chord") or "", placeholder="예: G")
            sheet_url = st.text_input("악보 링크", value=row.get("sheet_url") or "")
            usable = st.checkbox("콘티 추천에 사용", value=bool(row.get("usable")))
            checked = st.checkbox("검수 완료", value=bool(row.get("checked")))

        st.markdown("#### 메들리/예배실황 정보")
        st.caption("메들리로 체크하면 콘티 추천에서 1곡이 아니라 포함 곡 수만큼 카운트합니다. 예: 5곡짜리 빠른 메들리인데 구간에서 3곡만 필요하면 3곡만 사용하는 추천도 가능합니다.")
        m1, m2 = st.columns([1, 2])
        with m1:
            is_medley = st.checkbox("메들리/여러 곡 포함 영상", value=bool(row.get("is_medley")))
            medley_song_count = st.number_input("포함 곡 수", min_value=1, max_value=30, value=max(1, int(row.get("medley_song_count") or 1)), step=1)
        with m2:
            medley_songs_text = st.text_area(
                "포함 곡 목록",
                value=medley_songs_display(row.get("medley_songs") or ""),
                height=80,
                placeholder="예: 생명 주께 있네, 다와서 찬양해, 주 우리 아버지"
            )

        memo = st.text_area("메모", value=row.get("memo") or "", height=100)

        if st.button("검수 내용 저장", type="primary"):
            update_song(
                int(selected_id),
                {
                    "clean_title": clean_title,
                    "team": team,
                    "speed": speed,
                    "theme": theme,
                    "bpm": int(bpm) if bpm else None,
                    "song_key": song_key,
                    "available_keys": available_keys,
                    "energy": int(energy),
                    "first_chord": first_chord,
                    "last_chord": last_chord,
                    "sheet_url": sheet_url,
                    "is_medley": 1 if is_medley else 0,
                    "medley_song_count": int(medley_song_count) if is_medley else 1,
                    "medley_songs": json.dumps(normalize_medley_songs(medley_songs_text), ensure_ascii=False) if is_medley else "",
                    "usable": 1 if usable else 0,
                    "checked": 1 if checked else 0,
                    "memo": memo,
                },
            )
            st.success("저장했습니다. 화면을 새로고침하면 반영됩니다.")

elif menu == "4. 콘티 추천":
    st.subheader("4. 검수 완료 곡으로 콘티 추천")
    checked_df = query_df(
        """
        SELECT * FROM songs
        WHERE checked = 1 AND usable = 1
        ORDER BY team, clean_title
        """
    )
    if checked_df.empty:
        st.warning("검수 완료된 곡이 없습니다. 먼저 곡 검수를 완료해주세요.")
        st.stop()

    st.info(f"현재 콘티 추천에 사용할 수 있는 검수 완료 곡: {len(checked_df)}개")

    team_options = sorted([t for t in checked_df["team"].dropna().astype(str).unique().tolist() if t.strip()])

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        allowed_keys = st.multiselect("사용 가능한 키", [k for k in KEY_OPTIONS if k != "미확인"], default=[])
    with col2:
        preferred_teams = st.multiselect("참고할 찬양팀", team_options, default=team_options[: min(8, len(team_options))])
    with col3:
        max_per_section = st.number_input("구간별 후보 최대 개수", min_value=10, max_value=80, value=35, step=5)
    with col4:
        recommendation_count = st.number_input("추천 세트 개수", min_value=1, max_value=20, value=7, step=1)

    use_ai_conti_note = st.checkbox("NVIDIA AI로 콘티 흐름 설명 생성", value=False, help="추천 결과가 나온 뒤 구간별 분위기와 연결 이유를 자연어로 정리합니다.")

    st.markdown("### 콘티 구간 설정")
    st.caption("예: 앞부분 느린곡 3곡은 은혜/임재/회복, 중간 빠른곡 3곡은 기쁨/감사/선포, 마지막 느린곡 1곡은 결단/기도")
    num_sections = st.number_input("구간 수", min_value=1, max_value=5, value=3, step=1)
    sections = []
    default_values = [
        ("느림", 3, True, DEFAULT_SECTION_THEME_PRESETS["느린 시작"], "예배 초반: 은혜, 임재, 회복, 경배처럼 마음을 여는 분위기"),
        ("빠름", 3, True, DEFAULT_SECTION_THEME_PRESETS["빠른 찬양"], "예배 중반: 기쁨, 감사, 선포, 승리처럼 에너지를 올리는 분위기"),
        ("느림", 1, False, DEFAULT_SECTION_THEME_PRESETS["마무리"], "예배 후반: 결단, 기도, 임재, 회복처럼 고백으로 마무리하는 분위기"),
    ]
    for i in range(int(num_sections)):
        default_speed, default_count, default_connected, default_themes, default_mood = default_values[i] if i < len(default_values) else ("느림", 1, False, [], "")
        st.markdown(f"#### 구간 {i+1}")
        c1, c2, c3 = st.columns(3)
        with c1:
            speed = st.selectbox(
                f"구간 {i+1} 빠르기",
                ["느림", "중간", "빠름", "상관없음"],
                index=["느림", "중간", "빠름", "상관없음"].index(default_speed),
                key=f"sec_speed_{i}",
            )
        with c2:
            count = st.number_input(f"구간 {i+1} 곡 수", min_value=1, max_value=15, value=default_count, step=1, key=f"sec_count_{i}")
        with c3:
            connected = st.checkbox(f"구간 {i+1} 이어부르기", value=default_connected, key=f"sec_connected_{i}")
        theme_choices = [t for t in THEME_OPTIONS if t != "미확인"]
        selected_themes = st.multiselect(
            f"구간 {i+1} 원하는 분위기/주제",
            theme_choices,
            default=[t for t in default_themes if t in theme_choices],
            key=f"sec_themes_{i}",
        )
        mood_text = st.text_input(f"구간 {i+1} 분위기 설명", value=default_mood, key=f"sec_mood_{i}")
        sections.append({"speed": speed, "count": int(count), "connected": connected, "themes": selected_themes, "mood": mood_text})

    if st.button("콘티 추천 생성", type="primary"):
        rows = checked_df.to_dict("records")
        if preferred_teams:
            rows = [r for r in rows if r.get("team") in preferred_teams]

        section_results = []
        for sec in sections:
            candidates = []
            for r in rows:
                if sec["speed"] != "상관없음" and r.get("speed") != sec["speed"]:
                    continue
                if allowed_keys:
                    r_key = r.get("song_key")
                    r_available = parse_available_keys(r.get("available_keys", ""))
                    if r_key not in allowed_keys and not any(k in allowed_keys for k in r_available):
                        continue
                if sec.get("themes") and r.get("theme") not in sec["themes"]:
                    continue
                candidates.append(r)

            def quality(row):
                q = 0
                for f in ["bpm", "song_key", "first_chord", "last_chord", "theme"]:
                    if row.get(f) and row.get(f) != "미확인":
                        q += 1
                return q

            candidates = sorted(candidates, key=quality, reverse=True)
            sequences = generate_section_sequences(candidates, sec["count"], sec["connected"], max_candidates=int(max_per_section))
            section_results.append(sequences)
            theme_label = ", ".join(sec.get("themes") or ["전체"])
            st.write(f"구간 {len(section_results)} 후보곡 {len(candidates)}개 / 조합 {len(sequences)}개 / 분위기: {theme_label}")

        contis = build_contis(section_results, top_n=int(recommendation_count))
        if not contis:
            st.error("조건에 맞는 콘티를 만들 수 없습니다. 검수 완료 곡 수를 늘리거나 키/주제/찬양팀 조건을 줄여보세요.")
        else:
            for idx, conti in enumerate(contis, start=1):
                st.markdown(f"## 추천 콘티 {idx}안 — 점수 {conti['score']}")
                out_rows = []
                current_order = 1
                total_song_count = 0
                for s in conti["songs"]:
                    selected_count = selected_song_count_value(s)
                    full_count = song_count_value(s)
                    order_label = str(current_order) if selected_count == 1 else f"{current_order}~{current_order + selected_count - 1}"
                    selected_medley = s.get("_selected_medley_songs") or []
                    note = ""
                    if full_count > selected_count:
                        note = f"전체 메들리 {full_count}곡 중 {selected_count}곡만 사용"
                    elif full_count > 1:
                        note = f"메들리 {selected_count}곡 전체 사용"
                    out_rows.append(
                        {
                            "순서": order_label,
                            "곡 수": selected_count,
                            "곡명/사용 구간": conti_title_for_item(s),
                            "원본 DB 제목": s.get("clean_title"),
                            "찬양팀": s.get("team"),
                            "빠르기": s.get("speed"),
                            "키": s.get("song_key"),
                            "BPM": s.get("bpm"),
                            "첫 코드": s.get("first_chord"),
                            "마지막 코드": s.get("last_chord"),
                            "주제": s.get("theme"),
                            "메들리 포함 곡": ", ".join(selected_medley),
                            "비고": note,
                            "유튜브 링크": s.get("video_url"),
                        }
                    )
                    current_order += selected_count
                    total_song_count += selected_count
                result_df = pd.DataFrame(out_rows)
                st.caption(f"이 콘티의 실제 곡 수 합계: {total_song_count}곡")
                st.dataframe(result_df, use_container_width=True)
                st.markdown("#### 순서 + 유튜브 링크")
                for row in out_rows:
                    st.markdown(f"{row['순서']}. **{row['곡명/사용 구간']}** - {row['찬양팀']} / {row['빠르기']} / {row['키']} / BPM {row['BPM']} / [{row['유튜브 링크']}]({row['유튜브 링크']})")
                    if row.get("비고"):
                        st.caption(row["비고"])

                with st.expander("연결 이유 보기"):
                    st.text(conti["reasons"])

                if use_ai_conti_note and nvidia_api_key:
                    try:
                        prompt = {
                            "sections": sections,
                            "songs": out_rows,
                            "connection_reasons": conti["reasons"],
                        }
                        ai_text = nvidia_chat_completion(
                            api_key=nvidia_api_key,
                            base_url=nvidia_base_url,
                            model=nvidia_model,
                            messages=[
                                {"role": "system", "content": "너는 교회 찬양단 콘티를 돕는 예배 음악 코치야. 과장하지 말고 실제 인도자가 이해하기 쉽게 설명해."},
                                {"role": "user", "content": "다음 콘티를 구간별 분위기, 곡 연결 방식, 인도 팁 중심으로 한국어로 짧게 정리해줘.\n" + json.dumps(prompt, ensure_ascii=False)},
                            ],
                            max_tokens=1000,
                            temperature=0.4,
                        )
                        with st.expander("NVIDIA AI 콘티 설명", expanded=True):
                            st.write(ai_text)
                    except Exception as e:
                        st.warning(f"NVIDIA AI 설명 생성 실패: {sanitize_error_message(e, nvidia_api_key)}")

elif menu == "5. DB 관리":
    st.subheader("5. DB 관리")
    df = query_df(
        """
        SELECT id, clean_title, team, channel_title, speed, bpm, song_key, first_chord, last_chord,
               theme, energy, is_medley, medley_song_count, medley_songs, sheet_url, checked, usable, video_url, created_at, updated_at
        FROM songs ORDER BY id DESC
        """
    )
    st.metric("전체 곡 수", len(df))
    if not df.empty:
        st.metric("검수 완료 곡 수", int(df["checked"].sum()))
        display_df = df.copy()
        if "medley_songs" in display_df.columns:
            display_df["포함 곡"] = display_df["medley_songs"].apply(medley_songs_display)
        st.dataframe(display_df, use_container_width=True)
        csv = df.to_csv(index=False).encode("utf-8-sig")
        st.download_button("CSV로 내보내기", data=csv, file_name="worship_songs_export.csv", mime="text/csv")

    st.divider()
    st.subheader("수집 진행상태")
    state_df = query_df(
        """
        SELECT team, source_query, completed, pages_fetched, videos_seen, songs_inserted, songs_skipped,
               next_page_token, last_error, updated_at
        FROM collection_state
        ORDER BY completed ASC, updated_at DESC
        """
    )
    if state_df.empty:
        st.info("아직 저장된 수집 진행상태가 없습니다.")
    else:
        st.dataframe(state_df, use_container_width=True)
        if st.button("모든 수집 진행상태 초기화"):
            conn = get_conn()
            conn.execute("DELETE FROM collection_state")
            conn.commit()
            conn.close()
            st.success("수집 진행상태를 초기화했습니다. DB에 저장된 곡은 삭제하지 않았습니다.")

    st.divider()
    st.subheader("기본 찬양팀 목록 수정")
    sources = load_sources()
    st.write("worship_sources.csv 파일을 수정하면 자동 수집 대상 찬양팀과 검색어를 늘릴 수 있습니다. 우선순위는 사용하지 않습니다.")
    st.dataframe(sources, use_container_width=True)
