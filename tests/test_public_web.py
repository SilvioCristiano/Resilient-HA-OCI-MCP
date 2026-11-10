"""Authorization and ownership at the real public HTTP boundary."""
import copy
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
import unittest
import uuid

WEB = Path(__file__).resolve().parents[1] / 'web'
sys.path.insert(0, str(WEB))
import control as real_control
spec = importlib.util.spec_from_file_location('public_web_server', WEB/'server.py')
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)


class Memory:
    def __init__(self):
        self.data = {}
        self.calls = []

    def session(self, sid):
        return copy.deepcopy(self.data.get(sid, {'messages': [], 'last_result': None, 'busy': False}))

    def get(self, *_):
        return {}

    def events(self):
        return []

    def rate(self, *_):
        return True


class Control:
    def __init__(self):
        self.calls = []

    def state(self):
        return {'phase': 'ready'}

    def call(self, action):
        self.calls.append(action)
        return {'phase': 'fault'}

    def verify(self, *_):
        return False


class PublicWebTests(unittest.TestCase):
    def setUp(self):
        self.store, self.control = Memory(), Control()
        self.app = web.Application(self.store, None, 'separate-session-signing-key-at-least-32-bytes',
            hashlib.sha256(b'private-presenter-password').hexdigest(), self.control, 'sa-saopaulo-1')
        self.app.start = lambda *args: self.store.calls.append(args) or 'accepted'
        self.http = web.ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
        self.http.app = self.app
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.sid = str(uuid.uuid4())
        self.cookie = web.COOKIE + '=' + self.app.signed('session', self.sid)

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.app.pool.shutdown()

    def request(self, path, method='GET', body=None, cookie=None, origin=web.ORIGIN, csrf=True):
        conn = http.client.HTTPConnection('127.0.0.1', self.http.server_port)
        headers = {'Host': web.urlsplit(web.ORIGIN).netloc, 'Cookie': cookie or self.cookie,
                   'Origin': origin, 'Content-Type': 'application/json'}
        if csrf:
            headers['X-Demo-CSRF'] = self.app.mac('csrf', self.sid)
        conn.request(method, path, json.dumps(body or {}) if method == 'POST' else None, headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        conn.close()
        return result

    def test_participant_has_chat_but_no_presenter_controls_or_authority(self):
        status, _, html = self.request('/chat/')
        self.assertEqual(status, 200)
        self.assertIn('chat-form', html)
        self.assertNotIn('id="interrupt"', html)
        self.assertNotIn('id="act2"', html)
        self.assertNotIn('id="restore"', html)
        for path in ('fault', 'restore'):
            status, _, _ = self.request('/chat/api/admin/' + path, 'POST')
            self.assertEqual(status, 403)
        self.assertEqual(self.control.calls, [])

    def test_login_grants_presenter_control_with_secure_cookie_and_rejects_forgery(self):
        self.assertEqual(self.request('/chat/api/presenter/login', 'POST', {'password':'wrong'})[0], 401)
        status, headers, body = self.request('/chat/api/presenter/login', 'POST', {'password':'private-presenter-password'})
        self.assertEqual(status, 200)
        auth = headers['Set-Cookie']
        for flag in ('Secure', 'HttpOnly', 'SameSite=Strict'):
            self.assertIn(flag, auth)
        combined = self.cookie + '; ' + auth.split(';')[0]
        self.assertEqual(self.request('/chat/api/admin/fault', 'POST', cookie=combined)[0], 200)
        self.assertEqual(self.control.calls, ['fault'])
        forged = self.cookie + '; ' + web.PRESENTER + '=9999999999:attacker.forged'
        self.assertEqual(self.request('/chat/api/admin/restore', 'POST', cookie=forged)[0], 403)
        self.assertEqual(self.control.calls, ['fault'])

    def test_cross_origin_and_missing_csrf_never_reach_chat_or_control(self):
        self.assertEqual(self.request('/chat/api/chat', 'POST', {'prompt':'hi'}, origin='https://attacker.test')[0], 403)
        self.assertEqual(self.request('/chat/api/chat', 'POST', {'prompt':'hi'}, csrf=False)[0], 403)
        self.assertEqual(self.store.calls, [])

    def test_session_ownership_cannot_be_changed_by_body_or_query(self):
        other = str(uuid.uuid4())
        self.store.data[other] = {'messages':[{'text':'other private conversation'}], 'last_result':None, 'busy':False}
        status, _, data = self.request('/chat/api/status?session_id=' + other)
        self.assertEqual(status, 200)
        self.assertNotIn('other private conversation', data)
        rid = str(uuid.uuid4())
        self.assertEqual(self.request('/chat/api/chat', 'POST', {'session_id':other,'prompt':'hello','request_id':rid})[0], 202)
        self.assertEqual(self.store.calls, [(self.sid, 'hello', rid)])

    def test_unsigned_session_and_internal_control_are_rejected(self):
        self.assertEqual(self.request('/chat/api/status', cookie=web.COOKIE+'='+self.sid)[0], 401)
        self.assertEqual(self.request('/chat/internal/control', 'POST', {'action':'fault'})[0], 403)
        self.assertFalse(self.app.presenter_cookie(self.app.signed('presenter', str(int(time.time())-1)+':expired')))
        self.assertEqual(self.control.calls, [])

    def test_cross_region_control_signature_expires_and_cannot_be_replayed(self):
        class Nonces:
            def __init__(self): self.seen = set()
            def set(self, key, value, **kwargs):
                if key in self.seen: return False
                self.seen.add(key)
                return True
        class ControlStore:
            def __init__(self): self.db = Nonces()
            def key(self, value): return 'racing:web:v1:' + value
        controller = real_control.Control(ControlStore(), 'sa-saopaulo-1', 'x'*48, None)
        body = b'{"action":"fault"}'
        headers = {'X-Control-Time':str(int(time.time())), 'X-Control-Nonce':uuid.uuid4().hex}
        headers['X-Control-Signature'] = controller.signature(headers['X-Control-Time'], headers['X-Control-Nonce'], body)
        self.assertFalse(controller.verify({}, body))
        self.assertFalse(controller.verify(headers, b'{"action":"restore"}'))
        self.assertTrue(controller.verify(headers, body))
        self.assertFalse(controller.verify(headers, body))
        headers['X-Control-Time'] = str(int(time.time())-60)
        headers['X-Control-Nonce'] = uuid.uuid4().hex
        headers['X-Control-Signature'] = controller.signature(headers['X-Control-Time'], headers['X-Control-Nonce'], body)
        self.assertFalse(controller.verify(headers, body))


if __name__ == '__main__':
    unittest.main()
