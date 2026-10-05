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
