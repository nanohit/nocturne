# Alphy Stream Worker

Cloudflare Worker for HDRezka stream extraction. This worker runs on Cloudflare's edge network, so stream tokens are bound to the edge node IP (geographically close to the user), enabling direct CDN streaming without proxy bandwidth.

## Setup

1. **Install Wrangler CLI** (if not already installed):
   ```bash
   npm install -g wrangler
   ```

2. **Login to Cloudflare**:
   ```bash
   wrangler login
   ```

3. **Install dependencies**:
   ```bash
   cd worker
   npm install
   ```

4. **Test locally**:
   ```bash
   npm run dev
   ```

5. **Deploy to Cloudflare**:
   ```bash
   npm run deploy
   ```

6. **Get your worker URL** (shown after deploy, e.g., `https://alphy-stream.your-subdomain.workers.dev`)

7. **Update frontend** - Edit `frontend/index.html` and set `WORKER_URL`:
   ```javascript
   const WORKER_URL = 'https://alphy-stream.your-subdomain.workers.dev';
   ```

8. **Commit and push** to redeploy the frontend with the worker URL.

## Endpoints

- `GET /health` - Health check
- `GET /api/search?q=query` - Search content
- `GET /api/content?url=...` - Get content metadata
- `GET /api/stream?url=...&season=1&episode=1` - Get stream URLs

## Free Tier Limits

Cloudflare Workers free tier includes:
- 100,000 requests per day
- 10ms CPU time per request (plenty for API calls)
- No egress bandwidth charges

Since the worker only handles API calls (not video streaming), you'll stay well within limits even with heavy use.
