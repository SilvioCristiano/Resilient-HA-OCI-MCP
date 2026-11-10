"""Small OCI SDK phases for a new customer compartment; never contains lab OCIDs."""
import argparse
import base64
import hashlib
import json
import os
import subprocess
import time
import oci
from oci._vendor import requests
from common import ROOT,RUNTIME,REGIONS,LAB,config,state,private_json


def client(cls,region=None):
    cfg=oci.config.from_file()
    if region:cfg=dict(cfg,region=region)
    return cls(cfg,timeout=(5,30),retry_strategy=oci.retry.NoneRetryStrategy())


def records(call,**kw):
    result=[];page=None
    while True:
        response=call(**kw,**({'page':page} if page else {}))
        result.extend(response.data if isinstance(response.data,list) else response.data.items)
        page=response.headers.get('opc-next-page')
        if not page:return result


def wait(get,resource_id,limit=120):
    for _ in range(limit):
        item=get(resource_id).data
        if item.lifecycle_state=='ACTIVE':return item
        if item.lifecycle_state in ('FAILED','DELETED'):raise RuntimeError('Recurso não ficou ativo: '+item.lifecycle_state)
        time.sleep(5)
    raise RuntimeError('Operação ainda pendente. Aguarde ACTIVE e execute a mesma fase novamente.')


def registry(cfg):
    svc=client(oci.artifacts.ArtifactsClient,REGIONS[0])
    repositories=[]
    for kind in ('mcp','web'):
        name=cfg['repository_'+kind]
        existing=records(svc.list_container_repositories,compartment_id=cfg['compartment_id'],display_name=name)
        existing=[r for r in existing if r.lifecycle_state!='DELETED']
        if len(existing)>1:raise RuntimeError('Nome de repositório ambíguo')
        if not existing:
            repo=svc.create_container_repository(oci.artifacts.models.CreateContainerRepositoryDetails(
                compartment_id=cfg['compartment_id'],display_name=name,is_public=False,freeform_tags={'Lab':LAB})).data
        else:
            repo=existing[0]
            if repo.is_public:raise RuntimeError('O repositório existente é público; revise a configuração antes de continuar.')
        repositories.append(repo.id)
    auth=oci.config.from_file()
    signer=oci.signer.Signer(auth['tenancy'],auth['user'],auth['fingerprint'],auth['key_file'],auth.get('pass_phrase'))
    response=requests.get('https://'+cfg['registry']+'/20180419/docker/token',auth=signer,
                          headers={'Accept':'application/json'},timeout=(5,30),allow_redirects=False)
    response.raise_for_status()
    docker={'auths':{cfg['registry']:{'auth':base64.b64encode(('BEARER_TOKEN:'+response.json()['token']).encode()).decode()}}}
    private_json(RUNTIME/'registry-config.json',docker)
    private_json(RUNTIME/'docker/config.json',docker)
    observed=state();observed['repositories']=repositories;private_json(RUNTIME/'state.json',observed)
    print('Repositórios privados prontos. Token efêmero salvo em .runtime/; renove esta fase antes de novos pushes/deploys.')


def iam(cfg):
    svc=client(oci.identity.IdentityClient,cfg['home_region'])
    tenancy=oci.config.from_file()['tenancy']
    workers=[worker for region in REGIONS for worker in cfg['regions'][region]['workers']]
    if len(workers)!=4 or len(set(workers))!=4 or any(not w.startswith('ocid1.instance.') for w in workers):
        raise ValueError('Informe exatamente os quatro OCIDs reais dos workers em config/lab.json')
    name='resilient-mcp-workers'
    rule='Any {'+', '.join("instance.id = '"+w+"'" for w in sorted(workers))+'}'
    groups=[g for g in records(svc.list_dynamic_groups,compartment_id=tenancy) if g.name==name and g.lifecycle_state!='DELETED']
    if len(groups)>1:raise RuntimeError('Dynamic group ambíguo')
    if groups:
        group=groups[0]
        if group.matching_rule!=rule:raise RuntimeError('Dynamic group existente tem outro escopo; não será ampliado automaticamente.')
    else:group=svc.create_dynamic_group(oci.identity.models.CreateDynamicGroupDetails(compartment_id=tenancy,name=name,
        description='Exactly four OKE workers for '+LAB,matching_rule=rule)).data
    statements=['Allow dynamic-group id '+group.id+' to use generative-ai-chat in compartment id '+cfg['compartment_id']]
    for kind in ('mcp','web'):
        statements.append('Allow dynamic-group id '+group.id+" to read repos in tenancy where target.repo.name = '"+cfg['repository_'+kind]+"'")
    policies=[p for p in records(svc.list_policies,compartment_id=tenancy) if p.name=='resilient-mcp-workers-access' and p.lifecycle_state!='DELETED']
    if policies:
        if sorted(policies[0].statements)!=sorted(statements):raise RuntimeError('Política existente difere do plano; revise antes de alterar.')
        policy=policies[0]
    else:policy=svc.create_policy(oci.identity.models.CreatePolicyDetails(compartment_id=tenancy,name='resilient-mcp-workers-access',
        description=LAB,statements=statements)).data
    observed=state();observed['iam']={'dynamic_group_id':group.id,'policy_id':policy.id};private_json(RUNTIME/'state.json',observed)
    print('IAM restrito aos quatro workers, inferência Chat no compartment e leitura dos dois repositórios. Aguarde propagação.')


def cache(cfg):
    keys=json.loads((RUNTIME/'secrets.json').read_text())
    observed=state();observed.setdefault('cache',{})
    for region in REGIONS:
        suffix='gru' if region==REGIONS[0] else 'vcp'
        svc=client(oci.redis.RedisClusterClient,region)
        sets=client(oci.redis.OciCacheConfigSetClient,region)
        users=client(oci.redis.OciCacheUserClient,region)
        name='mcp-context-'+suffix
        configs=[x for x in records(sets.list_oci_cache_config_sets,compartment_id=cfg['compartment_id'],display_name='mcp-config-'+suffix) if x.lifecycle_state!='DELETED']
        if configs:config_set=configs[0]
        else:config_set=sets.create_oci_cache_config_set(oci.redis.models.CreateOciCacheConfigSetDetails(
            compartment_id=cfg['compartment_id'],display_name='mcp-config-'+suffix,software_version='REDIS_7_0',freeform_tags={'Lab':LAB},
            configuration_details=oci.redis.models.ConfigurationDetails(items=[
                oci.redis.models.ConfigurationInfo(config_key='reserved-memory-percentage',config_value='35'),
                oci.redis.models.ConfigurationInfo(config_key='maxmemory-policy',config_value='volatile-lru')]))).data
        wait(sets.get_oci_cache_config_set,config_set.id)
        clusters=[c for c in records(svc.list_redis_clusters,compartment_id=cfg['compartment_id'],display_name=name) if c.lifecycle_state!='DELETED']
        if len(clusters)>1:raise RuntimeError('Nome de Cache ambíguo')
        if clusters:
            item=svc.get_redis_cluster(clusters[0].id).data
            if item.freeform_tags.get('Lab')!=LAB:raise RuntimeError('Cache existente não pertence a esta implantação; não será alterado.')
        else:
            body={'compartmentId':cfg['compartment_id'],'displayName':name,'softwareVersion':'REDIS_7_0','nodeCount':2,
                  'nodeMemoryInGBs':8,'clusterMode':'NONSHARDED','subnetId':cfg['regions'][region]['cache_subnet_id'],
                  'nsgIds':[cfg['regions'][region]['cache_nsg_id']],'ociCacheConfigSetId':config_set.id,'freeformTags':{'Lab':LAB}}
            if region!=REGIONS[0]:body['primaryClusterId']=observed['cache'][REGIONS[0]]['id']
            item=svc.create_redis_cluster(body).data
        observed['cache'][region]={'id':item.id,'config_set_id':config_set.id}
        private_json(RUNTIME/'state.json',observed)
        print(region+': aguardando Cache ACTIVE',flush=True)
        item=wait(svc.get_redis_cluster,item.id)
        observed['cache'][region]['endpoint']=item.primary_fqdn
        private_json(RUNTIME/'state.json',observed)
        mcp_acl='~mcp:* +get +set +watch +unwatch +multi +exec +ping +info +client|setname'
        web_acl='~racing:web:v1:* +get +set +del +eval +expire +incr +lpush +ltrim +lrange +zadd +zrem +zcard +zremrangebyscore +multi +exec +ping +client|setname'
        user_specs=[('default','OFF','allkeys allcommands allchannels',keys['REDIS_MCP_PASSWORD']),
                    ('mcp','ON',mcp_acl,keys['REDIS_MCP_PASSWORD'])]
        if region==REGIONS[0]:user_specs.append(('mcpweb','ON',web_acl,keys['REDIS_WEB_PASSWORD']))
        all_users=records(users.list_oci_cache_users,compartment_id=cfg['compartment_id'])
        attached=svc.list_attached_oci_cache_users(item.id).data
        if not isinstance(attached,list):attached=attached.items
        attached_ids={getattr(u,'oci_cache_user_id',getattr(u,'id',None)) for u in attached}
        ids=[]
        for username,status,acl,password in user_specs:
            desc=LAB+' '+suffix+' '+username
            matches=[u for u in all_users if u.description==desc and u.lifecycle_state!='DELETED']
            if len(matches)>1:raise RuntimeError('Usuário Cache ambíguo')
            user=users.get_oci_cache_user(matches[0].id).data if matches else users.create_oci_cache_user(oci.redis.models.CreateOciCacheUserDetails(
                name=username,description=desc,compartment_id=cfg['compartment_id'],status=status,acl_string=acl,freeform_tags={'Lab':LAB},
                authentication_mode=oci.redis.models.PasswordAuthenticationMode(hashed_passwords=[hashlib.sha256(password.encode()).hexdigest()]))).data
            if user.acl_string!=acl or user.status!=status:raise RuntimeError('ACL existente difere do esperado')
            wait(users.get_oci_cache_user,user.id)
            if user.id not in attached_ids:ids.append(user.id)
        if ids:svc.attach_oci_cache_users(item.id,oci.redis.models.AttachOciCacheUsersDetails(oci_cache_users=ids))
        print(region+': Cache e ACLs configurados. Verifique o papel e a replicação no console antes do deploy.',flush=True)


def dns(cfg):
    observed=state();ips=observed.get('ips',{})
    if any(not ips.get(r) for r in REGIONS):raise ValueError('Execute discover depois que os dois LBs tiverem IP')
    h=oci.healthchecks.models;d=oci.dns.models
    checks=client(oci.healthchecks.HealthChecksClient,REGIONS[0]);svc=client(oci.dns.DnsClient,REGIONS[0])
    resources=observed.setdefault('dns',{})
    if not resources.get('monitor_id'):
        monitor=checks.create_http_monitor(h.CreateHttpMonitorDetails(compartment_id=cfg['compartment_id'],display_name='resilient-mcp',
            protocol='HTTPS',port=443,method='GET',path='/readyz',headers={'Host':cfg['mcp_host']},
            interval_in_seconds=10,timeout_in_seconds=10,targets=[ips[r] for r in REGIONS],is_enabled=True)).data
        resources['monitor_id']=monitor.id;private_json(RUNTIME/'state.json',observed)
    if not resources.get('policy_id'):
        policy=svc.create_steering_policy(d.CreateSteeringPolicyDetails(compartment_id=cfg['compartment_id'],display_name='resilient-mcp',
            template='FAILOVER',ttl=30,health_check_monitor_id=resources['monitor_id'],answers=[
                d.SteeringPolicyAnswer(name='gru',rdata=ips[REGIONS[0]],rtype='A',pool='primary'),
                d.SteeringPolicyAnswer(name='vcp',rdata=ips[REGIONS[1]],rtype='A',pool='secondary')],rules=[
                d.SteeringPolicyFilterRule(rule_type='FILTER',default_answer_data=[d.SteeringPolicyFilterAnswerData(answer_condition='answer.isDisabled != true',should_keep=True)]),
                d.SteeringPolicyHealthRule(rule_type='HEALTH'),d.SteeringPolicyPriorityRule(rule_type='PRIORITY',default_answer_data=[
                    d.SteeringPolicyPriorityAnswerData(answer_condition="answer.pool == 'primary'",value=1),
                    d.SteeringPolicyPriorityAnswerData(answer_condition="answer.pool == 'secondary'",value=2)]),
                d.SteeringPolicyLimitRule(rule_type='LIMIT',default_count=1)])).data
        resources['policy_id']=policy.id;private_json(RUNTIME/'state.json',observed)
    resources.setdefault('attachments',{})
    existing=records(svc.list_steering_policy_attachments,compartment_id=cfg['compartment_id'],zone_id=cfg['dns_zone_id'])
    for hostname in (cfg['domain'],cfg['mcp_host']):
        matches=[a for a in existing if a.domain_name==hostname]
        if matches and matches[0].steering_policy_id!=resources['policy_id']:raise RuntimeError('Hostname já está em outra política; nenhuma substituição será feita')
        if matches:attachment=matches[0]
        else:
            if svc.get_rr_set(cfg['dns_zone_id'],hostname,'A',scope='GLOBAL').data.items:raise RuntimeError('Há registros A existentes em '+hostname+'; revise a migração antes de continuar')
            attachment=svc.create_steering_policy_attachment(d.CreateSteeringPolicyAttachmentDetails(
                steering_policy_id=resources['policy_id'],zone_id=cfg['dns_zone_id'],domain_name=hostname)).data
        resources['attachments'][hostname]=attachment.id;private_json(RUNTIME/'state.json',observed)
    print('DNS FAILOVER configurado. TTL 30 s e monitor 10 s não garantem recuperação em menos de dois segundos.')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['registry','iam','cache','dns']);p.add_argument('--apply',action='store_true');args=p.parse_args()
    cfg=config()
    if not args.apply:print('Plano: fase '+args.phase+' no compartment configurado. Nenhuma alteração aplicada; use --apply após revisar config/lab.json.')
    else:globals()[args.phase](cfg)
