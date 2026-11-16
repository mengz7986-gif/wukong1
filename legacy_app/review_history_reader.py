"""Opt-in, allowlisted historical evidence decryption; no writes or logging."""
import base64
import hashlib
import json
import os
import re
import stat
from cryptography.fernet import Fernet,InvalidToken

def read_historical_detail(token):
    descriptor=None
    try:
        path=os.environ.get('RAINBOW_REVIEW_HISTORY_KEYRING_PATH','')
        if not os.path.isabs(path):raise InvalidToken
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        info=os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid not in (0,os.geteuid()) or info.st_size>1024*1024:raise InvalidToken
        with os.fdopen(descriptor,'r',encoding='utf-8') as source:
            descriptor=None
            ring=json.loads(source.read(1024*1024+1))
        if set(ring)!={'version','keys'} or ring['version']!=1 or not isinstance(ring['keys'],list) or not 1<=len(ring['keys'])<=4:raise InvalidToken
        digest=hashlib.sha256(token.encode()).hexdigest()
        for entry in ring['keys']:
            if set(entry)!={'secret','ciphertext_sha256'}:raise InvalidToken
            allowed=entry['ciphertext_sha256'];secret=entry['secret']
            if not isinstance(secret,str) or not 1<=len(secret)<=4096:raise InvalidToken
            if not isinstance(allowed,list) or not 1<=len(allowed)<=10000 or any(not isinstance(x,str) or not re.fullmatch('[0-9a-f]{64}',x) for x in allowed):raise InvalidToken
            if digest not in allowed:continue
            cipher=Fernet(base64.urlsafe_b64encode(hashlib.sha256(('failure-analysis-v1:'+secret).encode()).digest()))
            value=json.loads(cipher.decrypt(token.encode()))
            if not isinstance(value,dict):raise InvalidToken
            return value
        raise InvalidToken
    except Exception:
        raise InvalidToken from None
    finally:
        if descriptor is not None:os.close(descriptor)
