#!/usr/bin/env python3

from flask import Flask, jsonify, request
import threading
import time
from collections import deque
import logging
import os

app = Flask(__name__)

CACHE_LIMIT = int(os.getenv("CACHE_LIMIT", 300))
CACHE_ENTRY_TTL = int(os.getenv("CACHE_ENTRY_TTL", 5400))
CACHE_CLEAR_INTERVAL = int(os.getenv("CACHE_CLEAR_INTERVAL", 1800))
BOT_TIMEOUT = int(os.getenv("BOT_TIMEOUT", 300))  # 5 minutes - bot considered inactive after this

server_cache = deque()
cache_set = set()
jobs_assigned = 0
total_received = 0
visited_servers = {}
visit_stats = {"total_visits": 0, "unique_servers": 0, "repeat_visits": 0}

# Bot tracking - using unique bot IDs instead of IPs
active_bots = {}  # {bot_id: {"last_seen": timestamp, "requests": count, "first_seen": timestamp}}
bot_lock = threading.Lock()

lock = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format='[MAIN-API] %(asctime)s %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)

def _now():
    return time.time()

def get_bot_id():
    """Get bot ID from request headers or query params - expects Roblox username"""
    # Try X-Bot-Name header first (Roblox username)
    bot_id = request.headers.get('X-Bot-Name')
    if bot_id:
        return bot_id.strip()
    
    # Try X-Bot-ID header (backward compatibility)
    bot_id = request.headers.get('X-Bot-ID')
    if bot_id:
        return bot_id.strip()
    
    # Try query parameter
    bot_id = request.args.get('bot_name') or request.args.get('bot_id')
    if bot_id:
        return bot_id.strip()
    
    # Try in JSON body for POST requests
    if request.is_json:
        data = request.get_json(silent=True)
        if data:
            bot_id = data.get('bot_name') or data.get('bot_id')
            if bot_id:
                return str(bot_id).strip()
    
    # No bot ID provided - return None (won't be tracked)
    return None

def track_bot_activity():
    """Track bot activity - call this whenever a bot makes a request"""
    bot_id = get_bot_id()
    if not bot_id:
        # Log when bot ID is missing for debugging
        logging.debug(f"No bot ID provided - IP: {request.remote_addr}, Headers: {dict(request.headers)}")
        return  # Don't track if no bot ID provided
    
    now = _now()
    
    with bot_lock:
        if bot_id in active_bots:
            active_bots[bot_id]["last_seen"] = now
            active_bots[bot_id]["requests"] += 1
        else:
            active_bots[bot_id] = {
                "last_seen": now,
                "first_seen": now,
                "requests": 1
            }
            logging.info(f"New bot connected: {bot_id} (Total active: {len(active_bots)})")

def cleanup_inactive_bots():
    """Remove bots that haven't been seen in BOT_TIMEOUT seconds"""
    now = _now()
    inactive = []
    
    with bot_lock:
        for ip, data in active_bots.items():
            if now - data["last_seen"] > BOT_TIMEOUT:
                inactive.append(ip)
        
        for ip in inactive:
            del active_bots[ip]
    
    if inactive:
        logging.info(f"Cleaned {len(inactive)} inactive bots")

def get_active_bot_count():
    """Get count of currently active bots"""
    cleanup_inactive_bots()
    with bot_lock:
        return len(active_bots)

def cleanup_expired():
    now = _now()
    cleaned = 0
    while server_cache and server_cache[0][1] <= now:
        jid, _ = server_cache.popleft()
        cache_set.discard(jid)
        cleaned += 1
    if cleaned > 0:
        logging.info(f"Cleaned {cleaned} expired servers")

def add_to_cache(new_servers):
    now = _now()
    added = 0
    duplicates = 0
    for job_id in new_servers:
        if not job_id:
            continue
        if job_id in cache_set:
            duplicates += 1
            continue
        server_cache.append((job_id, now + CACHE_ENTRY_TTL))
        cache_set.add(job_id)
        added += 1
    removed = 0
    while len(server_cache) > CACHE_LIMIT:
        jid, _ = server_cache.popleft()
        cache_set.discard(jid)
        removed += 1
    if removed > 0:
        logging.warning(f"Cache full: removed {removed} oldest servers")
    return added, duplicates

@app.route("/", methods=["GET"])
@app.route("/status", methods=["GET"])
def status():
    cleanup_expired()
    with lock:
        repeat_rate = 0
        if visit_stats["total_visits"] > 0:
            repeat_rate = (visit_stats["repeat_visits"] / visit_stats["total_visits"]) * 100
        
        active_bots_count = get_active_bot_count()
        
        return jsonify({
            "cache_jobs": len(server_cache),
            "jobs_assigned": jobs_assigned,
            "total_received": total_received,
            "cache_limit": CACHE_LIMIT,
            "health": "healthy" if len(server_cache) > 100 else "low",
            "active_bots": active_bots_count,
            "visit_tracking": {
                "total_visits": visit_stats["total_visits"],
                "unique_servers": visit_stats["unique_servers"],
                "repeat_rate": f"{repeat_rate:.1f}%"
            }
        })

@app.route("/get-server", methods=["GET"])
def get_server():
    global jobs_assigned
    track_bot_activity()  # Track bot activity
    cleanup_expired()
    
    with lock:
        if not server_cache:
            return jsonify({"error": "No servers available"}), 404
        
        job_id, _ = server_cache.popleft()
        cache_set.discard(job_id)
        jobs_assigned += 1
        return jsonify({
            "job_id": job_id,
            "remaining": len(server_cache)
        })

@app.route("/get-servers", methods=["GET"])
def get_servers():
    track_bot_activity()  # Track bot activity
    cleanup_expired()
    
    with lock:
        return jsonify({
            "job_ids": [jid for jid, _ in server_cache],
            "total": len(server_cache)
        })

@app.route("/get-batch", methods=["GET"])
def get_batch():
    global jobs_assigned
    track_bot_activity()  # Track bot activity
    cleanup_expired()
    
    count = min(int(request.args.get("count", 10)), 100)
    
    with lock:
        if not server_cache:
            return jsonify({"error": "No servers available", "servers": []}), 404
        
        batch = []
        for _ in range(min(count, len(server_cache))):
            if not server_cache:
                break
            job_id, _ = server_cache.popleft()
            cache_set.discard(job_id)
            batch.append({"job_id": job_id})
        
        jobs_assigned += len(batch)
        return jsonify({
            "servers": batch,
            "count": len(batch),
            "remaining": len(server_cache)
        })

@app.route("/jobs-assigned", methods=["GET"])
def jobs_assigned_endpoint():
    return jsonify({
        "jobs_assigned": jobs_assigned,
        "total_received": total_received
    })

@app.route("/add-pool", methods=["POST"])
def add_pool():
    global total_received
    cleanup_expired()
    
    data = request.get_json()
    if not data or "servers" not in data:
        return jsonify({"error": "Missing 'servers' field"}), 400
    
    servers = data["servers"]
    if not isinstance(servers, list):
        return jsonify({"error": "'servers' must be a list"}), 400
    
    with lock:
        added, duplicates = add_to_cache([str(s) for s in servers if s])
        total = len(server_cache)
        total_received += len(servers)
    
    if added > 0 and total % 500 < added:
        logging.info(
            f"Pool update: received={len(servers)}, "
            f"added={added}, duplicates={duplicates}, "
            f"cache_total={total}"
        )
    
    return jsonify({
        "added": added,
        "duplicates": duplicates,
        "cache_total": total
    })

@app.route("/report-visit", methods=["POST"])
def report_visit():
    track_bot_activity()  # Track bot activity
    data = request.get_json()
    if not data or "jobId" not in data:
        return jsonify({"error": "Missing jobId"}), 400
    
    job_id = str(data["jobId"])
    found_items = data.get("found_items", False)
    
    with lock:
        visit_stats["total_visits"] += 1
        if job_id in visited_servers:
            visited_servers[job_id]["count"] += 1
            visited_servers[job_id]["last_visit"] = _now()
            visited_servers[job_id]["found_items"] = visited_servers[job_id]["found_items"] or found_items
            visit_stats["repeat_visits"] += 1
        else:
            visited_servers[job_id] = {
                "count": 1,
                "last_visit": _now(),
                "found_items": found_items
            }
            visit_stats["unique_servers"] += 1
    
    return jsonify({"status": "recorded"})

@app.route("/visit-stats", methods=["GET"])
def get_visit_stats():
    with lock:
        now = _now()
        old_visits = [jid for jid, data in visited_servers.items() if now - data["last_visit"] > 7200]
        for jid in old_visits:
            del visited_servers[jid]
        
        repeat_rate = (visit_stats["repeat_visits"] / visit_stats["total_visits"]) * 100 if visit_stats["total_visits"] > 0 else 0
        
        top_repeated = sorted(
            [(jid, data["count"]) for jid, data in visited_servers.items()],
            key=lambda x: x[1],
            reverse=True
        )[:10]
        
        return jsonify({
            "summary": {
                "total_visits": visit_stats["total_visits"],
                "unique_servers": visit_stats["unique_servers"],
                "repeat_visits": visit_stats["repeat_visits"],
                "repeat_rate": f"{repeat_rate:.1f}%"
            },
            "top_repeated_servers": [
                {"job_id": jid[:20] + "...", "visits": count}
                for jid, count in top_repeated
            ],
            "active_tracking": len(visited_servers)
        })

@app.route("/active-bots", methods=["GET"])
def active_bots_endpoint():
    """Get information about active bots"""
    cleanup_inactive_bots()
    
    with bot_lock:
        now = _now()
        bots_info = []
        real_bots = 0
        
        for bot_id, data in active_bots.items():
            bots_info.append({
                "bot_name": bot_id,
                "last_seen_ago": int(now - data["last_seen"]),
                "first_seen_ago": int(now - data["first_seen"]),
                "requests": data["requests"],
                "status": "active" if now - data["last_seen"] < BOT_TIMEOUT else "inactive"
            })
        
        # Sort by last_seen (most recent first)
        bots_info.sort(key=lambda x: x["last_seen_ago"])
        
        return jsonify({
            "total_active_bots": len(active_bots),
            "bot_timeout_seconds": BOT_TIMEOUT,
            "bots": bots_info
        })

@app.route("/clear-cache", methods=["POST"])
def clear_cache():
    with lock:
        server_cache.clear()
        cache_set.clear()
        logging.warning("Cache cleared completely")
    return jsonify({"message": "Cache cleared"})

@app.route("/health", methods=["GET"])
def health():
    with lock:
        cache_size = len(server_cache)
    
    active_bots_count = get_active_bot_count()
    
    if cache_size < 50:
        status = "critical"
    elif cache_size < 200:
        status = "warning"
    else:
        status = "healthy"
    
    return jsonify({
        "status": status,
        "cache_size": cache_size,
        "active_bots": active_bots_count,
        "uptime": int(time.time() - start_time)
    }), 200 if status == "healthy" else 503

def periodic_cleanup():
    while True:
        time.sleep(30)
        with lock:
            cleanup_expired()
        cleanup_inactive_bots()  # Also clean inactive bots

def cache_clear_30min():
    while True:
        time.sleep(CACHE_CLEAR_INTERVAL)
        with lock:
            old_size = len(server_cache)
            server_cache.clear()
            cache_set.clear()
            logging.info(f"30-minute cache clear: removed {old_size} servers")

def stats_logger():
    while True:
        time.sleep(60)
        with lock:
            elapsed = time.time() - start_time
            rate = jobs_assigned / elapsed if elapsed > 0 else 0
            active_bots_count = get_active_bot_count()
            logging.info(
                f"STATS - Cache: {len(server_cache)}, "
                f"Assigned: {jobs_assigned}, "
                f"Received: {total_received}, "
                f"Rate: {rate:.1f} jobs/sec, "
                f"Active Bots: {active_bots_count}"
            )

threading.Thread(target=periodic_cleanup, daemon=True).start()
threading.Thread(target=cache_clear_30min, daemon=True).start()
threading.Thread(target=stats_logger, daemon=True).start()

start_time = time.time()

if __name__ == "__main__":
    port = int(os.environ["PORT"])
    logging.info(f"MAIN API started on 0.0.0.0:{port}")
    logging.info(f"Config: CACHE_LIMIT={CACHE_LIMIT}, TTL={CACHE_ENTRY_TTL}s, CLEAR_INTERVAL={CACHE_CLEAR_INTERVAL}s")
    logging.info(f"Bot tracking: BOT_TIMEOUT={BOT_TIMEOUT}s")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)

