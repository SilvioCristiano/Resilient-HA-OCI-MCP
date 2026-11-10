"""User-authorized Let's Encrypt DNS-01 using only this lab's OCI DNS zone.

Keys remain local (0600). No ports are opened, no credentials sent to the CA.
Renew by rerunning this script and applying the resulting TLS secret to both OKEs.
"""
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import oci
from oci._vendor import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.x509.oid import NameOID

from common import ROOT,RUNTIME,config
CFG=config()
STATE = RUNTIME/'tls'
STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
NAMES = [CFG['domain'],CFG['mcp_host']]
ZONE = CFG['dns_zone_id']


def save_private(path, content):
    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(content)


def key(path):
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    value = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    save_private(path, value.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return value


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()


def integer(value):
    return b64(value.to_bytes((value.bit_length()+7)//8, 'big'))


class Acme:
    def __init__(self):
        self.key = key(STATE/'account.key')
        numbers = self.key.public_key().public_numbers()
        self.jwk = {'e':integer(numbers.e), 'kty':'RSA', 'n':integer(numbers.n)}
        self.thumbprint = b64(hashlib.sha256(json.dumps(self.jwk, separators=(',', ':'), sort_keys=True).encode()).digest())
        self.http = requests.Session()
        self.http.headers.update({'User-Agent':'OCI-Brazil-MCP-Lab/1.0'})
        self.directory = self.http.get('https://acme-v02.api.letsencrypt.org/directory', timeout=(5,20)).json()
        self.nonce, self.kid = None, None
        path = STATE/'account.json'
        if path.exists():
            self.kid = json.loads(path.read_text())['kid']
        else:
            if '--agree-terms' not in sys.argv:
                raise RuntimeError('Leia https://letsencrypt.org/repository/ e use --agree-terms para autorizar a criação da conta ACME.')
            response = self.post(self.directory['newAccount'], {'termsOfServiceAgreed':True}, account=True)
            self.kid = response.headers['Location']
            path.write_text(json.dumps({'kid':self.kid, 'terms':self.directory.get('meta',{}).get('termsOfService'),
                                        'approved_by_user':True}))

    def post(self, url, payload=None, account=False):
        if not url.startswith('https://acme-v02.api.letsencrypt.org/'):
            raise RuntimeError('Unexpected ACME host')
        for _ in range(3):
            nonce = self.nonce or self.http.head(self.directory['newNonce'], timeout=(5,20)).headers['Replay-Nonce']
            protected = {'alg':'RS256','nonce':nonce,'url':url}
            protected.update({'jwk':self.jwk} if account else {'kid':self.kid})
            encoded_header = b64(json.dumps(protected,separators=(',',':')).encode())
            encoded_payload = b64(json.dumps(payload,separators=(',',':')).encode()) if payload is not None else ''
            signature = self.key.sign((encoded_header+'.'+encoded_payload).encode(),padding.PKCS1v15(),hashes.SHA256())
            response = self.http.post(url, json={'protected':encoded_header,'payload':encoded_payload,'signature':b64(signature)},
                                      headers={'Content-Type':'application/jose+json'}, timeout=(5,25), allow_redirects=False)
            self.nonce = response.headers.get('Replay-Nonce')
            if response.status_code == 400 and response.json().get('type','').endswith(':badNonce'):
                continue
            if response.status_code not in (200,201,202):
                raise RuntimeError('ACME request failed: ' + response.text[:500])
            return response
        raise RuntimeError('ACME nonce rejected repeatedly')


def main():
    existing = STATE/'fullchain.pem'
    if existing.exists():
        cert = x509.load_pem_x509_certificate(existing.read_bytes())
        if set(NAMES)==set(cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)) and (cert.not_valid_after_utc - datetime.now(timezone.utc)).days > 30:
            print('[TLS] Certificado público existente ainda válido por mais de 30 dias.', flush=True)
            return
    dns = oci.dns.DnsClient(dict(oci.config.from_file(), region='sa-saopaulo-1'),
                           timeout=(5,20), retry_strategy=oci.retry.NoneRetryStrategy())
    d = oci.dns.models
    ca = Acme()
    response = ca.post(ca.directory['newOrder'], {'identifiers':[{'type':'dns','value':n} for n in NAMES]})
    order, order_url = response.json(), response.headers['Location']
    (STATE/'order.json').write_text(json.dumps({'order_url':order_url, **order},indent=2))
    challenges = []
    try:
        for url in order['authorizations']:
            auth = ca.post(url).json()
            if auth['status'] == 'valid': continue
            domain = '_acme-challenge.' + auth['identifier']['value']
            challenge = next(c for c in auth['challenges'] if c['type']=='dns-01')
            digest = b64(hashlib.sha256((challenge['token']+'.'+ca.thumbprint).encode()).digest())
            rdata = '"' + digest + '"'
            challenges.append((url, challenge['url'], domain, rdata))
            # Atomic ADD of only our exact record: preserve all unrelated TXT values.
            dns.patch_rr_set(ZONE,domain,'TXT',d.PatchRRSetDetails(items=[
                d.RecordOperation(operation='ADD',domain=domain,rtype='TXT',rdata=rdata,ttl=30)]),scope='GLOBAL')
            print('[TLS] Desafio DNS criado: ' + domain, flush=True)
        # Check recursive DNS propagation before spending a CA validation attempt.
        for _, _, domain, value in challenges:
            for _ in range(45):
                probe = subprocess.run(['dig','+short','TXT',domain,'@1.1.1.1'],capture_output=True,text=True,timeout=8)
                if value in probe.stdout: break
                time.sleep(4)
            else: raise RuntimeError('DNS TXT ainda não propagou: ' + domain)
        for auth_url, challenge_url, domain, _ in challenges:
            ca.post(challenge_url, {})
            for _ in range(45):
                auth = ca.post(auth_url).json()
                if auth['status']=='valid': break
                if auth['status']=='invalid': raise RuntimeError('CA recusou o desafio DNS: '+domain)
                time.sleep(4)
            else: raise RuntimeError('Validação do certificado ainda pendente')
            print('[TLS] Controle do domínio validado: ' + domain, flush=True)
        private = key(STATE/'server.key')
        csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME,NAMES[0])])).add_extension(x509.SubjectAlternativeName([
                x509.DNSName(n) for n in NAMES]),critical=False).sign(private,hashes.SHA256())
        ca.post(order['finalize'], {'csr':b64(csr.public_bytes(serialization.Encoding.DER))})
        for _ in range(45):
            order = ca.post(order_url).json()
            if order['status']=='valid': break
            if order['status']=='invalid': raise RuntimeError('Pedido de certificado inválido')
            time.sleep(3)
        else: raise RuntimeError('Emissão ainda não concluída')
        certificate = ca.post(order['certificate']).content
        save_private(STATE/'fullchain.pem',certificate)
        cert = x509.load_pem_x509_certificate(certificate)
        report = {'issuer':cert.issuer.rfc4514_string(),'domains':NAMES,
                  'not_after':cert.not_valid_after_utc.isoformat(),'sha256':cert.fingerprint(hashes.SHA256()).hex(),
                  'provider':'Lets Encrypt','approved_by_user':True,'validation':'DNS-01 OCI DNS',
                  'renewal':'manual: scripts/issue_public_tls.py, then scripts/lab.py deploy --apply for both regions'}
        (STATE/'certificate.json').write_text(json.dumps(report,indent=2))
        print('[TLS] Certificado público emitido. Validade: '+report['not_after'],flush=True)
    finally:
        for _, _, domain, value in challenges:
            dns.patch_rr_set(ZONE,domain,'TXT',d.PatchRRSetDetails(items=[
                d.RecordOperation(operation='REMOVE',domain=domain,rtype='TXT',rdata=value)]),scope='GLOBAL')
        if challenges: print('[TLS] Desafios TXT temporários removidos.',flush=True)


if __name__ == '__main__':
    main()
