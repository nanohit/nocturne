"""FastAPI backend for HDRezka streaming."""
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.responses import Response
from typing import Optional
import os

from backend.services.extractor import (
    search_content,
    get_stream,
    get_content_info,
    initialize
)

app = FastAPI(title="nocturne")

# CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    """Initialize HDRezka client on startup."""
    await initialize()


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
    url: str = Query(..., description="Content URL"),
    season: Optional[int] = Query(None, description="Season number (for series)"),
    episode: Optional[int] = Query(None, description="Episode number (for series)"),
    translator_id: Optional[int] = Query(None, description="Translation ID")
):
    """Get stream URL for playback."""
    result = await get_stream(
        content_url=url,
        season=season,
        episode=episode,
        translator_id=translator_id
    )

    if not result:
        raise HTTPException(status_code=404, detail="Stream not found")

    return {
        "stream_url": result.stream_url,
        "qualities": result.qualities,
        "subtitles": result.subtitles,
        "all_urls": result.all_urls
    }


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
