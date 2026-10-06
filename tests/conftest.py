"""Test setup: dummy configuration so main.py can be imported without any real service.

main.py refuses to start without its required environment variables, and its load_dotenv()
call never overrides a variable that is already set. Setting every variable here first means
a real .env file is not used by the tests. Nothing is contacted at import time: the Supabase
and Redis clients only connect when a query is made, and none of the tested functions make one.
"""
import os
import sys

_DUMMY_ENV = {
    "JWT_SECRET_KEY": "test-secret-not-used-anywhere-else",
    "SUPABASE_URL": "https://example.invalid",
    "SUPABASE_KEY": "test.dummy.key",
    "MAPBOX_API_KEY": "test-mapbox-key",
    "RESEND_API_KEY": "test-resend-key",
    "REDIS_URL": "redis://localhost:6379/0",
    "BASE_URL": "http://localhost:8000",
    "SENTRY_DSN": "",  # empty: error tracking stays off during tests
}
os.environ.update(_DUMMY_ENV)

# The backend is a single module in the repository root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


# Integration test fixtures ----------------------------------------------------------------
# The endpoint tests call the real FastAPI app over HTTP (httpx + ASGI transport). Only the
# outside world is replaced: Supabase by the in-memory client in fakes.py, Redis by fakeredis,
# and the Mapbox travel-time call by a fixed answer.
import fakeredis  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402

from fakes import FakeSupabase  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def db(monkeypatch):
    import main

    fake = FakeSupabase()
    monkeypatch.setattr(main, "supabase", fake)

    async def fixed_route(*args, **kwargs):
        return {"km": 4.2, "dakika": 11}

    monkeypatch.setattr(main, "yol_mesafesi_verisi_async", fixed_route)
    main._auth_cache.clear()
    main.limiter.reset()
    return fake


@pytest.fixture
async def client(db, monkeypatch):
    import main

    monkeypatch.setattr(main, "redis_client", fakeredis.FakeAsyncRedis(decode_responses=True))
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
