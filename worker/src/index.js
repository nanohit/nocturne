/**
 * Cloudflare Worker for HDRezka stream extraction
 *
 * This worker runs on Cloudflare's edge network, so the stream tokens
 * are bound to CF's edge IP which is geographically close to the user.
 * This allows direct CDN streaming without proxy bandwidth.
 */

const HDREZKA_MIRROR = 'https://hdrezka.me';

// Browser-like headers
const BROWSER_HEADERS = {
  'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
  'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
  'Accept-Language': 'en-US,en;q=0.9,ru;q=0.8',
};

// Deobfuscation regex pattern (from hdrezka library)
const TRASH_PATTERN = /^#h|\/\/_\/\/|(?:I[01UV]|[JQ][EF]|X[kl])(?:[A4][=hjk]|[B5][Ae])|(?:I[Sy]|[JQ]C|Xi)(?:[EMQ][=hjk]|[FNR][Ae])/g;

/**
 * Decode base64 and remove trash from HDRezka obfuscated string
 */
function clearTrash(trashString) {
  const cleaned = trashString.replace(TRASH_PATTERN, '');
  // Add padding if needed
  const padded = cleaned + '==';
  try {
    return atob(padded);
  } catch (e) {
    // Try without extra padding
    try {
      return atob(cleaned);
    } catch (e2) {
      console.error('Deobfuscation failed:', e2);
      return '';
    }
  }
}

/**
 * Parse video URLs from deobfuscated string
 */
function parseVideoUrls(data) {
  const decoded = clearTrash(data);
  const urls = {};

  // Format: [quality]url1 or url2,...
  const parts = decoded.split(',');
  for (const part of parts) {
    const match = part.match(/^\[([^\]]+)\](.+)$/);
    if (match) {
      const quality = match[1];
      const urlPart = match[2];
      // Get the first .m3u8 URL
      const urlOptions = urlPart.split(' or ');
      for (const url of urlOptions) {
        if (url.endsWith('.m3u8')) {
          urls[quality] = url;
          break;
        }
      }
    }
  }

  return urls;
}

/**
 * Parse subtitle URLs from response
 */
function parseSubtitles(subtitleData, subtitleLns) {
  const subtitles = [];
  if (subtitleData && subtitleLns) {
    for (const [code, name] of Object.entries(subtitleLns)) {
      if (code !== 'off' && subtitleData) {
        // Subtitle URL pattern varies, try to extract
        subtitles.push({
          lang: code,
          name: name,
          url: subtitleData // This might need adjustment based on actual format
        });
      }
    }
  }
  return subtitles;
}

/**
 * Get content ID and translator ID from page
 */
async function getContentInfo(url) {
  const response = await fetch(url, {
    headers: BROWSER_HEADERS
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch page: ${response.status}`);
  }

  const html = await response.text();

  // Extract content ID
  const idMatch = html.match(/data-id="(\d+)"/);
  const contentId = idMatch ? idMatch[1] : null;

  // Extract translator ID (first one)
  const translatorMatch = html.match(/data-translator_id="(\d+)"/);
  const translatorId = translatorMatch ? translatorMatch[1] : null;

  // Determine content type
  const isSeries = html.includes('sof.tv') || html.includes('data-season_id') || url.includes('/series/');

  // Extract available translators
  const translators = [];
  const transRegex = /data-translator_id="(\d+)"[^>]*>([^<]+)</g;
  let match;
  while ((match = transRegex.exec(html)) !== null) {
    translators.push({ id: parseInt(match[1]), name: match[2].trim() });
  }

  // Extract seasons/episodes for series
  const seasons = {};
  if (isSeries) {
    const seasonRegex = /data-season_id="(\d+)"/g;
    const episodeRegex = /data-season_id="(\d+)"[^>]*data-episode_id="(\d+)"/g;

    // This is simplified - actual parsing would be more complex
    let epMatch;
    while ((epMatch = episodeRegex.exec(html)) !== null) {
      const seasonId = epMatch[1];
      const episodeId = parseInt(epMatch[2]);
      if (!seasons[seasonId]) {
        seasons[seasonId] = [];
      }
      if (!seasons[seasonId].includes(episodeId)) {
        seasons[seasonId].push(episodeId);
      }
    }
  }

  return {
    id: contentId,
    translatorId: translatorId,
    isSeries: isSeries,
    translators: translators,
    seasons: seasons
  };
}

/**
 * Get stream URLs from HDRezka AJAX API
 */
async function getStream(contentId, translatorId, season = null, episode = null) {
  const formData = new URLSearchParams();
  formData.append('id', contentId);
  formData.append('translator_id', translatorId);

  if (season !== null && episode !== null) {
    formData.append('action', 'get_stream');
    formData.append('season', season);
    formData.append('episode', episode);
  } else {
    formData.append('action', 'get_movie');
  }

  const response = await fetch(`${HDREZKA_MIRROR}/ajax/get_cdn_series/`, {
    method: 'POST',
    headers: {
      ...BROWSER_HEADERS,
      'Content-Type': 'application/x-www-form-urlencoded',
      'X-Requested-With': 'XMLHttpRequest',
      'Origin': HDREZKA_MIRROR,
      'Referer': HDREZKA_MIRROR + '/',
    },
    body: formData.toString()
  });

  if (!response.ok) {
    throw new Error(`AJAX request failed: ${response.status}`);
  }

  const data = await response.json();

  if (!data.success && data.success !== undefined) {
    throw new Error(data.message || 'AJAX request failed');
  }

  // Parse the obfuscated URL data
  const videoUrls = data.url ? parseVideoUrls(data.url) : {};
  const subtitles = parseSubtitles(data.subtitle, data.subtitle_lns);

  // Get the best quality URL
  const qualities = Object.keys(videoUrls).sort((a, b) => {
    const aNum = parseInt(a) || 0;
    const bNum = parseInt(b) || 0;
    return aNum - bNum;
  });

  const bestQuality = qualities[qualities.length - 1];
  const streamUrl = videoUrls[bestQuality] || '';

  return {
    stream_url: streamUrl,
    qualities: qualities,
    subtitles: subtitles,
    all_urls: videoUrls
  };
}

/**
 * Search for content
 */
async function searchContent(query) {
  const searchUrl = `${HDREZKA_MIRROR}/search/?do=search&subaction=search&q=${encodeURIComponent(query)}`;

  const response = await fetch(searchUrl, {
    headers: BROWSER_HEADERS
  });

  if (!response.ok) {
    throw new Error(`Search failed: ${response.status}`);
  }

  const html = await response.text();
  const results = [];

  // Parse search results
  const itemRegex = /<div class="b-content__inline_item"[^>]*data-url="([^"]+)"[^>]*>[\s\S]*?<img[^>]*src="([^"]+)"[\s\S]*?<div class="b-content__inline_item-link">\s*<a[^>]*>([^<]+)<\/a>[\s\S]*?<div>([^<]*)<\/div>/g;

  let match;
  while ((match = itemRegex.exec(html)) !== null) {
    const url = match[1];
    const poster = match[2];
    const title = match[3].trim();
    const info = match[4].trim();

    // Extract year from info
    const yearMatch = info.match(/(\d{4})/);
    const year = yearMatch ? yearMatch[1] : null;

    // Determine type from URL
    let contentType = 'movie';
    if (url.includes('/series/')) {
      contentType = 'series';
    } else if (url.includes('/cartoons/')) {
      contentType = url.includes('-sezon') ? 'series' : 'movie';
    }

    results.push({
      url: url,
      title: title,
      type: contentType,
      year: year,
      poster: poster
    });
  }

  return results;
}

/**
 * CORS headers
 */
const corsHeaders = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

/**
 * Handle incoming requests
 */
export default {
  async fetch(request, env, ctx) {
    // Handle CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: corsHeaders });
    }

    const url = new URL(request.url);
    const path = url.pathname;

    try {
      // Health check
      if (path === '/health' || path === '/') {
        return new Response(JSON.stringify({ status: 'ok', service: 'alphy-worker' }), {
          headers: { ...corsHeaders, 'Content-Type': 'application/json' }
        });
      }

      // Search endpoint
      if (path === '/api/search') {
        const query = url.searchParams.get('q');
        if (!query) {
          return new Response(JSON.stringify({ error: 'Missing query parameter' }), {
            status: 400,
            headers: { ...corsHeaders, 'Content-Type': 'application/json' }
          });
        }

        const results = await searchContent(query);
        return new Response(JSON.stringify({ query, results }), {
          headers: { ...corsHeaders, 'Content-Type': 'application/json' }
        });
      }

      // Content info endpoint
      if (path === '/api/content') {
        const contentUrl = url.searchParams.get('url');
        if (!contentUrl) {
          return new Response(JSON.stringify({ error: 'Missing url parameter' }), {
            status: 400,
            headers: { ...corsHeaders, 'Content-Type': 'application/json' }
          });
        }

        const info = await getContentInfo(contentUrl);
        return new Response(JSON.stringify({
          url: contentUrl,
          translations: info.translators,
          seasons: info.seasons,
          is_series: info.isSeries
        }), {
          headers: { ...corsHeaders, 'Content-Type': 'application/json' }
        });
      }

      // Stream endpoint
      if (path === '/api/stream') {
        const contentUrl = url.searchParams.get('url');
        const season = url.searchParams.get('season');
        const episode = url.searchParams.get('episode');
        const translatorId = url.searchParams.get('translator_id');

        if (!contentUrl) {
          return new Response(JSON.stringify({ error: 'Missing url parameter' }), {
            status: 400,
            headers: { ...corsHeaders, 'Content-Type': 'application/json' }
          });
        }

        // Get content info to find ID
        const info = await getContentInfo(contentUrl);
        if (!info.id) {
          return new Response(JSON.stringify({ error: 'Could not find content ID' }), {
            status: 404,
            headers: { ...corsHeaders, 'Content-Type': 'application/json' }
          });
        }

        const tid = translatorId || info.translatorId;
        const stream = await getStream(
          info.id,
          tid,
          season ? parseInt(season) : null,
          episode ? parseInt(episode) : null
        );

        return new Response(JSON.stringify(stream), {
          headers: { ...corsHeaders, 'Content-Type': 'application/json' }
        });
      }

      return new Response(JSON.stringify({ error: 'Not found' }), {
        status: 404,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' }
      });

    } catch (error) {
      console.error('Worker error:', error);
      return new Response(JSON.stringify({ error: error.message }), {
        status: 500,
        headers: { ...corsHeaders, 'Content-Type': 'application/json' }
      });
    }
  }
};
