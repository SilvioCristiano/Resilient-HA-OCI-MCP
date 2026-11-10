import json
import sys
import types
import unittest
from unittest.mock import Mock
import oci
from app.oci_llm_client import OCILLMClient, REGIONS

# Exercise the actual CAS code without requiring a running Cache instance.
try:
    import redis
except ImportError:
    sys.modules["redis"] = types.SimpleNamespace(WatchError=type("WatchError", (Exception,), {}))
from app.state import SessionStore, ConflictError


class Pipeline:
    def __init__(self, db):
        self.db = db
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def watch(self, key):
        pass
    def get(self, key):
        return self.db.get(key)
    def multi(self):
        pass
    def set(self, key, value):
        self.pending = key, value
    def execute(self):
        self.db.values[self.pending[0]] = self.pending[1]


class Database:
    def __init__(self):
        self.values = {}
    def get(self, key):
        return self.values.get(key)
    def pipeline(self):
        return Pipeline(self)


class ResilienceTests(unittest.TestCase):
    def test_read_only_cache_does_not_advertise_write_readiness(self):
        store = object.__new__(SessionStore)
        store.db = Mock()
        store.db.ping.return_value = True
        store.db.info.return_value = {"role": "slave"}
        with self.assertRaises(RuntimeError):
            store.health()

    def test_writable_cache_advertises_readiness(self):
        store = object.__new__(SessionStore)
        store.db = Mock()
        store.db.ping.return_value = True
        store.db.info.return_value = {"role": "master"}
        self.assertTrue(store.health())

    def test_secondary_process_resumes_external_state(self):
        first, second = object.__new__(SessionStore), object.__new__(SessionStore)
        first.db = second.db = Database()
        raw, state = first.load("session")
        state["history"] = [{"role": "USER", "text": "Remember 4937"}]
        state["tool_context"] = {"reference": "4937"}
        state["revision"] = 1
        first.commit("session", raw, state)
        self.assertEqual(second.load("session")[1], state)

    def test_concurrent_update_does_not_overwrite_history(self):
        store = object.__new__(SessionStore)
        store.db = Database()
        old, state = store.load("same")
        store.commit("same", old, dict(state, revision=1))
        with self.assertRaises(ConflictError):
            store.commit("same", old, dict(state, revision=2))
        self.assertEqual(store.load("same")[1]["revision"], 1)

    def test_state_outage_is_not_empty_history(self):
        store = object.__new__(SessionStore)
        store.db = Mock()
        store.db.get.side_effect = ConnectionError("Cache unavailable")
        with self.assertRaises(ConnectionError):
            store.load("session")

    def client(self, failure):
        client = object.__new__(OCILLMClient)
        client.compartment = "compartment"
        client.models = {r: "model" for r in REGIONS}
        response = types.SimpleNamespace(data=types.SimpleNamespace(chat_response=types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(
                content=[types.SimpleNamespace(text="4937")]))])), headers={})
        client.clients = {REGIONS[0]: Mock(), REGIONS[1]: Mock()}
        client.clients[REGIONS[0]].chat.side_effect = failure
        client.clients[REGIONS[1]].chat.return_value = response
        return client

    def test_transient_inference_error_routes_with_full_history(self):
        client = self.client(oci.exceptions.ServiceError(503, "Unavailable", {}, "down"))
        history = [{"role": "USER", "text": "Remember 4937"},
                   {"role": "ASSISTANT", "text": "OK"}, {"role": "USER", "text": "Recall"}]
        result = client.chat(history)
        details = client.clients[REGIONS[1]].chat.call_args.args[0]
        self.assertEqual([m.content[0].text for m in details.chat_request.messages],
                         [m["text"] for m in history])
        self.assertEqual(result["inference_api_region"], REGIONS[1])
        self.assertEqual(result["model_execution_region"], REGIONS[0])

    def test_auth_error_does_not_masquerade_as_regional_outage(self):
        client = self.client(oci.exceptions.ServiceError(401, "Unauthorized", {}, "bad credentials"))
        with self.assertRaises(oci.exceptions.ServiceError):
            client.chat([{"role": "USER", "text": "test"}])
        client.clients[REGIONS[1]].chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
