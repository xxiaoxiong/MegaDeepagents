"""Functional check: cold_memory + fts share the canonical connection."""

from app.infrastructure.database.connection import get_connection as canonical
from app.memory.cold_memory import get_cold_memory, get_connection
from app.memory.fts import get_cold_memory_conn, search_fts

c0 = canonical()
c1 = get_connection()
c2 = get_cold_memory_conn()
print("c1 is canonical:", c1 is c0)
print("c2 is canonical:", c2 is c0)
print("c1 is c2:", c1 is c2)
print("c1 id:", id(c1), "c2 id:", id(c2), "c0 id:", id(c0))

cm = get_cold_memory()
sid = "verify_mem_session"
cm.create_session(sid)
cm.add_message(sid, "user", "Please review the authentication module refactor.")
hits = search_fts("authentication", limit=5)
print("english fts hits:", len(hits))
