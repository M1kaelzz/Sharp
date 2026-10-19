"""Data-access layer: SQL lives here, one module per domain.

Every function takes an open ``sqlite3.Connection`` and never opens its own —
the transaction boundary stays with the caller's ``with get_conn()`` block, so
multi-statement routes (claim/complete lock sequences, reopen) remain atomic.
HTTP concerns (HTTPException, status codes) stay in the routers/services;
functions here return rows, models, or None.
"""
