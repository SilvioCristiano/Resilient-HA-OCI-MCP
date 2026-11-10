"""Render the same architecture for a customer's own addresses and image registry."""
import base64
import json
import hashlib
from urllib.parse import quote
from common import ROOT,REGIONS,image,LAB

NS='mcp-lab'
PRIMARY=REGIONS[0]


def obj(kind,name,spec=None,version='v1',**extra):
    data={'apiVersion':version,'kind':kind,'metadata':{'name':name,'namespace':NS,'labels':{'app.kubernetes.io/part-of':LAB}}}
    if spec is not None:data['spec']=spec
    data.update(extra)
    return data


def secret(name,values,kind='Opaque'):
    return obj('Secret',name,type=kind,data={k:base64.b64encode(v.encode() if isinstance(v,str) else v).decode() for k,v in values.items()})


def build(cfg,region,observed,keys,tls,registry):
    primary=region==PRIMARY
    regional=cfg['regions'][region]
    ips=observed.get('ips',{})
    cache=observed['cache'][PRIMARY]
    host=cache['endpoint']
    if not primary and not ips.get(PRIMARY):raise ValueError('Implante São Paulo e execute discover antes de implantar Vinhedo.')
    port=6379 if primary else 6380
    tls_name='mcp-tls-'+hashlib.sha256(tls['tls.crt']).hexdigest()[:12]
    def redis_url(user,password):return 'rediss://'+user+':'+quote(password,safe='')+'@'+host+':'+str(port)+'/0'
    docs=[{'apiVersion':'v1','kind':'Namespace','metadata':{'name':NS,'labels':{'app.kubernetes.io/part-of':LAB}}},
        secret('mcp-secrets',{'MCP_TOKEN':keys['MCP_TOKEN'],'REDIS_URL':redis_url('mcp',keys['REDIS_MCP_PASSWORD'])}),
        secret('mcp-web-secrets',{**{k:keys[k] for k in ('WEB_SESSION_SECRET','CONTROL_SECRET','PRESENTER_PASSWORD_HASH')},
                                  'REDIS_URL':redis_url('mcpweb',keys['REDIS_WEB_PASSWORD'])}),
        secret('mcp-ocir',{'.dockerconfigjson':registry},'kubernetes.io/dockerconfigjson'),
        secret(tls_name,tls,'kubernetes.io/tls'),
        obj('ConfigMap','mcp-config',data={'OCI_REGION':region,'OCI_COMPARTMENT_ID':cfg['compartment_id'],
            'OCI_MODEL_ID':cfg['model_id'],'OCI_VCP_ROUTING_PROFILE_ID':cfg['vinhedo_routing_profile_id']}),
        obj('ConfigMap','mcp-web-config',data={'OCI_REGION':region,'WEB_ORIGIN':'https://'+cfg['domain'],
            'MCP_HOST':cfg['mcp_host'],'MCP_REGION_IPS':json.dumps({r:ips.get(r,'') for r in REGIONS}),
            'PRIMARY_LB_IP':ips.get(PRIMARY,''),'MCP_CA_FILE':'/tls/ca.crt'}),
        obj('ConfigMap','mcp-web-ca',data={'ca.crt':''}),obj('ServiceAccount','mcp-web')]
    security={'runAsNonRoot':True,'runAsUser':10001,'runAsGroup':10001,'seccompProfile':{'type':'RuntimeDefault'}}
    container_security={'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}}
    aliases=[] if primary else [{'ip':ips[PRIMARY],'hostnames':[host]}]
    for name in ('mcp-server','mcp-web','mcp-edge'):
        web=name=='mcp-web';edge=name=='mcp-edge'
        app_image='docker.io/library/haproxy:lts-alpine' if edge else image(cfg,'web' if web else 'mcp')
        container={'name':'edge' if edge else ('web' if web else 'mcp'),'image':app_image,'imagePullPolicy':'IfNotPresent',
            'securityContext':container_security,'resources':{'requests':{'cpu':'100m','memory':'128Mi'},'limits':{'cpu':'500m','memory':'384Mi'}},
            'ports':[{'name':'http','containerPort':8080 if edge else (8088 if web else 8000)}]}
        pod={'securityContext':security,'automountServiceAccountToken':web and primary,'imagePullSecrets':[{'name':'mcp-ocir'}],
            'containers':[container],'terminationGracePeriodSeconds':45,
            'topologySpreadConstraints':[{'maxSkew':1,'topologyKey':'kubernetes.io/hostname','whenUnsatisfiable':'ScheduleAnyway','labelSelector':{'matchLabels':{'app':name}}}]}
        if not primary and not edge:pod['hostAliases']=aliases
        if edge:
            container.update(args=['haproxy','-f','/config/haproxy.cfg','-db'],volumeMounts=[{'name':'config','mountPath':'/config','readOnly':True}],
                readinessProbe={'tcpSocket':{'port':'http'},'periodSeconds':5},livenessProbe={'tcpSocket':{'port':'http'},'periodSeconds':15})
            if primary:container['ports'].append({'name':'redis-tls','containerPort':6380})
            pod['volumes']=[{'name':'config','configMap':{'name':'mcp-edge'}}]
        else:
            container['envFrom']=[{'configMapRef':{'name':'mcp-web-config' if web else 'mcp-config'}},
                {'secretRef':{'name':'mcp-web-secrets' if web else 'mcp-secrets'}}]
            if web:
                container['env']=[{'name':'MCP_TOKEN','valueFrom':{'secretKeyRef':{'name':'mcp-secrets','key':'MCP_TOKEN'}}}]
                container['volumeMounts']=[{'name':'ca','mountPath':'/tls','readOnly':True}]
                pod['volumes']=[{'name':'ca','configMap':{'name':'mcp-web-ca'}}]
                pod['serviceAccountName']='mcp-web'
            container['readinessProbe']={'httpGet':{'path':'/readyz','port':'http'},'initialDelaySeconds':5,'periodSeconds':5,'timeoutSeconds':4}
            container['livenessProbe']={'httpGet':{'path':'/livez','port':'http'},'initialDelaySeconds':10,'periodSeconds':15}
        docs.append(obj('Deployment',name,{'replicas':2,'selector':{'matchLabels':{'app':name}},
            'strategy':{'type':'RollingUpdate','rollingUpdate':{'maxUnavailable':0,'maxSurge':1}},
            'template':{'metadata':{'labels':{'app':name}},'spec':pod}},version='apps/v1'))
        docs.append(obj('PodDisruptionBudget',name,{'minAvailable':1,'selector':{'matchLabels':{'app':name}}},version='policy/v1'))
        if not edge:docs.append(obj('Service',name,{'type':'ClusterIP','selector':{'app':name},'ports':[{'name':'http','port':8088 if web else 8000,'targetPort':'http'}]}))
    cfg_text='''global
  log stdout format raw local0
defaults
  log global
  timeout connect 3s
  timeout client 120s
  timeout server 120s
resolvers kube_dns
  parse-resolv-conf
  hold valid 10s
frontend https_terminated_by_oci
  bind :8080
  mode http
  http-request redirect location /chat/ code 302 if { path / }
  use_backend web if { path_beg /chat }
  default_backend mcp
backend mcp
  mode http
  server mcp mcp-server.mcp-lab.svc.cluster.local:8000 resolvers kube_dns init-addr libc,none
backend web
  mode http
  option httpchk GET /readyz
  server web mcp-web.mcp-lab.svc.cluster.local:8088 check resolvers kube_dns init-addr libc,none
'''
    if primary:cfg_text+='''frontend redis_tls_passthrough
  bind :6380
  mode tcp
  default_backend redis_primary
backend redis_primary
  mode tcp
  server cache '''+host+''':6379 resolvers kube_dns init-addr libc,none check inter 3s
'''
    docs.append(obj('ConfigMap','mcp-edge',data={'haproxy.cfg':cfg_text}))
    ports=[{'name':'https','port':443,'targetPort':'http','nodePort':32443}]
    if primary:ports.append({'name':'redis-tls','port':6380,'targetPort':'redis-tls','nodePort':32380})
    service=obj('Service','mcp-public',{'type':'LoadBalancer','selector':{'app':'mcp-edge'},'externalTrafficPolicy':'Cluster','ports':ports})
    service['metadata']['annotations']={'oci.oraclecloud.com/load-balancer-type':'lb','oci.oraclecloud.com/ingress-ip-mode':'Proxy',
        'service.beta.kubernetes.io/oci-load-balancer-shape':'flexible',
        'service.beta.kubernetes.io/oci-load-balancer-shape-flex-min':'10','service.beta.kubernetes.io/oci-load-balancer-shape-flex-max':'10',
        'service.beta.kubernetes.io/oci-load-balancer-subnet1':regional['lb_subnet_id'],
        'service.beta.kubernetes.io/oci-load-balancer-security-list-management-mode':'None',
        'service.beta.kubernetes.io/oci-load-balancer-ssl-ports':'443',
        'service.beta.kubernetes.io/oci-load-balancer-tls-secret':tls_name}
    docs.append(service)
    if primary:
        docs += [obj('Role','mcp-web-controller',version='rbac.authorization.k8s.io/v1',rules=[
            {'apiGroups':['apps'],'resources':['deployments'],'resourceNames':['mcp-server'],'verbs':['get']},
            {'apiGroups':['apps'],'resources':['deployments/scale'],'resourceNames':['mcp-server'],'verbs':['get','patch']}]),
            obj('RoleBinding','mcp-web-controller',version='rbac.authorization.k8s.io/v1',
                roleRef={'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':'mcp-web-controller'},
                subjects=[{'kind':'ServiceAccount','name':'mcp-web','namespace':NS}])]
        web_deployment=next(d for d in docs if d['kind']=='Deployment' and d['metadata']['name']=='mcp-web')
        import copy
        recovery=copy.deepcopy(web_deployment['spec']['template']['spec'])
        recovery['restartPolicy']='Never'
        recovery.pop('topologySpreadConstraints',None)
        c=recovery['containers'][0]
        for k in ('readinessProbe','livenessProbe','ports'):c.pop(k,None)
        c['command']=['python','-c','from server import create_app; create_app().control.reconcile()']
        docs.append(obj('CronJob','mcp-web-recovery',{'schedule':'* * * * *','concurrencyPolicy':'Forbid',
            'successfulJobsHistoryLimit':1,'failedJobsHistoryLimit':2,'jobTemplate':{'spec':{'activeDeadlineSeconds':45,'backoffLimit':1,'template':{'spec':recovery}}}},version='batch/v1'))
    # Keep the short-lived OCIR pull secret renewable without giving repo writes to workers.
    docs += [obj('ServiceAccount','mcp-registry-refresh'),
        obj('Role','mcp-registry-refresh',version='rbac.authorization.k8s.io/v1',rules=[
            {'apiGroups':[''],'resources':['secrets'],'resourceNames':['mcp-ocir'],'verbs':['patch']}]),
        obj('RoleBinding','mcp-registry-refresh',version='rbac.authorization.k8s.io/v1',
            roleRef={'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':'mcp-registry-refresh'},
            subjects=[{'kind':'ServiceAccount','name':'mcp-registry-refresh','namespace':NS}]),
        obj('ConfigMap','mcp-registry-refresh',data={'refresh.py':(ROOT/'scripts/refresh_registry.py').read_text()})]
    refresh={'serviceAccountName':'mcp-registry-refresh','restartPolicy':'Never','securityContext':security,
        'imagePullSecrets':[{'name':'mcp-ocir'}],'containers':[{'name':'refresh','image':image(cfg,'mcp'),
            'command':['python','/refresh/refresh.py'],'env':[{'name':'OCIR_REGISTRY','value':cfg['registry']}],
            'resources':{'requests':{'cpu':'50m','memory':'128Mi'},'limits':{'cpu':'250m','memory':'256Mi'}},
            'securityContext':container_security,'volumeMounts':[{'name':'script','mountPath':'/refresh','readOnly':True},{'name':'tmp','mountPath':'/tmp'}]}],
        'volumes':[{'name':'script','configMap':{'name':'mcp-registry-refresh'}},{'name':'tmp','emptyDir':{}}]}
    docs.append(obj('CronJob','mcp-registry-refresh',{'schedule':'*/15 * * * *','concurrencyPolicy':'Forbid',
        'successfulJobsHistoryLimit':1,'failedJobsHistoryLimit':2,'jobTemplate':{'spec':{'activeDeadlineSeconds':120,'backoffLimit':1,'template':{'spec':refresh}}}},version='batch/v1'))
    return docs
