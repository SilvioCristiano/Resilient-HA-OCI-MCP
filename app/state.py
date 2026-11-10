"""One authoritative OCI Cache. CAS prevents concurrent turns from overwriting history.

No silent empty-history fallback on connectivity errors. This backend preserves context
across MCP node failures, but is NOT a cross-region durable replicated database.
"""
import hashlib
import json
import redis


class ConflictError(RuntimeError):
    pass


class SessionStore:
    def __init__(self, url):
        if not url.startswith("rediss://"):
            raise ValueError("OCI Cache requires a TLS rediss:// URL")
        self.db = redis.Redis.from_url(url, decode_responses=True,
                                      socket_connect_timeout=1, socket_timeout=2,
                                      ssl_cert_reqs="required")

    @staticmethod
    def key(session_id):
        if not 1 <= len(session_id) <= 256:
            raise ValueError("session_id length must be 1..256")
        return "mcp:session:" + hashlib.sha256(session_id.encode()).hexdigest()

    def load(self, session_id):
        raw = self.db.get(self.key(session_id))
        return raw, json.loads(raw) if raw else {"history": [], "tool_context": {}, "revision": 0}

    def commit(self, session_id, expected, state):
        key = self.key(session_id)
        with self.db.pipeline() as tx:
            try:
                tx.watch(key)
                if tx.get(key) != expected:
                    raise ConflictError("Concurrent session update; retry this turn")
                tx.multi()
                tx.set(key, json.dumps(state))
                tx.execute()
            except redis.WatchError as exc:
                raise ConflictError("Concurrent session update; retry this turn") from exc

    def health(self):
        if not self.db.ping():
            raise RuntimeError("Cache unavailable")
        # A read-only regional replica must not advertise the MCP as ready for writes.
        if self.db.info("replication").get("role") != "master":
            raise RuntimeError("Cache is not writable; role promotion required")
        return True
