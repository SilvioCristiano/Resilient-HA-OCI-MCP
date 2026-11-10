"""Public multi-user chat and authenticated presenter view, on the existing OKEs."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import threading
import time
from urllib.parse import urlsplit
import uuid

from control import Control, PRIMARY
from mcp_client import MCPClient, REGIONS, HOST, Unavailable
from storage import Store, Busy, TTL

ROOT = Path(__file__).resolve().parent
ORIGIN = os.environ.get('WEB_ORIGIN', 'https://chat.example.invalid').rstrip('/')
HOSTS = {urlsplit(ORIGIN).netloc.lower(), os.environ.get('MCP_HOST', 'mcp.example.invalid').lower()}
COOKIE = '__Host-racing-session'
PRESENTER = '__Host-racing-presenter'


def now():
    return datetime.now(timezone.utc).isoformat()


class Application:
    def __init__(self, store, client, secret, password_hash, control, region):
        self.store, self.client, self.secret = store, client, secret.encode()
        self.password_hash, self.control, self.region = password_hash, control, region
        self.pool = ThreadPoolExecutor(max_workers=12)

    def mac(self, purpose, value):
        return hmac.new(self.secret, (purpose + ':' + value).encode(), hashlib.sha256).hexdigest()

    def signed(self, purpose, value):
        return value + '.' + self.mac(purpose, value)

    def verify(self, purpose, value):
        try:
            raw, signature = value.rsplit('.', 1)
            if hmac.compare_digest(signature, self.mac(purpose, raw)):
                return raw
        except (ValueError, AttributeError):
            pass
        return None

    def session_cookie(self, value):
        raw = self.verify('session', value)
        try:
            return str(uuid.UUID(raw)) if raw else None
        except ValueError:
            return None

    def presenter_cookie(self, value):
        raw = self.verify('presenter', value)
        try:
            return bool(raw and time.time() < int(raw.split(':')[0]))
        except ValueError:
            return False

    def snapshot(self, sid, presenter=False):
        session = self.store.session(sid)
        control = self.control.state()
        public_control = {k: control[k] for k in ('phase', 'message', 'fault_at', 'restore_at', 'restored_at') if k in control}
        return {**self.store.get('status', {}), 'session_id': sid, 'session': session,
                'role': 'presenter' if presenter else 'participant',
                'admin': {**public_control, 'enabled': presenter},
                'events': self.store.events(), 'web_region': self.region}

    def start(self, sid, prompt, request_id):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000:
            raise ValueError('Escreva uma mensagem de até 8.000 caracteres.')
        rid = str(uuid.UUID(request_id))
        job_id = sid + ':' + rid
        if self.store.get('job:' + job_id):
            return job_id
        lock = self.store.lock('session-lock:' + sid, 600)
        accepted = False
        slot = False
        try:
            if self.store.get('job:' + job_id):
                return job_id
            session = self.store.session(sid)
            if session['busy']:
                raise Busy('Aguarde a resposta da sua mensagem anterior.')
            if len(session['messages']) >= 100:
                raise ValueError('Esta conversa atingiu o limite de turnos. Inicie uma nova conversa.')
            for name, limit, window in [('user:' + sid, 6, 60), ('global-minute', 60, 60),
                                       ('global-hour', 600, 3600), ('global-day', 2000, 86400)]:
                if not self.store.rate(name, limit, window):
                    raise Busy('Limite de participação atingido. Aguarde um pouco antes de enviar novamente.')
            slot = bool(self.store.db.eval("redis.call('zremrangebyscore',KEYS[1],'-inf',ARGV[1]); if redis.call('zcard',KEYS[1])>=12 then return 0 end redis.call('zadd',KEYS[1],ARGV[2],ARGV[3]); redis.call('expire',KEYS[1],660); return 1",
                1, self.store.key('slots'), time.time(), time.time()+600, job_id))
            if not slot:
                raise Busy('O laboratório está atendendo outros participantes. Tente novamente em alguns instantes.')
            job = {'stage': 'sending', 'started_at': time.time(), 'attempts': 0,
                   'message': 'Consultando o MCP pelo endereço global…'}
            session['messages'].append({'role': 'user', 'text': prompt, 'at': now()})
            session.pop('job', None)
            session['busy'] = True
            # One atomic visibility point for the accepted turn and its durable job.
            tx = self.store.db.pipeline(transaction=True)
            tx.set(self.store.key('session:' + sid), json.dumps(session, ensure_ascii=False), ex=TTL)
            tx.set(self.store.key('job:' + job_id), json.dumps(job), ex=TTL)
            tx.set(self.store.key('active:' + sid), json.dumps(job_id), ex=TTL)
            tx.execute()
            self.pool.submit(self.work, sid, prompt, job_id, lock)
            accepted = True
            return job_id
        finally:
            if not accepted:
                self.store.release('session-lock:' + sid, lock)
                if slot:
                    self.store.db.zrem(self.store.key('slots'), job_id)

    def work(self, sid, prompt, job_id, lock):
        job = self.store.get('job:' + job_id)
        started = time.monotonic()
        try:
            while time.monotonic() - started < 240:
                job['attempts'] += 1
                self.store.put('job:' + job_id, job)
                try:
                    result, latency = self.client.call_tool('ask_oci_genai', {'prompt': prompt, 'session_id': sid})
                    break
                except Unavailable:
                    job.update(stage='reconnecting', message='Reconectando pelo DNS global. Sua conversa permanece aberta.')
                    self.store.put('job:' + job_id, job)
                    time.sleep(2)
            else:
                raise RuntimeError('Tempo de reconexão excedido')
            session = self.store.get('session:' + sid)
            previous = session['last_result']
            measured = {**result, 'request_seconds': round(latency, 3),
                'elapsed_seconds': round(time.monotonic() - started, 3),
                'region_changed': bool(previous and previous['app_region'] != result['app_region']),
                'received_at': now(), 'context_available': result.get('history_messages', 0) >= 4}
            state = self.control.state()
            if state.get('fault_at') and result['app_region'] == 'sa-vinhedo-1':
                measured['since_fault_seconds'] = round(time.time() - state['fault_at'], 3)
            session['messages'].append({'role': 'assistant', 'text': result['answer'], 'at': now(), 'telemetry': measured})
            session.update(last_result=measured, busy=False)
            job.update(stage='done', message='Resposta confirmada', result=measured)
            tx = self.store.db.pipeline(transaction=True)
            for suffix, value in [('session:' + sid, session), ('job:' + job_id, job)]:
                tx.set(self.store.key(suffix), json.dumps(value, ensure_ascii=False), ex=TTL)
            tx.execute()
            # No user prompts, answers, session IDs or credentials in global telemetry/logs.
        except Exception as exc:
            job.update(stage='error', message='Não foi possível confirmar a resposta. O turno não foi repetido após um erro de execução ou transporte incerto.')
            self.store.put('job:' + job_id, job)
            session = self.store.get('session:' + sid)
            session['busy'] = False
            self.store.put('session:' + sid, session)
            print(json.dumps({'event': 'chat_unconfirmed', 'type': type(exc).__name__}), flush=True)
        finally:
            self.store.release('session-lock:' + sid, lock)
            self.store.db.zrem(self.store.key('slots'), job_id)

    def monitor(self):
        last_health = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            while True:
                try:
                    # Recovery is not tied to a browser, a presenter login or a Bastion.
                    self.control.reconcile()
                    lease = self.store.lock('monitor-lock', 15)
                    try:
                        regional = {r: f.result() for r, f in
                                    [(r, pool.submit(self.client.regional_health, r)) for r in REGIONS]}
                        for region, health in regional.items():
                            if region in last_health and last_health[region] != health['healthy']:
                                self.store.event(('Endpoint recuperado: ' if health['healthy'] else 'Endpoint indisponível: ') + region,
                                                 'Sonda HTTPS /readyz: ' + str(health.get('http_status') or health.get('error')),
                                                 'success' if health['healthy'] else 'danger')
                            last_health[region] = health['healthy']
                        try:
                            dns = sorted({r[4][0] for r in socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)})
                        except OSError:
                            dns = []
                        active = None
                        try:
                            active, _ = self.client.call_tool('analyze_resilience_status', {})
                        except Exception:
                            pass
                        self.store.put('status', {'regions': regional, 'dns_ips': dns, 'global': active, 'observed_at': now()}, ttl=45)
                    finally:
                        self.store.release('monitor-lock', lease)
                except Busy:
                    pass
                except Exception as exc:
                    print(json.dumps({'event': 'monitor_error', 'type': type(exc).__name__}), flush=True)
                time.sleep(4)


class Handler(BaseHTTPRequestHandler):
    server_version = 'RacingChat'

    @property
    def app(self):
        return self.server.app

    def log_message(self, *_):
        pass

    def cookie(self, name):
        try:
            return SimpleCookie(self.headers.get('Cookie', '')).get(name).value
        except (AttributeError, KeyError):
            return ''

    def send(self, status, data, content_type='application/json; charset=utf-8', cookies=None, location=None):
        raw = data if isinstance(data, bytes) else (data.encode() if isinstance(data, str) else json.dumps(data, ensure_ascii=False).encode())
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if location:
            self.send_header('Location', location)
        for name, value, age in cookies or []:
            self.send_header('Set-Cookie', f'{name}={value}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={age}')
        self.end_headers()
        self.wfile.write(raw)

    def allowed_host(self):
        return self.headers.get('Host', '').lower() in HOSTS

    def identity(self):
        return self.app.session_cookie(self.cookie(COOKIE))

    def presenter(self):
        return self.app.presenter_cookie(self.cookie(PRESENTER))

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/livez':
            return self.send(200, {'live': True})
        if path == '/readyz':
            try:
                self.app.store.db.ping()
                return self.send(200, {'ready': True, 'web_region': self.app.region})
            except Exception:
                return self.send(503, {'ready': False})
        if not self.allowed_host():
            return self.send(421, {'error': 'Hostname inválido'})
        if path in ('/', '/chat'):
            return self.send(302, b'', location='/chat/')
        assets = {'/chat/app.js': ('app.js', 'text/javascript'), '/chat/style.css': ('style.css', 'text/css'),
                  '/chat/racing-car.webp': ('racing-car.webp', 'image/webp')}
        if path in assets:
            name, mime = assets[path]
            return self.send(200, (ROOT / name).read_bytes(), mime)
        sid = self.identity()
        if path in ('/chat/', '/chat/presenter'):
            sid = sid or str(uuid.uuid4())
            name = 'login.html' if path == '/chat/presenter' and not self.presenter() else 'index.html'
            html = (ROOT / name).read_text()
            role = 'presenter' if self.presenter() else 'participant'
            if role != 'presenter':
                html = re.sub(r'<!-- PRESENTER -->.*?<!-- /PRESENTER -->', '', html, flags=re.S)
            html = html.replace('__DEMO_CSRF__', self.app.mac('csrf', sid)).replace('__SESSION_ID__', sid).replace('__ROLE__', role)
            return self.send(200, html, 'text/html; charset=utf-8',
                             cookies=[(COOKIE, self.app.signed('session', sid), TTL)])
        if path == '/chat/api/status':
            if not sid:
                return self.send(401, {'error': 'Abra o chat para iniciar sua sessão.'})
            try:
                return self.send(200, self.app.snapshot(sid, self.presenter()))
            except Exception:
                return self.send(503, {'error': 'A memória compartilhada está temporariamente indisponível.'})
        return self.send(404, {'error': 'Não encontrado'})

    def do_POST(self):
        path = urlsplit(self.path).path
        if not self.allowed_host():
            return self.send(421, {'error': 'Hostname inválido'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json' or not 0 < size <= 40000:
                return self.send(400, {'error': 'Corpo inválido'})
            body = self.rfile.read(size)
            if path == '/chat/internal/control':
                if self.app.region != PRIMARY or not self.app.control.verify(self.headers, body):
                    return self.send(403, {'error': 'Controle não autorizado'})
                return self.send(200, self.app.control.call(json.loads(body)['action']))
            sid = self.identity()
            if not sid or self.headers.get('Origin') != ORIGIN or not hmac.compare_digest(
                    self.headers.get('X-Demo-CSRF', ''), self.app.mac('csrf', sid)):
                return self.send(403, {'error': 'Sessão ou origem não autorizada'})
            data = json.loads(body)
            if path == '/chat/api/presenter/login':
                # One global login limiter is deliberate: spoofed forwarding headers are not trusted.
                if not self.app.store.rate('presenter-login', 10, 60):
                    return self.send(429, {'error': 'Aguarde antes de tentar entrar novamente.'})
                password = data.get('password', '')
                if not isinstance(password, str) or len(password) > 256 or not hmac.compare_digest(
                    hashlib.sha256(password.encode()).hexdigest(), self.app.password_hash):
                    return self.send(401, {'error': 'Acesso do apresentador inválido.'})
                token = self.app.signed('presenter', str(int(time.time()) + 14400) + ':' + secrets.token_hex(16))
                return self.send(200, {'authenticated': True}, cookies=[(PRESENTER, token, 14400)])
            if path.startswith('/chat/api/admin/'):
                if not self.presenter():
                    return self.send(403, {'error': 'Somente o apresentador pode controlar o ambiente.'})
                action = path.rsplit('/', 1)[1]
                if action == 'logout':
                    return self.send(200, {'authenticated': False}, cookies=[(PRESENTER, '', 0)])
                return self.send(200, self.app.control.call(action))
            if path == '/chat/api/session':
                if self.app.store.session(sid)['busy']:
                    raise Busy('Aguarde a mensagem em andamento.')
                new_id = str(uuid.uuid4())
                return self.send(200, {'session_id': new_id}, cookies=[(COOKIE, self.app.signed('session', new_id), TTL)])
            if path == '/chat/api/chat':
                # Ignore any caller-supplied session_id. Ownership comes only from the signed HttpOnly cookie.
                return self.send(202, {'job_id': self.app.start(sid, data.get('prompt'), data.get('request_id'))})
        except Busy as exc:
            return self.send(429, {'error': str(exc)})
        except (ValueError, TypeError, KeyError):
            return self.send(400, {'error': 'Requisição inválida ou estado do ensaio incompatível.'})
        except Exception as exc:
            print(json.dumps({'event': 'request_error', 'type': type(exc).__name__}), flush=True)
            return self.send(503, {'error': 'A operação não foi confirmada. Consulte o estado antes de tentar novamente.'})
        return self.send(404, {'error': 'Não encontrado'})


def create_app():
    import redis
    if urlsplit(ORIGIN).scheme != 'https' or any(host.endswith('.invalid') for host in HOSTS):
        raise ValueError('Configure WEB_ORIGIN HTTPS e MCP_HOST para o seu domínio')
    if any(len(os.environ[name]) < 32 for name in ('WEB_SESSION_SECRET', 'CONTROL_SECRET', 'MCP_TOKEN')):
        raise ValueError('Credenciais de configuração insuficientes')
    db = redis.Redis.from_url(os.environ['REDIS_URL'], decode_responses=True, socket_connect_timeout=2,
                              socket_timeout=3, ssl_cert_reqs='required')
    client = MCPClient()
    store = Store(db)
    region = os.environ['OCI_REGION']
    control = Control(store, region, os.environ['CONTROL_SECRET'], client.context)
    return Application(store, client, os.environ['WEB_SESSION_SECRET'], os.environ['PRESENTER_PASSWORD_HASH'], control, region)


if __name__ == '__main__':
    app = create_app()
    threading.Thread(target=app.monitor, daemon=True).start()
    server = ThreadingHTTPServer(('0.0.0.0', 8088), Handler)
    server.daemon_threads = True
    server.app = app
    print(json.dumps({'event': 'web_started', 'region': app.region, 'participant_path': '/chat/', 'presenter_path': '/chat/presenter'}), flush=True)
    server.serve_forever()
