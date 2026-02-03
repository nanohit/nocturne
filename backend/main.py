"""FastAPI backend for HDRezka streaming."""
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.responses import Response
from typing import Optional
import os
import re
import hashlib
import base64
from urllib.parse import urlparse, urljoin, quote
from collections import defaultdict

import httpx
from bs4 import BeautifulSoup

from backend.services.extractor import (
    search_content,
    get_stream,
    get_content_info,
    initialize,
    BROWSER_HEADERS,
)

SOAP_LOGIN = os.getenv("SOAP_LOGIN")
SOAP_PASSWORD = os.getenv("SOAP_PASSWORD")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "London2006)")

app = FastAPI(title="alphy")


def _decode_admin_token(token: str) -> Optional[tuple[str, str]]:
    try:
        padding = 4 - len(token) % 4
        if padding != 4:
            token += "=" * padding
        decoded = base64.b64decode(token.encode()).decode()
        if ":" not in decoded:
            return None
        user, password = decoded.split(":", 1)
        return user, password
    except Exception:
        return None


def require_admin(request: Request, allow_query: bool = True) -> None:
    user = request.headers.get("X-Admin-User")
    password = request.headers.get("X-Admin-Pass")
    if user == ADMIN_USER and password == ADMIN_PASSWORD:
        return

    if allow_query:
        token = request.query_params.get("admin") or request.query_params.get("admin_token")
        decoded = _decode_admin_token(token) if token else None
        if decoded and decoded[0] == ADMIN_USER and decoded[1] == ADMIN_PASSWORD:
            return

    raise HTTPException(status_code=401, detail="Admin authentication required")


@app.get("/health")
async def health_check():
    """Health check endpoint for Render deployment."""
    return {"status": "ok", "service": "alphy"}


@app.get("/api/admin/check")
async def admin_check(request: Request):
    require_admin(request)
    return {"status": "ok"}


# ==================== SOAP4YOU API ====================
@app.get("/api/soap/search")
async def soap_search(q: str = Query(..., min_length=1)):
    """Search movies and series on soap4youand.me."""
    if not SOAP_LOGIN or not SOAP_PASSWORD:
        raise HTTPException(status_code=500, detail="SOAP credentials not configured")
    await ensure_soap_logged_in()
    client = await get_soap_client()
    response = await client.get("https://soap4youand.me/search/", params={"q": q})
    results = parse_search_results(response.text)
    return {"results": results}


@app.get("/api/soap/movie/{movie_id}")
async def soap_movie(movie_id: str):
    """Get movie details and stream URL."""
    if not SOAP_LOGIN or not SOAP_PASSWORD:
        raise HTTPException(status_code=500, detail="SOAP credentials not configured")
    await ensure_soap_logged_in()
    client = await get_soap_client()

    response = await client.get(f"https://soap4youand.me/movies/{movie_id}/")
    html = response.text

    file_match = re.search(r'file:\s*["\']([^"\']+)["\']', html)
    if not file_match:
        raise HTTPException(status_code=400, detail="Could not find stream URL")
    stream_url = file_match.group(1)

    title_match = re.search(r'<h1[^>]*>([^<]+)', html)
    title = title_match.group(1).strip() if title_match else f"Movie {movie_id}"

    poster_match = re.search(r'<img[^>]*class="[^"]*poster[^"]*"[^>]*src="([^"]+)"', html)
    if not poster_match:
        poster_match = re.search(r'poster:\s*["\']([^"\']+)["\']', html)
    poster = poster_match.group(1) if poster_match else None
    if poster and not poster.startswith("http"):
        poster = f"https://soap4youand.me{poster}"

    subtitles = {}
    subs_match = re.search(r'subtitle:\s*["\']([^"\']+)["\']', html)
    if subs_match:
        subs_str = subs_match.group(1)
        for sub in subs_str.split(','):
            if ']' in sub:
                label, path = sub.split(']', 1)
                label = label.strip('[')
                if not path.startswith('http'):
                    path = f"https://soap4youand.me{path}"
                subtitles[label] = build_subtitle_proxy_url(path)

    return {
        "type": "movie",
        "id": movie_id,
        "title": title,
        "stream_url": stream_url,
        "poster": poster,
        "subtitles": subtitles,
    }


@app.get("/api/soap/series/{slug}")
async def soap_series(slug: str):
    """Get series details including seasons."""
    if not SOAP_LOGIN or not SOAP_PASSWORD:
        raise HTTPException(status_code=500, detail="SOAP credentials not configured")
    await ensure_soap_logged_in()
    client = await get_soap_client()

    response = await client.get(f"https://soap4youand.me/soap/{slug}/")
    html = response.text

    title_match = re.search(r'<h1[^>]*>([^<]+)', html)
    title = title_match.group(1).strip() if title_match else slug

    season_matches = re.findall(r'href="/soap/' + re.escape(slug) + r'/(\d+)/"', html)
    seasons = sorted(list(set(int(s) for s in season_matches)))

    poster_match = re.search(r'<img[^>]*src="(/assets/covers/soap/[^"]+)"', html)
    poster = f"https://soap4youand.me{poster_match.group(1)}" if poster_match else None

    return {
        "type": "series",
        "slug": slug,
        "title": title,
        "seasons": seasons,
        "poster": poster,
    }


@app.get("/api/soap/series/{slug}/season/{season}")
async def soap_season(slug: str, season: int):
    """Get episodes for a season with quality and translation options."""
    if not SOAP_LOGIN or not SOAP_PASSWORD:
        raise HTTPException(status_code=500, detail="SOAP credentials not configured")
    await ensure_soap_logged_in()
    client = await get_soap_client()

    response = await client.get(f"https://soap4youand.me/soap/{slug}/{season}/")
    html = response.text

    quality_pattern = r'<li><a class="dropdown-item quality-filter"[^>]*data:param="(\d+)"[^>]*>([^<]+)</a></li>'
    qualities = re.findall(quality_pattern, html)
    quality_map = {qid: qname.strip() for qid, qname in qualities}

    trans_pattern = r'<li><a class="dropdown-item translate-filter"[^>]*data:param="([^"]+)"[^>]*>([^<]+)</a></li>'
    translations = re.findall(trans_pattern, html)
    translation_map = {tid: tname.strip() for tid, tname in translations}

    soup = BeautifulSoup(html, "html.parser")
    episode_cards = soup.find_all("div", class_="episode-card")

    episodes_data = defaultdict(lambda: defaultdict(dict))

    for card in episode_cards:
        translate_id = card.get("data:translate")
        quality_id = card.get("data:quality")
        ep_num = card.get("data:episode")

        if not (translate_id and quality_id and ep_num):
            continue

        play_btn = card.find("div", attrs={"data:play": "true"})
        if not play_btn:
            continue

        eid = play_btn.get("data:eid")
        sid = play_btn.get("data:sid")

        hash_elem = card.find(attrs={"data:hash": True})
        hash_val = hash_elem.get("data:hash") if hash_elem else None

        if eid and sid and hash_val:
            episodes_data[int(ep_num)][quality_id][translate_id] = {
                "eid": eid,
                "sid": sid,
                "hash": hash_val,
            }

    episodes = []
    for ep_num in sorted(episodes_data.keys()):
        episodes.append({
            "episode": ep_num,
            "variants": dict(episodes_data[ep_num]),
        })

    api_token = extract_api_token(html)

    return {
        "slug": slug,
        "season": season,
        "episodes": episodes,
        "qualities": quality_map,
        "translations": translation_map,
        "api_token": api_token,
    }


@app.get("/api/soap/stream/{eid}")
async def soap_stream(
    eid: str,
    sid: str,
    hash: str,
    token: Optional[str] = None,
    quality: Optional[str] = None,
    translation: Optional[str] = None,
):
    """Get stream URL for a series episode."""
    if not SOAP_LOGIN or not SOAP_PASSWORD:
        raise HTTPException(status_code=500, detail="SOAP credentials not configured")
    await ensure_soap_logged_in()
    client = await get_soap_client()

    if not token:
        raise HTTPException(status_code=400, detail="API token required")

    hash_input = token + eid + sid + hash
    request_hash = hashlib.md5(hash_input.encode()).hexdigest()

    api_response = await client.post(
        f"https://soap4youand.me/api/v2/play/episode/{eid}",
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-api-token": token,
            "x-user-agent": "browser: public v0.1",
        },
        content=f"eid={eid}&hash={request_hash}",
    )

    if api_response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to get stream")

    data = api_response.json()
    if not data.get("ok"):
        raise HTTPException(status_code=400, detail=data.get("msg", "Failed to get stream URL"))

    stream_url = data.get("stream")
    stream_type = "hls"

    if stream_url and "storage2.soap4youand.me" in stream_url:
        try:
            redirect_response = await client.head(stream_url, follow_redirects=False)
            if redirect_response.status_code in (301, 302, 303, 307, 308):
                final_url = redirect_response.headers.get("location")
                if final_url:
                    stream_url = final_url
                    stream_type = "mp4"
        except Exception as e:
            print(f"Failed to follow redirect: {e}")

    if stream_url:
        if stream_url.endswith(".m3u8") or "/hls/" in stream_url:
            stream_type = "hls"
        elif not stream_url.endswith("/"):
            stream_type = "mp4"
        elif stream_url.endswith("/") and "cdn-fi" in stream_url:
            stream_type = "mp4"

    subtitles = {}
    subs_data = data.get("subs", {})

    def add_subtitle(label: str, src: str) -> None:
        if src:
            subtitles[label] = build_subtitle_proxy_url(src)

    if isinstance(subs_data, dict):
        ru_val = subs_data.get("ru")
        en_val = subs_data.get("en")

        if isinstance(ru_val, str):
            add_subtitle("Русский", ru_val)
        elif ru_val:
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
        "translation": translation,
    }


@app.get("/api/subtitle")
async def proxy_subtitle(src: str):
    """Proxy subtitle files to avoid CORS and normalize to WebVTT."""
    await ensure_soap_logged_in()

    if not src:
        raise HTTPException(status_code=400, detail="Missing subtitle source")

    if src.startswith("/"):
        src = f"https://soap4youand.me{src}"

    parsed = urlparse(src)
    if parsed.scheme not in ("http", "https") or parsed.netloc not in ALLOWED_SUBTITLE_HOSTS:
        raise HTTPException(status_code=400, detail="Invalid subtitle source")

    client = await get_soap_client()
    response = await client.get(src)
    if response.status_code != 200:
        raise HTTPException(status_code=404, detail="Subtitle not found")

    text = response.text or ""
    if text.lstrip().startswith("WEBVTT"):
        vtt_text = text
    else:
        vtt_text = srt_to_vtt(text)

    return Response(content=vtt_text, media_type="text/vtt")


@app.post("/api/ajax-proxy")
async def ajax_proxy(request: Request):
    """
    CORS proxy for HDRezka AJAX endpoint.

    This proxies the AJAX call but the resulting tokens will be bound to
    our server's IP, not the user's. We need to test if this matters.
    """
    require_admin(request)
    body = await request.body()
    client = await get_proxy_client()

    # Forward the request to HDRezka
    try:
        resp = await client.post(
            "https://hdrezka.me/ajax/get_cdn_series/",
            content=body,
            headers={
                **BROWSER_HEADERS,
                'Content-Type': 'application/x-www-form-urlencoded',
                'X-Requested-With': 'XMLHttpRequest',
                'Origin': 'https://hdrezka.me',
                'Referer': 'https://hdrezka.me/',
            }
        )

        # Return the raw response with CORS headers
        return Response(
            content=resp.content,
            media_type=resp.headers.get('content-type', 'application/json'),
            headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'POST, OPTIONS',
                'Access-Control-Allow-Headers': '*',
            }
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared HTTP client for proxying (reuses connections)
_proxy_client: httpx.AsyncClient | None = None


async def get_proxy_client() -> httpx.AsyncClient:
    global _proxy_client
    if _proxy_client is None or _proxy_client.is_closed:
        _proxy_client = httpx.AsyncClient(
            headers=BROWSER_HEADERS,
            follow_redirects=True,
            timeout=30.0,
        )
    return _proxy_client


# SOAP4YOU client/session
soap_client: httpx.AsyncClient | None = None
soap_session_token: Optional[str] = None


async def get_soap_client() -> httpx.AsyncClient:
    global soap_client
    if soap_client is None or soap_client.is_closed:
        soap_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
    return soap_client


async def login_to_soap():
    """Login to soap4youand.me and establish session"""
    global soap_session_token

    if not SOAP_LOGIN or not SOAP_PASSWORD:
        raise RuntimeError("SOAP_LOGIN and SOAP_PASSWORD must be set")

    client = await get_soap_client()

    # Get initial page to establish session
    await client.get("https://soap4youand.me/")

    # Login
    response = await client.post(
        "https://soap4youand.me/login/",
        data={"login": SOAP_LOGIN, "password": SOAP_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    if response.status_code == 200:
        print(f"Logged in to soap4youand.me as {SOAP_LOGIN}")
    else:
        print(f"Login failed: {response.status_code}")


async def ensure_soap_logged_in():
    """Ensure we're logged in, re-login if session expired"""
    client = await get_soap_client()
    response = await client.get("https://soap4youand.me/dashboard/")
    if "login" in str(response.url):
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
    """Parse search results from soap4youand.me HTML."""
    results = []
    items = re.findall(
        r'<div class="search-item[^"]*"[^>]*>(.*?)</div>\s*</div>',
        html, re.DOTALL
    )

    for item in items:
        url_match = re.search(r'href="(/(movies|soap)/([^/]+)/)"', item)
        if not url_match:
            continue

        url, content_type, id_or_slug = url_match.groups()

        poster_match = re.search(r'<img[^>]*src="([^"]+)"', item)
        poster = poster_match.group(1) if poster_match else None
        if poster and not poster.startswith('http'):
            poster = f"https://soap4youand.me{poster}"

        title_match = re.search(
            r'<h5[^>]*>.*?<a[^>]*>([^<]+(?:<span[^>]*>[^<]*</span>[^<]*)*)</a>',
            item, re.DOTALL
        )
        if title_match:
            title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip()
            if ' — ' in title:
                parts = title.split(' — ')
                title = parts[0].strip()
                title_ru = parts[1].strip() if len(parts) > 1 else None
            else:
                title_ru = None
        else:
            title = "Unknown"
            title_ru = None

        year_match = re.search(r'\((\d{4})\)', item)
        year = year_match.group(1) if year_match else None

        results.append({
            "type": "movie" if content_type == "movies" else "series",
            "id": id_or_slug,
            "url": url,
            "title": title,
            "title_ru": title_ru,
            "year": year,
            "poster": poster,
        })

    return results


def encode_url(url: str) -> str:
    """Base64-encode a URL for safe use in path segments."""
    return base64.urlsafe_b64encode(url.encode()).decode()


def decode_url(encoded: str) -> str:
    """Decode a base64-encoded URL."""
    # Add padding if needed
    padding = 4 - len(encoded) % 4
    if padding != 4:
        encoded += '=' * padding
    return base64.urlsafe_b64decode(encoded.encode()).decode()


def rewrite_m3u8(content: str, base_url: str, proxy_base: str, proxy_suffix: str = "") -> str:
    """Rewrite URLs in m3u8 manifest to go through our proxy."""
    lines = content.split('\n')
    result = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#'):
            # This is a URL line (segment or sub-playlist)
            if stripped.startswith('http://') or stripped.startswith('https://'):
                abs_url = stripped
            else:
                abs_url = urljoin(base_url, stripped)
            encoded = encode_url(abs_url)
            result.append(f'{proxy_base}/{encoded}{proxy_suffix}')
        elif stripped.startswith('#EXT-X-MAP:'):
            # Rewrite URI in EXT-X-MAP tags
            def replace_uri(m):
                uri = m.group(1)
                if uri.startswith('http://') or uri.startswith('https://'):
                    abs_url = uri
                else:
                    abs_url = urljoin(base_url, uri)
                encoded = encode_url(abs_url)
                return f'URI="{proxy_base}/{encoded}{proxy_suffix}"'
            result.append(re.sub(r'URI="([^"]+)"', replace_uri, stripped))
        else:
            result.append(line)
    return '\n'.join(result)


@app.on_event("startup")
async def startup():
    """Initialize HDRezka client on startup.

    Uses lazy initialization to avoid Render deployment timeouts.
    Mirror validation happens on first actual request.
    """
    # Fast init - just sets up HTTP client, no blocking network calls
    await initialize()
    if SOAP_LOGIN and SOAP_PASSWORD:
        try:
            await login_to_soap()
        except Exception as e:
            print(f"SOAP login failed: {e}")
    print("alphy backend started successfully")


@app.on_event("shutdown")
async def shutdown():
    global _proxy_client, soap_client
    if _proxy_client and not _proxy_client.is_closed:
        await _proxy_client.aclose()
    if soap_client and not soap_client.is_closed:
        await soap_client.aclose()


@app.get("/api/search")
async def api_search(request: Request, q: str = Query(..., min_length=1)):
    """Search for content."""
    require_admin(request)
    results = await search_content(q)
    return {
        "query": q,
        "results": [
            {
                "url": r.url,
                "title": r.title,
                "type": r.content_type,
                "year": r.year,
                "poster": r.poster
            }
            for r in results
        ]
    }


@app.get("/api/content")
async def api_content(request: Request, url: str = Query(...)):
    """Get content metadata (translations, seasons, episodes)."""
    require_admin(request)
    info = await get_content_info(url)
    if not info:
        raise HTTPException(status_code=404, detail="Content not found")
    return info


@app.get("/api/stream")
async def api_stream(
    request: Request,
    url: str = Query(..., description="Content URL"),
    season: Optional[int] = Query(None, description="Season number (for series)"),
    episode: Optional[int] = Query(None, description="Episode number (for series)"),
    translator_id: Optional[int] = Query(None, description="Translation ID"),
    proxy: bool = Query(False, description="Force proxy mode (for CORS issues)")
):
    """Get stream URL for playback. Returns direct CDN URLs by default."""
    require_admin(request)
    result = await get_stream(
        content_url=url,
        season=season,
        episode=episode,
        translator_id=translator_id
    )

    if not result:
        raise HTTPException(status_code=404, detail="Stream not found")

    # If proxy mode requested, convert URLs to proxy URLs
    if proxy:
        proxy_base = str(request.base_url).rstrip('/') + '/api/proxy'
        admin_token = request.query_params.get("admin_token")
        proxy_suffix = f"?admin={admin_token}" if admin_token else ""

        proxied_all_urls = {}
        for quality, cdn_url in result.all_urls.items():
            encoded = encode_url(cdn_url)
            proxied_all_urls[quality] = f'{proxy_base}/{encoded}{proxy_suffix}'

        encoded_main = encode_url(result.stream_url)
        proxied_stream_url = f'{proxy_base}/{encoded_main}{proxy_suffix}'

        proxied_subtitles = []
        for sub in result.subtitles:
            if sub.get('url'):
                encoded_sub = encode_url(sub['url'])
                proxied_subtitles.append({**sub, 'url': f'{proxy_base}/{encoded_sub}{proxy_suffix}'})
            else:
                proxied_subtitles.append(sub)

        return {
            "stream_url": proxied_stream_url,
            "qualities": result.qualities,
            "subtitles": proxied_subtitles,
            "all_urls": proxied_all_urls
        }

    # Default: return direct CDN URLs (no proxy, no server bandwidth)
    return {
        "stream_url": result.stream_url,
        "qualities": result.qualities,
        "subtitles": result.subtitles,
        "all_urls": result.all_urls
    }


@app.get("/api/proxy/{encoded_url:path}")
async def proxy_stream(encoded_url: str, request: Request):
    """Proxy HLS manifests and segments from CDN."""
    require_admin(request)
    try:
        target_url = decode_url(encoded_url.rstrip('='))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid proxy URL")

    client = await get_proxy_client()

    # Set Referer to the CDN origin so it doesn't block us
    parsed = urlparse(target_url)
    headers = {
        **BROWSER_HEADERS,
        'Referer': f'{parsed.scheme}://{parsed.netloc}/',
        'Origin': f'{parsed.scheme}://{parsed.netloc}',
    }

    try:
        resp = await client.get(target_url, headers=headers)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Upstream timeout")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Proxy error: {e}")

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail="Upstream error")

    content_type = resp.headers.get('content-type', '')

    # If this is an m3u8 manifest, rewrite URLs inside it
    if 'm3u8' in target_url or 'mpegurl' in content_type.lower():
        proxy_base = str(request.base_url).rstrip('/') + '/api/proxy'
        admin_token = request.query_params.get("admin") or request.query_params.get("admin_token")
        proxy_suffix = f"?admin={admin_token}" if admin_token else ""
        body = resp.text
        rewritten = rewrite_m3u8(body, target_url, proxy_base, proxy_suffix)
        return Response(
            content=rewritten,
            # Use x-mpegURL which Video.js handles better across browsers
            media_type='application/x-mpegURL',
            headers={
                'Access-Control-Allow-Origin': '*',
                'Cache-Control': 'no-cache',
            }
        )

    # For .ts segments, .vtt subtitles, etc — stream the raw bytes
    return Response(
        content=resp.content,
        media_type=content_type or 'application/octet-stream',
        headers={
            'Access-Control-Allow-Origin': '*',
            'Cache-Control': 'public, max-age=3600',
        }
    )


# Serve frontend static files
frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.exists(frontend_path):
    app.mount("/static", StaticFiles(directory=frontend_path), name="static")

    @app.get("/")
    async def serve_frontend():
        return FileResponse(
            os.path.join(frontend_path, "soap.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
        )

    @app.get("/hdrezka")
    async def serve_hdrezka_frontend():
        return FileResponse(
            os.path.join(frontend_path, "index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
