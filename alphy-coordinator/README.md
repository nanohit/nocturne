# Alphy Coordinator

Direct streaming frontend for soap4youand.me. This service returns CDN URLs
directly to the client; video bandwidth does not pass through this server.

## Environment Variables

```
SOAP_LOGIN=your-username
SOAP_PASSWORD=your-password
```

## Deployment (Current)

Deploy only this service and set the credentials it needs to log in to
soap4youand.me. No proxy nodes are required.

- `SOAP_LOGIN`
- `SOAP_PASSWORD`
- Health check: `/health`

## Legacy Architecture (Deprecated)

The sections below describe the old proxy-node system and are kept only for
historical reference.

```
┌─────────────────────────────────────────────────────────────────┐
│                     ALPHY COORDINATOR                           │
│                  (this repo - your main account)                │
│                                                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐ │
│  │  Dashboard  │  │  Rezka API  │  │  Load Balancer/Router   │ │
│  │  /dashboard │  │  /api/*     │  │  selects proxy nodes    │ │
│  └─────────────┘  └─────────────┘  └───────────┬─────────────┘ │
└───────────────────────────────────────────────┼─────────────────┘
                                                │
                    ┌───────────────────────────┼───────────────────────────┐
                    │                           │                           │
                    ▼                           ▼                           ▼
        ┌───────────────────┐       ┌───────────────────┐       ┌───────────────────┐
        │   PROXY NODE 1    │       │   PROXY NODE 2    │       │   PROXY NODE N    │
        │   (separate repo) │       │   (separate repo) │       │   (separate repo) │
        └───────────────────┘       └───────────────────┘       └───────────────────┘
```

## Deployment

### 1. Deploy Proxy Nodes First

Deploy 5-15 instances of `alphy-proxy-node` across different accounts/platforms:
- Each on a different GitHub account
- Each connected to Render.com/Railway/Fly.io free tier
- Each gets 100GB bandwidth/month

### 2. Deploy This Coordinator

1. Connect this repo to Render.com (your main account)
2. Set environment variables:

```
NODE_SECRET=your-secret-key-here-make-it-strong

NODES_CONFIG=[
  {"id": "node-1", "url": "https://alphy-node-1.onrender.com"},
  {"id": "node-2", "url": "https://alphy-node-2.onrender.com"},
  {"id": "node-3", "url": "https://alphy-node-3.onrender.com"}
]
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| NODE_SECRET | Yes | Shared secret with all proxy nodes |
| NODES_CONFIG | Yes | JSON array of node configs |

## Endpoints

### Public API
- `GET /api/search?q=...` - Search HDRezka
- `GET /api/details?url=...` - Get content details
- `GET /api/stream?url=...` - Get proxied stream URL

### Node Management
- `GET /api/nodes` - List all nodes and stats
- `POST /api/node/register` - Register a new node
- `POST /api/node/report` - Receive node stats

### Dashboard
- `GET /dashboard` - Visual monitoring dashboard

## How It Works

1. User searches for content → Coordinator queries HDRezka
2. User clicks play → Coordinator extracts stream URL
3. Coordinator selects least-loaded healthy proxy node
4. Returns proxied URL pointing to selected node
5. Video player streams through that node
6. Nodes report bandwidth usage back to coordinator

## Load Balancing

- **Sticky Sessions**: Once a stream starts, it stays on one node
- **Least Loaded First**: New streams go to node with lowest bandwidth usage
- **Health Checks**: Nodes are checked every 30 seconds
- **Automatic Failover**: Unhealthy nodes are removed from rotation

## Bandwidth Estimation

- **1 hour of 720p video**: ~1.5 GB
- **1 hour of 1080p video**: ~3 GB
- **Per free node (100GB)**: ~33-66 hours of streaming
- **15 nodes**: ~500-1000 hours per month

After trial period, upgrade to paid plans for better reliability.
