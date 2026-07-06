import itertools
import json
import math
import os
import re
import sqlite3
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
            thumbnail_url TEXT,
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
    }
    for col, ddl in migrations.items():
        if col not in existing_cols:
            cur.execute(ddl)
    cur.execute("UPDATE songs SET source_video_id = video_id WHERE source_video_id IS NULL OR source_video_id = ''")
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
                raw_title, clean_title, team, channel_title, video_url, thumbnail_url,
                published_at, duration_iso, duration_seconds, description, speed, theme,
                source_query, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                song.get("thumbnail_url"),
                song.get("published_at"),
                song.get("duration_iso"),
                song.get("duration_seconds"),
                song.get("description", ""),
                song.get("speed", "미확인"),
                song.get("theme", "미확인"),
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
        return pd.DataFrame(columns=["team", "search_queries", "priority", "notes"])
    df = pd.read_csv(SOURCES_PATH)
    df = df.dropna(how="all")
    return df


def save_sources(df: pd.DataFrame) -> None:
    df = df[["team", "search_queries", "priority", "notes"]].copy()
    df = df.dropna(how="all")
    df.to_csv(SOURCES_PATH, index=False, encoding="utf-8-sig")


def add_or_update_source(team: str, search_queries: str, priority: int = 2, notes: str = "사용자 추가") -> None:
    team = normalize_text(team)
    search_queries = normalize_text(search_queries)
    if not team or not search_queries:
        raise ValueError("찬양팀 이름과 검색어를 모두 입력해야 합니다.")
    df = load_sources()
    new_row = {"team": team, "search_queries": search_queries, "priority": int(priority), "notes": notes}
    if not df.empty and team in df["team"].astype(str).tolist():
        df.loc[df["team"].astype(str) == team, ["search_queries", "priority", "notes"]] = [search_queries, int(priority), notes]
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
    thumbnail_url = snippet.get("thumbnails", {}).get("high", {}).get("url") or snippet.get("thumbnails", {}).get("default", {}).get("url", "")

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
                "thumbnail_url": thumbnail_url,
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
    candidates = candidates[:max_candidates]
    if count <= 0 or len(candidates) < count:
        return []

    results = []
    # For count 1, simple ranking by completeness.
    if count == 1:
        for c in candidates:
            completeness = 0
            for field in ["song_key", "bpm", "theme", "sheet_url"]:
                if c.get(field) and c.get(field) != "미확인":
                    completeness += 5
            results.append((completeness + 20, [c], "단독 마무리 곡"))
        return sorted(results, key=lambda x: x[0], reverse=True)[:20]

    # Keep it fast: use permutations only for smaller candidate sets.
    for seq in itertools.permutations(candidates, count):
        sc, reason = sequence_score(list(seq), connected)
        # Prefer variety of teams but do not force it.
        unique_teams = len(set(s.get("team", "") for s in seq))
        sc += min(unique_teams * 3, 9)
        results.append((sc, list(seq), reason))
        if len(results) > 8000:
            break

    return sorted(results, key=lambda x: x[0], reverse=True)[:20]


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
st.caption("YouTube 기반 자동 수집 → 사람이 키/BPM/코드/악보 링크 검수 → 검수 완료 곡으로 콘티 추천")

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

    menu = st.radio("메뉴", ["1. DB 자동 수집", "2. 곡 검수", "3. 콘티 추천", "4. DB 관리"])

if menu == "1. DB 자동 수집":
    st.subheader("1. 유튜브 기반 찬양곡 후보 자동 수집")
    st.write("기본 찬양팀 목록에서 선택하면 팀별 검색어를 돌려서 영상 후보를 DB에 저장합니다.")

    sources = load_sources()
    if sources.empty:
        st.error("worship_sources.csv 파일이 없습니다.")
        st.stop()

    with st.expander("➕ 자동 수집 찬양팀 직접 추가/수정", expanded=False):
        st.write("예: 아가파오 워십 / 아가파오 워십 찬양|AGAPAO Worship Korea|아가파오 워십 라이브")
        add_c1, add_c2 = st.columns([1, 2])
        with add_c1:
            new_team = st.text_input("추가할 찬양팀 이름")
            new_priority = st.number_input("우선순위", min_value=1, max_value=9, value=2, step=1)
        with add_c2:
            new_queries = st.text_input("검색어 묶음", placeholder="검색어를 | 로 구분해서 입력")
            new_notes = st.text_input("메모", value="사용자 추가")
        if st.button("찬양팀 목록에 저장"):
            try:
                add_or_update_source(new_team, new_queries, int(new_priority), new_notes)
                st.success("찬양팀 목록에 저장했습니다. 화면을 새로고침하면 선택 목록에 보입니다.")
                sources = load_sources()
            except Exception as e:
                st.error(str(e))

    col_a, col_b = st.columns([2, 1])
    with col_a:
        default_selected = sources[sources["priority"] <= 2]["team"].tolist()
        selected_teams = st.multiselect(
            "수집할 찬양팀 선택",
            options=sources["team"].tolist(),
            default=default_selected,
        )
    with col_b:
        per_query_results = st.number_input("검색어당 가져올 영상 수", min_value=1, max_value=50, value=10, step=1)
        min_sec = st.number_input("최소 영상 길이(초)", min_value=0, max_value=600, value=120, step=30)
        max_sec = st.number_input("최대 영상 길이(초)", min_value=180, max_value=14400, value=3600, step=60)
        use_nvidia_enrichment = st.checkbox(
            "NVIDIA AI로 DB 자동 분류",
            value=True,
            help="곡명/느림·빠름/주제를 추정하고, 한 영상에 여러 곡이 있으면 곡별로 나눠 저장합니다.",
        )
        split_multi_song_videos = st.checkbox(
            "예배실황/메들리 영상은 여러 곡으로 분리 저장",
            value=True,
            help="NVIDIA API가 필요합니다. 설명란 타임스탬프가 있으면 같은 영상을 여러 곡으로 저장합니다.",
        )
        max_songs_per_video = st.number_input("영상 1개당 최대 분리 곡 수", min_value=1, max_value=30, value=12, step=1)

    exclude_text = st.text_area("제외 키워드", value=", ".join(DEFAULT_EXCLUDE_KEYWORDS), height=90)
    exclude_keywords = [x.strip() for x in exclude_text.split(",") if x.strip()]

    st.dataframe(sources[sources["team"].isin(selected_teams)], use_container_width=True)

    st.divider()
    st.subheader("🔗 유튜브 URL 직접 분석/저장")
    st.caption("예배실황처럼 영상 하나에 여러 곡이 들어있는 링크를 붙여 넣으면, AI가 곡별로 나눠서 같은 영상 URL+시작시간 형태로 저장합니다.")
    url_c1, url_c2 = st.columns([2, 1])
    with url_c1:
        direct_youtube_url = st.text_input("유튜브 영상 URL", placeholder="https://youtu.be/...")
    with url_c2:
        direct_team = st.text_input("찬양팀/출처", value="피아워십")
    if st.button("이 유튜브 URL 분석해서 DB에 저장"):
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
            song_rows, row_logs = build_song_rows_from_video_item(
                item=item,
                team=direct_team or "미확인",
                source_query="직접 URL",
                use_nvidia_enrichment=True,
                split_multi_song_videos=True,
                nvidia_api_key=nvidia_api_key,
                nvidia_base_url=nvidia_base_url,
                nvidia_model=nvidia_model,
                max_songs_per_video=int(max_songs_per_video),
            )
            inserted = 0
            skipped = 0
            for song in song_rows:
                if upsert_song(song):
                    inserted += 1
                else:
                    skipped += 1
            if row_logs:
                st.warning("\n".join(row_logs))
            st.success(f"저장 완료: 신규 {inserted}곡, 중복 {skipped}곡")
            st.dataframe(pd.DataFrame(song_rows)[["song_index", "clean_title", "team", "speed", "theme", "video_url"]], use_container_width=True)
        except Exception as e:
            st.error(f"URL 분석/저장 실패: {e}")

    st.divider()

    if st.button("선택한 찬양팀 자동 수집 시작", type="primary"):
        if not api_key:
            st.error("YouTube Data API Key를 입력해주세요.")
            st.stop()

        selected = sources[sources["team"].isin(selected_teams)]
        total_inserted = 0
        total_skipped = 0
        progress = st.progress(0)
        log_box = st.empty()
        logs = []
        tasks = []
        for _, row in selected.iterrows():
            for q in str(row["search_queries"]).split("|"):
                tasks.append((row["team"], q.strip()))

        for idx, (team, query) in enumerate(tasks, start=1):
            try:
                search_json = youtube_search(api_key, query, max_results=int(per_query_results))
                video_ids = [item["id"].get("videoId") for item in search_json.get("items", []) if item.get("id", {}).get("videoId")]
                details_json = youtube_videos(api_key, video_ids)

                for item in details_json.get("items", []):
                    snippet = item.get("snippet", {})
                    content = item.get("contentDetails", {})
                    duration_sec = iso8601_duration_to_seconds(content.get("duration", ""))
                    raw_title = snippet.get("title", "")
                    if should_exclude(raw_title, exclude_keywords, int(min_sec), int(max_sec), duration_sec):
                        total_skipped += 1
                        continue

                    song_rows, row_logs = build_song_rows_from_video_item(
                        item=item,
                        team=team,
                        source_query=query,
                        use_nvidia_enrichment=bool(use_nvidia_enrichment),
                        split_multi_song_videos=bool(split_multi_song_videos),
                        nvidia_api_key=nvidia_api_key,
                        nvidia_base_url=nvidia_base_url,
                        nvidia_model=nvidia_model,
                        max_songs_per_video=int(max_songs_per_video),
                    )
                    logs.extend(row_logs)
                    for song in song_rows:
                        if upsert_song(song):
                            total_inserted += 1
                        else:
                            total_skipped += 1
                logs.append(f"✅ {team} / {query}: 완료")
            except Exception as e:
                logs.append(f"⚠️ {team} / {query}: 오류 - {e}")
            progress.progress(idx / max(len(tasks), 1))
            log_box.text("\n".join(logs[-12:]))

        st.success(f"수집 완료: 신규 저장 {total_inserted}개, 중복/제외 {total_skipped}개")

    st.divider()
    st.subheader("최근 수집된 곡")
    recent = query_df(
        """
        SELECT id, clean_title, team, channel_title, speed, theme, duration_seconds, checked, video_url, created_at
        FROM songs ORDER BY id DESC LIMIT 50
        """
    )
    st.dataframe(recent, use_container_width=True)

elif menu == "2. 곡 검수":
    st.subheader("2. 곡 검수")
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
               last_chord, theme, energy, sheet_url, checked, usable, video_url, duration_seconds, memo
        FROM songs {where_sql}
        ORDER BY checked ASC, id DESC
        LIMIT 200
        """,
        tuple(params),
    )

    if songs.empty:
        st.info("표시할 곡이 없습니다. 먼저 DB 자동 수집을 실행하세요.")
    else:
        st.dataframe(songs[["id", "clean_title", "team", "speed", "bpm", "song_key", "theme", "checked", "usable", "video_url"]], use_container_width=True)
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
                    "usable": 1 if usable else 0,
                    "checked": 1 if checked else 0,
                    "memo": memo,
                },
            )
            st.success("저장했습니다. 화면을 새로고침하면 반영됩니다.")

elif menu == "3. 콘티 추천":
    st.subheader("3. 검수 완료 곡으로 콘티 추천")
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

    col1, col2, col3 = st.columns(3)
    with col1:
        allowed_keys = st.multiselect("사용 가능한 키", [k for k in KEY_OPTIONS if k != "미확인"], default=[])
    with col2:
        preferred_teams = st.multiselect("참고할 찬양팀", team_options, default=team_options[: min(8, len(team_options))])
    with col3:
        max_per_section = st.number_input("구간별 후보 최대 개수", min_value=10, max_value=80, value=35, step=5)

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
            count = st.number_input(f"구간 {i+1} 곡 수", min_value=1, max_value=5, value=default_count, step=1, key=f"sec_count_{i}")
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

        contis = build_contis(section_results, top_n=3)
        if not contis:
            st.error("조건에 맞는 콘티를 만들 수 없습니다. 검수 완료 곡 수를 늘리거나 키/주제/찬양팀 조건을 줄여보세요.")
        else:
            for idx, conti in enumerate(contis, start=1):
                st.markdown(f"## 추천 콘티 {idx}안 — 점수 {conti['score']}")
                out_rows = []
                for order, s in enumerate(conti["songs"], start=1):
                    out_rows.append(
                        {
                            "순서": order,
                            "곡명": s.get("clean_title"),
                            "찬양팀": s.get("team"),
                            "빠르기": s.get("speed"),
                            "키": s.get("song_key"),
                            "BPM": s.get("bpm"),
                            "첫 코드": s.get("first_chord"),
                            "마지막 코드": s.get("last_chord"),
                            "주제": s.get("theme"),
                            "유튜브 링크": s.get("video_url"),
                        }
                    )
                result_df = pd.DataFrame(out_rows)
                st.dataframe(result_df, use_container_width=True)
                st.markdown("#### 순서 + 유튜브 링크")
                for row in out_rows:
                    st.markdown(f"{row['순서']}. **{row['곡명']}** - {row['찬양팀']} / {row['빠르기']} / {row['키']} / BPM {row['BPM']} / [{row['유튜브 링크']}]({row['유튜브 링크']})")

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
                        st.warning(f"NVIDIA AI 설명 생성 실패: {e}")

elif menu == "4. DB 관리":
    st.subheader("4. DB 관리")
    df = query_df(
        """
        SELECT id, clean_title, team, channel_title, speed, bpm, song_key, first_chord, last_chord,
               theme, energy, sheet_url, checked, usable, video_url, created_at, updated_at
        FROM songs ORDER BY id DESC
        """
    )
    st.metric("전체 곡 수", len(df))
    if not df.empty:
        st.metric("검수 완료 곡 수", int(df["checked"].sum()))
        st.dataframe(df, use_container_width=True)
        csv = df.to_csv(index=False).encode("utf-8-sig")
        st.download_button("CSV로 내보내기", data=csv, file_name="worship_songs_export.csv", mime="text/csv")

    st.divider()
    st.subheader("기본 찬양팀 목록 수정")
    sources = load_sources()
    st.write("worship_sources.csv 파일을 수정하면 자동 수집 대상 찬양팀과 검색어를 늘릴 수 있습니다.")
    st.dataframe(sources, use_container_width=True)
