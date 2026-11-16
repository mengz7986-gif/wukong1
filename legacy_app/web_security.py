"""Exact-host and trusted-loopback proxy preparation; no bot/network behavior."""
from urllib.parse import urlsplit
import os

DEFAULT_HOSTS=('chdf666.com','64.90.27.76','localhost','127.0.0.1','testserver')


class WebBoundary:
    def __init__(self,app,allowed_hosts=None,trusted_proxies=('127.0.0.1','::1')):
        if allowed_hosts is None:
            configured=os.getenv('RAINBOW_ALLOWED_HOSTS','').strip()
            allowed_hosts=tuple(h.strip() for h in configured.split(',') if h.strip()) if configured else DEFAULT_HOSTS
        if not allowed_hosts or any('*' in h for h in allowed_hosts):
            raise ValueError('Allowed Hosts必须是明确主机名，禁止通配符')
        self.app=app;self.hosts=set(allowed_hosts);self.proxies=set(trusted_proxies)

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        headers=dict(scope.get('headers',[]))
        raw_host=headers.get(b'host',b'').decode('latin1')
        try:
            parsed=urlsplit('//'+raw_host)
            hostname=parsed.hostname
            valid=hostname in self.hosts and not parsed.username and not parsed.password
            valid=valid and not parsed.path and not parsed.query and not parsed.fragment
            port=parsed.port
        except ValueError: valid=False
        async def reject(status,message):
            await send({'type':'http.response.start','status':status,'headers':[(b'content-type',b'text/plain; charset=utf-8')]})
            await send({'type':'http.response.body','body':message.encode()})
        if not valid:return await reject(400,'Invalid host')
        scope=dict(scope)
        peer=(scope.get('client') or ('',0))[0]
        proto=headers.get(b'x-forwarded-proto',b'').decode('latin1')
        if peer in self.proxies and proto in ('http','https'):
            scope['scheme']=proto
        origin=headers.get(b'origin',b'').decode('latin1')
        if scope['method'] not in ('GET','HEAD','OPTIONS') and origin:
            if origin != scope['scheme']+'://'+raw_host:
                return await reject(403,'Origin mismatch')
        async def secure_send(message):
            if message['type']=='http.response.start' and scope['scheme']=='https':
                message=dict(message)
                message['headers']=[(k,v+b'; Secure' if k.lower()==b'set-cookie'
                    and b'; secure' not in v.lower() else v) for k,v in message.get('headers',[])]
            await send(message)
        await self.app(scope,receive,secure_send)
