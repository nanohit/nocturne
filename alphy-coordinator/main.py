"""
Alphy - Streaming frontend for soap4youand.me
Direct stream URLs - no proxy needed!
"""

import os
import re
import hashlib
from typing import Optional
from urllib.parse import urlparse, quote
from contextlib import asynccontextmanager
from collections import defaultdict

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv, find_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response

# Configuration - soap4youand.me credentials
load_dotenv(find_dotenv())
SOAP_LOGIN = os.getenv("SOAP_LOGIN")
SOAP_PASSWORD = os.getenv("SOAP_PASSWORD")

# HTTP client with session
http_client: Optional[httpx.AsyncClient] = None
session_token: Optional[str] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    # Login on startup
    await login_to_soap()

    yield

    await http_client.aclose()


app = FastAPI(title="Alphy", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def login_to_soap():
    """Login to soap4youand.me and establish session"""
    global session_token
    
    if not SOAP_LOGIN or not SOAP_PASSWORD:
        raise RuntimeError("SOAP_LOGIN and SOAP_PASSWORD must be set in .env")

    # Get initial page to establish session
    await http_client.get("https://soap4youand.me/")

    # Login
    response = await http_client.post(
        "https://soap4youand.me/login/",
        data={"login": SOAP_LOGIN, "password": SOAP_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )

    if response.status_code == 200:
        print(f"Logged in to soap4youand.me as {SOAP_LOGIN}")
    else:
        print(f"Login failed: {response.status_code}")


async def ensure_logged_in():
    """Ensure we're logged in, re-login if session expired"""
    # Try to access a protected page
    response = await http_client.get("https://soap4youand.me/dashboard/")
    if "login" in str(response.url):
        # Session expired, re-login
        await login_to_soap()


def extract_api_token(html: str) -> Optional[str]:
    """Extract API token from page"""
    match = re.search(r'data:token="([^"]+)"', html)
    return match.group(1) if match else None


ALLOWED_SUBTITLE_HOSTS = {"soap4youand.me", "www.soap4youand.me"}


def srt_to_vtt(srt_text: str) -> str:
    """Convert SRT subtitle text to WebVTT format."""
    if not srt_text:
        return "WEBVTT\n\n"

    # Strip BOM if present
    if srt_text.startswith("\ufeff"):
        srt_text = srt_text.lstrip("\ufeff")

    lines = srt_text.splitlines()
    vtt_lines = ["WEBVTT", ""]
    for line in lines:
        if "-->" in line:
            line = line.replace(",", ".")
        vtt_lines.append(line)
    return "\n".join(vtt_lines) + "\n"


def build_subtitle_proxy_url(src: str) -> str:
    """Build a same-origin proxy URL for subtitle sources."""
    return f"/api/subtitle?src={quote(src, safe='')}"


def parse_search_results(html: str) -> list:
    """Parse search results from HTML"""
    results = []

    # Find all search-item divs
    items = re.findall(
        r'<div class="search-item[^"]*"[^>]*>(.*?)</div>\s*</div>',
        html, re.DOTALL
    )

    for item in items:
        # Get URL (movie or soap)
        url_match = re.search(r'href="(/(movies|soap)/([^/]+)/)"', item)
        if not url_match:
            continue

        url, content_type, id_or_slug = url_match.groups()

        # Get poster
        poster_match = re.search(r'<img[^>]*src="([^"]+)"', item)
        poster = poster_match.group(1) if poster_match else None
        if poster and not poster.startswith('http'):
            poster = f"https://soap4youand.me{poster}"

        # Get title (clean HTML tags)
        title_match = re.search(r'<h5[^>]*>.*?<a[^>]*>([^<]+(?:<span[^>]*>[^<]*</span>[^<]*)*)</a>', item, re.DOTALL)
        if title_match:
            title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip()
            # Split Russian/English title
            if ' — ' in title:
                parts = title.split(' — ')
                title = parts[0].strip()
                title_ru = parts[1].strip() if len(parts) > 1 else None
            else:
                title_ru = None
        else:
            title = "Unknown"
            title_ru = None

        # Get year
        year_match = re.search(r'\((\d{4})\)', item)
        year = year_match.group(1) if year_match else None

        results.append({
            "type": "movie" if content_type == "movies" else "series",
            "id": id_or_slug,
            "url": url,
            "title": title,
            "title_ru": title_ru,
            "year": year,
            "poster": poster
        })

    return results


# ==================== API Endpoints ====================

@app.get("/api/search")
async def search_content(q: str):
    """Search for movies and series"""
    await ensure_logged_in()

    response = await http_client.get(
        f"https://soap4youand.me/search/?q={q}"
    )

    results = parse_search_results(response.text)
    return {"results": results}


@app.get("/api/movie/{movie_id}")
async def get_movie(movie_id: str):
    """Get movie details and stream URL with quality/audio options"""
    await ensure_logged_in()

    response = await http_client.get(f"https://soap4youand.me/movies/{movie_id}/")
    html = response.text

    # Movies have stream URL directly in Playerjs initialization
    # Format: file: "https://cdn-fi11.soap4youand.me/hls/...token.../master.m3u8"
    file_match = re.search(r'file:\s*["\']([^"\']+)["\']', html)
    if not file_match:
        raise HTTPException(status_code=400, detail="Could not find stream URL")

    stream_url = file_match.group(1)

    # Extract title from page
    title_match = re.search(r'<h1[^>]*>([^<]+)', html)
    title = title_match.group(1).strip() if title_match else f"Movie {movie_id}"

    # Extract poster
    poster_match = re.search(r'<img[^>]*class="[^"]*poster[^"]*"[^>]*src="([^"]+)"', html)
    if not poster_match:
        poster_match = re.search(r'poster:\s*["\']([^"\']+)["\']', html)
    poster = poster_match.group(1) if poster_match else None
    if poster and not poster.startswith('http'):
        poster = f"https://soap4youand.me{poster}"

    # Extract subtitles if available
    subs_match = re.search(r'subtitle:\s*["\']([^"\']+)["\']', html)
    subtitles = {}
    if subs_match:
        subs_str = subs_match.group(1)
        # Format: [Label]/path,[Label2]/path2
        for sub in subs_str.split(','):
            if ']' in sub:
                label, path = sub.split(']', 1)
                label = label.strip('[')
                if not path.startswith('http'):
                    path = f"https://soap4youand.me{path}"
                subtitles[label] = build_subtitle_proxy_url(path)

    # Fetch and parse the master.m3u8 to get quality and audio options
    qualities = []
    audio_tracks = []
    try:
        m3u8_response = await http_client.get(stream_url)
        m3u8_content = m3u8_response.text
        
        # Parse audio tracks
        audio_pattern = r'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="([^"]+)",NAME="([^"]+)",LANGUAGE="([^"]+)"'
        for group_id, name, lang in re.findall(audio_pattern, m3u8_content):
            audio_tracks.append({
                "group_id": group_id,
                "name": name,
                "language": lang
            })
        
        # Parse video quality levels
        quality_pattern = r'#EXT-X-STREAM-INF:.*?BANDWIDTH=(\d+),RESOLUTION=(\d+x\d+)'
        resolutions = re.findall(quality_pattern, m3u8_content)
        
        # Map resolutions to quality names
        quality_mapping = {
            (720, 300): "SD",
            (1280, 534): "HD",
            (1920, 802): "Full HD",
            (3832, 1600): "4K UHD"
        }
        
        seen_resolutions = set()
        for bandwidth, resolution in resolutions:
            width, height = map(int, resolution.split('x'))
            if resolution not in seen_resolutions:
                seen_resolutions.add(resolution)
                quality_name = quality_mapping.get((width, height), resolution)
                qualities.append({
                    "name": quality_name,
                    "resolution": resolution,
                    "bandwidth": int(bandwidth)
                })
        
        # Sort by bandwidth
        qualities.sort(key=lambda x: x['bandwidth'])
        
    except Exception as e:
        print(f"Failed to parse m3u8: {e}")

    return {
        "type": "movie",
        "id": movie_id,
        "title": title,
        "stream_url": stream_url,
        "poster": poster,
        "subtitles": subtitles,
        "qualities": qualities,
        "audio_tracks": audio_tracks
    }


@app.get("/api/series/{slug}")
async def get_series(slug: str):
    """Get series details including seasons"""
    await ensure_logged_in()

    response = await http_client.get(f"https://soap4youand.me/soap/{slug}/")
    html = response.text

    # Extract title
    title_match = re.search(r'<h1[^>]*>([^<]+)', html)
    title = title_match.group(1).strip() if title_match else slug

    # Find seasons
    season_matches = re.findall(r'href="/soap/' + re.escape(slug) + r'/(\d+)/"', html)
    seasons = sorted(list(set(int(s) for s in season_matches)))

    # Get poster
    poster_match = re.search(r'<img[^>]*src="(/assets/covers/soap/[^"]+)"', html)
    poster = f"https://soap4youand.me{poster_match.group(1)}" if poster_match else None

    return {
        "type": "series",
        "slug": slug,
        "title": title,
        "seasons": seasons,
        "poster": poster
    }


@app.get("/api/series/{slug}/season/{season}")
async def get_season(slug: str, season: int):
    """Get episodes for a season with quality and translation options"""
    await ensure_logged_in()

    response = await http_client.get(f"https://soap4youand.me/soap/{slug}/{season}/")
    html = response.text

    # Extract quality options
    quality_pattern = r'<li><a class="dropdown-item quality-filter"[^>]*data:param="(\d+)"[^>]*>([^<]+)</a></li>'
    qualities = re.findall(quality_pattern, html)
    quality_map = {qid: qname.strip() for qid, qname in qualities}
    
    # Extract translation options
    trans_pattern = r'<li><a class="dropdown-item translate-filter"[^>]*data:param="([^"]+)"[^>]*>([^<]+)</a></li>'
    translations = re.findall(trans_pattern, html)
    translation_map = {tid: tname.strip() for tid, tname in translations}

    # Use BeautifulSoup to parse episode cards more reliably
    soup = BeautifulSoup(html, 'html.parser')
    episode_cards = soup.find_all('div', class_='episode-card')
    
    # Structure: episodes[ep_num][quality_id][translation_id] = {eid, sid, hash}
    episodes_data = defaultdict(lambda: defaultdict(dict))
    
    for card in episode_cards:
        translate_id = card.get('data:translate')
        quality_id = card.get('data:quality')
        ep_num = card.get('data:episode')
        
        if not (translate_id and quality_id and ep_num):
            continue
        
        # Find play button within the card
        play_btn = card.find('div', attrs={'data:play': 'true'})
        if not play_btn:
            continue
        
        eid = play_btn.get('data:eid')
        sid = play_btn.get('data:sid')
        
        # Find hash in any child element
        hash_elem = card.find(attrs={'data:hash': True})
        hash_val = hash_elem.get('data:hash') if hash_elem else None
        
        if eid and sid and hash_val:
            episodes_data[int(ep_num)][quality_id][translate_id] = {
                "eid": eid,
                "sid": sid,
                "hash": hash_val
            }

    # Convert to list format
    episodes = []
    for ep_num in sorted(episodes_data.keys()):
        episodes.append({
            "episode": ep_num,
            "variants": dict(episodes_data[ep_num])
        })

    # Extract API token for this page
    api_token = extract_api_token(html)

    return {
        "slug": slug,
        "season": season,
        "episodes": episodes,
        "qualities": quality_map,
        "translations": translation_map,
        "api_token": api_token
    }


@app.get("/api/stream/{eid}")
async def get_stream(
    eid: str,
    sid: str,
    hash: str,
    token: Optional[str] = None,
    quality: Optional[str] = None,
    translation: Optional[str] = None
):
    """Get stream URL for an episode
    
    For series: quality and translation parameters are used to select the correct eid/hash
    For movies: quality and translation are parsed from the HLS master.m3u8
    """
    await ensure_logged_in()

    # If no token provided, we need to fetch the page to get one
    if not token:
        raise HTTPException(status_code=400, detail="API token required")

    # Calculate request hash
    hash_input = token + eid + sid + hash
    request_hash = hashlib.md5(hash_input.encode()).hexdigest()

    # Get stream URL from API
    api_response = await http_client.post(
        f"https://soap4youand.me/api/v2/play/episode/{eid}",
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-api-token": token,
            "x-user-agent": "browser: public v0.1"
        },
        content=f"eid={eid}&hash={request_hash}"
    )

    if api_response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to get stream")

    data = api_response.json()
    if not data.get("ok"):
        raise HTTPException(status_code=400, detail=data.get("msg", "Failed to get stream URL"))

    stream_url = data.get("stream")
    stream_type = "hls"  # Default to HLS
    
    # Series episodes use storage2 URLs that redirect to MP4
    # Follow the redirect to get the final CDN URL
    if stream_url and "storage2.soap4youand.me" in stream_url:
        try:
            # Make HEAD request with no redirect to get the Location header
            redirect_response = await http_client.head(
                stream_url,
                follow_redirects=False
            )
            if redirect_response.status_code in (301, 302, 303, 307, 308):
                final_url = redirect_response.headers.get("location")
                if final_url:
                    stream_url = final_url
                    stream_type = "mp4"
        except Exception as e:
            print(f"Failed to follow redirect: {e}")
    
    # Detect stream type from URL
    if stream_url:
        if stream_url.endswith('.m3u8') or '/hls/' in stream_url:
            stream_type = "hls"
        elif not stream_url.endswith('/'):
            # Likely a direct file
            stream_type = "mp4"
        elif stream_url.endswith('/') and 'cdn-fi' in stream_url:
            # CDN directory URL - this is MP4
            stream_type = "mp4"

    # Parse subtitles - the API returns a complex structure
    subtitles = {}
    subs_data = data.get("subs", {})

    def add_subtitle(label: str, src: str) -> None:
        if src:
            subtitles[label] = build_subtitle_proxy_url(src)

    if isinstance(subs_data, dict):
        ru_val = subs_data.get("ru")
        en_val = subs_data.get("en")

        # Direct URLs (movies)
        if isinstance(ru_val, str):
            add_subtitle("Русский", ru_val)
        elif ru_val:
            # Series pattern from site player: /subs/{sid}/{eid}/1.srt
            add_subtitle("Русский", f"https://soap4youand.me/subs/{sid}/{eid}/1.srt")

        if isinstance(en_val, str):
            add_subtitle("English", en_val)
        elif en_val:
            add_subtitle("English", f"https://soap4youand.me/subs/{sid}/{eid}/2.srt")
    elif isinstance(subs_data, list):
        for item in subs_data:
            if not isinstance(item, dict):
                continue
            label = item.get("label") or item.get("lang") or item.get("name")
            src = item.get("url") or item.get("src")
            if label and src:
                add_subtitle(label, src)

    return {
        "stream_url": stream_url,
        "stream_type": stream_type,
        "poster": data.get("poster"),
        "title": data.get("title"),
        "subtitles": subtitles,
        "start_from": data.get("start_from"),
        "quality": quality,
        "translation": translation
    }


@app.get("/api/subtitle")
async def proxy_subtitle(src: str):
    """Proxy subtitle files to avoid CORS and normalize to WebVTT."""
    await ensure_logged_in()

    if not src:
        raise HTTPException(status_code=400, detail="Missing subtitle source")

    # Allow relative paths from soap4youand.me
    if src.startswith("/"):
        src = f"https://soap4youand.me{src}"

    parsed = urlparse(src)
    if parsed.scheme not in ("http", "https") or parsed.netloc not in ALLOWED_SUBTITLE_HOSTS:
        raise HTTPException(status_code=400, detail="Invalid subtitle source")

    response = await http_client.get(src)
    if response.status_code != 200:
        raise HTTPException(status_code=404, detail="Subtitle not found")

    text = response.text or ""
    if text.lstrip().startswith("WEBVTT"):
        vtt_text = text
    else:
        vtt_text = srt_to_vtt(text)

    return Response(content=vtt_text, media_type="text/vtt")


# ==================== Frontend ====================

FRONTEND_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>alphy</title>
    
    <!-- Video.js for HLS playback with quality and subtitle controls -->
    <link href="https://vjs.zencdn.net/8.10.0/video-js.css" rel="stylesheet">
    <script src="https://vjs.zencdn.net/8.10.0/video.min.js"></script>
    
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0a0a0a;
            color: #fff;
            min-height: 100vh;
        }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }

        header {
            display: flex;
            align-items: center;
            gap: 20px;
            margin-bottom: 30px;
            padding-bottom: 20px;
            border-bottom: 1px solid #222;
        }
        .logo {
            font-family: monospace;
            font-size: 28px;
            font-weight: 700;
            color: #e50914;
            cursor: pointer;
        }
        .search-box { flex: 1; display: flex; gap: 10px; }
        .search-box input {
            flex: 1;
            padding: 12px 16px;
            border: none;
            border-radius: 4px;
            background: #222;
            color: #fff;
            font-size: 16px;
        }
        .search-box input:focus { outline: 2px solid #e50914; }
        .search-box button {
            padding: 12px 24px;
            border: none;
            border-radius: 4px;
            background: #e50914;
            color: #fff;
            font-size: 16px;
            cursor: pointer;
        }
        .search-box button:hover { background: #b2070f; }

        /* Player Section */
        .player-section { display: none; margin-bottom: 30px; }
        .player-section.active { display: block; }
        .player-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            flex-wrap: wrap;
            gap: 10px;
        }
        .player-title { font-size: 20px; }
        .player-controls { display: flex; gap: 10px; flex-wrap: wrap; }
        .player-controls select {
            padding: 8px 12px;
            border: none;
            border-radius: 4px;
            background: #222;
            color: #fff;
        }
        .video-container {
            width: 100%;
            background: #000;
            border-radius: 8px;
            overflow: hidden;
            position: relative;
        }
        
        /* Video.js player styles */
        .video-js {
            width: 100%;
            aspect-ratio: 16/9;
            max-height: 80vh;
        }
        
        .vjs-fluid {
            padding-top: 0 !important;
        }
        
        .video-js .vjs-big-play-button {
            border-color: #e50914;
            background-color: rgba(229, 9, 20, 0.8);
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
        }
        
        .video-js .vjs-big-play-button:hover {
            background-color: rgba(229, 9, 20, 1);
        }
        
        .video-js .vjs-control-bar {
            background: rgba(0, 0, 0, 0.7);
        }

        /* Results Section */
        .results-section { display: none; }
        .results-section.active { display: block; }
        .results-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
            gap: 20px;
        }
        .result-card {
            background: #1a1a1a;
            border-radius: 8px;
            overflow: hidden;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .result-card:hover {
            transform: scale(1.05);
            box-shadow: 0 10px 30px rgba(0,0,0,0.5);
        }
        .result-poster {
            width: 100%;
            aspect-ratio: 2/3;
            background: #333;
            object-fit: cover;
        }
        .result-info { padding: 12px; }
        .result-title {
            font-size: 14px;
            font-weight: 600;
            margin-bottom: 4px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .result-meta { font-size: 12px; color: #888; }
        .result-type {
            display: inline-block;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 10px;
            text-transform: uppercase;
            margin-right: 5px;
        }
        .result-type.movie { background: #e50914; }
        .result-type.series { background: #0066cc; }

        /* Episode Section */
        .episode-section { display: none; margin-bottom: 30px; }
        .episode-section.active { display: block; }
        .season-tabs {
            display: flex;
            gap: 10px;
            margin-bottom: 15px;
            overflow-x: auto;
            padding-bottom: 5px;
        }
        .season-tab {
            padding: 10px 20px;
            border: none;
            border-radius: 4px;
            background: #222;
            color: #fff;
            cursor: pointer;
            white-space: nowrap;
        }
        .season-tab.active { background: #e50914; }
        .season-tab:hover:not(.active) { background: #333; }
        .episodes-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(70px, 1fr));
            gap: 10px;
        }
        .episode-btn {
            padding: 15px;
            border: none;
            border-radius: 4px;
            background: #222;
            color: #fff;
            cursor: pointer;
            font-size: 14px;
        }
        .episode-btn.active { background: #e50914; }
        .episode-btn:hover:not(.active) { background: #333; }

        /* Loading & Error */
        .loading {
            text-align: center;
            padding: 60px 20px;
            color: #888;
        }
        .spinner {
            border: 3px solid #333;
            border-top: 3px solid #e50914;
            border-radius: 50%;
            width: 40px;
            height: 40px;
            animation: spin 1s linear infinite;
            margin: 0 auto 15px;
        }
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        .error {
            color: #e50914;
            padding: 40px 20px;
            text-align: center;
            background: rgba(229, 9, 20, 0.1);
            border-radius: 8px;
            margin: 20px 0;
        }

        /* Empty State */
        .empty-state {
            text-align: center;
            padding: 60px 20px;
            color: #666;
        }
        .empty-state h2 {
            font-size: 24px;
            margin-bottom: 10px;
            color: #888;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="logo" onclick="goHome()">alphy</div>
            <div class="search-box">
                <input type="text" id="searchInput" placeholder="Search movies and TV shows..." autofocus>
                <button onclick="search()">Search</button>
            </div>
        </header>

        <div id="playerSection" class="player-section">
            <div class="player-header">
                <h2 id="playerTitle" class="player-title"></h2>
                <div class="player-controls">
                    <select id="qualitySelect" onchange="changeQuality()" style="display:none">
                        <option value="auto">Auto</option>
                    </select>
                    <select id="speedSelect" onchange="changeSpeed()">
                        <option value="0.5">0.5x</option>
                        <option value="0.75">0.75x</option>
                        <option value="1" selected>1x</option>
                        <option value="1.25">1.25x</option>
                        <option value="1.5">1.5x</option>
                        <option value="2">2x</option>
                    </select>
                </div>
            </div>
            <div class="video-container">
                <video id="videoPlayer" class="video-js vjs-big-play-centered" controls preload="auto"></video>
            </div>
        </div>

        <div id="episodeSection" class="episode-section">
            <div class="player-controls" style="margin-bottom: 15px;">
                <select id="episodeQualitySelect" onchange="updateEpisodeSelection()">
                    <option value="">Select Quality</option>
                </select>
                <select id="episodeTranslationSelect" onchange="updateEpisodeSelection()">
                    <option value="">Select Audio</option>
                </select>
            </div>
            <div id="seasonTabs" class="season-tabs"></div>
            <div id="episodesGrid" class="episodes-grid"></div>
        </div>

        <div id="resultsSection" class="results-section">
            <div id="resultsGrid" class="results-grid"></div>
        </div>

        <div id="emptyState" class="empty-state">
            <h2>Welcome to Alphy</h2>
            <p>Search for movies and TV shows to get started</p>
        </div>

        <div id="loading" class="loading" style="display: none;">
            <div class="spinner"></div>
            <div>Loading...</div>
        </div>

        <div id="error" class="error" style="display: none;"></div>
    </div>

    <script>
        let currentContent = null;
        let currentSeason = 1;
        let currentEpisodeData = null;
        let seasonData = {};
        let apiToken = null;
        let currentQualityId = null;
        let currentTranslationId = null;
        let qualitiesMap = {};
        let translationsMap = {};
        let player = null;
        let currentMovieData = null;
        
        // Initialize Video.js player
        function initPlayer() {
            if (player) {
                return;
            }
            
            player = videojs('videoPlayer', {
                controls: true,
                preload: 'auto',
                responsive: true,
                fill: true,
                html5: {
                    vhs: {
                        overrideNative: true
                    },
                    nativeAudioTracks: false,
                    nativeVideoTracks: false,
                    nativeTextTracks: true  // Enable native text tracks for MP4 subtitles
                }
            });
            
            // Handle quality levels for HLS
            player.on('loadedmetadata', function() {
                const qualitySelect = document.getElementById('qualitySelect');
                const qualityLevels = player.qualityLevels ? player.qualityLevels() : null;
                
                if (qualityLevels && qualityLevels.length > 1) {
                    qualitySelect.style.display = 'block';
                    qualitySelect.innerHTML = '<option value="auto">Auto</option>';
                    
                    // Get unique resolutions
                    const resolutions = [];
                    for (let i = 0; i < qualityLevels.length; i++) {
                        const level = qualityLevels[i];
                        const height = level.height;
                        if (height && !resolutions.find(r => r.height === height)) {
                            resolutions.push({ height, index: i, label: height + 'p' });
                        }
                    }
                    
                    // Sort by resolution (highest first)
                    resolutions.sort((a, b) => b.height - a.height);
                    
                    resolutions.forEach(res => {
                        const opt = document.createElement('option');
                        opt.value = res.height;
                        opt.textContent = res.label;
                        qualitySelect.appendChild(opt);
                    });
                } else {
                    qualitySelect.style.display = 'none';
                }
                
                // Audio tracks - prefer English
                const audioTracks = player.audioTracks();
                if (audioTracks && audioTracks.length > 1) {
                    for (let i = 0; i < audioTracks.length; i++) {
                        if (audioTracks[i].language === 'en' || 
                            audioTracks[i].label.toLowerCase().includes('english')) {
                            audioTracks[i].enabled = true;
                        } else {
                            audioTracks[i].enabled = false;
                        }
                    }
                }
                
                // Text tracks (subtitles) - enable English if available
                const textTracks = player.textTracks();
                if (textTracks && textTracks.length > 0) {
                    for (let i = 0; i < textTracks.length; i++) {
                        if (textTracks[i].kind === 'subtitles' || textTracks[i].kind === 'captions') {
                            if (textTracks[i].language === 'en' || 
                                textTracks[i].label.toLowerCase().includes('english')) {
                                textTracks[i].mode = 'showing';
                            }
                        }
                    }
                }
            });
        }
        
        function changeQuality() {
            if (!player) return;
            const qualityLevels = player.qualityLevels ? player.qualityLevels() : null;
            if (!qualityLevels) return;
            
            const selectedHeight = document.getElementById('qualitySelect').value;
            
            for (let i = 0; i < qualityLevels.length; i++) {
                if (selectedHeight === 'auto') {
                    qualityLevels[i].enabled = true;
                } else {
                    qualityLevels[i].enabled = (qualityLevels[i].height == selectedHeight);
                }
            }
        }

        async function search() {
            const query = document.getElementById('searchInput').value.trim();
            if (!query) return;

            showLoading();
            hideAll();

            try {
                const response = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
                const data = await response.json();
                displayResults(data.results);
            } catch (e) {
                showError('Search failed: ' + e.message);
            }
        }

        function displayResults(results) {
            hideLoading();

            if (results.length === 0) {
                document.getElementById('resultsGrid').innerHTML =
                    '<div class="empty-state"><p>No results found</p></div>';
                document.getElementById('resultsSection').classList.add('active');
                return;
            }

            const grid = document.getElementById('resultsGrid');
            grid.innerHTML = results.map(item => `
                <div class="result-card" onclick='selectContent(${JSON.stringify(item)})'>
                    <img class="result-poster"
                         src="${item.poster || ''}"
                         alt="${item.title}"
                         onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 200 300%22><rect fill=%22%23333%22 width=%22200%22 height=%22300%22/><text fill=%22%23666%22 x=%22100%22 y=%22150%22 text-anchor=%22middle%22>No Image</text></svg>'">
                    <div class="result-info">
                        <div class="result-title">${item.title}</div>
                        <div class="result-meta">
                            <span class="result-type ${item.type}">${item.type}</span>
                            ${item.year || ''}
                        </div>
                    </div>
                </div>
            `).join('');

            document.getElementById('resultsSection').classList.add('active');
            document.getElementById('emptyState').style.display = 'none';
        }

        async function selectContent(item) {
            currentContent = item;
            showLoading();
            hideAll();

            try {
                if (item.type === 'movie') {
                    await playMovie(item.id);
                } else {
                    await loadSeries(item.id);
                }
            } catch (e) {
                showError('Failed to load: ' + e.message);
            }
        }

        async function playMovie(movieId) {
            const response = await fetch(`/api/movie/${movieId}`);
            const data = await response.json();
            currentMovieData = data;

            if (data.stream_url) {
                // Movies use HLS (master.m3u8) - Video.js handles quality/audio internally
                playVideo(data.stream_url, data.title, 'hls', data.subtitles || {});
            } else {
                throw new Error('No stream URL found');
            }
        }

        async function loadSeries(slug) {
            const response = await fetch(`/api/series/${slug}`);
            const data = await response.json();

            currentContent.seriesData = data;
            seasonData = {};

            hideLoading();
            setupSeasons(data);
        }

        function setupSeasons(data) {
            const seasons = data.seasons || [1];

            document.getElementById('seasonTabs').innerHTML = seasons.map(s =>
                `<button class="season-tab ${s === seasons[0] ? 'active' : ''}"
                         onclick="selectSeason(${s})">Season ${s}</button>`
            ).join('');

            selectSeason(seasons[0]);
            document.getElementById('episodeSection').classList.add('active');
        }

        async function selectSeason(season) {
            currentSeason = season;

            // Update tab UI
            document.querySelectorAll('.season-tab').forEach(tab => {
                tab.classList.toggle('active', tab.textContent === `Season ${season}`);
            });

            // Check if we already have this season's data
            if (seasonData[season]) {
                displayEpisodes(seasonData[season]);
                return;
            }

            // Fetch season data
            showLoading();

            try {
                const response = await fetch(
                    `/api/series/${currentContent.id}/season/${season}`
                );
                const data = await response.json();

                seasonData[season] = data;
                apiToken = data.api_token;
                qualitiesMap = data.qualities;
                translationsMap = data.translations;

                // Populate quality dropdown
                const qualitySelect = document.getElementById('episodeQualitySelect');
                qualitySelect.innerHTML = Object.entries(qualitiesMap).map(([id, name]) =>
                    `<option value="${id}">${name}</option>`
                ).join('');
                
                // Set default quality to Full HD (3) or highest available
                currentQualityId = '3'; // Full HD
                if (!qualitiesMap['3']) {
                    currentQualityId = Object.keys(qualitiesMap)[Object.keys(qualitiesMap).length - 1];
                }
                qualitySelect.value = currentQualityId;

                // Populate translation dropdown
                const translationSelect = document.getElementById('episodeTranslationSelect');
                translationSelect.innerHTML = Object.entries(translationsMap).map(([id, name]) =>
                    `<option value="${id}">${name}</option>`
                ).join('');
                
                // Set default translation to Субтитры (sub) or first available
                currentTranslationId = 'sub'; // Original with subtitles
                if (!translationsMap['sub']) {
                    currentTranslationId = Object.keys(translationsMap)[0];
                }
                translationSelect.value = currentTranslationId;

                hideLoading();
                displayEpisodes(data);
            } catch (e) {
                showError('Failed to load season: ' + e.message);
            }
        }

        function updateEpisodeSelection() {
            // Update current selections
            currentQualityId = document.getElementById('episodeQualitySelect').value;
            currentTranslationId = document.getElementById('episodeTranslationSelect').value;
            
            // If an episode is currently playing, reload it with new quality/translation
            if (currentEpisodeData) {
                playEpisode(currentEpisodeData);
            }
        }

        function displayEpisodes(data) {
            const episodes = data.episodes || [];

            document.getElementById('episodesGrid').innerHTML = episodes.map(ep =>
                `<button class="episode-btn"
                         onclick='playEpisode(${JSON.stringify(ep).replace(/'/g, "&#39;")})'>
                    E${ep.episode}
                </button>`
            ).join('');
        }

        async function playEpisode(episodeData) {
            currentEpisodeData = episodeData;

            // Update episode button UI
            document.querySelectorAll('.episode-btn').forEach(btn => {
                btn.classList.toggle('active', btn.textContent === `E${episodeData.episode}`);
            });

            showLoading();

            try {
                // Get the selected quality and translation
                let qualityId = document.getElementById('episodeQualitySelect').value;
                let translationId = document.getElementById('episodeTranslationSelect').value;
                
                // Find available variant - fallback if selected combo doesn't exist
                let variant = null;
                const variants = episodeData.variants;
                
                // Try selected combo first
                if (variants[qualityId] && variants[qualityId][translationId]) {
                    variant = variants[qualityId][translationId];
                } else {
                    // Fallback: try other translations for selected quality
                    if (variants[qualityId]) {
                        const availableTrans = Object.keys(variants[qualityId]);
                        if (availableTrans.length > 0) {
                            translationId = availableTrans[0];
                            variant = variants[qualityId][translationId];
                        }
                    }
                    
                    // Fallback: try other qualities
                    if (!variant) {
                        for (const qId of Object.keys(variants).reverse()) {
                            if (variants[qId][translationId]) {
                                qualityId = qId;
                                variant = variants[qId][translationId];
                                break;
                            }
                            const availableTrans = Object.keys(variants[qId]);
                            if (availableTrans.length > 0) {
                                qualityId = qId;
                                translationId = availableTrans[0];
                                variant = variants[qId][translationId];
                                break;
                            }
                        }
                    }
                }
                
                if (!variant) {
                    throw new Error('No stream available for this episode');
                }

                const params = new URLSearchParams({
                    sid: variant.sid,
                    hash: variant.hash,
                    token: apiToken,
                    quality: qualityId,
                    translation: translationId
                });

                const response = await fetch(`/api/stream/${variant.eid}?${params}`);
                const data = await response.json();

                if (data.stream_url) {
                    const title = `${currentContent.title} S${currentSeason}E${episodeData.episode}`;
                    playVideo(data.stream_url, title, data.stream_type, data.subtitles || {});
                } else {
                    throw new Error('No stream URL');
                }
            } catch (e) {
                showError('Failed to play: ' + e.message);
            }
        }

        function playVideo(url, title, streamType = 'hls', subtitles = {}) {
            hideLoading();
            
            initPlayer();

            // Clear previous subtitle tracks
            const remoteTracks = player.remoteTextTracks();
            for (let i = remoteTracks.length - 1; i >= 0; i--) {
                player.removeRemoteTextTrack(remoteTracks[i]);
            }
            const textTracks = player.textTracks();
            for (let i = 0; i < textTracks.length; i++) {
                textTracks[i].mode = 'disabled';
            }

            // Determine MIME type based on stream type
            let mimeType = 'application/x-mpegURL';
            if (streamType === 'mp4' || (!url.includes('.m3u8') && !url.includes('/hls/'))) {
                mimeType = 'video/mp4';
            }
            
            console.log('Playing:', url, 'Type:', mimeType);

            // Set video source
            player.src({
                src: url,
                type: mimeType
            });

            // Add subtitle tracks via same-origin proxy
            const subtitleEntries = Object.entries(subtitles || {});
            let defaultSubtitleSet = false;
            subtitleEntries.forEach(([label, src]) => {
                if (!src) return;

                const lower = label.toLowerCase();
                let lang = 'und';
                if (lower.includes('english') || lower.includes('eng')) {
                    lang = 'en';
                } else if (lower.includes('рус') || lower.includes('ru')) {
                    lang = 'ru';
                }

                const track = player.addRemoteTextTrack({
                    kind: 'subtitles',
                    label: label,
                    srclang: lang,
                    src: src
                }, false);

                if (!defaultSubtitleSet && lang === 'en') {
                    track.track.mode = 'showing';
                    defaultSubtitleSet = true;
                }
            });

            if (!defaultSubtitleSet && subtitleEntries.length > 0) {
                const tracks = player.textTracks();
                for (let i = 0; i < tracks.length; i++) {
                    if (tracks[i].kind === 'subtitles' || tracks[i].kind === 'captions') {
                        tracks[i].mode = 'showing';
                        break;
                    }
                }
            }

            document.getElementById('playerTitle').textContent = title;
            document.getElementById('playerSection').classList.add('active');

            player.play().catch(() => {});
        }

        function changeSpeed() {
            const speed = parseFloat(document.getElementById('speedSelect').value);
            if (player) {
                player.playbackRate(speed);
            }
        }

        function hidePlayer() {
            document.getElementById('playerSection').classList.remove('active');
            if (player) {
                player.pause();
            }

            // Show appropriate section
            if (currentContent?.type === 'series') {
                document.getElementById('episodeSection').classList.add('active');
            } else {
                document.getElementById('resultsSection').classList.add('active');
            }
        }

        function hideAll() {
            document.getElementById('playerSection').classList.remove('active');
            document.getElementById('episodeSection').classList.remove('active');
            document.getElementById('resultsSection').classList.remove('active');
            document.getElementById('emptyState').style.display = 'none';
            document.getElementById('error').style.display = 'none';
        }

        function goHome() {
            hideAll();
            if (player) {
                player.pause();
            }
            document.getElementById('searchInput').value = '';
            document.getElementById('emptyState').style.display = 'block';
            currentContent = null;
            seasonData = {};
        }

        function showLoading() {
            document.getElementById('loading').style.display = 'block';
            document.getElementById('error').style.display = 'none';
        }

        function hideLoading() {
            document.getElementById('loading').style.display = 'none';
        }

        function showError(message) {
            hideLoading();
            const errorEl = document.getElementById('error');
            errorEl.textContent = message;
            errorEl.style.display = 'block';
        }

        // Enter key to search
        document.getElementById('searchInput').addEventListener('keypress', e => {
            if (e.key === 'Enter') search();
        });
    </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    """Serve the main streaming frontend"""
    return FRONTEND_HTML


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "ok", "service": "alphy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
