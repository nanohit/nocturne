# Alphy Distributed Deployment Guide

## Overview

This guide sets up a distributed streaming proxy with:
- 1 Coordinator (your main account) - handles API and load balancing
- 15 Proxy Nodes (15 GitHub accounts) - each handles 100GB bandwidth

Total free bandwidth: **1.5 TB/month**

---

## Step 1: Generate Shared Secret

Generate a strong secret to share between all nodes:

```bash
openssl rand -hex 32
```

Example output: `a1b2c3d4e5f6...` (save this!)

---

## Step 2: Deploy Proxy Nodes (15x)

### For Each GitHub Account (1-15):

1. **Create Repository**
   ```
   Repository name: alphy-streaming-worker
   (or any innocuous name)
   ```

2. **Upload Files**
   Copy contents from `alphy-proxy-node/`:
   - `main.py`
   - `requirements.txt`
   - `Dockerfile`
   - `render.yaml`
   - `.gitignore`

3. **Connect to Render.com**
   - Go to render.com
   - "New" → "Web Service"
   - Connect GitHub account
   - Select the repository
   - Environment: Docker
   - Plan: Free

4. **Set Environment Variables**
   ```
   NODE_SECRET = [your shared secret from step 1]
   NODE_ID = node-1  (increment: node-2, node-3, etc.)
   ```

5. **Deploy and Note the URL**
   Example: `https://alphy-streaming-worker-abc123.onrender.com`

### Repeat for all 15 accounts

---

## Step 3: Collect All Node URLs

After deploying all nodes, collect the URLs:

```json
[
  {"id": "node-1", "url": "https://alphy-node-1-xxx.onrender.com"},
  {"id": "node-2", "url": "https://alphy-node-2-yyy.onrender.com"},
  {"id": "node-3", "url": "https://alphy-node-3-zzz.onrender.com"},
  {"id": "node-4", "url": "https://alphy-node-4-aaa.onrender.com"},
  {"id": "node-5", "url": "https://alphy-node-5-bbb.onrender.com"},
  {"id": "node-6", "url": "https://alphy-node-6-ccc.onrender.com"},
  {"id": "node-7", "url": "https://alphy-node-7-ddd.onrender.com"},
  {"id": "node-8", "url": "https://alphy-node-8-eee.onrender.com"},
  {"id": "node-9", "url": "https://alphy-node-9-fff.onrender.com"},
  {"id": "node-10", "url": "https://alphy-node-10-ggg.onrender.com"},
  {"id": "node-11", "url": "https://alphy-node-11-hhh.onrender.com"},
  {"id": "node-12", "url": "https://alphy-node-12-iii.onrender.com"},
  {"id": "node-13", "url": "https://alphy-node-13-jjj.onrender.com"},
  {"id": "node-14", "url": "https://alphy-node-14-kkk.onrender.com"},
  {"id": "node-15", "url": "https://alphy-node-15-lll.onrender.com"}
]
```

---

## Step 4: Deploy Coordinator

1. **Use Your Main GitHub Account**

2. **Create Repository**
   ```
   Repository name: alphy-coordinator (or alphy-main)
   ```

3. **Upload Files**
   Copy contents from `alphy-coordinator/`:
   - `main.py`
   - `requirements.txt`
   - `Dockerfile`
   - `render.yaml`
   - `.gitignore`

4. **Connect to Render.com**
   - "New" → "Web Service"
   - Connect your main GitHub
   - Select the repository
   - Environment: Docker
   - Plan: Free (or paid for custom domain)

5. **Set Environment Variables**
   ```
   NODE_SECRET = [same secret from step 1]
   NODES_CONFIG = [paste the JSON array from step 3]
   ```

6. **Deploy**

   Your coordinator URL: `https://alphy-coordinator-xxx.onrender.com`

---

## Step 5: Add Frontend

Either:
A) Add static frontend files to coordinator repo
B) Use separate frontend pointing to coordinator API

Update frontend `API_BASE` to your coordinator URL.

---

## Step 6: Verify Setup

1. **Check Dashboard**
   ```
   https://your-coordinator.onrender.com/dashboard
   ```
   Should show all 15 nodes

2. **Check Node Health**
   ```
   https://your-coordinator.onrender.com/api/nodes
   ```

3. **Test Stream**
   Search for content and try playing

---

## Monitoring

### Dashboard
Access at `/dashboard` - shows:
- Total bandwidth used
- Per-node bandwidth
- Health status
- Request counts

### Health Checks
Coordinator pings each node every 30 seconds.
Unhealthy nodes (3 failures) are removed from rotation.

---

## Bandwidth Math

| Scenario | Per Node | 15 Nodes |
|----------|----------|----------|
| 720p (1.5 GB/hr) | 66 hrs | 1000 hrs |
| 1080p (3 GB/hr) | 33 hrs | 500 hrs |

**Assuming average 5 concurrent users at 720p:**
- ~7.5 GB/hour
- ~180 GB/day
- ~5.4 TB/month (need paid plan or more nodes)

**Assuming 2 concurrent users at 720p:**
- ~3 GB/hour
- ~72 GB/day
- ~2.2 TB/month (15 nodes sufficient)

---

## Scaling Up

When ready to pay:

1. **Option A: Keep distributed**
   - Upgrade some nodes to paid plans
   - More bandwidth per node

2. **Option B: Consolidate**
   - Single high-bandwidth server
   - Update NODES_CONFIG to point to one node
   - Simpler architecture

---

## Troubleshooting

### Node Shows Unhealthy
- Check node logs in Render dashboard
- Verify NODE_SECRET matches coordinator
- Check if free tier quota exceeded

### Streams Not Playing
- Check browser console for CORS errors
- Verify manifest URL is being rewritten
- Check proxy node logs

### High Latency
- Free tier nodes "sleep" after 15 min inactivity
- First request wakes them (30s delay)
- Consider keeping at least one node on paid plan

---

## Quick Reference

| Component | URL Pattern |
|-----------|-------------|
| Coordinator | `https://alphy-coordinator-xxx.onrender.com` |
| Dashboard | `/dashboard` |
| Node List | `/api/nodes` |
| Stream | `/api/stream?url=...` |
| Node Health | `https://node-url/health` |
