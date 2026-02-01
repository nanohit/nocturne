"""
Alphy Coordinator - Meta service that coordinates distributed proxy nodes
Handles: HDRezka API, load balancing across proxy nodes, bandwidth monitoring
"""

import os
import re
import time
import asyncio
import hashlib
import base64
import json
from urllib.parse import urlparse, quote
from typing import Optional, Dict, List
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from fastapi import FastAPI, Request, Response, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Configuration
NODE_SECRET = os.getenv("NODE_SECRET", "change-me-in-production")
NODES_CONFIG = os.getenv("NODES_CONFIG", "")  # JSON: [{"id": "node-1", "url": "https://..."}]

# Node registry
@dataclass
class ProxyNode:
    id: str
    url: str
    healthy: bool = True
    last_check: float = 0
    bytes_out: int = 0
    requests: int = 0
    streams_served: int = 0
    last_report: float = 0
    consecutive_failures: int = 0

nodes: Dict[str, ProxyNode] = {}
node_round_robin_index = 0

# Stream session tracking (sticky sessions)
stream_sessions: Dict[str, str] = {}  # stream_id -> node_id
SESSION_TIMEOUT = 3600  # 1 hour

# HTTP client
http_client: Optional[httpx.AsyncClient] = None


def load_nodes_from_config():
    """Load nodes from NODES_CONFIG environment variable"""
    global nodes
    if NODES_CONFIG:
        try:
            config = json.loads(NODES_CONFIG)
            for node_config in config:
                node_id = node_config["id"]
                nodes[node_id] = ProxyNode(
                    id=node_id,
                    url=node_config["url"].rstrip("/")
                )
            print(f"Loaded {len(nodes)} nodes from config")
        except Exception as e:
            print(f"Failed to parse NODES_CONFIG: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True
    )

    load_nodes_from_config()

    # Start health check loop
    asyncio.create_task(health_check_loop())

    # Start session cleanup loop
    asyncio.create_task(cleanup_sessions_loop())

    yield

    await http_client.aclose()

app = FastAPI(title="Alphy Coordinator", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def generate_auth_token() -> str:
    """Generate auth token for requests to proxy nodes"""
    timestamp = int(time.time())
    hash_val = hashlib.sha256(f"{timestamp}:{NODE_SECRET}".encode()).hexdigest()[:16]
    token = base64.b64encode(f"{timestamp}:{hash_val}".encode()).decode()
    return f"Bearer {token}"


def verify_node_request(auth_header: Optional[str]) -> bool:
    """Verify request comes from a proxy node"""
    if not auth_header:
        return False
    try:
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header[7:]
        decoded = base64.b64decode(token).decode()
        timestamp_str, provided_hash = decoded.split(":", 1)
        timestamp = int(timestamp_str)
        if abs(time.time() - timestamp) > 300:
            return False
        expected_hash = hashlib.sha256(f"{timestamp}:{NODE_SECRET}".encode()).hexdigest()[:16]
        return provided_hash == expected_hash
    except Exception:
        return False


def get_healthy_nodes() -> List[ProxyNode]:
    """Get list of healthy nodes"""
    return [n for n in nodes.values() if n.healthy]


def select_node_for_stream(stream_id: str) -> Optional[ProxyNode]:
    """Select a node for a stream - sticky session or round-robin"""
    global node_round_robin_index

    # Check for existing session
    if stream_id in stream_sessions:
        node_id = stream_sessions[stream_id]
        if node_id in nodes and nodes[node_id].healthy:
            return nodes[node_id]
        # Node went down, remove session
        del stream_sessions[stream_id]

    # Select new node (round-robin among healthy nodes)
    healthy = get_healthy_nodes()
    if not healthy:
        return None

    # Sort by bytes_out to load balance
    healthy.sort(key=lambda n: n.bytes_out)
    node = healthy[0]  # Pick least loaded

    # Create sticky session
    stream_sessions[stream_id] = node.id

    return node


# ==================== HDRezka API ====================

def clear_trash(data: str) -> str:
    """Decode HDRezka obfuscated URLs - uses pattern from hdrezka library"""
    # Comprehensive list of trash patterns (base64-encoded special char combinations)
    trash_pattern = re.compile(
        '#h|//_//|I0A=|I0Ah|I0Aj|I0Ak|I0BA|I0Be|I14=|I14h|I14j|I14k|I15A|I15e|ISE=|ISEh|ISEj|ISEk|ISFA|ISFe|ISM=|ISMh'
        '|ISMj|ISMk|ISNA|ISNe|ISQ=|ISQh|ISQj|ISQk|ISRA|ISRe|IUA=|IUAh|IUAj|IUAk|IUBA|IUBe|IV4=|IV4h|IV4j|IV4k|IV5A|IV5e'
        '|IyE=|IyEh|IyEj|IyEk|IyFA|IyFe|IyM=|IyMh|IyMj|IyMk|IyNA|IyNe|IyQ=|IyQh|IyQj|IyQk|IyRA|IyRe|JCE=|JCEh|JCEj|JCEk'
        '|JCFA|JCFe|JCM=|JCMh|JCMj|JCMk|JCNA|JCNe|JCQ=|JCQh|JCQj|JCQk|JCRA|JCRe|JEA=|JEAh|JEAj|JEAk|JEBA|JEBe|JF4=|JF4h'
        '|JF4j|JF4k|JF5A|JF5e|QCE=|QCEh|QCEj|QCEk|QCFA|QCFe|QCM=|QCMh|QCMj|QCMk|QCNA|QCNe|QCQ=|QCQh|QCQj|QCQk|QCRA|QCRe'
        '|QEA=|QEAh|QEAj|QEAk|QEBA|QEBe|QF4=|QF4h|QF4j|QF4k|QF5A|QF5e|XiE=|XiEh|XiEj|XiEk|XiFA|XiFe|XiM=|XiMh|XiMj|XiMk'
        '|XiNA|XiNe|XiQ=|XiQh|XiQj|XiQk|XiRA|XiRe|XkA=|XkAh|XkAj|XkAk|XkBA|XkBe|Xl4=|Xl4h|Xl4j|Xl4k|Xl5A|Xl5e')
    cleaned = trash_pattern.sub('', data)
    return base64.b64decode(cleaned + '==').decode()


@app.get("/api/search")
async def search_content(q: str):
    """Search HDRezka for content"""
    try:
        response = await http_client.post(
            "https://rezka.ag/engine/ajax/search.php",
            data={"q": q},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": "https://rezka.ag",
                "Referer": "https://rezka.ag/"
            }
        )
        return Response(content=response.text, media_type="text/html")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/details")
async def get_details(url: str):
    """Get content details from HDRezka page"""
    try:
        response = await http_client.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
        )
        return Response(content=response.text, media_type="text/html")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stream")
async def get_stream(
    url: str,
    translation_id: Optional[str] = None,
    season: Optional[str] = None,
    episode: Optional[str] = None
):
    """
    Get stream URL from HDRezka and return proxied URL through a selected node
    """
    try:
        # Fetch page to get IDs
        response = await http_client.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        html = response.text

        # Extract content ID
        id_match = re.search(r'data-id="(\d+)"', html)
        if not id_match:
            raise HTTPException(status_code=400, detail="Could not find content ID")
        content_id = id_match.group(1)

        # Determine if it's a series
        is_series = season is not None and episode is not None

        # Get translation ID if not provided
        if not translation_id:
            trans_match = re.search(r'data-translator_id="(\d+)"', html)
            if trans_match:
                translation_id = trans_match.group(1)
            else:
                translation_id = "238"  # Default

        # Prepare API request
        if is_series:
            api_url = "https://rezka.ag/ajax/get_cdn_series/"
            data = {
                "id": content_id,
                "translator_id": translation_id,
                "season": season,
                "episode": episode,
                "action": "get_stream"
            }
        else:
            api_url = "https://rezka.ag/ajax/get_cdn_series/"
            data = {
                "id": content_id,
                "translator_id": translation_id,
                "action": "get_movie"
            }

        # Get stream data
        stream_response = await http_client.post(
            api_url,
            data=data,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": "https://rezka.ag",
                "Referer": url
            }
        )

        result = stream_response.json()
        if not result.get("success"):
            raise HTTPException(status_code=400, detail="Failed to get stream")

        # Decode the URL
        encoded_url = result.get("url", "")
        decoded = clear_trash(encoded_url)

        # Parse qualities
        qualities = {}
        for item in decoded.split(","):
            if "]" in item:
                match = re.match(r'\[(\d+p)[^\]]*\](.+)', item.strip())
                if match:
                    quality = match.group(1)
                    urls = match.group(2).split(" or ")
                    qualities[quality] = urls[0]  # Take first URL

        if not qualities:
            raise HTTPException(status_code=400, detail="No streams found")

        # Select best quality
        quality_order = ["1080p", "720p", "480p", "360p"]
        selected_url = None
        selected_quality = None
        for q in quality_order:
            if q in qualities:
                selected_url = qualities[q]
                selected_quality = q
                break

        if not selected_url:
            selected_url = list(qualities.values())[0]
            selected_quality = list(qualities.keys())[0]

        # Generate stream ID for sticky session
        stream_id = hashlib.md5(f"{content_id}:{translation_id}:{season}:{episode}".encode()).hexdigest()[:12]

        # Select a proxy node
        node = select_node_for_stream(stream_id)
        if not node:
            raise HTTPException(status_code=503, detail="No healthy proxy nodes available")

        # Encode the stream URL and create proxied URL
        encoded_stream = base64.urlsafe_b64encode(selected_url.encode()).decode()
        auth_token = generate_auth_token()

        # The proxy URL includes auth in query param (since video player can't set headers)
        proxy_manifest_url = f"{node.url}/proxy/manifest/{encoded_stream}?auth={quote(auth_token)}"

        return {
            "success": True,
            "url": proxy_manifest_url,
            "quality": selected_quality,
            "available_qualities": list(qualities.keys()),
            "node": node.id,
            "stream_id": stream_id
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Node Management ====================

@app.post("/api/node/register")
async def register_node(request: Request):
    """Register a new proxy node"""
    data = await request.json()
    node_id = data.get("id")
    node_url = data.get("url")

    if not node_id or not node_url:
        raise HTTPException(status_code=400, detail="Missing id or url")

    nodes[node_id] = ProxyNode(id=node_id, url=node_url.rstrip("/"))
    return {"success": True, "message": f"Node {node_id} registered"}


@app.post("/api/node/report")
async def receive_node_report(
    request: Request,
    authorization: Optional[str] = Header(None)
):
    """Receive bandwidth report from a node"""
    if not verify_node_request(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    data = await request.json()
    node_id = data.get("node_id")

    if node_id in nodes:
        stats = data.get("stats", {})
        nodes[node_id].bytes_out = stats.get("bytes_out", 0)
        nodes[node_id].requests = stats.get("requests", 0)
        nodes[node_id].streams_served = stats.get("streams_served", 0)
        nodes[node_id].last_report = time.time()

    return {"success": True}


@app.get("/api/nodes")
async def list_nodes():
    """List all nodes and their status"""
    return {
        "nodes": [
            {
                "id": n.id,
                "url": n.url,
                "healthy": n.healthy,
                "bytes_out_mb": round(n.bytes_out / (1024 * 1024), 2),
                "bytes_out_gb": round(n.bytes_out / (1024 * 1024 * 1024), 3),
                "requests": n.requests,
                "streams_served": n.streams_served,
                "last_check": datetime.fromtimestamp(n.last_check).isoformat() if n.last_check else None
            }
            for n in nodes.values()
        ],
        "total_bytes_gb": round(sum(n.bytes_out for n in nodes.values()) / (1024 * 1024 * 1024), 3),
        "healthy_count": len(get_healthy_nodes()),
        "total_count": len(nodes)
    }


# ==================== Dashboard ====================

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Alphy - Node Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            color: #fff;
            padding: 20px;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 {
            text-align: center;
            margin-bottom: 30px;
            font-size: 2.5em;
            background: linear-gradient(90deg, #667eea, #764ba2);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .stat-card {
            background: rgba(255,255,255,0.1);
            border-radius: 15px;
            padding: 20px;
            text-align: center;
            backdrop-filter: blur(10px);
        }
        .stat-value {
            font-size: 2.5em;
            font-weight: bold;
            background: linear-gradient(90deg, #667eea, #764ba2);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .stat-label { color: #aaa; margin-top: 5px; }
        .nodes-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
        }
        .node-card {
            background: rgba(255,255,255,0.05);
            border-radius: 15px;
            padding: 20px;
            border: 1px solid rgba(255,255,255,0.1);
        }
        .node-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
        }
        .node-name { font-size: 1.2em; font-weight: bold; }
        .status-badge {
            padding: 5px 12px;
            border-radius: 20px;
            font-size: 0.8em;
            font-weight: bold;
        }
        .status-healthy { background: #10b981; }
        .status-unhealthy { background: #ef4444; }
        .node-stats { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
        .node-stat {
            background: rgba(255,255,255,0.05);
            padding: 10px;
            border-radius: 8px;
        }
        .node-stat-value { font-size: 1.3em; font-weight: bold; color: #667eea; }
        .node-stat-label { font-size: 0.8em; color: #888; }
        .progress-bar {
            height: 8px;
            background: rgba(255,255,255,0.1);
            border-radius: 4px;
            margin-top: 15px;
            overflow: hidden;
        }
        .progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #667eea, #764ba2);
            border-radius: 4px;
            transition: width 0.5s ease;
        }
        .refresh-btn {
            position: fixed;
            bottom: 20px;
            right: 20px;
            background: linear-gradient(90deg, #667eea, #764ba2);
            border: none;
            padding: 15px 25px;
            border-radius: 30px;
            color: white;
            font-weight: bold;
            cursor: pointer;
            box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
        }
        .refresh-btn:hover { transform: scale(1.05); }
        .last-update { text-align: center; color: #666; margin-top: 20px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎬 Alphy Node Dashboard</h1>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-value" id="total-bandwidth">-</div>
                <div class="stat-label">Total Bandwidth (GB)</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="healthy-nodes">-</div>
                <div class="stat-label">Healthy Nodes</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="total-requests">-</div>
                <div class="stat-label">Total Requests</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" id="total-streams">-</div>
                <div class="stat-label">Streams Served</div>
            </div>
        </div>

        <div class="nodes-grid" id="nodes-container">
            <p style="text-align: center; color: #666;">Loading nodes...</p>
        </div>

        <div class="last-update">Last updated: <span id="last-update">-</span></div>
    </div>

    <button class="refresh-btn" onclick="loadData()">🔄 Refresh</button>

    <script>
        const BANDWIDTH_LIMIT_GB = 100; // Free tier limit per node

        async function loadData() {
            try {
                const response = await fetch('/api/nodes');
                const data = await response.json();

                // Update summary stats
                document.getElementById('total-bandwidth').textContent = data.total_bytes_gb.toFixed(2);
                document.getElementById('healthy-nodes').textContent = `${data.healthy_count}/${data.total_count}`;
                document.getElementById('total-requests').textContent =
                    data.nodes.reduce((sum, n) => sum + n.requests, 0).toLocaleString();
                document.getElementById('total-streams').textContent =
                    data.nodes.reduce((sum, n) => sum + n.streams_served, 0).toLocaleString();

                // Update nodes grid
                const container = document.getElementById('nodes-container');
                if (data.nodes.length === 0) {
                    container.innerHTML = '<p style="text-align: center; color: #666;">No nodes registered yet</p>';
                    return;
                }

                container.innerHTML = data.nodes.map(node => {
                    const usagePercent = Math.min((node.bytes_out_gb / BANDWIDTH_LIMIT_GB) * 100, 100);
                    return `
                        <div class="node-card">
                            <div class="node-header">
                                <span class="node-name">${node.id}</span>
                                <span class="status-badge ${node.healthy ? 'status-healthy' : 'status-unhealthy'}">
                                    ${node.healthy ? 'HEALTHY' : 'DOWN'}
                                </span>
                            </div>
                            <div class="node-stats">
                                <div class="node-stat">
                                    <div class="node-stat-value">${node.bytes_out_gb.toFixed(2)}</div>
                                    <div class="node-stat-label">Bandwidth (GB)</div>
                                </div>
                                <div class="node-stat">
                                    <div class="node-stat-value">${node.requests.toLocaleString()}</div>
                                    <div class="node-stat-label">Requests</div>
                                </div>
                                <div class="node-stat">
                                    <div class="node-stat-value">${node.streams_served}</div>
                                    <div class="node-stat-label">Streams</div>
                                </div>
                                <div class="node-stat">
                                    <div class="node-stat-value">${usagePercent.toFixed(1)}%</div>
                                    <div class="node-stat-label">of 100GB limit</div>
                                </div>
                            </div>
                            <div class="progress-bar">
                                <div class="progress-fill" style="width: ${usagePercent}%"></div>
                            </div>
                        </div>
                    `;
                }).join('');

                document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
            } catch (error) {
                console.error('Failed to load data:', error);
            }
        }

        // Initial load
        loadData();

        // Auto-refresh every 30 seconds
        setInterval(loadData, 30000);
    </script>
</body>
</html>
"""

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Node monitoring dashboard"""
    return DASHBOARD_HTML


# ==================== Background Tasks ====================

async def health_check_loop():
    """Periodically check health of all nodes"""
    while True:
        await asyncio.sleep(30)  # Check every 30 seconds

        for node in nodes.values():
            try:
                response = await http_client.get(
                    f"{node.url}/health",
                    timeout=10.0
                )
                if response.status_code == 200:
                    node.healthy = True
                    node.consecutive_failures = 0
                    # Update stats from health check
                    data = response.json()
                    if "stats" in data:
                        node.bytes_out = data["stats"].get("bytes_out", node.bytes_out)
                        node.requests = data["stats"].get("requests", node.requests)
                else:
                    node.consecutive_failures += 1
            except Exception as e:
                node.consecutive_failures += 1
                print(f"Health check failed for {node.id}: {e}")

            # Mark unhealthy after 3 consecutive failures
            if node.consecutive_failures >= 3:
                node.healthy = False

            node.last_check = time.time()


async def cleanup_sessions_loop():
    """Clean up old stream sessions"""
    while True:
        await asyncio.sleep(300)  # Every 5 minutes

        now = time.time()
        expired = [sid for sid, _ in stream_sessions.items()]
        # Simple cleanup - in production you'd track timestamps
        if len(stream_sessions) > 1000:
            # Just clear old ones
            stream_sessions.clear()


# ==================== Frontend ====================

FRONTEND_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>alphy</title>
    <meta name="referrer" content="no-referrer">
    <link href="https://vjs.zencdn.net/8.10.0/video-js.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@700&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0a; color: #fff; min-height: 100vh; }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        header { display: flex; align-items: center; gap: 20px; margin-bottom: 30px; padding-bottom: 20px; border-bottom: 1px solid #222; }
        .logo { font-family: 'IBM Plex Mono', monospace; font-size: 24px; font-weight: 700; color: #e50914; cursor: pointer; }
        .search-box { flex: 1; display: flex; gap: 10px; }
        .search-box input { flex: 1; padding: 12px 16px; border: none; border-radius: 4px; background: #222; color: #fff; font-size: 16px; }
        .search-box input:focus { outline: 2px solid #e50914; }
        .search-box button { padding: 12px 24px; border: none; border-radius: 4px; background: #e50914; color: #fff; font-size: 16px; cursor: pointer; }
        .player-section { display: none; margin-bottom: 30px; }
        .player-section.active { display: block; }
        .player-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; flex-wrap: wrap; gap: 10px; }
        .player-title { font-size: 20px; }
        .player-controls { display: flex; gap: 10px; flex-wrap: wrap; }
        .player-controls select { padding: 8px 12px; border: none; border-radius: 4px; background: #222; color: #fff; }
        .back-btn { padding: 8px 16px; border: none; border-radius: 4px; background: #333; color: #fff; cursor: pointer; }
        .video-container { width: 100%; background: #000; border-radius: 8px; overflow: hidden; }
        .video-js { width: 100%; }
        .results-section { display: none; }
        .results-section.active { display: block; }
        .results-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 20px; }
        .result-card { background: #1a1a1a; border-radius: 8px; overflow: hidden; cursor: pointer; transition: transform 0.2s; }
        .result-card:hover { transform: scale(1.05); }
        .result-poster { width: 100%; aspect-ratio: 2/3; background: #333; object-fit: cover; }
        .result-info { padding: 12px; }
        .result-title { font-size: 14px; margin-bottom: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .result-meta { font-size: 12px; color: #888; }
        .episode-section { display: none; margin-bottom: 30px; }
        .episode-section.active { display: block; }
        .season-tabs { display: flex; gap: 10px; margin-bottom: 15px; overflow-x: auto; }
        .season-tab { padding: 8px 16px; border: none; border-radius: 4px; background: #222; color: #fff; cursor: pointer; }
        .season-tab.active { background: #e50914; }
        .episodes-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(80px, 1fr)); gap: 10px; }
        .episode-btn { padding: 12px; border: none; border-radius: 4px; background: #222; color: #fff; cursor: pointer; }
        .episode-btn.active { background: #e50914; }
        .loading { text-align: center; padding: 40px; color: #888; }
        .spinner { border: 3px solid #333; border-top: 3px solid #e50914; border-radius: 50%; width: 30px; height: 30px; animation: spin 1s linear infinite; margin: 0 auto 10px; }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        .error { color: #e50914; padding: 20px; text-align: center; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="logo" onclick="goHome()">alphy</div>
            <div class="search-box">
                <input type="text" id="searchInput" placeholder="Search movies and TV shows...">
                <button onclick="search()">Search</button>
            </div>
        </header>
        <div id="playerSection" class="player-section">
            <div class="player-header">
                <h2 id="playerTitle" class="player-title"></h2>
                <div class="player-controls">
                    <select id="translationSelect" onchange="changeTranslation()"></select>
                    <select id="speedSelect" onchange="changeSpeed()">
                        <option value="0.5">0.5x</option><option value="0.75">0.75x</option>
                        <option value="1" selected>1x</option><option value="1.25">1.25x</option>
                        <option value="1.5">1.5x</option><option value="2">2x</option>
                    </select>
                    <button class="back-btn" onclick="hidePlayer()">Back</button>
                </div>
            </div>
            <div class="video-container">
                <video id="videoPlayer" class="video-js vjs-big-play-centered" controls preload="auto"></video>
            </div>
        </div>
        <div id="episodeSection" class="episode-section">
            <div id="seasonTabs" class="season-tabs"></div>
            <div id="episodesGrid" class="episodes-grid"></div>
        </div>
        <div id="resultsSection" class="results-section">
            <div id="resultsGrid" class="results-grid"></div>
        </div>
        <div id="loading" class="loading" style="display: none;"><div class="spinner"></div><div>Loading...</div></div>
        <div id="error" class="error" style="display: none;"></div>
    </div>
    <script src="https://vjs.zencdn.net/8.10.0/video.min.js"></script>
    <script>
        let player = null, currentContent = null, currentSeason = 1, currentEpisode = 1;
        let currentTranslation = null, currentSpeed = 1;

        function initPlayer() {
            if (player) return;
            player = videojs('videoPlayer', {
                fluid: false, techOrder: ['html5'],
                html5: { vhs: { overrideNative: true }, nativeAudioTracks: false, nativeVideoTracks: false }
            });
            player.on('loadedmetadata', () => {
                const vw = player.videoWidth(), vh = player.videoHeight();
                if (vw && vh) {
                    const container = document.querySelector('.video-container');
                    player.dimensions(container.clientWidth, Math.round(container.clientWidth * (vh / vw)));
                }
            });
        }

        async function search() {
            const query = document.getElementById('searchInput').value.trim();
            if (!query) return;
            showLoading(); hidePlayer();
            try {
                const response = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
                const html = await response.text();
                displayResults(parseSearchResults(html));
            } catch (e) { showError('Search failed: ' + e.message); }
        }

        function parseSearchResults(html) {
            const results = [];
            const parser = new DOMParser();
            const doc = parser.parseFromString(html, 'text/html');
            doc.querySelectorAll('.b-search__section_list li').forEach(li => {
                const link = li.querySelector('a');
                const img = li.querySelector('img');
                if (link) {
                    const text = link.textContent.trim();
                    const match = text.match(/^(.+?)\\s*\\((\\d{4})(?:[-](\\d{4}|...))?.*\\)$/);
                    results.push({
                        url: link.href.replace(/^.*rezka\\.ag/, 'https://rezka.ag'),
                        title: match ? match[1].trim() : text,
                        year: match ? match[2] : '',
                        poster: img ? img.src : null
                    });
                }
            });
            return results;
        }

        function displayResults(results) {
            hideLoading();
            const grid = document.getElementById('resultsGrid');
            grid.innerHTML = results.length === 0 ? '<p style="color:#888">No results</p>' : '';
            results.forEach(item => {
                const card = document.createElement('div');
                card.className = 'result-card';
                card.onclick = () => selectContent(item);
                card.innerHTML = `<img class="result-poster" src="${item.poster || ''}" alt="">
                    <div class="result-info"><div class="result-title">${item.title}</div><div class="result-meta">${item.year}</div></div>`;
                grid.appendChild(card);
            });
            document.getElementById('resultsSection').classList.add('active');
        }

        async function selectContent(item) {
            showLoading();
            currentContent = item;
            try {
                const response = await fetch(`/api/details?url=${encodeURIComponent(item.url)}`);
                const html = await response.text();
                currentContent.info = parseDetails(html);
                const transSelect = document.getElementById('translationSelect');
                transSelect.innerHTML = currentContent.info.translations.map(t => `<option value="${t.id}">${t.name}</option>`).join('');
                currentTranslation = currentContent.info.translations[0]?.id;
                if (currentContent.info.is_series) setupEpisodes(currentContent.info);
                else playStream(item.url);
            } catch (e) { showError('Failed: ' + e.message); }
        }

        function parseDetails(html) {
            const parser = new DOMParser();
            const doc = parser.parseFromString(html, 'text/html');
            const info = { translations: [], seasons: {}, is_series: false };
            doc.querySelectorAll('#translators-list li').forEach(li => {
                info.translations.push({ id: li.dataset.translatorId, name: li.textContent.trim() });
            });
            if (info.translations.length === 0) {
                const match = html.match(/initCDNSeriesEvents\\(\\d+,\\s*(\\d+)/);
                if (match) info.translations.push({ id: match[1], name: 'Default' });
            }
            const seasonsList = doc.querySelector('#simple-seasons-tabs');
            if (seasonsList) {
                info.is_series = true;
                seasonsList.querySelectorAll('li').forEach(li => { info.seasons[parseInt(li.dataset.tabId)] = []; });
                doc.querySelectorAll('#simple-episodes-tabs .b-simple_episode__item').forEach(ep => {
                    const s = parseInt(ep.dataset.seasonId), e = parseInt(ep.dataset.episodeId);
                    if (!info.seasons[s]) info.seasons[s] = [];
                    if (!info.seasons[s].includes(e)) info.seasons[s].push(e);
                });
            }
            return info;
        }

        function setupEpisodes(info) {
            hideLoading();
            const seasons = Object.keys(info.seasons).map(Number).sort((a,b) => a-b);
            const first = seasons[0] || 1;
            document.getElementById('seasonTabs').innerHTML = seasons.map(s =>
                `<button class="season-tab ${s === first ? 'active' : ''}" onclick="selectSeason(${s})">S${s}</button>`
            ).join('');
            selectSeason(first);
            document.getElementById('episodeSection').classList.add('active');
            document.getElementById('resultsSection').classList.remove('active');
        }

        function selectSeason(s) {
            currentSeason = s;
            document.querySelectorAll('.season-tab').forEach(t => t.classList.toggle('active', t.textContent === `S${s}`));
            const eps = currentContent.info.seasons[s] || [];
            document.getElementById('episodesGrid').innerHTML = eps.map(e =>
                `<button class="episode-btn" onclick="playEpisode(${s}, ${e})">E${e}</button>`
            ).join('');
        }

        function playEpisode(s, e) {
            currentSeason = s; currentEpisode = e;
            document.querySelectorAll('.episode-btn').forEach(b => b.classList.toggle('active', b.textContent === `E${e}`));
            playStream(currentContent.url, s, e);
        }

        async function playStream(url, season = null, episode = null) {
            showLoading();
            initPlayer();
            try {
                let apiUrl = `/api/stream?url=${encodeURIComponent(url)}`;
                if (season !== null) apiUrl += `&season=${season}&episode=${episode}`;
                if (currentTranslation) apiUrl += `&translation_id=${currentTranslation}`;

                const response = await fetch(apiUrl);
                const data = await response.json();
                if (!data.url) throw new Error(data.detail || 'No stream');

                player.src({ src: data.url, type: 'application/x-mpegURL' });
                player.playbackRate(currentSpeed);
                document.getElementById('playerTitle').textContent = currentContent.title + (season ? ` S${season}E${episode}` : '');
                document.getElementById('playerSection').classList.add('active');
                document.getElementById('resultsSection').classList.remove('active');
                hideLoading();
            } catch (e) { showError('Stream failed: ' + e.message); }
        }

        function changeTranslation() {
            currentTranslation = document.getElementById('translationSelect').value;
            if (currentContent.info?.is_series) playEpisode(currentSeason, currentEpisode);
            else playStream(currentContent.url);
        }

        function changeSpeed() {
            currentSpeed = parseFloat(document.getElementById('speedSelect').value);
            if (player) player.playbackRate(currentSpeed);
        }

        function hidePlayer() {
            document.getElementById('playerSection').classList.remove('active');
            document.getElementById('episodeSection').classList.remove('active');
            document.getElementById('resultsSection').classList.add('active');
            if (player) player.pause();
        }

        function goHome() {
            hidePlayer();
            document.getElementById('resultsSection').classList.remove('active');
            document.getElementById('searchInput').value = '';
        }

        function showLoading() { document.getElementById('loading').style.display = 'block'; document.getElementById('error').style.display = 'none'; }
        function hideLoading() { document.getElementById('loading').style.display = 'none'; }
        function showError(m) { hideLoading(); document.getElementById('error').textContent = m; document.getElementById('error').style.display = 'block'; }

        document.getElementById('searchInput').addEventListener('keypress', e => { if (e.key === 'Enter') search(); });
    </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    """Serve the main streaming frontend"""
    return FRONTEND_HTML


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
