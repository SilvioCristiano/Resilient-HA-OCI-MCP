"""Shared presentation state; authoritative LLM context stays in the existing MCP."""
import json
import time
import uuid

PREFIX = 'racing:web:v1:'
TTL = 86400


class Busy(RuntimeError):
    pass


class Store:
    def __init__(self, db):
        self.db = db

    def key(self, suffix):
        return PREFIX + suffix

    def get(self, suffix, default=None):
        raw = self.db.get(self.key(suffix))
        return json.loads(raw) if raw else default

    def put(self, suffix, value, ttl=TTL):
        self.db.set(self.key(suffix), json.dumps(value, ensure_ascii=False), ex=ttl)

    def release(self, suffix, token):
        self.db.eval("if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) end return 0", 1, self.key(suffix), token)

    def lock(self, suffix, ttl=30):
        token = uuid.uuid4().hex
        if not self.db.set(self.key(suffix), token, nx=True, ex=ttl):
            raise Busy('Já existe uma operação em andamento. Aguarde e tente novamente.')
        return token

    def session(self, sid):
        data = self.get('session:' + sid, {'messages': [], 'last_result': None})
        job = self.get('active:' + sid)
        if job:
            data['job'] = self.get('job:' + job)
            if data['job'] and data['job']['stage'] in ('sending', 'reconnecting'):
                if time.time() - data['job']['started_at'] > 600:
                    data['job'].update(stage='error', message='A execução perdeu a confirmação. O turno não foi repetido automaticamente.')
                    self.put('job:' + job, data['job'])
        data['busy'] = bool(data.get('job') and data['job']['stage'] in ('sending', 'reconnecting'))
        return data

    def event(self, title, detail='', level='info'):
        record = {'id': uuid.uuid4().hex, 'at': time.time() * 1000,
                  'title': title, 'detail': detail, 'level': level}
        tx = self.db.pipeline()
        tx.lpush(self.key('events'), json.dumps(record, ensure_ascii=False))
        tx.ltrim(self.key('events'), 0, 59)
        tx.expire(self.key('events'), TTL)
        tx.execute()

    def events(self):
        return [json.loads(r) for r in reversed(self.db.lrange(self.key('events'), 0, 59))]

    def rate(self, name, limit, seconds):
        # A bounded key cardinality per fixed time window; INCR and EXPIRE are atomic.
        bucket = str(int(time.time()) // seconds)
        count = self.db.eval("local n=redis.call('incr',KEYS[1]); if n==1 then redis.call('expire',KEYS[1],ARGV[1]) end return n",
                             1, self.key('rate:' + name + ':' + bucket), seconds + 2)
        return int(count) <= limit
