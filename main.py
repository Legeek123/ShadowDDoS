#!/usr/bin/env python3

ascii_art = r"""
╔────────────────────────────────────────────────────────────────────────╗
│     _____ _               _               _____  _____        _____    │
│    / ____| |             | |             |  __ \|  __ \      / ____|   │
│   | (___ | |__   __ _  __| | _____      _| |  | | |  | | ___| (___     │
│    \___ \| '_ \ / _` |/ _` |/ _ \ \ /\ / / |  | | |  | |/ _ \\___ \    │
│    ____) | | | | (_| | (_| | (_) \ V  V /| |__| | |__| | (_) |___) |   │
│   |_____/|_| |_|\__,_|\__,_|\___/ \_/\_/ |_____/|_____/ \___/_____/    │
╚────────────────────────────────────────────────────────────────────────╝
"""

print(ascii_art)

import asyncio, aiohttp, time, csv, argparse, logging, sys, math, json
from typing import List, Dict, Any
from base64 import b64encode
import matplotlib.pyplot as plt
from jinja2 import Template
import random, os

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ---------------- Utility ----------------
def percentile(sorted_list: List[float], p: float) -> float:
    if not sorted_list: return 0.0
    k = (len(sorted_list)-1) * (p/100.0)
    f, c = math.floor(k), math.ceil(k)
    if f==c: return sorted_list[int(k)]
    return sorted_list[int(f)]*(c-k) + sorted_list[int(c)]*(k-f)

# ---------------- Worker ----------------
async def worker(name: str, session: aiohttp.ClientSession, queue: asyncio.Queue, results: List[Dict[str,Any]], cfg):
    while True:
        try:
            item = await queue.get()
        except asyncio.CancelledError:
            break
        if item is None:
            queue.task_done()
            break

        idx = item["idx"]
        method, url, headers, data = cfg.method.upper(), cfg.url, cfg.headers.copy(), cfg.data
        timeout = aiohttp.ClientTimeout(total=cfg.request_timeout)

        if method == "POST" and data:
            try:
                json.loads(data)
                headers.setdefault("Content-Type", "application/json")
            except Exception:
                pass

        bytes_sent = len(data.encode() if isinstance(data, str) else (data or b"")) if data else 0

        start = time.time()
        status, error, bytes_recv = None, "", 0
        try:
            if method == "GET":
                async with session.get(url, headers=headers, timeout=timeout) as resp:
                    status = resp.status
                    if cfg.no_body:
                        # Ne pas lire le corps : juste relâcher/fermer proprement
                        maybe = getattr(resp, "release", None)
                        if maybe:
                            maybe_call = maybe()
                            if asyncio.iscoroutine(maybe_call):
                                await maybe_call
                        else:
                            resp.close()
                        bytes_recv = 0
                    else:
                        body = await resp.read()
                        bytes_recv = len(body) if body else 0
            else:
                async with session.request(method, url, headers=headers, data=data, timeout=timeout) as resp:
                    status = resp.status
                    if cfg.no_body:
                        maybe = getattr(resp, "release", None)
                        if maybe:
                            maybe_call = maybe()
                            if asyncio.iscoroutine(maybe_call):
                                await maybe_call
                        else:
                            resp.close()
                        bytes_recv = 0
                    else:
                        body = await resp.read()
                        bytes_recv = len(body) if body else 0

            elapsed = time.time() - start
            logging.debug(f"[{name}] #{idx} {method} -> {status} ({elapsed:.3f}s) sent={bytes_sent}B recv={bytes_recv}B")
        except Exception as e:
            elapsed = time.time() - start
            error = str(e)
            logging.debug(f"[{name}] #{idx} ERREUR: {error} ({elapsed:.3f}s) sent={bytes_sent}B")

        results.append({
            "idx": idx,
            "method": method,
            "url": url,
            "status": status,
            "elapsed": round(elapsed,6),
            "error": error,
            "timestamp": time.time(),
            "bytes_sent": bytes_sent,
            "bytes_received": bytes_recv
        })
        queue.task_done()

# ---------------- Producer with ramp-up/ramp-down ----------------
async def producer(queue: asyncio.Queue, total_requests:int, duration:float, ramp_up:float, ramp_down:float, dry_run:bool):
    if total_requests <= 0: return
    steady_duration = duration - ramp_up - ramp_down
    if steady_duration < 0:
        ramp_up = ramp_down = duration/2
        steady_duration = 0

    interval_steady = steady_duration / total_requests if total_requests else 0
    logging.info(f"Producer: ramp-up {ramp_up}s, steady {steady_duration}s, ramp-down {ramp_down}s")

    # Ramp-up phase
    for i in range(1, total_requests+1):
        if dry_run:
            logging.info(f"[DRY RUN] would enqueue request #{i}")
        else:
            await queue.put({"idx": i})
        await asyncio.sleep(duration/total_requests)  # simple pacing

# ---------------- Main Test ----------------
async def run_test(cfg):
    queue = asyncio.Queue(maxsize=cfg.concurrency * 2)
    results: List[Dict[str,Any]] = []

    headers = dict(cfg.headers) if cfg.headers else {}
    if cfg.auth_user and cfg.auth_pass:
        token = b64encode(f"{cfg.auth_user}:{cfg.auth_pass}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"

    connector = aiohttp.TCPConnector(limit=0, ssl=cfg.allow_insecure_ssl)
    timeout = aiohttp.ClientTimeout(total=cfg.request_timeout)
    session_kwargs = {}
    if cfg.proxy: session_kwargs["trust_env"]=False

    async with aiohttp.ClientSession(connector=connector, timeout=timeout, **session_kwargs) as session:
        workers = [asyncio.create_task(worker(f"worker-{i+1}", session, queue, results, cfg)) for i in range(cfg.concurrency)]
        prod = asyncio.create_task(producer(queue, cfg.total_requests, cfg.duration, cfg.ramp_up, cfg.ramp_down, cfg.dry_run))
        start_time = time.time()
        await prod

        if cfg.dry_run:
            for _ in workers: await queue.put(None)
            await asyncio.gather(*workers, return_exceptions=True)
            logging.info("Dry run complete.")
            return results, time.time()-start_time

        await queue.join()
        for _ in workers: await queue.put(None)
        await asyncio.gather(*workers, return_exceptions=True)
        return results, time.time()-start_time

# ---------------- CSV + Stats + HTML ----------------
def save_csv(results: List[Dict[str,Any]], csv_path: str):
    if not results: return
    keys = ["idx","method","url","status","elapsed","bytes_sent","bytes_received","error","timestamp"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for r in sorted(results, key=lambda x: x["idx"]):
            # assure les clés présentes
            row = {k: r.get(k,"") for k in keys}
            writer.writerow(row)
    logging.info(f"Saved results CSV: {csv_path}")

def compute_stats(results: List[Dict[str,Any]], total_time: float):
    total = len(results)
    statuses = [r["status"] for r in results if r["status"] is not None]
    errors = [r for r in results if r["error"]]
    elapsed_list = sorted([r["elapsed"] for r in results if isinstance(r.get("elapsed"), (int,float))])

    success_count = len([s for s in statuses if s and 200 <= s < 400])
    error_count = len(errors) + len([s for s in statuses if s and s >= 400])
    throughput = total / total_time if total_time > 0 else 0.0
    avg = sum(elapsed_list)/len(elapsed_list) if elapsed_list else 0.0
    minimum = elapsed_list[0] if elapsed_list else 0.0
    maximum = elapsed_list[-1] if elapsed_list else 0.0
    p50 = percentile(elapsed_list, 50)
    p90 = percentile(elapsed_list, 90)
    p99 = percentile(elapsed_list, 99)

    # bytes totals
    total_bytes_sent = sum(int(r.get("bytes_sent") or 0) for r in results)
    total_bytes_recv = sum(int(r.get("bytes_received") or 0) for r in results)
    total_bits_sent = total_bytes_sent * 8
    total_bits_recv = total_bytes_recv * 8
    bits_per_sec_sent = total_bits_sent / total_time if total_time > 0 else 0.0
    bits_per_sec_recv = total_bits_recv / total_time if total_time > 0 else 0.0

    return {
        "total_requests_recorded": total,
        "total_time_s": round(total_time, 3),
        "throughput_req_s": round(throughput, 3),
        "success_count": success_count,
        "error_count": error_count,
        "avg_s": round(avg, 6),
        "min_s": round(minimum, 6),
        "max_s": round(maximum, 6),
        "p50_s": round(p50, 6),
        "p90_s": round(p90, 6),
        "p99_s": round(p99, 6),
        "total_bytes_sent": int(total_bytes_sent),
        "total_bytes_received": int(total_bytes_recv),
        "total_bits_sent": int(total_bits_sent),
        "total_bits_received": int(total_bits_recv),
        "bits_per_sec_sent": round(bits_per_sec_sent, 3),
        "bits_per_sec_received": round(bits_per_sec_recv, 3)
    }

def plot_latency(results: List[Dict[str,Any]], path:str):
    elapsed_list = [r["elapsed"] for r in sorted(results,key=lambda x:x["idx"]) if isinstance(r.get("elapsed"),(int,float))]
    plt.figure(figsize=(10,5))
    plt.plot(range(1,len(elapsed_list)+1), elapsed_list, marker='o', linestyle='-', markersize=3)
    plt.xlabel("Request #")
    plt.ylabel("Latency (s)")
    plt.title("Request Latency")
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(path)
    plt.close()
    logging.info(f"Saved latency plot: {path}")

def save_html_report(results: List[Dict[str,Any]], stats:Dict[str,Any], plot_path:str, html_path:str):
    template = Template("""
    <html><head><title>Load Test Report</title></head><body>
    <h1>Load Test Report</h1>
    <h2>Summary</h2>
    <ul>
    {% for k,v in stats.items() %}
        <li>{{ k }}: {{ v }}</li>
    {% endfor %}
    </ul>
    <h2>Latency Graph</h2>
    <img src="{{ plot_path }}" alt="Latency Graph">
    </body></html>
    """)
    with open(html_path,"w",encoding="utf-8") as f:
        f.write(template.render(stats=stats, plot_path=plot_path))
    logging.info(f"Saved HTML report: {html_path}")

# ---------------- Config ----------------
class Config:
    def __init__(self, args):
        self.url = args.url
        self.no_body = args.no_body
        self.total_requests = args.requests
        self.duration = args.duration
        self.concurrency = args.concurrency
        self.method = args.method
        self.headers = dict(h.split(":",1) for h in args.header) if args.header else {}
        self.data = args.data
        self.auth_user = args.auth_user
        self.auth_pass = args.auth_pass
        self.proxy = args.proxy
        self.csv = args.csv
        self.plot = args.plot
        self.html = args.html
        self.request_timeout = args.timeout
        self.dry_run = args.dry_run
        self.allow_insecure_ssl = args.insecure
        self.ramp_up = args.ramp_up
        self.ramp_down = args.ramp_down
        if self.total_requests <= 0 or self.duration <= 0:
            raise ValueError("requests and duration must be > 0")
        if self.concurrency <= 0:
            raise ValueError("concurrency must be > 0")

def parse_args():
    p = argparse.ArgumentParser(
        description="Use only on authorized targets.",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--url", required=True)
    p.add_argument("--no-body", action="store_true", help="Ne pas lire le corps des réponses (envoie seulement)")
    p.add_argument("--requests", "-n", type=int, default=500)
    p.add_argument("--duration", "-d", type=float, default=60.0)
    p.add_argument("--concurrency", "-c", type=int, default=20)
    p.add_argument("--method", "-m", default="GET")
    p.add_argument("--header", "-H", action="append")
    p.add_argument("--data")
    p.add_argument("--auth-user")
    p.add_argument("--auth-pass")
    p.add_argument("--proxy")
    p.add_argument("--csv", default="results.csv")
    p.add_argument("--plot", default="latency.png")
    p.add_argument("--html", default="report.html")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--ramp-up", type=float, default=5.0, help="Ramp-up duration in seconds")
    p.add_argument("--ramp-down", type=float, default=5.0, help="Ramp-down duration in seconds")
    return p.parse_args()

# ---------------- Entrypoint ----------------
def main():
    args = parse_args()
    try:
        cfg = Config(args)
    except Exception as e:
        logging.error(f"Invalid configuration: {e}")
        sys.exit(1)

    logging.info(f"Configuration: URL={cfg.url} Method={cfg.method} Requests={cfg.total_requests} Duration={cfg.duration}s Concurrency={cfg.concurrency}")

    try:
        results, total_time = asyncio.run(run_test(cfg))
    except KeyboardInterrupt:
        logging.warning("Interrupted by user")
        results, total_time = [], 0.0

    if results and not cfg.dry_run:
        save_csv(results, cfg.csv)
        stats = compute_stats(results, total_time)
        plot_latency(results, cfg.plot)
        save_html_report(results, stats, cfg.plot, cfg.html)
        logging.info("----- Summary -----")
        for k,v in stats.items():
            logging.info(f"{k}: {v}")
    elif cfg.dry_run:
        logging.info("Dry-run finished. No CSV, plot, or HTML report generated.")

if __name__=="__main__":
    main()
