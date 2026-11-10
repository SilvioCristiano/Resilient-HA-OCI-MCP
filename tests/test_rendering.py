import base64
import json
from pathlib import Path
import sys
import unittest
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from rendering import build


class DeploymentRenderingTests(unittest.TestCase):
    def setUp(self):
        self.cfg=json.loads((ROOT/'config/lab.example.json').read_text())
        self.observed={'cache':{'sa-saopaulo-1':{'endpoint':'primary.cache.example.invalid'}},
                       'ips':{'sa-saopaulo-1':'192.0.2.10','sa-vinhedo-1':'192.0.2.20'}}
        self.keys={k:'test-value-never-a-real-secret' for k in ('MCP_TOKEN','REDIS_MCP_PASSWORD','REDIS_WEB_PASSWORD','WEB_SESSION_SECRET','CONTROL_SECRET','PRESENTER_PASSWORD_HASH')}
        self.tls={'tls.crt':b'dummy certificate for render test','tls.key':b'dummy private key for render test'}

    def render(self,region):
        return build(self.cfg,region,self.observed,self.keys,self.tls,'{"auths":{}}')

    def test_secondary_uses_verified_primary_cache_tls_bridge(self):
        docs=self.render('sa-vinhedo-1')
        secret=next(d for d in docs if d['kind']=='Secret' and d['metadata']['name']=='mcp-secrets')
        url=base64.b64decode(secret['data']['REDIS_URL']).decode()
        self.assertEqual(urlsplit(url).hostname,'primary.cache.example.invalid')
        self.assertEqual(urlsplit(url).port,6380)
        for d in docs:
            if d['kind']=='Deployment' and d['metadata']['name'] in ('mcp-server','mcp-web'):
                self.assertEqual(d['spec']['template']['spec']['hostAliases'],[{'ip':'192.0.2.10','hostnames':['primary.cache.example.invalid']}])

    def test_presenter_rbac_and_recovery_exist_only_in_primary(self):
        primary=self.render('sa-saopaulo-1');secondary=self.render('sa-vinhedo-1')
        role=next(d for d in primary if d['kind']=='Role' and d['metadata']['name']=='mcp-web-controller')
        self.assertTrue(all(r['resourceNames']==['mcp-server'] for r in role['rules']))
        self.assertFalse(any(d['metadata']['name']=='mcp-web-controller' for d in secondary))
        self.assertTrue(any(d['kind']=='CronJob' and d['metadata']['name']=='mcp-web-recovery' for d in primary))
        self.assertFalse(any(d['kind']=='CronJob' and d['metadata']['name']=='mcp-web-recovery' for d in secondary))

    def test_load_balancer_has_tls_only_on_443_and_preserves_proxy_mode(self):
        for region in self.cfg['regions']:
            service=next(d for d in self.render(region) if d['kind']=='Service' and d['metadata']['name']=='mcp-public')
            ann=service['metadata']['annotations']
            self.assertEqual(ann['oci.oraclecloud.com/ingress-ip-mode'],'Proxy')
            self.assertEqual(ann['service.beta.kubernetes.io/oci-load-balancer-ssl-ports'],'443')
            self.assertEqual([p['port'] for p in service['spec']['ports']],[443,6380] if region=='sa-saopaulo-1' else [443])

    def test_certificate_rotation_changes_named_secret_and_service_reference(self):
        first=self.render('sa-saopaulo-1')
        old=next(d['metadata']['name'] for d in first if d['kind']=='Secret' and d['type']=='kubernetes.io/tls')
        self.tls['tls.crt']=b'new dummy certificate'
        second=self.render('sa-saopaulo-1')
        new=next(d['metadata']['name'] for d in second if d['kind']=='Secret' and d['type']=='kubernetes.io/tls')
        self.assertNotEqual(old,new)
        service=next(d for d in second if d['kind']=='Service' and d['metadata']['name']=='mcp-public')
        self.assertEqual(service['metadata']['annotations']['service.beta.kubernetes.io/oci-load-balancer-tls-secret'],new)

    def test_missing_primary_address_blocks_secondary_deployment(self):
        self.observed['ips'].pop('sa-saopaulo-1')
        with self.assertRaisesRegex(ValueError,'São Paulo'):
            self.render('sa-vinhedo-1')


if __name__=='__main__':unittest.main()
