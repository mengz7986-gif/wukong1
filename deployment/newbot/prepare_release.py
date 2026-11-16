"""Package only new-project source and cleaned data; no backup trees or old credentials."""
from contextlib import closing
import hashlib,json,os,shutil,sqlite3,tarfile,uuid
from pathlib import Path
from dotenv import dotenv_values
from cryptography.fernet import Fernet
from data_evidence import evidence

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'work/server-deployment'

def main():
    os.umask(0o077)
    release=OUT/('release-'+uuid.uuid4().hex)
    app=release/'legacy_app';app.mkdir(parents=True)
    for p in (ROOT/'legacy_app').glob('*.py'):
        if p.name!='test_bot_local.py':shutil.copy2(p,app/p.name)
    shutil.copy2(ROOT/'legacy_app/requirements.txt',app/'requirements.txt')
    for name in ['settings.json','learning_evidence.json','learning_evidence_access.json','ai_usage_ledger.json','aliyun_cost_ledger.json','aliyun_address_settings.usage.json','performance_metrics.json','ai_diagnostics.json']:
        source=ROOT/'legacy_app'/name
        if source.exists():shutil.copy2(source,app/name)
    for name in ['templates','static','data']:
        shutil.copytree(ROOT/'legacy_app'/name,app/name,ignore=shutil.ignore_patterns('*.lock','__pycache__','knowledge_backups'))
    shutil.copytree(ROOT/'deployment',release/'deployment',ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(ROOT/'docs',release/'docs')
    runtime=release/'runtime';runtime.mkdir()
    local=ROOT/'work/legacy-data'
    for src,dst in [(local/'imported-catalog-20261021.db','orders.sqlite3'),(local/'verified-addresses.db','verified-addresses.sqlite3'),(local/'recognition-reviews.db','recognition-reviews.sqlite3')]:
        if not src.exists():continue
        with closing(sqlite3.connect(src.as_uri()+'?mode=ro',uri=True)) as c,closing(sqlite3.connect(runtime/dst)) as d:
            c.backup(d)
            assert d.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
            assert not d.execute('PRAGMA foreign_key_check').fetchall()
    baseline=evidence(runtime/'orders.sqlite3')
    (release/'data-evidence.json').write_text(json.dumps(baseline,ensure_ascii=False,indent=2))
    candidate='/var/lib/newbot/candidates/'+release.name
    for name in ['recipient-policy.json','imported-rule-dictionary-20261021.json']:
        shutil.copy2(local/name,runtime/name)
    newkey=Fernet.generate_key();old=Fernet((local/'local-settings.key').read_bytes().strip());new=Fernet(newkey)
    (runtime/'local-settings.key').write_bytes(newkey)
    for name in ['ai_settings.json','aliyun_address_settings.json']:
        value=json.loads((ROOT/'legacy_app'/name).read_text())
        def rotate(item):
            if isinstance(item,dict):
                for k,v in item.items():
                    if k.endswith('ciphertext') and isinstance(v,str) and v:
                        plain=old.decrypt(v.encode());item[k]=new.encrypt(plain).decode();assert new.decrypt(item[k].encode())==plain
                    else:rotate(v)
            elif isinstance(item,list):
                for v in item:rotate(v)
        rotate(value)
        (runtime/name).write_text(json.dumps(value,ensure_ascii=False))
    shutil.copy2(ROOT/'legacy_app/settings.json',runtime/'settings.json')
    credentials=dotenv_values(local/'admin-login.env')
    token=dotenv_values(ROOT/'work/test-bot.env')['BOT_TOKEN']
    env=dict(NEWBOT_ROLE='backend-only',BOT_TOKEN=token,TELEGRAM_BOT_TOKEN=token,
        DATABASE_URL='sqlite:///'+candidate+'/orders.sqlite3',ADMIN_USERNAME=credentials['ADMIN_USERNAME'],ADMIN_PASSWORD=credentials['ADMIN_PASSWORD'],
        API_SECRET_KEY=os.urandom(32).hex(),SESSION_SECRET_KEY=os.urandom(32).hex(),AI_SETTINGS_MASTER_KEY=newkey.decode(),
        API_BASE_URL='http://127.0.0.1:8765',RAINBOW_ALLOWED_HOSTS='admin.chdf888.com,api.chdf888.com,127.0.0.1,localhost',
        RAINBOW_DICTIONARY_PATH=candidate+'/imported-rule-dictionary-20261021.json',
        RAINBOW_RECIPIENT_POLICY_PATH=candidate+'/recipient-policy.json',
        RAINBOW_VERIFIED_ADDRESS_CACHE_PATH=candidate+'/verified-addresses.sqlite3',
        RAINBOW_FAILURE_ANALYSIS_PATH=candidate+'/recognition-reviews.sqlite3',
        AI_SETTINGS_PATH=candidate+'/ai_settings.json',RAINBOW_ALIYUN_ADDRESS_PATH=candidate+'/aliyun_address_settings.json',
        RAINBOW_INVENTORY_CACHE='/var/cache/newbot',RAINBOW_RECEIPT_AUDIT_PATH='/var/log/newbot/success_receipt_audit.jsonl',
        RAINBOW_DEPLOY_DRAIN_PATH=candidate+'/deployment-drain.json',
        RAINBOW_DISABLE_BACKGROUND_WORKERS='1',RAINBOW_FAILURE_ANALYSIS_ENABLED='0',RAINBOW_RECOGNITION_REVIEWS_ENABLED='1',
        WEBHOOK_URL='',DEEPSEEK_ENABLED='0',IMAGE_ORDER_RECOGNITION_ENABLED='0',ORDER_NOTIFY_ENABLED='0',
        SPLIT_NOTIFICATION_CHAT_ID='',SPLIT_ORDER_GROUP_IDS='',ADMIN_TELEGRAM_IDS='',ADMIN_ID='',AGENT_GROUP_IDS='',
        AFTERSALE_NOTIFY_IDS='',ORDER_NOTIFICATION_TELEGRAM_IDS='')
    (runtime/'runtime.env').write_text('\n'.join(k+'='+json.dumps(v) for k,v in env.items())+'\n')
    facts={str(p.relative_to(release)):hashlib.sha256(p.read_bytes()).hexdigest() for p in release.rglob('*') if p.is_file()}
    (release/'manifest.json').write_text(json.dumps(facts,indent=2))
    archive=OUT/(release.name+'.tar.gz')
    with tarfile.open(archive,'w:gz') as tf:tf.add(release,arcname=release.name)
    print(json.dumps(dict(release=release.name,archive=str(archive),sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),files=len(facts))))

if __name__=='__main__':main()
