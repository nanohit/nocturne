"""FastAPI backend for HDRezka streaming."""
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.responses import Response
from typing import Optional
import os
import re
import base64
from urllib.parse import urlparse, urljoin

import httpx

from backend.services.extractor import (
    search_content,
    get_stream,
    get_content_info,
    initialize,
    BROWSER_HEADERS,
)

app = FastAPI(title="alphy")

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


def rewrite_m3u8(content: str, base_url: str, proxy_base: str) -> str:
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
            result.append(f'{proxy_base}/{encoded}')
        elif stripped.startswith('#EXT-X-MAP:'):
            # Rewrite URI in EXT-X-MAP tags
            def replace_uri(m):
                uri = m.group(1)
                if uri.startswith('http://') or uri.startswith('https://'):
                    abs_url = uri
                else:
                    abs_url = urljoin(base_url, uri)
                encoded = encode_url(abs_url)
                return f'URI="{proxy_base}/{encoded}"'
            result.append(re.sub(r'URI="([^"]+)"', replace_uri, stripped))
        else:
            result.append(line)
    return '\n'.join(result)


@app.on_event("startup")
async def startup():
    """Initialize HDRezka client on startup."""
    await initialize()


@app.on_event("shutdown")
async def shutdown():
    global _proxy_client
    if _proxy_client and not _proxy_client.is_closed:
        await _proxy_client.aclose()


@app.get("/api/search")
async def api_search(q: str = Query(..., min_length=1)):
    """Search for content."""
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
async def api_content(url: str = Query(...)):
    """Get content metadata (translations, seasons, episodes)."""
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

        proxied_all_urls = {}
        for quality, cdn_url in result.all_urls.items():
            encoded = encode_url(cdn_url)
            proxied_all_urls[quality] = f'{proxy_base}/{encoded}'

        encoded_main = encode_url(result.stream_url)
        proxied_stream_url = f'{proxy_base}/{encoded_main}'

        proxied_subtitles = []
        for sub in result.subtitles:
            if sub.get('url'):
                encoded_sub = encode_url(sub['url'])
                proxied_subtitles.append({**sub, 'url': f'{proxy_base}/{encoded_sub}'})
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
        body = resp.text
        rewritten = rewrite_m3u8(body, target_url, proxy_base)
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
            os.path.join(frontend_path, "index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
