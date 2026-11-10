"""Generate per-region kubeconfig while preserving the original API CA and name."""
import argparse
import os
import subprocess
from urllib.parse import urlsplit
import yaml
from common import ROOT,RUNTIME,REGIONS,config


def main():
    p=argparse.ArgumentParser();p.add_argument('--region',required=True,choices=REGIONS)
    p.add_argument('--port',type=int);p.add_argument('--direct',action='store_true');args=p.parse_args()
    cfg=config();region=cfg['regions'][args.region]
    path=RUNTIME/('kubeconfig-'+region['context'])
    RUNTIME.mkdir(mode=0o700,exist_ok=True)
    subprocess.run(['oci','ce','cluster','create-kubeconfig','--cluster-id',region['cluster_id'],'--file',str(path),
        '--region',args.region,'--token-version','2.0.0','--kube-endpoint','PUBLIC_ENDPOINT' if args.direct else 'PRIVATE_ENDPOINT'],check=True)
    data=yaml.safe_load(path.read_text())
    selected=next(c for c in data['contexts'] if c['name']==data['current-context'])
    cluster=next(c['cluster'] for c in data['clusters'] if c['name']==selected['context']['cluster'])
    original=urlsplit(cluster['server']).hostname
    if not args.direct:
        if not args.port or not 1024<=args.port<=65535:raise ValueError('Informe --port (16443 para gru ou 26443 para vcp).')
        cluster['tls-server-name']=original
        cluster['server']='https://127.0.0.1:'+str(args.port)
    selected['name']=region['context'];data['current-context']=region['context']
    with path.open('w') as stream:yaml.safe_dump(data,stream,sort_keys=False)
    os.chmod(path,0o600)
    print('Kubeconfig privado:',path.relative_to(ROOT))
    print('Endpoint original verificado por CA:',original)
    print('Contexto:',region['context'])
    if not args.direct:print('Abra o túnel Bastion aprovado para esse IP na porta 6443 antes de usar kubectl.')


if __name__=='__main__':main()
