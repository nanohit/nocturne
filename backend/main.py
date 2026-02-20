"""FastAPI backend for HDRezka streaming."""
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.responses import Response
from typing import Optional
import json
import uuid
import os
import re
import hashlib
import base64
import time
import difflib
from urllib.parse import urlparse, urljoin, quote, unquote
from collections import defaultdict
from html import unescape
from datetime import datetime, timezone

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
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
TMDB_BEARER_TOKEN = os.getenv("TMDB_BEARER_TOKEN")

app = FastAPI(title="alphy")

LEGACY_LISTS_FILE = os.path.join(os.path.dirname(__file__), "data", "admin_lists.json")
ADMIN_LISTS_FILE_ENV = os.getenv("ADMIN_LISTS_FILE")
ADMIN_LISTS_DIR_ENV = os.getenv("ADMIN_LISTS_DIR")
RENDER_DISK_PATH_ENV = os.getenv("RENDER_DISK_PATH")
DEFAULT_RENDER_DISK_PATH = "/var/data"
_LISTS_STORAGE_READY = False


def _resolve_lists_file() -> str:
    if ADMIN_LISTS_FILE_ENV:
        return ADMIN_LISTS_FILE_ENV
    if ADMIN_LISTS_DIR_ENV:
        return os.path.join(ADMIN_LISTS_DIR_ENV, "admin_lists.json")
    if RENDER_DISK_PATH_ENV:
        return os.path.join(RENDER_DISK_PATH_ENV, "admin_lists.json")
    if os.path.isdir(DEFAULT_RENDER_DISK_PATH):
        return os.path.join(DEFAULT_RENDER_DISK_PATH, "admin_lists.json")
    return LEGACY_LISTS_FILE


LISTS_FILE = _resolve_lists_file()


def _admin_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _empty_admin_payload() -> dict:
    return {
        "lists": [],
        "revision": 0,
        "updated_at": None,
    }


def _normalize_admin_payload(data: object) -> dict:
    if not isinstance(data, dict):
        return _empty_admin_payload()

    lists = data.get("lists")
    normalized_lists = _normalize_admin_lists({"lists": lists}).get("lists", [])

    raw_revision = data.get("revision")
    try:
        revision = int(raw_revision)
    except Exception:
        revision = 1 if normalized_lists else 0
    if revision < 0:
        revision = 0
    if revision == 0 and normalized_lists:
        revision = 1

    updated_at = data.get("updated_at")
    updated_at_value = str(updated_at).strip() if updated_at else None
    if updated_at_value == "":
        updated_at_value = None

    return {
        "lists": normalized_lists,
        "revision": revision,
        "updated_at": updated_at_value,
    }


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


def _load_admin_lists() -> dict:
    _ensure_lists_storage_initialized()
    if not os.path.exists(LISTS_FILE):
        if os.path.exists(LEGACY_LISTS_FILE):
            try:
                with open(LEGACY_LISTS_FILE, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                    normalized = _normalize_admin_payload(data)
                    if normalized.get("lists"):
                        return normalized
            except Exception:
                pass
        return _empty_admin_payload()
    try:
        with open(LISTS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return _normalize_admin_payload(data)
    except Exception:
        pass
    return _empty_admin_payload()


def _save_admin_lists(payload: dict) -> None:
    _ensure_lists_storage_initialized()
    normalized = _normalize_admin_payload(payload)
    os.makedirs(os.path.dirname(LISTS_FILE), exist_ok=True)
    tmp_path = LISTS_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(normalized, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, LISTS_FILE)
    # Keep a mirrored copy in the legacy path for easier recovery/debug.
    if LISTS_FILE != LEGACY_LISTS_FILE:
        os.makedirs(os.path.dirname(LEGACY_LISTS_FILE), exist_ok=True)
        legacy_tmp = LEGACY_LISTS_FILE + ".tmp"
        with open(legacy_tmp, "w", encoding="utf-8") as handle:
            json.dump(normalized, handle, ensure_ascii=False, indent=2)
        os.replace(legacy_tmp, LEGACY_LISTS_FILE)


def _normalize_admin_lists(payload: dict) -> dict:
    lists = payload.get("lists", []) if isinstance(payload, dict) else []
    normalized = []
    for entry in lists:
        if not isinstance(entry, dict):
            continue
        list_id = str(entry.get("id") or uuid.uuid4())
        title = str(entry.get("title") or "").strip() or "Новый список"
        items = []
        for item in entry.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            item_id = item.get("id")
            if not item_type or not item_id:
                continue
            items.append({
                "type": str(item_type),
                "id": str(item_id),
                "title": str(item.get("title") or "").strip(),
                "year": item.get("year"),
                "poster": item.get("poster"),
                "url": item.get("url"),
            })
        normalized.append({
            "id": list_id,
            "title": title,
            "items": items,
        })
    return {"lists": normalized}


def _ensure_lists_storage_initialized() -> None:
    global _LISTS_STORAGE_READY
    if _LISTS_STORAGE_READY:
        return
    _LISTS_STORAGE_READY = True

    if LISTS_FILE == LEGACY_LISTS_FILE:
        return

    os.makedirs(os.path.dirname(LISTS_FILE), exist_ok=True)
    if os.path.exists(LISTS_FILE):
        return

    if os.path.exists(LEGACY_LISTS_FILE):
        try:
            with open(LEGACY_LISTS_FILE, "r", encoding="utf-8") as src:
                data = json.load(src)
            normalized = _normalize_admin_payload(data)
            if normalized.get("lists"):
                tmp_path = LISTS_FILE + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as dst:
                    json.dump(normalized, dst, ensure_ascii=False, indent=2)
                os.replace(tmp_path, LISTS_FILE)
        except Exception:
            pass


@app.get("/health")
async def health_check():
    """Health check endpoint for Render deployment."""
    return {"status": "ok", "service": "alphy"}


@app.get("/api/admin/check")
async def admin_check(request: Request):
    require_admin(request)
    return {"status": "ok"}


@app.get("/api/admin/storage")
async def admin_storage_info(request: Request):
    require_admin(request)
    path = LISTS_FILE
    persistent_dir = os.getenv("ADMIN_LISTS_DIR") or os.getenv("RENDER_DISK_PATH") or DEFAULT_RENDER_DISK_PATH
    is_persistent_target = bool(path and os.path.abspath(path).startswith(os.path.abspath(persistent_dir)))
    return {
        "file": path,
        "persistent_target": is_persistent_target,
    }


@app.get("/api/lists")
async def public_lists():
    return _load_admin_lists()


@app.get("/api/admin/lists")
async def admin_lists(request: Request):
    require_admin(request)
    return _load_admin_lists()


@app.put("/api/admin/lists")
async def update_admin_lists(request: Request):
    require_admin(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")
    current = _load_admin_lists()

    base_revision = payload.get("base_revision")
    if base_revision is not None:
        try:
            base_revision_value = int(base_revision)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="base_revision must be an integer") from exc
        if base_revision_value != current.get("revision", 0):
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Admin lists revision conflict",
                    "current": current,
                },
            )

    normalized_lists = _normalize_admin_lists(payload).get("lists", [])
    next_payload = {
        "lists": normalized_lists,
        "revision": int(current.get("revision", 0)) + 1,
        "updated_at": _admin_now_iso(),
    }
    _save_admin_lists(next_payload)
    return next_payload


@app.post("/api/admin/lists/sync")
async def sync_admin_lists(request: Request):
    """
    Keepalive-friendly sync endpoint.
    Supports query-based admin auth for beacon-style requests.
    """
    require_admin(request, allow_query=True)
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="Missing payload")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    current = _load_admin_lists()
    base_revision = payload.get("base_revision")
    if base_revision is not None:
        try:
            base_revision_value = int(base_revision)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="base_revision must be an integer") from exc
        if base_revision_value != current.get("revision", 0):
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Admin lists revision conflict",
                    "current": current,
                },
            )

    normalized_lists = _normalize_admin_lists(payload).get("lists", [])
    next_payload = {
        "lists": normalized_lists,
        "revision": int(current.get("revision", 0)) + 1,
        "updated_at": _admin_now_iso(),
    }
    _save_admin_lists(next_payload)
    return next_payload


# ==================== SOAP4YOU API ====================
@app.get("/api/soap/search")
async def soap_search(q: str = Query(..., min_length=1)):
    """Search movies and series on soap4youand.me."""
    if not SOAP_LOGIN or not SOAP_PASSWORD:
        raise HTTPException(status_code=500, detail="SOAP credentials not configured")
    await ensure_soap_or_503()
    response = await soap_get("https://soap4youand.me/search/", params={"q": q})
    results = parse_search_results(response.text)
    return {"results": results}


@app.get("/api/soap/meta")
@app.get("/api/soap/meta/")
async def soap_meta(
    url: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    id: Optional[str] = Query(None),
    slug: Optional[str] = Query(None),
):
    """Fetch rating + duration metadata for a soap4youand.me movie or series page."""
    if not SOAP_LOGIN or not SOAP_PASSWORD:
        raise HTTPException(status_code=500, detail="SOAP credentials not configured")

    target_url = None
    if url:
        target_url = _sanitize_soap_page_url(url)
    elif type == "movie" and id:
        target_url = _sanitize_soap_page_url(f"/movies/{id}/")
    elif type in {"series", "soap"} and slug:
        target_url = _sanitize_soap_page_url(f"/soap/{slug}/")

    if not target_url:
        raise HTTPException(status_code=400, detail="Invalid soap page url")

    now = time.time()
    cached = SOAP_META_CACHE.get(target_url)
    if cached and now - cached.get("ts", 0) < SOAP_META_TTL_SECONDS:
        return cached.get("data", {})

    await ensure_soap_or_503()
    try:
        response = await soap_get(target_url)
    except HTTPException:
        if cached:
            return cached.get("data", {})
        raise
    if response.status_code != 200:
        if cached:
            return cached.get("data", {})
        raise HTTPException(status_code=503, detail="SOAP metadata unavailable")
    data = parse_soap_meta(response.text)
    SOAP_META_CACHE[target_url] = {"ts": now, "data": data}
    return data


@app.get("/api/soap/player-meta")
async def soap_player_meta(
    type: str = Query(...),
    id: Optional[str] = Query(None),
    slug: Optional[str] = Query(None),
    url: Optional[str] = Query(None),
    title: Optional[str] = Query(None),
    year: Optional[str] = Query(None),
):
    """Fetch enriched metadata for the desktop player sidebar."""
    if not SOAP_LOGIN or not SOAP_PASSWORD:
        raise HTTPException(status_code=500, detail="SOAP credentials not configured")

    item_type = (type or "").strip().lower()
    if item_type not in {"movie", "series", "soap"}:
        raise HTTPException(status_code=400, detail="Invalid content type")

    cache_key = _build_player_meta_cache_key(item_type, id, slug, url, title, year)
    now = time.time()
    cached = PLAYER_META_CACHE.get(cache_key)
    if cached and now - cached.get("ts", 0) < PLAYER_META_TTL_SECONDS:
        return cached.get("data", {})

    target_url = None
    if url:
        target_url = _sanitize_soap_page_url(url)
    elif item_type == "movie" and id:
        target_url = _sanitize_soap_page_url(f"/movies/{id}/")
    elif item_type in {"series", "soap"}:
        series_slug = slug or id
        if series_slug:
            target_url = _sanitize_soap_page_url(f"/soap/{series_slug}/")

    if not target_url:
        raise HTTPException(status_code=400, detail="Invalid soap page url")

    fallback = {
        "type": "series" if item_type in {"series", "soap"} else "movie",
        "title": (title or "").strip() or None,
        "year": year,
        "duration": None,
        "description": None,
        "cover": None,
        "covers": [],
        "ratings": {
            "imdb": {"value": None, "url": None},
            "kp": {"value": None, "url": None},
            "lbxd": None,
        },
    }

    await ensure_soap_or_503()
    try:
        soap_response = await soap_get(target_url)
    except HTTPException:
        PLAYER_META_CACHE[cache_key] = {"ts": now, "data": fallback}
        return fallback

    if soap_response.status_code != 200:
        PLAYER_META_CACHE[cache_key] = {"ts": now, "data": fallback}
        return fallback

    soap_data = parse_soap_meta(soap_response.text)
    enriched = await enrich_player_meta(
        item_type=item_type,
        soap_data=soap_data,
        title=title,
        year=year,
    )
    PLAYER_META_CACHE[cache_key] = {"ts": now, "data": enriched}
    return enriched


@app.get("/api/soap/movie/{movie_id}")
async def soap_movie(movie_id: str):
    """Get movie details and stream URL."""
    if not SOAP_LOGIN or not SOAP_PASSWORD:
        raise HTTPException(status_code=500, detail="SOAP credentials not configured")
    await ensure_soap_or_503()
    response = await soap_get(f"https://soap4youand.me/movies/{movie_id}/")
    html = response.text

    file_match = re.search(r'file:\s*["\']([^"\']+)["\']', html)
    if not file_match:
        raise HTTPException(status_code=400, detail="Could not find stream URL")
    stream_url = file_match.group(1).replace("\\/", "/")
    stream_url = _normalize_soap_url(stream_url) or stream_url
    stream_type = await detect_stream_type(stream_url)

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
        "stream_type": stream_type,
        "poster": poster,
        "subtitles": subtitles,
    }


@app.get("/api/soap/series/{slug}")
async def soap_series(slug: str):
    """Get series details including seasons."""
    if not SOAP_LOGIN or not SOAP_PASSWORD:
        raise HTTPException(status_code=500, detail="SOAP credentials not configured")
    await ensure_soap_or_503()
    response = await soap_get(f"https://soap4youand.me/soap/{slug}/")
    html = response.text

    title_match = re.search(r'<h1[^>]*>([^<]+)', html)
    title = title_match.group(1).strip() if title_match else slug

    season_matches = re.findall(r'href="/soap/' + re.escape(slug) + r'/(\d+)/"', html)
    seasons = sorted(list(set(int(s) for s in season_matches)))
    if not seasons:
        seasons = [1]

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
    await ensure_soap_or_503()
    response = await soap_get(f"https://soap4youand.me/soap/{slug}/{season}/")
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
    await ensure_soap_or_503()

    if not token:
        raise HTTPException(status_code=400, detail="API token required")

    hash_input = token + eid + sid + hash
    request_hash = hashlib.md5(hash_input.encode()).hexdigest()

    api_response = await soap_post(
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

    stream_url = _normalize_soap_url(data.get("stream")) or data.get("stream")
    stream_type = await detect_stream_type(stream_url)

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
    await ensure_soap_or_503()

    if not src:
        raise HTTPException(status_code=400, detail="Missing subtitle source")

    if src.startswith("/"):
        src = f"https://soap4youand.me{src}"

    parsed = urlparse(src)
    if parsed.scheme not in ("http", "https") or parsed.netloc not in ALLOWED_SUBTITLE_HOSTS:
        raise HTTPException(status_code=400, detail="Invalid subtitle source")

    response = await soap_get(src)
    if response.status_code != 200:
        raise HTTPException(status_code=404, detail="Subtitle not found")

    text = response.text or ""
    if text.lstrip().startswith("WEBVTT"):
        vtt_text = text
    else:
        vtt_text = srt_to_vtt(text)

    return Response(content=vtt_text, media_type="text/vtt")


@app.get("/api/soap/hls")
async def proxy_soap_hls(
    src: str,
    request: Request,
    cdn: Optional[str] = None,
    hevc: Optional[str] = None,
):
    """Proxy only HLS playlists; segments stay on CDN."""
    if not src:
        raise HTTPException(status_code=400, detail="Missing playlist source")

    if cdn:
        print(f"SOAP HLS playlist proxy: cdn={cdn}")

    parsed = urlparse(src)
    allowed_playlist_hosts = {"soap4youand.me", "www.soap4youand.me"}
    if parsed.scheme not in ("http", "https") or (
        parsed.netloc not in allowed_playlist_hosts and
        not parsed.netloc.endswith(ALLOWED_SOAP_CDN_HOST_SUFFIX)
    ):
        raise HTTPException(status_code=400, detail="Invalid playlist source")

    resp = await soap_get(src)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Playlist not found")

    content_type = resp.headers.get("content-type", "")
    body_text = resp.text
    final_url = str(resp.url)
    is_playlist = _is_probably_m3u8(content_type, final_url, body_text)

    if is_playlist:
        prefer_non_hevc = hevc == "0" if hevc in {"0", "1"} else (not _is_safari_user_agent(request))
        if prefer_non_hevc:
            body_text = filter_non_hevc_variants(body_text)
        base_url = final_url.rsplit("/", 1)[0] + "/"
        rewritten = rewrite_soap_m3u8(body_text, base_url, cdn)
        return Response(
            content=rewritten,
            media_type="application/x-mpegURL",
            headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
        )

    return Response(
        content=resp.content,
        media_type=content_type or "application/octet-stream",
        headers={"Access-Control-Allow-Origin": "*"},
    )


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
SOAP_META_CACHE: dict[str, dict] = {}
SOAP_META_TTL_SECONDS = 6 * 60 * 60
PLAYER_META_CACHE: dict[str, dict] = {}
PLAYER_META_TTL_SECONDS = 24 * 60 * 60
PLAYER_META_CACHE_VERSION = "imdb-cover-v1"
_meta_client: httpx.AsyncClient | None = None
SOAP_LAST_CHECK_TS = 0.0
SOAP_CHECK_INTERVAL_SECONDS = 90
SOAP_LOGIN_OK = False
STREAM_TYPE_CACHE: dict[str, dict] = {}
STREAM_TYPE_TTL_SECONDS = 12 * 60 * 60


async def get_soap_client() -> httpx.AsyncClient:
    global soap_client
    if soap_client is None or soap_client.is_closed:
        soap_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/133.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
    return soap_client


async def soap_get(url: str, **kwargs) -> httpx.Response:
    client = await get_soap_client()
    try:
        return await client.get(url, **kwargs)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=503, detail="SOAP upstream timeout") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"SOAP upstream error: {exc}") from exc


async def soap_post(url: str, **kwargs) -> httpx.Response:
    client = await get_soap_client()
    try:
        return await client.post(url, **kwargs)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=503, detail="SOAP upstream timeout") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"SOAP upstream error: {exc}") from exc


async def get_meta_client() -> httpx.AsyncClient:
    global _meta_client
    if _meta_client is None or _meta_client.is_closed:
        _meta_client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=7.0),
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/133.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
    return _meta_client


async def login_to_soap():
    """Login to soap4youand.me and establish session"""
    global soap_session_token, SOAP_LOGIN_OK, SOAP_LAST_CHECK_TS

    if not SOAP_LOGIN or not SOAP_PASSWORD:
        raise RuntimeError("SOAP_LOGIN and SOAP_PASSWORD must be set")

    # Get initial page to establish session
    try:
        await soap_get("https://soap4youand.me/")
    except HTTPException as exc:
        SOAP_LOGIN_OK = False
        raise RuntimeError(f"SOAP bootstrap request failed: {exc.detail}") from exc

    # Login
    try:
        response = await soap_post(
            "https://soap4youand.me/login/",
            data={"login": SOAP_LOGIN, "password": SOAP_PASSWORD},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except HTTPException as exc:
        SOAP_LOGIN_OK = False
        raise RuntimeError(f"SOAP login request failed: {exc.detail}") from exc

    if response.status_code == 200:
        SOAP_LOGIN_OK = True
        SOAP_LAST_CHECK_TS = time.time()
        print(f"Logged in to soap4youand.me as {SOAP_LOGIN}")
        return

    SOAP_LOGIN_OK = False
    raise RuntimeError(f"SOAP login failed: {response.status_code}")


async def ensure_soap_logged_in():
    """Ensure we're logged in, re-login if session expired"""
    global SOAP_LAST_CHECK_TS, SOAP_LOGIN_OK
    now = time.time()
    if SOAP_LOGIN_OK and (now - SOAP_LAST_CHECK_TS) < SOAP_CHECK_INTERVAL_SECONDS:
        return

    client = await get_soap_client()
    try:
        response = await client.get("https://soap4youand.me/dashboard/")
        SOAP_LAST_CHECK_TS = now
    except httpx.HTTPError as exc:
        # Dashboard ping may fail transiently; if we already have a working session,
        # keep using it and let the actual content request decide.
        if SOAP_LOGIN_OK:
            print(f"SOAP dashboard check failed, using existing session: {exc}")
            return
        await login_to_soap()
        return

    if "login" in str(response.url):
        await login_to_soap()
        return

    SOAP_LOGIN_OK = True


async def ensure_soap_or_503():
    try:
        await ensure_soap_logged_in()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"SOAP upstream unavailable: {exc}") from exc


def extract_api_token(html: str) -> Optional[str]:
    """Extract API token from page"""
    match = re.search(r'data:token="([^"]+)"', html)
    return match.group(1) if match else None


ALLOWED_SUBTITLE_HOSTS = {"soap4youand.me", "www.soap4youand.me"}
ALLOWED_SOAP_CDN_HOST_SUFFIX = ".soap4youand.me"


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


def build_hls_proxy_url(src: str, cdn: Optional[str] = None) -> str:
    """Build a same-origin proxy URL for HLS playlist sources."""
    suffix = f"&cdn={quote(cdn, safe='')}" if cdn else ""
    return f"/api/soap/hls?src={quote(src, safe='')}{suffix}"


def _rewrite_cdn_host(url: str, cdn: Optional[str]) -> str:
    if not cdn:
        return url
    cdn = cdn.strip().lower()
    if cdn not in ("cdn-fi", "cdn-r"):
        return url
    return re.sub(
        r'://cdn-(fi|r)(\d+)\.soap4youand\.me',
        lambda m: f"://{cdn}{m.group(2)}.soap4youand.me",
        url
    )


def _is_probably_m3u8(content_type: str, url: str, body_text: str) -> bool:
    lowered_type = (content_type or "").lower()
    if "mpegurl" in lowered_type or "vnd.apple.mpegurl" in lowered_type:
        return True
    lowered_url = (url or "").lower()
    if lowered_url.endswith(".m3u8") or ".m3u8?" in lowered_url:
        return True
    return (body_text or "").lstrip().startswith("#EXTM3U")


def _is_safari_user_agent(request: Request) -> bool:
    ua = request.headers.get("user-agent", "").lower()
    if "safari" not in ua:
        return False
    blocked = ("chrome", "crios", "chromium", "edg", "opr", "firefox", "fxios")
    return not any(token in ua for token in blocked)


def filter_non_hevc_variants(content: str) -> str:
    """
    Remove HEVC-only variants from master playlists on non-Safari browsers.
    If filtering would remove all stream variants, return the original content.
    """
    if "#EXT-X-STREAM-INF" not in content and "#EXT-X-I-FRAME-STREAM-INF" not in content:
        return content

    lines = content.splitlines()
    kept_lines: list[str] = []
    kept_stream_variants = 0
    original_stream_variants = 0
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("#EXT-X-I-FRAME-STREAM-INF"):
            codec_match = re.search(r'CODECS="([^"]+)"', stripped, flags=re.IGNORECASE)
            codecs = codec_match.group(1).lower() if codec_match else ""
            if "hvc1" in codecs or "hev1" in codecs:
                i += 1
                continue
            kept_lines.append(line)
            i += 1
            continue

        if stripped.startswith("#EXT-X-STREAM-INF"):
            original_stream_variants += 1
            codec_match = re.search(r'CODECS="([^"]+)"', stripped, flags=re.IGNORECASE)
            codecs = codec_match.group(1).lower() if codec_match else ""
            drop_variant = "hvc1" in codecs or "hev1" in codecs

            if drop_variant:
                i += 1
                if i < len(lines) and not lines[i].lstrip().startswith("#"):
                    i += 1
                continue

            kept_lines.append(line)
            i += 1
            if i < len(lines):
                kept_lines.append(lines[i])
                if not lines[i].lstrip().startswith("#"):
                    kept_stream_variants += 1
                i += 1
            continue

        kept_lines.append(line)
        i += 1

    if original_stream_variants == 0:
        return content
    if kept_stream_variants == 0:
        return content
    return "\n".join(kept_lines)


def rewrite_soap_m3u8(content: str, base_url: str, cdn: Optional[str] = None) -> str:
    """Rewrite playlist URIs to go through our proxy; keep segments direct."""
    lines = content.splitlines()
    rewritten = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#EXT-X-MEDIA") and "URI=\"" in stripped:
            def replace_uri(match):
                uri = match.group(1)
                if not uri.startswith("http"):
                    uri = urljoin(base_url, uri)
                # Proxy nested playlists
                if ".m3u8" in uri:
                    uri = build_hls_proxy_url(uri, cdn)
                return f'URI="{uri}"'

            rewritten.append(re.sub(r'URI="([^"]+)"', replace_uri, stripped))
            continue

        if stripped and not stripped.startswith("#"):
            if not stripped.startswith("http"):
                stripped = urljoin(base_url, stripped)
            # Proxy nested playlists, leave segments direct
            if ".m3u8" in stripped:
                stripped = build_hls_proxy_url(stripped, cdn)
            else:
                stripped = _rewrite_cdn_host(stripped, cdn)
            rewritten.append(stripped)
        else:
            rewritten.append(line)

    return "\n".join(rewritten)


def _normalize_soap_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    url = url.strip()
    if url.startswith("//"):
        url = f"https:{url}"
    if url.startswith("/"):
        url = f"https://soap4youand.me{url}"
    return url


def _sanitize_soap_page_url(url: Optional[str]) -> Optional[str]:
    normalized = _normalize_soap_url(url)
    if not normalized:
        return None
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc not in {"soap4youand.me", "www.soap4youand.me"}:
        return None
    return normalized


def _extract_rating(text: str, label_pattern: str) -> Optional[str]:
    match = re.search(label_pattern, text, re.IGNORECASE)
    if not match:
        return None
    value = match.group(1).replace(",", ".")
    return value


def _normalize_duration(value: str) -> Optional[str]:
    if not value:
        return None
    hours_match = re.search(r'(\d+)\s*ч', value, re.IGNORECASE)
    mins_match = re.search(r'(\d+)\s*м', value, re.IGNORECASE)
    if hours_match and mins_match:
        return f"{hours_match.group(1)} ч {mins_match.group(1)} м"
    if mins_match:
        return f"{mins_match.group(1)} м"
    return value.strip()


def _normalize_external_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    value = url.strip()
    if value.startswith("//"):
        value = f"https:{value}"
    if value.startswith("/"):
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return None
    return value


def _decode_entities(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = unescape(str(value)).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _extract_soap_title(soup: BeautifulSoup) -> Optional[str]:
    title_node = soup.select_one("h1")
    if title_node:
        text = title_node.get_text(" ", strip=True)
        if text:
            return _decode_entities(text)
    return None


def _extract_soap_description(soup: BeautifulSoup) -> Optional[str]:
    for selector in (
        'meta[property="og:description"]',
        'meta[name="description"]',
    ):
        node = soup.select_one(selector)
        if node and node.get("content"):
            value = node.get("content", "").strip()
            value = re.sub(r"\s+", " ", value)
            if value:
                return _decode_entities(value)

    for selector in (
        ".description",
        ".plot",
        ".movie-description",
        ".movie-info p",
        ".info p",
        ".card-body p",
    ):
        node = soup.select_one(selector)
        if node:
            text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
            if len(text) > 24:
                return _decode_entities(text)
    return None


def _extract_soap_poster(soup: BeautifulSoup) -> Optional[str]:
    meta_og = soup.select_one('meta[property="og:image"]')
    if meta_og and meta_og.get("content"):
        normalized = _normalize_soap_url(meta_og["content"])
        if normalized:
            return normalized

    for selector in (
        "img.poster",
        ".poster img",
        ".movie-poster img",
        ".details-poster img",
        "img[src*='/assets/covers/']",
    ):
        node = soup.select_one(selector)
        if node and node.get("src"):
            normalized = _normalize_soap_url(node.get("src"))
            if normalized:
                return normalized
    return None


def _extract_soap_links(soup: BeautifulSoup) -> tuple[Optional[str], Optional[str]]:
    imdb_url = None
    kp_url = None
    for link in soup.find_all("a", href=True):
        href = _normalize_external_url(link.get("href"))
        if not href:
            continue
        lowered = href.lower()
        if not imdb_url and "imdb.com/title/tt" in lowered:
            imdb_url = href
        if not kp_url and "kinopoisk" in lowered:
            kp_url = href
        if imdb_url and kp_url:
            break
    return imdb_url, kp_url


def parse_soap_meta(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    imdb = _extract_rating(
        text,
        r'рейтинг\s*imdb\s*[:\-]?\s*([0-9]+(?:[.,][0-9]+)?)',
    )
    kp = _extract_rating(
        text,
        r'рейтинг\s*кинопоиск\s*[:\-]?\s*([0-9]+(?:[.,][0-9]+)?)',
    )
    duration_match = re.search(
        r'длительность[^0-9]{0,12}([0-9]+\s*ч\s*[0-9]+\s*м|[0-9]+\s*м)',
        text,
        re.IGNORECASE,
    )
    duration = _normalize_duration(duration_match.group(1)) if duration_match else None
    imdb_url, kp_url = _extract_soap_links(soup)
    soap_description = _extract_soap_description(soup)
    soap_poster = _extract_soap_poster(soup)
    soap_title = _extract_soap_title(soup)

    return {
        "imdb": imdb,
        "kp": kp,
        "duration": duration,
        "imdb_url": imdb_url,
        "kp_url": kp_url,
        "soap_description": soap_description,
        "soap_poster": soap_poster,
        "soap_title": soap_title,
    }


def _safe_float(value: Optional[str | float | int]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ".").strip())
    except Exception:
        return None


def _format_rating(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    return f"{value:.1f}".rstrip("0").rstrip(".") if value % 1 else f"{value:.1f}"


def _build_player_meta_cache_key(
    item_type: str,
    item_id: Optional[str],
    slug: Optional[str],
    item_url: Optional[str],
    title: Optional[str],
    year: Optional[str],
) -> str:
    parts = [
        PLAYER_META_CACHE_VERSION,
        item_type or "",
        item_id or "",
        slug or "",
        item_url or "",
        title or "",
        year or "",
    ]
    joined = "|".join(part.strip() for part in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def _parse_json_ld_objects(html: str) -> list[dict]:
    objects: list[dict] = []
    for match in re.findall(
        r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        html,
        re.DOTALL | re.IGNORECASE,
    ):
        cleaned = re.sub(r"/\*.*?\*/", "", match, flags=re.DOTALL).strip()
        cleaned = cleaned.strip(";\n\r\t ")
        try:
            parsed = json.loads(cleaned)
        except Exception:
            continue
        if isinstance(parsed, dict):
            objects.append(parsed)
        elif isinstance(parsed, list):
            objects.extend(obj for obj in parsed if isinstance(obj, dict))
    return objects


def _imdb_title_id(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    match = re.search(r"(tt\d+)", url, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).lower()


def _normalize_imdb_url(url: Optional[str]) -> Optional[str]:
    title_id = _imdb_title_id(url)
    if not title_id:
        return None
    return f"https://www.imdb.com/title/{title_id}/"


def _extract_ddg_targets(html: str) -> list[str]:
    targets: list[str] = []
    for href in re.findall(r'class="result__a" href="([^"]+)"', html):
        raw = href.replace("&amp;", "&")
        if "uddg=" in raw:
            uddg_part = raw.split("uddg=", 1)[1]
            uddg = uddg_part.split("&", 1)[0]
            target = unquote(uddg)
        else:
            target = raw
        target = _normalize_external_url(target)
        if target:
            targets.append(target)
    return targets


def _normalize_letterboxd_film_url(url: Optional[str]) -> Optional[str]:
    normalized = _normalize_external_url(url)
    if not normalized:
        return None
    parsed = urlparse(normalized)
    if parsed.netloc not in {"letterboxd.com", "www.letterboxd.com"}:
        return None
    match = re.match(r"^/film/([^/?#]+)/?$", parsed.path)
    if not match:
        return None
    slug = match.group(1)
    return f"https://letterboxd.com/film/{slug}/"


def _extract_letterboxd_canonical_url(html: str) -> Optional[str]:
    match = re.search(
        r'<meta property="og:url"\s+content="([^"]+)"',
        html or "",
        re.IGNORECASE,
    )
    if not match:
        return None
    return _normalize_letterboxd_film_url(match.group(1))


def _extract_letterboxd_page_year(html: str) -> Optional[str]:
    match = re.search(r'<small class="number">\s*<a[^>]*>(\d{4})</a>', html or "", re.IGNORECASE)
    if match:
        return match.group(1)
    title_match = re.search(r"<title>.*?\\((\\d{4})\\).*?</title>", html or "", re.IGNORECASE | re.DOTALL)
    if title_match:
        return title_match.group(1)
    return None


def _is_cloudflare_block_page(body: str) -> bool:
    lowered = (body or "").lower()
    return "just a moment" in lowered and "cloudflare" in lowered


def _slugify_for_letterboxd(value: str) -> str:
    text = unescape(value or "").lower()
    text = text.replace("’", "").replace("'", "")
    text = re.sub(r"&", " and ", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text


def _normalize_title_for_match(value: Optional[str]) -> str:
    text = unescape(value or "").lower()
    text = re.sub(r"[\"'’`]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tmdb_headers() -> dict:
    headers = {"Accept": "application/json"}
    if TMDB_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {TMDB_BEARER_TOKEN}"
    return headers


def _tmdb_params(extra: dict | None = None) -> dict:
    params = {"language": "en-US", "include_adult": "false"}
    if TMDB_API_KEY:
        params["api_key"] = TMDB_API_KEY
    if extra:
        params.update(extra)
    return params


async def _tmdb_get(path: str, params: dict | None = None) -> Optional[dict]:
    if not TMDB_API_KEY and not TMDB_BEARER_TOKEN:
        return None
    client = await get_meta_client()
    try:
        response = await client.get(
            f"https://api.themoviedb.org/3{path}",
            params=_tmdb_params(params),
            headers=_tmdb_headers(),
        )
    except Exception:
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except Exception:
        return None


def _tmdb_image_url(path: Optional[str], size: str = "w780") -> Optional[str]:
    if not path:
        return None
    return f"https://image.tmdb.org/t/p/{size}{path}"


def _tmdb_item_year(item: dict, media_type: str) -> Optional[str]:
    if media_type == "movie":
        date = item.get("release_date") or ""
    else:
        date = item.get("first_air_date") or ""
    match = re.match(r"^(\d{4})", str(date))
    return match.group(1) if match else None


async def _find_tmdb_by_imdb_id(imdb_url: Optional[str], tmdb_media_type: str) -> Optional[dict]:
    imdb_id = _imdb_title_id(imdb_url)
    if not imdb_id:
        return None
    payload = await _tmdb_get(f"/find/{imdb_id}", params={"external_source": "imdb_id"})
    if not payload:
        return None
    result_key = "movie_results" if tmdb_media_type == "movie" else "tv_results"
    results = payload.get(result_key) or []
    if not results:
        return None
    results = sorted(
        results,
        key=lambda item: (
            bool(item.get("poster_path")),
            float(item.get("vote_average") or 0.0),
            float(item.get("popularity") or 0.0),
        ),
        reverse=True,
    )
    return results[0]


def _score_tmdb_candidate(item: dict, media_type: str, query_title: str, query_year: Optional[str]) -> float:
    query_norm = _normalize_title_for_match(query_title)
    candidate_title = item.get("title") if media_type == "movie" else item.get("name")
    candidate_norm = _normalize_title_for_match(candidate_title)
    alt_norms = []
    if item.get("original_title"):
        alt_norms.append(_normalize_title_for_match(item.get("original_title")))
    if item.get("original_name"):
        alt_norms.append(_normalize_title_for_match(item.get("original_name")))

    best_ratio = 0.0
    for variant in [candidate_norm] + alt_norms:
        if not variant:
            continue
        ratio = difflib.SequenceMatcher(None, query_norm, variant).ratio()
        if variant == query_norm:
            ratio += 0.25
        elif variant.startswith(query_norm) or query_norm.startswith(variant):
            ratio += 0.12
        best_ratio = max(best_ratio, ratio)

    year_score = 0.0
    if query_year:
        item_year = _tmdb_item_year(item, media_type)
        if item_year:
            try:
                item_year_num = int(item_year)
                query_year_num = int(query_year)
            except ValueError:
                item_year_num = None
                query_year_num = None
            if item_year == query_year:
                year_score = 0.25
            elif (
                item_year_num is not None
                and query_year_num is not None
                and abs(item_year_num - query_year_num) <= 1
            ):
                year_score = 0.1
            else:
                year_score = -0.25

    popularity = min(float(item.get("popularity") or 0.0), 100.0) / 1000.0
    poster_bonus = 0.02 if item.get("poster_path") else -0.1
    return best_ratio + year_score + popularity + poster_bonus


async def fetch_tmdb_meta(
    title: Optional[str],
    year: Optional[str],
    media_type: str,
    imdb_url: Optional[str] = None,
) -> dict:
    clean_title = (title or "").strip()
    if media_type not in {"movie", "series"}:
        return {}
    if not clean_title and not imdb_url:
        return {}
    if not TMDB_API_KEY and not TMDB_BEARER_TOKEN:
        return {}

    year_match = re.search(r"(\d{4})", str(year or ""))
    query_year = year_match.group(1) if year_match else None
    tmdb_media = "movie" if media_type == "movie" else "tv"
    search_path = "/search/movie" if tmdb_media == "movie" else "/search/tv"
    year_param = "year" if media_type == "movie" else "first_air_date_year"
    candidate_queries = []
    if clean_title:
        if query_year:
            candidate_queries.append(_tmdb_params({"query": clean_title, year_param: query_year}))
        candidate_queries.append(_tmdb_params({"query": clean_title}))

    best_item = await _find_tmdb_by_imdb_id(imdb_url, tmdb_media)
    best_score = 999.0 if best_item else -999.0
    if not best_item and candidate_queries:
        client = await get_meta_client()
        for params in candidate_queries:
            # _tmdb_params already adds auth params; request directly to avoid double merge.
            try:
                response = await client.get(
                    f"https://api.themoviedb.org/3{search_path}",
                    params=params,
                    headers=_tmdb_headers(),
                )
            except Exception:
                continue
            if response.status_code != 200:
                continue
            payload = response.json()
            for item in payload.get("results", [])[:12]:
                score = _score_tmdb_candidate(
                    item=item,
                    media_type=tmdb_media,
                    query_title=clean_title,
                    query_year=query_year,
                )
                if score > best_score:
                    best_score = score
                    best_item = item
            if best_item and best_score >= 0.72:
                break

    if not best_item:
        return {}
    if best_score < 0.58:
        return {}

    tmdb_id = best_item.get("id")
    details = await _tmdb_get(f"/{tmdb_media}/{tmdb_id}")
    images = await _tmdb_get(f"/{tmdb_media}/{tmdb_id}/images")

    posters: list[str] = []
    image_items = (images or {}).get("posters") or []
    if image_items:
        sorted_posters = sorted(
            image_items,
            key=lambda p: (
                (p.get("iso_639_1") in (None, "en", "ru")),
                float(p.get("vote_average") or 0.0),
                int(p.get("vote_count") or 0),
                int(p.get("width") or 0),
            ),
            reverse=True,
        )
        for p in sorted_posters[:10]:
            url = _tmdb_image_url(p.get("file_path"), size="w780")
            if url:
                posters.append(url)

    main_cover = _tmdb_image_url((details or {}).get("poster_path"), size="w780") or _tmdb_image_url(best_item.get("poster_path"), size="w780")
    if main_cover:
        posters = [main_cover] + [url for url in posters if url != main_cover]

    year_value = _tmdb_item_year(details or best_item, tmdb_media)
    overview = (details or {}).get("overview") or best_item.get("overview")
    tmdb_title = (details or {}).get("title") or (details or {}).get("name") or best_item.get("title") or best_item.get("name")

    return {
        "title": _decode_entities(tmdb_title),
        "year": year_value,
        "description": _decode_entities(overview),
        "cover": posters[0] if posters else None,
        "covers": posters[:8],
        "tmdb_id": tmdb_id,
    }


async def search_letterboxd_film_url(title: Optional[str], year: Optional[str]) -> Optional[str]:
    cleaned_title = (title or "").strip()
    if not cleaned_title:
        return None

    client = await get_meta_client()
    year_text = str(year).strip() if year else ""
    plain_title = re.sub(r"[\"'’`]", "", cleaned_title)
    title_slug = _slugify_for_letterboxd(cleaned_title)
    year_slug = f"{title_slug}-{year_text}" if title_slug and year_text.isdigit() else None

    # First try deterministic slug candidates.
    for candidate in [year_slug, title_slug]:
        if not candidate:
            continue
        guessed = f"https://letterboxd.com/film/{candidate}/"
        try:
            guess_response = await client.get(guessed)
        except Exception:
            continue
        if guess_response.status_code != 200 or _is_cloudflare_block_page(guess_response.text):
            continue
        canonical = _extract_letterboxd_canonical_url(guess_response.text) or guessed
        parsed_year = _extract_letterboxd_page_year(guess_response.text)
        if year_text and parsed_year and parsed_year != year_text:
            continue
        return canonical

    queries = [
        f'site:letterboxd.com/film "{cleaned_title}" {year_text}'.strip(),
        f'site:letterboxd.com/film "{cleaned_title}"'.strip(),
        f'site:letterboxd.com/film "{plain_title}" {year_text}'.strip(),
        f'site:letterboxd.com/film "{plain_title}"'.strip(),
    ]

    for query in queries:
        try:
            response = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
            )
        except Exception:
            continue
        if response.status_code != 200:
            continue
        candidates = []
        for target in _extract_ddg_targets(response.text):
            normalized = _normalize_letterboxd_film_url(target)
            if normalized:
                score = 0
                parsed = urlparse(normalized)
                slug_path = parsed.path.strip("/").split("/")[-1]
                if year_text and slug_path.endswith(f"-{year_text}"):
                    score += 6
                if title_slug and slug_path == title_slug:
                    score += 3
                if title_slug and slug_path.startswith(f"{title_slug}-"):
                    score += 2
                candidates.append((score, normalized))
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            return candidates[0][1]

    return None


def _pick_letterboxd_poster(html: str) -> Optional[str]:
    poster_urls = re.findall(
        r'(https://a\.ltrbxd\.com/resized/film-poster/[^"\']+\.(?:jpg|jpeg|png)[^"\']*)',
        html or "",
        re.IGNORECASE,
    )
    if poster_urls:
        best = None
        best_height = -1
        for candidate in poster_urls:
            size_match = re.search(r"-0-(\d+)-0-(\d+)-crop", candidate)
            if size_match:
                try:
                    height = int(size_match.group(2))
                except Exception:
                    height = 0
            else:
                height = 0
            if height > best_height:
                best_height = height
                best = candidate
        if best:
            return re.sub(r"-0-\d+-0-\d+-crop", "-0-1000-0-1500-crop", best)

    # Only use og:image if it still points to a film poster asset.
    og_image_match = re.search(r'<meta property="og:image"\s+content="([^"]+)"', html or "", re.IGNORECASE)
    if og_image_match:
        og = _normalize_external_url(og_image_match.group(1))
        if og and "/film-poster/" in og:
            return og
    return None


async def fetch_letterboxd_meta(title: Optional[str], year: Optional[str]) -> dict:
    film_url = await search_letterboxd_film_url(title, year)
    if not film_url:
        return {}

    client = await get_meta_client()
    try:
        response = await client.get(film_url)
    except Exception:
        return {}

    if response.status_code != 200:
        return {}
    if _is_cloudflare_block_page(response.text):
        return {}

    json_ld = _parse_json_ld_objects(response.text)
    description = None
    lbxd_rating = None
    for obj in json_ld:
        if not description and obj.get("description"):
            description = _decode_entities(str(obj.get("description")).strip())
        aggregate = obj.get("aggregateRating")
        if isinstance(aggregate, dict) and aggregate.get("ratingValue") is not None:
            lbxd_rating = _safe_float(aggregate.get("ratingValue"))
            if lbxd_rating is not None:
                break

    if not description:
        meta_desc_match = re.search(
            r'<meta name="description"\s+content="([^"]+)"',
            response.text,
            re.IGNORECASE,
        )
        if meta_desc_match:
            description = _decode_entities(meta_desc_match.group(1).strip())

    poster = _pick_letterboxd_poster(response.text)
    rating_x10 = lbxd_rating * 2 if lbxd_rating is not None else None

    return {
        "url": film_url,
        "rating": _format_rating(rating_x10),
        "description": _decode_entities(description),
        "poster": poster,
    }


async def fetch_imdb_mobile_meta(imdb_url: Optional[str]) -> dict:
    normalized = _normalize_imdb_url(imdb_url)
    if not normalized:
        return {}

    title_id = _imdb_title_id(normalized)
    if not title_id:
        return {}

    mobile_url = f"https://m.imdb.com/title/{title_id}/"
    client = await get_meta_client()
    try:
        response = await client.get(mobile_url)
    except Exception:
        return {}

    if response.status_code != 200 or not response.text:
        return {}

    description = None
    poster = None
    rating = None
    for obj in _parse_json_ld_objects(response.text):
        if not description and obj.get("description"):
            description = _decode_entities(str(obj.get("description")).strip())
        if not poster and obj.get("image"):
            image_value = obj.get("image")
            if isinstance(image_value, str):
                poster = image_value
            elif isinstance(image_value, dict) and image_value.get("url"):
                poster = str(image_value.get("url"))
        aggregate = obj.get("aggregateRating")
        if isinstance(aggregate, dict):
            rating_value = _safe_float(aggregate.get("ratingValue"))
            if rating_value is not None:
                rating = _format_rating(rating_value)

    if not poster:
        og_image_match = re.search(r'<meta property="og:image"\s+content="([^"]+)"', response.text, re.IGNORECASE)
        if og_image_match:
            poster = _normalize_external_url(og_image_match.group(1))

    return {
        "description": _decode_entities(description),
        "poster": _normalize_external_url(poster),
        "rating": rating,
        "url": normalized,
    }


async def detect_stream_type(stream_url: Optional[str]) -> str:
    if not stream_url:
        return "hls"

    now = time.time()
    cached = STREAM_TYPE_CACHE.get(stream_url)
    if cached and (now - cached.get("ts", 0) < STREAM_TYPE_TTL_SECONDS):
        return cached.get("type", "mp4")

    lowered = stream_url.lower()
    if lowered.endswith(".m3u8") or ".m3u8?" in lowered or "/hls/" in lowered:
        STREAM_TYPE_CACHE[stream_url] = {"ts": now, "type": "hls"}
        return "hls"
    if lowered.endswith(".mp4"):
        STREAM_TYPE_CACHE[stream_url] = {"ts": now, "type": "mp4"}
        return "mp4"

    client = await get_soap_client()
    headers = {"Range": "bytes=0-511"}
    try:
        async with client.stream("GET", stream_url, headers=headers) as response:
            content_type = (response.headers.get("content-type") or "").lower()
            sample = b""
            async for chunk in response.aiter_bytes():
                if chunk:
                    sample += chunk
                if len(sample) >= 512:
                    break

        if "mpegurl" in content_type or "vnd.apple.mpegurl" in content_type:
            STREAM_TYPE_CACHE[stream_url] = {"ts": now, "type": "hls"}
            return "hls"
        if "video/mp4" in content_type or "application/mp4" in content_type:
            STREAM_TYPE_CACHE[stream_url] = {"ts": now, "type": "mp4"}
            return "mp4"
        if sample.lstrip().startswith(b"#EXTM3U"):
            STREAM_TYPE_CACHE[stream_url] = {"ts": now, "type": "hls"}
            return "hls"
        if b"ftyp" in sample[:128]:
            STREAM_TYPE_CACHE[stream_url] = {"ts": now, "type": "mp4"}
            return "mp4"
    except Exception:
        pass

    # Most non-manifest SOAP URLs without explicit .m3u8 are progressive files.
    STREAM_TYPE_CACHE[stream_url] = {"ts": now, "type": "mp4"}
    return "mp4"


async def enrich_player_meta(
    item_type: str,
    soap_data: dict,
    title: Optional[str],
    year: Optional[str],
) -> dict:
    imdb_value = soap_data.get("imdb")
    kp_value = soap_data.get("kp")
    imdb_url = _normalize_imdb_url(soap_data.get("imdb_url"))
    kp_url = _normalize_external_url(soap_data.get("kp_url"))
    base_title = (title or soap_data.get("soap_title") or "").strip()

    payload = {
        "type": "series" if item_type in {"series", "soap"} else "movie",
        "title": base_title or None,
        "year": year,
        "duration": soap_data.get("duration"),
        "description": soap_data.get("soap_description"),
        "cover": soap_data.get("soap_poster"),
        "covers": [soap_data.get("soap_poster")] if soap_data.get("soap_poster") else [],
        "ratings": {
            "imdb": {"value": imdb_value, "url": imdb_url},
            "kp": {"value": kp_value, "url": kp_url},
            "lbxd": None,
        },
    }

    imdb_meta = {}
    if imdb_url:
        imdb_meta = await fetch_imdb_mobile_meta(imdb_url)
        imdb_poster = imdb_meta.get("poster")
        if imdb_poster:
            # Force IMDb cover for RU compatibility.
            payload["cover"] = imdb_poster
            payload["covers"] = [imdb_poster]

    if payload["type"] == "movie":
        lbxd_meta = await fetch_letterboxd_meta(base_title, year)
        if lbxd_meta:
            payload["ratings"]["lbxd"] = {
                "value": lbxd_meta.get("rating"),
                "url": lbxd_meta.get("url"),
            }
            if lbxd_meta.get("description"):
                payload["description"] = lbxd_meta.get("description")

        if imdb_meta.get("description") and not payload.get("description"):
            payload["description"] = imdb_meta.get("description")
        if imdb_meta.get("rating") and not payload["ratings"]["imdb"]["value"]:
            payload["ratings"]["imdb"]["value"] = imdb_meta.get("rating")
    else:
        if imdb_meta.get("description"):
            payload["description"] = imdb_meta.get("description")
        if imdb_meta.get("rating") and not payload["ratings"]["imdb"]["value"]:
            payload["ratings"]["imdb"]["value"] = imdb_meta.get("rating")

    if payload.get("cover") and not payload.get("covers"):
        payload["covers"] = [payload["cover"]]

    return payload


def _pick_best_srcset(srcset: str) -> Optional[str]:
    if not srcset:
        return None
    candidates = []
    for part in srcset.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        url = bits[0]
        score = 0
        if len(bits) > 1:
            descriptor = bits[1].strip()
            try:
                if descriptor.endswith("w"):
                    score = int(re.sub(r"[^0-9]", "", descriptor))
                elif descriptor.endswith("x"):
                    score = float(descriptor[:-1]) * 1000
            except ValueError:
                score = 0
        candidates.append((score, url))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1]


def _extract_best_poster(item_html: str) -> Optional[str]:
    for attr in ("data-srcset", "srcset"):
        match = re.search(rf'{attr}=["\']([^"\']+)["\']', item_html)
        if match:
            best = _pick_best_srcset(match.group(1))
            if best:
                return _normalize_soap_url(best)

    for attr in ("data-src", "data-original", "data-lazy", "data-image", "data-img", "data-poster"):
        match = re.search(rf'{attr}=["\']([^"\']+)["\']', item_html)
        if match:
            return _normalize_soap_url(match.group(1))

    match = re.search(r'<img[^>]*src=["\']([^"\']+)["\']', item_html)
    if match:
        return _normalize_soap_url(match.group(1))
    return None


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

        poster = _extract_best_poster(item)

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
    _ensure_lists_storage_initialized()
    print(f"Admin lists storage file: {LISTS_FILE}")
    if LISTS_FILE == LEGACY_LISTS_FILE:
        print("WARNING: Admin lists are using legacy project storage. Configure ADMIN_LISTS_DIR to a persistent disk path for production durability.")
    if SOAP_LOGIN and SOAP_PASSWORD:
        print("SOAP credentials detected; session login will happen on first SOAP request")
    print("alphy backend started successfully")


@app.on_event("shutdown")
async def shutdown():
    global _proxy_client, soap_client, _meta_client
    if _proxy_client and not _proxy_client.is_closed:
        await _proxy_client.aclose()
    if soap_client and not soap_client.is_closed:
        await soap_client.aclose()
    if _meta_client and not _meta_client.is_closed:
        await _meta_client.aclose()


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
