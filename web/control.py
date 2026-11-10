"""Presenter-only control of one fixed MCP Deployment, never the web application."""
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import time
import urllib.request

PRIMARY = 'sa-saopaulo-1'
TARGET = '/apis/apps/v1/namespaces/mcp-lab/deployments/mcp-server'


class Control:
    def __init__(self, store, region, secret, context):
        self.store, self.region, self.secret, self.context = store, region, secret.encode(), context

    def state(self):
        return self.store.get('control', {'phase': 'ready', 'message': 'Somente o apresentador pode interromper o MCP de São Paulo.'})

    def kube(self, method, path, body=None):
        # Mounted, rotating, namespace-scoped ServiceAccount, with no OCI API keys.
        base = Path('/var/run/secrets/kubernetes.io/serviceaccount')
        host = os.environ['KUBERNETES_SERVICE_HOST']
        port = os.environ['KUBERNETES_SERVICE_PORT_HTTPS']
        request = urllib.request.Request(f'https://{host}:{port}' + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + (base/'token').read_text().strip(),
                     'Content-Type': 'application/merge-patch+json'})
        with urllib.request.urlopen(request, context=ssl.create_default_context(cafile=str(base/'ca.crt')), timeout=10) as response:
            return json.load(response)

    def call(self, action):
        if action not in ('fault', 'restore'):
            raise ValueError('Ação inválida')
        if self.region != PRIMARY:
            return self.forward(action)
        lock = self.store.lock('control-lock', 30)
        try:
            state = self.state()
            deployment = self.kube('GET', TARGET)
            if action == 'fault':
                if state['phase'] not in ('ready', 'idle') or deployment['spec']['replicas'] != 2 or deployment.get('status', {}).get('readyReplicas', 0) != 2:
                    raise ValueError('Aguarde as duas réplicas prontas e o encerramento do ensaio anterior.')
                started = time.time()
                state = {'phase': 'injecting', 'fault_at': started, 'restore_at': started + 300,
                         'original_replicas': 2, 'message': 'Interrupção do MCP primário solicitada pelo apresentador.'}
                # Recovery target is durable before the first destructive mutation.
                self.store.put('control', state)
                self.kube('PATCH', TARGET + '/scale', {'spec': {'replicas': 0}})
                state.update(phase='fault', message='MCP de São Paulo interrompido. Restauração automática em até cinco minutos.')
                self.store.put('control', state)
                self.store.event('Apresentador interrompeu o MCP de São Paulo', 'Somente os pods MCP; chat, Redis e GenAI continuam ativos.', 'danger')
            elif state['phase'] in ('fault', 'injecting', 'restoring'):
                self.kube('PATCH', TARGET + '/scale', {'spec': {'replicas': 2}})
                state.update(phase='restoring', message='Restaurando as duas réplicas MCP de São Paulo…')
                self.store.put('control', state)
                self.store.event('Restauração do MCP solicitada', 'Aguardando duas réplicas prontas.', 'warning')
            return state
        finally:
            self.store.release('control-lock', lock)

    def reconcile(self):
        if self.region != PRIMARY:
            return
        state = self.state()
        if state['phase'] in ('fault', 'injecting') and time.time() >= state.get('restore_at', 0):
            self.call('restore')
        elif state['phase'] == 'restoring':
            deployment = self.kube('GET', TARGET)
            if deployment['spec']['replicas'] == 2 and deployment.get('status', {}).get('readyReplicas') == 2:
                state.update(phase='ready', message='São Paulo restaurado: duas réplicas MCP prontas.', restored_at=time.time())
                self.store.put('control', state)
                self.store.event('MCP de São Paulo restaurado', 'Duas réplicas prontas; DNS fará o retorno ao primário.', 'success')

    def signature(self, timestamp, nonce, body):
        return hmac.new(self.secret, timestamp.encode()+b'\n'+nonce.encode()+b'\n'+body, hashlib.sha256).hexdigest()

    def verify(self, headers, body):
        try:
            timestamp, nonce = headers['X-Control-Time'], headers['X-Control-Nonce']
            if not isinstance(timestamp, str) or not isinstance(nonce, str):
                return False
            expected = self.signature(timestamp, nonce, body)
            if abs(time.time() - int(timestamp)) > 30 or len(nonce) != 32 or not hmac.compare_digest(expected, headers.get('X-Control-Signature', '')):
                return False
            return bool(self.store.db.set(self.store.key('control-nonce:' + nonce), '1', nx=True, ex=90))
        except (TypeError, ValueError, KeyError):
            return False

    def forward(self, action):
        # Fixed São Paulo LB, not the failover DNS. Same TLS hostname verified normally.
        host = os.environ.get('MCP_HOST', 'mcp.example.invalid')
        body = json.dumps({'action': action}).encode()
        timestamp, nonce = str(int(time.time())), secrets.token_hex(16)
        conn = http.client.HTTPSConnection(host, 443, context=self.context, timeout=20)
        conn.sock = self.context.wrap_socket(socket.create_connection((os.environ['PRIMARY_LB_IP'], 443), timeout=10), server_hostname=host)
        try:
            conn.request('POST', '/chat/internal/control', body=body, headers={'Content-Type': 'application/json',
                'X-Control-Time': timestamp, 'X-Control-Nonce': nonce,
                'X-Control-Signature': self.signature(timestamp, nonce, body)})
            response = conn.getresponse()
            data = json.loads(response.read(65536))
            if response.status != 200:
                raise RuntimeError('O controle primário não confirmou a operação.')
            return data
        finally:
            conn.close()
