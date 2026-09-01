"""Lightweight Supabase PostgREST client using httpx (no heavy SDK)."""

import os
import httpx

_URL = None
_KEY = None


def _init():
    global _URL, _KEY
    if _URL is None:
        _URL = os.environ["SUPABASE_URL"].strip()
        _KEY = os.environ["SUPABASE_ANON_KEY"].strip()


def _headers(extra=None):
    h = {"apikey": _KEY, "Authorization": f"Bearer {_KEY}", "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


class QueryResult:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class QueryBuilder:
    def __init__(self, table):
        _init()
        self._table = table
        self._select_cols = "*"
        self._filters = []
        self._order_col = None
        self._order_desc = False
        self._limit_val = None
        self._single = False
        self._count_mode = None

    def select(self, cols, count=None):
        self._select_cols = cols
        self._count_mode = count
        return self

    def eq(self, col, val):
        self._filters.append((col, "eq", val))
        return self

    def neq(self, col, val):
        self._filters.append((col, "neq", val))
        return self

    def order(self, col, desc=False):
        self._order_col = col
        self._order_desc = desc
        return self

    def limit(self, n):
        self._limit_val = n
        return self

    def single(self):
        self._single = True
        self._limit_val = 1
        return self

    def execute(self):
        params = {"select": self._select_cols}
        for col, op, val in self._filters:
            val_str = str(val).lower() if isinstance(val, bool) else str(val)
            params[col] = f"{op}.{val_str}"
        if self._order_col:
            params["order"] = f"{self._order_col}.{'desc' if self._order_desc else 'asc'}"
        if self._limit_val:
            params["limit"] = str(self._limit_val)

        headers = _headers()
        if self._count_mode == "exact":
            headers["Prefer"] = "count=exact"

        r = httpx.get(f"{_URL}/rest/v1/{self._table}", params=params, headers=headers, timeout=15)
        r.raise_for_status()

        count = None
        if self._count_mode == "exact":
            cr = r.headers.get("content-range", "")
            if "/" in cr:
                count = int(cr.split("/")[1]) if cr.split("/")[1] != "*" else 0

        data = r.json()
        if self._single and isinstance(data, list):
            data = data[0] if data else None

        return QueryResult(data, count)


class InsertBuilder:
    def __init__(self, table):
        _init()
        self._table = table

    def insert(self, row):
        self._row = row
        return self

    def execute(self):
        r = httpx.post(
            f"{_URL}/rest/v1/{self._table}",
            json=self._row,
            headers=_headers({"Prefer": "return=minimal"}),
            timeout=15,
        )
        r.raise_for_status()
        return QueryResult([])


class TableProxy:
    def __init__(self, table):
        self._table = table

    def select(self, cols, count=None):
        return QueryBuilder(self._table).select(cols, count=count)

    def insert(self, row):
        return InsertBuilder(self._table).insert(row)

    def eq(self, col, val):
        return QueryBuilder(self._table).eq(col, val)


class Client:
    def from_(self, table):
        return QueryBuilder(table)

    def table(self, table):
        return TableProxy(table)

    def rpc(self, fn_name, params):
        _init()
        r = httpx.post(
            f"{_URL}/rest/v1/rpc/{fn_name}",
            json=params,
            headers=_headers(),
            timeout=15,
        )
        r.raise_for_status()
        return QueryResult(r.json() if r.text else None)


_client = None


def get_client() -> Client:
    global _client
    if _client is None:
        _client = Client()
    return _client
