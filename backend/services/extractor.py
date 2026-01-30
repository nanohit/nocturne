"""HDRezka stream extraction service using hdrezka library (v4.0.3+).

Requires Python 3.10+ and hdrezka>=4.0.0
"""
import asyncio
import random
from typing import Optional
from dataclasses import dataclass

import httpx
from hdrezka import Search
from hdrezka.url import Request
from hdrezka.post.page import Page
from hdrezka.post.inline import InlineInfo
import hdrezka.api.http as hdrezka_http

from backend.config import HDREZKA_MIRROR
from backend.services.cache import cache


# Monkey-patch Page._inline_info to handle entries with missing fields
# The library expects exactly 3 comma-separated values (year, country, genre)
# but some HDRezka entries have fewer fields
@staticmethod
def _patched_inline_info(*args):
    # Pad with empty strings if fewer than 3 fields
    padded = list(args) + [''] * (3 - len(args))
    years, country, genre = padded[0], padded[1], padded[2]
    year, *finals = years.split('-')
    try:
        year_int = int(year.strip())
    except (ValueError, AttributeError):
        year_int = 0
    if finals:
        final = finals[0]
        year_final = ... if final.strip() == '...' else int(final)
    else:
        year_final = None
    return InlineInfo(year_int, year_final, country.strip(), genre.strip())

Page._inline_info = _patched_inline_info


# User agents pool to rotate
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
]

# Browser-like headers to avoid being blocked
BROWSER_HEADERS = {
    'User-Agent': random.choice(USER_AGENTS),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,ru;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
}


@dataclass
class StreamResult:
    stream_url: str
    qualities: list[str]
    subtitles: list[dict]
    all_urls: dict  # quality -> url mapping


@dataclass
class SearchResult:
    url: str
    title: str
    content_type: str  # 'movie' or 'series'
    year: Optional[str]
    poster: Optional[str]


_initialized = False


# Fallback mirrors in order of preference (no login required, no geo-block)
# Note: hdrezka.club uses voidboost.cc CDN which returns 404 - avoid it
FALLBACK_MIRRORS = [
    'https://hdrezka.me/',
    'https://hdrezka.kim/',
    'https://hdrezka.ag/',
]


async def initialize():
    """Initialize the HDRezka client with mirror (no login needed).

    Uses fast initialization - just sets up the client and mirror without
    blocking health checks. Mirror validation happens on first actual request.
    """
    global _initialized
    if _initialized:
        return

    # Create a custom HTTP client with browser-like headers
    custom_client = httpx.AsyncClient(
        headers=BROWSER_HEADERS,
        follow_redirects=True,
        timeout=30.0,
    )
    # Replace the default client used by hdrezka library
    hdrezka_http.DEFAULT_CLIENT = custom_client
    print("Configured custom HTTP client with browser headers")

    # Set the mirror directly without blocking health checks
    # This prevents Render deployment timeouts
    mirrors_to_try = []
    if HDREZKA_MIRROR:
        mirrors_to_try.append(HDREZKA_MIRROR)
    mirrors_to_try.extend([m for m in FALLBACK_MIRRORS if m not in mirrors_to_try])

    # Just use the first mirror - validation happens on actual requests
    Request.HOST = mirrors_to_try[0]
    print(f"Using mirror: {mirrors_to_try[0]} (lazy validation)")

    _initialized = True


async def try_mirror_fallback():
    """Try fallback mirrors if current one fails. Called on request errors."""
    current = Request.HOST
    mirrors_to_try = []
    if HDREZKA_MIRROR:
        mirrors_to_try.append(HDREZKA_MIRROR)
    mirrors_to_try.extend([m for m in FALLBACK_MIRRORS if m not in mirrors_to_try])

    # Find next mirror after current
    try:
        idx = mirrors_to_try.index(current)
        next_mirrors = mirrors_to_try[idx + 1:]
    except ValueError:
        next_mirrors = mirrors_to_try

    for mirror in next_mirrors:
        try:
            resp = await hdrezka_http.DEFAULT_CLIENT.get(mirror, timeout=5.0)
            if resp.status_code == 200:
                Request.HOST = mirror
                print(f"Switched to mirror: {mirror}")
                return True
        except Exception as e:
            print(f"Mirror {mirror} failed: {e}")
            continue

    return False


async def search_content(query: str) -> list[SearchResult]:
    """Search for content on HDRezka."""
    await initialize()

    cache_key = f"search:{query}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    # Try search with mirror fallback on failure
    for attempt in range(2):  # Try current mirror, then fallback once
        try:
            # Debug: test search URL directly first
            search_url = f"{Request.HOST}search/?do=search&subaction=search&q={query}"
            debug_resp = await hdrezka_http.DEFAULT_CLIENT.get(search_url, timeout=10.0)
            has_results = 'b-content__inline_item' in debug_resp.text
            print(f"DEBUG search: {search_url}")
            print(f"DEBUG status: {debug_resp.status_code}, has_results: {has_results}, length: {len(debug_resp.text)}")

            if not has_results and attempt == 0:
                # No results - might be mirror issue, try fallback
                print(f"DEBUG no results, trying fallback mirror...")
                if await try_mirror_fallback():
                    continue  # Retry with new mirror

            search = Search(query)
            results_page = await search.get_page(1)

            results = []
            for item in results_page:
                # hdrezka 4.x uses 'name' instead of 'title', 'info' for metadata
                title = getattr(item, 'name', '') or getattr(item, 'title', '')
                poster = getattr(item, 'poster', None)

                # Determine content type from URL or attributes
                url_str = str(item.url)
                if '/series/' in url_str:
                    content_type = 'series'
                elif '/films/' in url_str:
                    content_type = 'movie'
                else:
                    content_type = 'movie'

                # Extract year from info if available
                year = None
                if hasattr(item, 'info') and item.info:
                    info = str(item.info)
                    # Try to find year pattern
                    import re
                    year_match = re.search(r'(\d{4})', info)
                    if year_match:
                        year = year_match.group(1)

                results.append(SearchResult(
                    url=url_str,
                    title=title,
                    content_type=content_type,
                    year=year,
                    poster=str(poster) if poster else None
                ))

            # Cache search results for 24 hours
            cache.set(cache_key, results, ttl_seconds=86400)
            return results

        except Exception as e:
            print(f"Search error (attempt {attempt + 1}/2): {e}")
            if attempt == 0:
                # Try fallback mirror on first failure
                if await try_mirror_fallback():
                    continue
            import traceback
            traceback.print_exc()
            return []

    return []  # All attempts failed


async def get_stream(
    content_url: str,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    translator_id: Optional[int] = None
) -> Optional[StreamResult]:
    """Extract stream URL for content.

    Uses Player(url) directly - no re-searching needed.
    Returns stream URLs in HLS format (.m3u8) from HDRezka CDN.
    URLs typically expire after ~24 hours.
    """
    await initialize()

    from hdrezka import Player

    # Build cache key
    cache_key = f"stream:{content_url}:{season}:{episode}:{translator_id}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    last_error = None
    for attempt in range(3):
        try:
            # Create player directly from the content URL
            player = await Player(content_url)

            # Get translator ID from post.translators if not provided
            tid = translator_id
            if tid is None and hasattr(player, 'post'):
                post = player.post
                if hasattr(post, 'translators') and hasattr(post.translators, 'name_id'):
                    trans_dict = post.translators.name_id
                    if trans_dict:
                        tid = list(trans_dict.values())[0]

            # Get stream based on content type
            if season is not None and episode is not None:
                stream = await player.get_stream(season, episode, tid)
            else:
                stream = await player.get_stream(translator_id=tid)

            # Extract video URLs
            video = stream.video
            qualities = list(video.qualities) if hasattr(video, 'qualities') else []

            # Get the best quality URL (last_url returns tuple of CDN mirrors)
            url_tuple = video.last_url
            if isinstance(url_tuple, tuple):
                stream_url = str(url_tuple[0])  # Use first CDN
            else:
                stream_url = str(url_tuple)

            # Build quality -> URL mapping from raw_data
            all_urls = {}
            if hasattr(video, 'raw_data') and isinstance(video.raw_data, dict):
                for quality, url_data in video.raw_data.items():
                    if isinstance(url_data, tuple):
                        all_urls[quality] = str(url_data[0])
                    else:
                        all_urls[quality] = str(url_data)

            # Get subtitles (SubtitleURLs object)
            subtitles = []
            if hasattr(stream, 'subtitles') and stream.subtitles:
                subs = stream.subtitles
                if hasattr(subs, 'subtitle_codes'):
                    for code, sub in subs.subtitle_codes.items():
                        if hasattr(sub, 'url') and sub.url:
                            subtitles.append({
                                "lang": code,
                                "name": getattr(sub, 'name', code),
                                "url": str(sub.url)
                            })

            result = StreamResult(
                stream_url=stream_url,
                qualities=qualities,
                subtitles=subtitles,
                all_urls=all_urls
            )

            # Debug: log stream URL details
            print(f"DEBUG stream_url: {stream_url}")
            print(f"DEBUG qualities: {qualities}")
            print(f"DEBUG all_urls: {all_urls}")

            # Cache stream URLs for 1 hour (tokens expire after ~24h but cache shorter)
            cache.set(cache_key, result, ttl_seconds=3600)
            return result

        except (UnicodeDecodeError, UnicodeEncodeError) as e:
            # ASCII decode error in hdrezka deobfuscation — retry
            last_error = e
            print(f"Stream decode error (attempt {attempt + 1}/3): {e}")
            await asyncio.sleep(1)

        except Exception as e:
            print(f"Stream extraction error: {e}")
            import traceback
            traceback.print_exc()
            return None

    print(f"Stream extraction failed after 3 attempts: {last_error}")
    return None


async def get_content_info(content_url: str) -> Optional[dict]:
    """Get metadata about content (seasons, episodes, translations).

    Uses Player(url) directly - no re-searching needed.
    """
    await initialize()

    from hdrezka import Player, PlayerSeries

    cache_key = f"info:{content_url}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    try:
        player = await Player(content_url)

        # Get translations from post.translators
        trans_list = []
        if hasattr(player, 'post') and hasattr(player.post, 'translators'):
            translators = player.post.translators
            if hasattr(translators, 'name_id'):
                for name, tid in translators.name_id.items():
                    trans_list.append({"id": tid, "name": name})

        # Determine if series based on player type
        is_series = isinstance(player, PlayerSeries)

        # Get seasons/episodes for series
        seasons = {}
        if is_series:
            try:
                episodes_data = await player.get_episodes()
                # episodes_data varies by library version; inspect it
                if isinstance(episodes_data, dict):
                    seasons = {
                        str(k): list(v.keys()) if isinstance(v, dict) else list(v)
                        for k, v in episodes_data.items()
                    }
            except Exception as e:
                print(f"Could not get episodes: {e}")

        # Get title and content_id from post
        title = ''
        content_id = None
        if hasattr(player, 'post'):
            title = getattr(player.post, 'title', '') or getattr(player.post, 'name', '')
            # The post object has an 'id' attribute with the content ID
            content_id = getattr(player.post, 'id', None)

        info = {
            "url": content_url,
            "title": title,
            "content_id": content_id,  # Numeric ID for AJAX calls
            "translations": trans_list,
            "seasons": seasons,
            "is_series": is_series
        }

        # Cache for 7 days
        cache.set(cache_key, info, ttl_seconds=604800)
        return info

    except Exception as e:
        print(f"Content info error: {e}")
        import traceback
        traceback.print_exc()
        return None
