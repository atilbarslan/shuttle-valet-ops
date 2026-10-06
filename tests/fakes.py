"""In-memory stand-in for the Supabase client, used by the integration tests.

It implements only the part of the query builder that main.py uses (select, insert, update,
upsert, delete and the filters eq, neq, in_, is_, gt, gte, lt, lte, match, not_, plus order
and limit), with the semantics the code relies on:

- update, delete and insert return the affected rows, so "did this update match anything"
  checks behave as they do against Postgres;
- filters are applied atomically together with the write, so a conditional update such as
  `.update(...).eq("durum", old)` is a real compare-and-set;
- every execute() runs under one lock, so the client stays consistent when requests call it
  from several threads at once;
- an optional `latency` (seconds) is slept before each query, outside the lock, to stand in for
  the network round trip to the database in load measurements (scripts/load_test.py).

It is not Postgres: there are no constraints, triggers, types or joins. Columns that a table
defines with a default (see db/00_temel_sema.sql) get that default on insert only where the
tested code filters on them.
"""
import copy
import re
import threading
import time
import uuid
from datetime import datetime

# Column defaults from the schema that the tested code filters on.
_DEFAULTS = {
    "firmalar": {"is_active": True, "vale_aktif": False, "shuttle_aktif": True},
    "gorev_etaplari": {"iptal_edildi": False, "gercek_varis": None},
    "talepler": {"serviste_kapandi": False},
}

# Tables whose primary key is not `id`; upsert uses it to find the existing row.
_PRIMARY_KEYS = {"revoked_tokens": "jti", "kullanici_token_iptal": "kullanici_adi"}

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _comparable(value):
    """Compare ISO timestamps as datetimes, the way Postgres compares timestamptz values."""
    if isinstance(value, str) and _TIMESTAMP.match(value):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T"))
        except ValueError:
            return value
    return value


class FakeResponse:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, table):
        self._db = db
        self._table = table
        self._op = "select"
        self._payload = None
        self._columns = "*"
        self._filters = []
        self._negate_next = False
        self._order = None
        self._limit = None
        self._on_conflict = None

    # Operations -------------------------------------------------------------------------

    def select(self, columns="*"):
        self._op, self._columns = "select", columns
        return self

    def insert(self, data):
        self._op, self._payload = "insert", data
        return self

    def update(self, data):
        self._op, self._payload = "update", data
        return self

    def upsert(self, data, on_conflict=None):
        self._op, self._payload, self._on_conflict = "upsert", data, on_conflict
        return self

    def delete(self):
        self._op = "delete"
        return self

    # Filters ----------------------------------------------------------------------------

    def _add(self, test):
        if self._negate_next:
            self._negate_next = False
            self._filters.append(lambda row: not test(row))
        else:
            self._filters.append(test)
        return self

    @property
    def not_(self):
        self._negate_next = True
        return self

    def eq(self, column, value):
        return self._add(lambda row: _comparable(row.get(column)) == _comparable(value))

    def neq(self, column, value):
        return self._add(lambda row: _comparable(row.get(column)) != _comparable(value))

    def in_(self, column, values):
        values = list(values)
        return self._add(lambda row: row.get(column) in values)

    def is_(self, column, value):
        expected = None if value in (None, "null") else value
        return self._add(lambda row: row.get(column) is expected)

    def _compare(self, column, value, op):
        def test(row):
            current = row.get(column)
            if current is None:
                return False  # NULL never satisfies a comparison in SQL
            return op(_comparable(current), _comparable(value))
        return self._add(test)

    def gt(self, column, value):
        return self._compare(column, value, lambda a, b: a > b)

    def gte(self, column, value):
        return self._compare(column, value, lambda a, b: a >= b)

    def lt(self, column, value):
        return self._compare(column, value, lambda a, b: a < b)

    def lte(self, column, value):
        return self._compare(column, value, lambda a, b: a <= b)

    def match(self, criteria):
        for column, value in criteria.items():
            self.eq(column, value)
        return self

    # Modifiers --------------------------------------------------------------------------

    def order(self, column, desc=False):
        self._order = (column, desc)
        return self

    def limit(self, count):
        self._limit = count
        return self

    # Execution --------------------------------------------------------------------------

    def _project(self, row):
        if self._columns.strip() == "*":
            return copy.deepcopy(row)
        names = [c.strip() for c in self._columns.split(",") if c.strip()]
        return {name: copy.deepcopy(row.get(name)) for name in names}

    def _matching(self, rows):
        return [row for row in rows if all(test(row) for test in self._filters)]

    def execute(self):
        if self._db.latency:
            time.sleep(self._db.latency)  # blocking, like the real synchronous client
        with self._db.lock:
            rows = self._db.tables.setdefault(self._table, [])

            if self._op == "select":
                found = self._matching(rows)
                if self._order:
                    column, desc = self._order
                    found.sort(key=lambda r: (r.get(column) is None, _comparable(r.get(column))),
                               reverse=desc)
                if self._limit is not None:
                    found = found[: self._limit]
                return FakeResponse([self._project(r) for r in found])

            if self._op == "insert":
                payload = self._payload if isinstance(self._payload, list) else [self._payload]
                inserted = []
                for item in payload:
                    row = {"id": str(uuid.uuid4()), **_DEFAULTS.get(self._table, {}),
                           **copy.deepcopy(item)}
                    rows.append(row)
                    inserted.append(copy.deepcopy(row))
                return FakeResponse(inserted)

            if self._op == "upsert":
                key = self._on_conflict or _PRIMARY_KEYS.get(self._table, "id")
                payload = self._payload if isinstance(self._payload, list) else [self._payload]
                result = []
                for item in payload:
                    existing = next((r for r in rows if r.get(key) == item.get(key)), None)
                    if existing is not None:
                        existing.update(copy.deepcopy(item))
                        result.append(copy.deepcopy(existing))
                    else:
                        row = {"id": str(uuid.uuid4()), **_DEFAULTS.get(self._table, {}),
                               **copy.deepcopy(item)}
                        rows.append(row)
                        result.append(copy.deepcopy(row))
                return FakeResponse(result)

            if self._op == "update":
                changed = []
                for row in self._matching(rows):
                    row.update(copy.deepcopy(self._payload))
                    changed.append(copy.deepcopy(row))
                return FakeResponse(changed)

            if self._op == "delete":
                removed = self._matching(rows)
                self._db.tables[self._table] = [r for r in rows if r not in removed]
                return FakeResponse(copy.deepcopy(removed))

            raise NotImplementedError(self._op)


class FakeSupabase:
    """Drop-in replacement for the `supabase` object in main.py."""

    def __init__(self, latency=0.0):
        self.tables = {}
        self.lock = threading.RLock()
        self.latency = latency

    def table(self, name):
        return _Query(self, name)

    def rpc(self, *args, **kwargs):
        raise NotImplementedError("rpc is not used by the tested endpoints")

    # Helpers for tests ------------------------------------------------------------------

    def seed(self, table, row):
        """Insert a row directly and return it (with its generated id)."""
        return self.table(table).insert(row).execute().data[0]

    def rows(self, table, **criteria):
        """Return copies of the rows in `table` that match all `criteria`."""
        with self.lock:
            return [copy.deepcopy(r) for r in self.tables.get(table, [])
                    if all(r.get(k) == v for k, v in criteria.items())]

    def one(self, table, **criteria):
        found = self.rows(table, **criteria)
        assert len(found) == 1, f"expected one {table} row for {criteria}, found {len(found)}"
        return found[0]
