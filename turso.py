"""Lightweight Turso HTTP client — sqlite3.Connection-compatible interface.

No native builds required. Uses httpx over Turso's /v2/pipeline HTTPS API.
"""

import sqlite3
import threading

_local = threading.local()


class TursoCursor:
    def __init__(self, rows: list, lastrowid: int | None = None, rowcount: int = -1):
        self._rows = rows
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class TursoRow(dict):
    """dict that also supports integer index access like sqlite3.Row."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class TursoConnection:
    """sqlite3.Connection-compatible wrapper over Turso's HTTP API."""

    def __init__(self, url: str, token: str):
        import httpx
        self._http_url = url.replace("libsql://", "https://") + "/v2/pipeline"
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._client = httpx.Client(timeout=30)
        self.row_factory = None

    def execute(self, sql: str, parameters=()) -> TursoCursor:
        args = [
            {
                "type": (
                    "integer" if isinstance(p, int) else
                    "float"   if isinstance(p, float) else
                    "null"    if p is None else "text"
                ),
                "value": str(p) if p is not None else None,
            }
            for p in parameters
        ]

        payload = {
            "requests": [
                {"type": "execute", "stmt": {"sql": sql, "args": args}},
                {"type": "close"},
            ]
        }
        resp = self._client.post(self._http_url, json=payload, headers=self._headers)
        resp.raise_for_status()
        data = resp.json()

        result = data["results"][0]
        if result.get("type") == "error":
            raise sqlite3.OperationalError(result["error"]["message"])

        rows_data = result.get("response", {}).get("result", {})
        cols = [c["name"] for c in rows_data.get("cols", [])]
        rows = []
        for raw in rows_data.get("rows", []):
            row = TursoRow()
            for col, cell in zip(cols, raw):
                row[col] = cell.get("value") if cell.get("type") != "null" else None
            rows.append(row)

        lastrowid = rows_data.get("last_insert_rowid")
        affected = rows_data.get("affected_row_count", -1)
        return TursoCursor(
            rows,
            int(lastrowid) if lastrowid is not None else None,
            rowcount=int(affected) if affected is not None else -1,
        )

    def commit(self) -> None:
        pass  # Turso auto-commits each statement

    def close(self) -> None:
        self._client.close()
