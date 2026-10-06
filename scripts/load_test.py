"""Small load test: response times of a few typical endpoints under concurrent requests.

It starts the real app with uvicorn in a separate process, with the database replaced by the
in-memory client from tests/fakes.py (plus an artificial delay per query that stands in for
the network round trip to Supabase), Redis by fakeredis and the Mapbox call by a fixed answer.
Then it sends N requests per endpoint with a fixed number in flight and reports p50/p95
latency and requests per second.

This measures how the server handles waiting on the database, not how fast a real Supabase
project is. Rate limits are switched off in the server process so they do not hide the result.

Usage:
    python scripts/load_test.py --latency-ms 50 --concurrency 50 --requests 500
"""
import argparse
import asyncio
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DUMMY_ENV = {
    "JWT_SECRET_KEY": "load-test-secret-not-used-anywhere-else",
    "SUPABASE_URL": "https://example.invalid",
    "SUPABASE_KEY": "load.test.key",
    "MAPBOX_API_KEY": "load-test",
    "RESEND_API_KEY": "load-test",
    "REDIS_URL": "redis://localhost:6379/0",
    "BASE_URL": "http://localhost:8000",
    "SENTRY_DSN": "",
}


# Server process ---------------------------------------------------------------------------

def serve(port, latency_ms, info_path, threads=None):
    os.environ.update(DUMMY_ENV)
    sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]

    import fakeredis
    import uvicorn

    import main
    from fakes import FakeSupabase
    from world import make_company, make_valet_task, token_for

    db = FakeSupabase()
    main.supabase = db
    main.redis_client = fakeredis.FakeAsyncRedis(decode_responses=True)
    main.limiter.enabled = False

    async def fixed_route(*args, **kwargs):
        return {"km": 4.2, "dakika": 11}

    main.yol_mesafesi_verisi_async = fixed_route

    company = make_company(db, "load")
    for _ in range(20):  # a busy day on the panel
        make_valet_task(db, company, "VALE_ALIM", "TAMAM_SERVIS")
    task = make_valet_task(db, company, "VALE_ALIM", "VALE_YOLDA")
    db.table("araclar").update({
        "son_lat": 38.40, "son_lng": 27.10, "son_hareket_zamani": task["kayit_tarihi"],
    }).eq("id", company.valet_vehicle_id).execute()

    if threads:
        import anyio.to_thread

        async def resize_thread_pool():
            anyio.to_thread.current_default_thread_limiter().total_tokens = threads

        main.app.router.on_startup.append(resize_thread_pool)

    db.latency = latency_ms / 1000  # seeding above ran without the delay
    with open(info_path, "w") as f:
        json.dump({
            "company_id": company.id,
            "agent_token": token_for(company.user("DANISMAN")),
            "valet_token": token_for(company.user("VALE")),
            "customer_token": task["token"],
        }, f)
    uvicorn.run(main.app, host="127.0.0.1", port=port, log_level="warning")


# Client -----------------------------------------------------------------------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def percentile(values, p):
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(p / 100 * len(ordered)) - 1))
    return ordered[index]


async def measure(base, path, headers, total, concurrency):
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=base, headers=headers, limits=limits, timeout=60) as http:
        for _ in range(5):  # warm-up: auth cache, connections
            (await http.get(path)).raise_for_status()

        gate = asyncio.Semaphore(concurrency)
        latencies, errors = [], 0

        async def one():
            nonlocal errors
            async with gate:
                start = time.perf_counter()
                response = await http.get(path)
                latencies.append((time.perf_counter() - start) * 1000)
                if response.status_code != 200:
                    errors += 1

        started = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(total)))
        elapsed = time.perf_counter() - started

    return {
        "p50_ms": round(statistics.median(latencies), 1),
        "p95_ms": round(percentile(latencies, 95), 1),
        "rps": round(total / elapsed, 1),
        "errors": errors,
    }


async def run_all(base, info, total, concurrency):
    cases = {
        "GET /firma-talepleri (panel)": (
            f"/firma-talepleri?firma_id={info['company_id']}",
            {"Authorization": f"Bearer {info['agent_token']}"}),
        "GET /vale-gorevi (valet app)": (
            "/vale-gorevi", {"Authorization": f"Bearer {info['valet_token']}"}),
        "GET /talep-detay/{token} (customer page)": (
            f"/talep-detay/{info['customer_token']}", {}),
    }
    results = {}
    for name, (path, headers) in cases.items():
        results[name] = await measure(base, path, headers, total, concurrency)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--latency-ms", type=float, default=50)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--requests", type=int, default=500)
    parser.add_argument("--threads", type=int,
                        help="size of the server's thread pool (default: anyio's 40)")
    parser.add_argument("--json", help="also write the results to this file")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--info", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.serve:
        serve(args.port, args.latency_ms, args.info, args.threads)
        return

    port = free_port()
    info_path = os.path.join(tempfile.mkdtemp(), "info.json")
    command = [sys.executable, __file__, "--serve", "--port", str(port),
               "--latency-ms", str(args.latency_ms), "--info", info_path]
    if args.threads:
        command += ["--threads", str(args.threads)]
    server = subprocess.Popen(command)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(300):
            try:
                if os.path.exists(info_path) and httpx.get(base + "/sw.js").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        else:
            raise SystemExit("server did not start")
        with open(info_path) as f:
            info = json.load(f)

        results = asyncio.run(run_all(base, info, args.requests, args.concurrency))
    finally:
        server.terminate()
        server.wait()

    print(f"latency {args.latency_ms:g} ms per query, {args.concurrency} in flight, "
          f"{args.requests} requests per endpoint, thread pool {args.threads or 'default (40)'}")
    print(f"{'endpoint':42} {'p50 ms':>8} {'p95 ms':>8} {'req/s':>8} {'errors':>7}")
    for name, r in results.items():
        print(f"{name:42} {r['p50_ms']:>8} {r['p95_ms']:>8} {r['rps']:>8} {r['errors']:>7}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"settings": vars(args), "results": results}, f, indent=2)


if __name__ == "__main__":
    main()
