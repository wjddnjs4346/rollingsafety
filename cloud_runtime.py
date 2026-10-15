"""In-process ASGI adapter; no listening port or subprocess.
PostgreSQL stores a transactional compressed SQLite snapshot plus backup files.
All requests are serialized locally and SELECT FOR UPDATE serializes replicas.
"""
import io,os,sqlite3,tempfile,threading,zipfile,atexit
from pathlib import Path
from fastapi.testclient import TestClient

class CloudRuntime:
 def __init__(self,config):
  self.config=dict(config);self.mode=config.get('storage_mode','postgres')
  self.url=config.get('database_url','');self.lock=threading.RLock();self.revision=None
  if self.mode not in ('postgres','demo'):raise ValueError('storage_mode must be postgres or demo')
  if self.mode=='postgres' and not self.url:raise ValueError('database_url is required for persistent storage')
  self.temp=tempfile.TemporaryDirectory(prefix='rolling-cloud-');self.path=Path(self.temp.name)
  os.environ.update(DATA_DIR=str(self.path),COOKIE_SECURE='false',AUTO_BACKUP='false')
  from app.main import app
  self.client=TestClient(app,raise_server_exceptions=False);self.client.__enter__()
  atexit.register(self.close)
  self.initial=self.pack()
  if self.mode=='demo':self.bootstrap()
  else:
   import psycopg
   with psycopg.connect(self.url,connect_timeout=10) as conn:
    if conn.execute("SELECT to_regclass('rolling_safety_state')").fetchone()[0] is None:
     conn.execute('CREATE TABLE IF NOT EXISTS rolling_safety_state (id integer PRIMARY KEY CHECK(id=1), revision bigint NOT NULL DEFAULT 0, payload bytea NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())')
    conn.execute('INSERT INTO rolling_safety_state(id,payload) VALUES(1,%s) ON CONFLICT(id) DO NOTHING',(self.initial,))
    row=conn.execute('SELECT revision,payload FROM rolling_safety_state WHERE id=1 FOR UPDATE').fetchone()
    self.unpack(bytes(row[1]));self.bootstrap()
    conn.execute('UPDATE rolling_safety_state SET payload=%s,revision=revision+1,updated_at=now() WHERE id=1',(self.pack(),))
    self.revision=row[0]+1

 def close(self):
  if getattr(self,'closed',False):return
  self.closed=True
  self.client.__exit__(None,None,None);self.temp.cleanup()

 def bootstrap(self):
  from app.core import connect,password_hash
  with connect(self.path) as db:
   if db.execute("SELECT 1 FROM users WHERE role='owner'").fetchone():return
   username=str(self.config.get('admin_username','')).strip();password=str(self.config.get('admin_password',''))
   if not username or len(username)>100 or not 12<=len(password)<=256:raise ValueError('Set admin_username and a 12+ character admin_password in Secrets')
   db.execute('INSERT INTO users(username,password_hash,role) VALUES(?,?,?)',(username,password_hash(password),'owner'))

 def pack(self):
  with tempfile.TemporaryDirectory(prefix='rolling-snapshot-') as folder:
   snapshot=Path(folder)/'snapshot.db'
   with sqlite3.connect(snapshot) as target:
    with sqlite3.connect(self.path/'safety.db') as source:source.backup(target)
    target.execute('PRAGMA journal_mode=DELETE')
   data=snapshot.read_bytes()
  out=io.BytesIO()
  with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
   z.writestr('safety.db',data)
   for p in (self.path/'backups').glob('*.zip'):z.write(p,'backups/'+p.name)
  payload=out.getvalue()
  if len(payload)>50*1024*1024:raise ValueError('State exceeds 50 MiB. Archive old backups before retrying.')
  return payload

 def unpack(self,payload):
  with zipfile.ZipFile(io.BytesIO(payload)) as z:
   names=z.namelist()
   if 'safety.db' not in names or sum(i.file_size for i in z.infolist())>256*1024*1024:raise ValueError('Invalid cloud snapshot')
   mem=sqlite3.connect(':memory:')
   try:
    mem.deserialize(z.read('safety.db'))
    if mem.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Invalid SQLite snapshot')
    with sqlite3.connect(self.path/'safety.db') as target:mem.backup(target)
   finally:mem.close()
   from app.core import migrate_multi
   migrate_multi(self.path)
   expected=set()
   for name in names:
    if name.startswith('backups/') and name.endswith('.zip'):
     filename=name.split('/')[-1]
     if name!='backups/'+filename:raise ValueError('Invalid backup path')
     expected.add(filename);(self.path/'backups'/filename).write_bytes(z.read(name))
   for p in (self.path/'backups').glob('*.zip'):
    if p.name not in expected:p.unlink()

 def request(self,path,method='GET',body=None,cookie='',csrf=''):
  if not path.startswith('/api/'):raise ValueError('Only application routes are allowed')
  with self.lock:
   if self.mode=='demo':return self.dispatch(path,method,body,cookie,csrf)
   import psycopg
   try:
    with psycopg.connect(self.url,connect_timeout=10) as conn:
     conn.execute("SET LOCAL lock_timeout='15s'")
     row=conn.execute('SELECT revision,payload FROM rolling_safety_state WHERE id=1 FOR UPDATE').fetchone()
     if row is None:raise ValueError('Cloud state is missing; do not recreate silently')
     if self.revision!=row[0]:self.unpack(bytes(row[1]))
     response=self.dispatch(path,method,body,cookie,csrf)
     # GET updates session activity, so it also persists the state.
     conn.execute('UPDATE rolling_safety_state SET payload=%s,revision=revision+1,updated_at=now() WHERE id=1',(self.pack(),))
    self.revision=row[0]+1
    return response
   except Exception:
    self.revision=None # Reload committed state on next request; never report a failed commit as success.
    raise

 def dispatch(self,path,method,body,cookie,csrf):
  self.client.cookies.clear()
  headers={'X-CSRF-Token':csrf}
  if cookie:headers['Cookie']=cookie
  try:return self.client.request(method,path,json=body,headers=headers)
  finally:self.client.cookies.clear()
