import os, time, secrets, asyncio, logging, sqlite3
from fastapi.exceptions import RequestValidationError
from .backup import acquire_server_lock, daily_backup
from .domain import fail
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .core import ROOT, storage, initialize, connect, verify, token_hash

async def automatic_backups(app):
    while True:
        try:
            with connect(app.state.data) as db:
                ready=db.execute("SELECT 1 FROM users WHERE role='owner' AND active=1").fetchone()
            if ready:
                await asyncio.to_thread(daily_backup,app.state.data)
                app.state.backup_error=None
        except Exception:
            app.state.backup_error='Automatic backup failed; inspect server logs.'
            logging.getLogger('rolling_safety').exception('Automatic backup failed')
        await asyncio.sleep(60)

@asynccontextmanager
async def lifespan(app):
    app.state.data = storage()
    handle=acquire_server_lock(app.state.data)
    app.state.maintenance=False
    app.state.backup_error=None
    task=None
    try:
        initialize(app.state.data)
        if os.environ.get('AUTO_BACKUP','true').lower()=='true':
            task=asyncio.create_task(automatic_backups(app))
        yield
    finally:
        if task:
            task.cancel()
            try:await task
            except asyncio.CancelledError:pass
        handle.close()

app = FastAPI(title='Rolling Safety - Stage 2', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount('/static', StaticFiles(directory=ROOT / 'app/static'), name='static')

@app.middleware('http')
async def security_headers(request, call_next):
    if getattr(request.app.state,'maintenance',False) and request.url.path.startswith('/api/'):
        return JSONResponse({'detail':'\ubcf5\uad6c \uc911\uc785\ub2c8\ub2e4. \uc7a0\uc2dc \ud6c4 \uc7ac\uc2dc\ub3c4\ud558\uc138\uc694.'},status_code=503)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    return response

def current(request: Request):
    raw = request.cookies.get('session', '')
    with connect(request.app.state.data) as db:
        row = db.execute('SELECT s.*,u.username,u.display_name,u.role,u.active,u.auth_version AS current_version FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?', (token_hash(raw),)).fetchone()
        now = time.time()
        if not row or not row['active'] or row['auth_version'] != row['current_version'] or now-row['last_seen'] >= 1800:
            if row:
                db.execute('DELETE FROM sessions WHERE token_hash=?', (token_hash(raw),))
                db.commit()
            raise HTTPException(401, 'Login required or session expired')
        if request.method not in ('GET','HEAD','OPTIONS') and not secrets.compare_digest(request.headers.get('X-CSRF-Token',''), row['csrf']):
            raise HTTPException(403, 'Invalid CSRF token')
        db.execute('UPDATE sessions SET last_seen=? WHERE token_hash=?', (now, token_hash(raw)))
        return dict(row)

def require(*roles):
    def dependency(user=Depends(current)):
        if user['role'] not in roles:
            fail(403,'denied')
        return user
    return dependency

class Login(BaseModel):
    username: str = Field(min_length=1,max_length=100)
    password: str = Field(min_length=1,max_length=256)

@app.get('/')
def index():
    return FileResponse(ROOT / 'app/static/index.html')

@app.get('/health')
def health():
    return {'status':'ok','stage':2}

@app.post('/api/login')
def login(body: Login, request: Request):
    key = request.client.host if request.client else 'unknown'
    now = time.time()
    with connect(request.app.state.data) as db:
        attempt = db.execute('SELECT * FROM login_attempts WHERE client=?',(key,)).fetchone()
        if attempt and attempt['blocked_until'] > now:
            raise HTTPException(429, 'Too many attempts; retry later')
        user = db.execute('SELECT * FROM users WHERE username=?',(body.username,)).fetchone()
        valid = verify(body.password, user['password_hash'] if user else '00'*16+':'+'00'*64)
        if not user or not user['active'] or not valid:
            failures = (attempt['failures'] if attempt else 0)+1
            db.execute('INSERT OR REPLACE INTO login_attempts VALUES(?,?,?)',(key,failures,now+300 if failures>=5 else 0))
            response = JSONResponse({'detail':'Invalid credentials'},status_code=401)
        else:
            db.execute('DELETE FROM login_attempts WHERE client=?',(key,))
            raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            old = request.cookies.get('session')
            if old:
                db.execute('DELETE FROM sessions WHERE token_hash=?',(token_hash(old),))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',(token_hash(raw),user['id'],csrf,user['auth_version'],now))
            db.execute('INSERT INTO audit_logs(actor_id,action,entity_type,entity_id) VALUES(?,?,?,?)',(user['id'],'login','user',user['id']))
            response = JSONResponse({'username':user['username'],'role':user['role'],'csrf':csrf})
            response.set_cookie('session',raw,httponly=True,samesite='strict',secure=os.environ.get('COOKIE_SECURE','true').lower()=='true')
    return response

@app.get('/api/me')
def me(user=Depends(current)):
    return {'username':user['username'],'role':user['role'],'csrf':user['csrf'],'id':user['user_id'],'display_name':user['display_name']}

@app.post('/api/logout')
def logout(request: Request, user=Depends(current)):
    with connect(request.app.state.data) as db:
        db.execute('DELETE FROM sessions WHERE token_hash=?',(user['token_hash'],))
    response = JSONResponse({'ok':True})
    response.delete_cookie('session')
    return response

@app.get('/api/layout')
def layout(request: Request, user=Depends(current)):
    return FileResponse(request.app.state.data / 'layouts/layout.png')

@app.get('/api/admin/status')
def admin_status(request: Request,user=Depends(require('owner','admin'))):
    return {'stage':2,'equipment_management':'ready','assignments':'ready','backup_error':request.app.state.backup_error}

@app.exception_handler(RequestValidationError)
async def validation_error(request,error):
    fields=['.'.join(map(str,e['loc'])) for e in error.errors()]
    return JSONResponse({'detail':'\uc785\ub825\uac12\uc744 \ud655\uc778\ud558\uc138\uc694.','fields':fields},status_code=422)

@app.exception_handler(sqlite3.Error)
async def database_error(request,error):
    logging.getLogger('rolling_safety').error('Database operation failed: %s',type(error).__name__)
    return JSONResponse({'detail':'\uc800\uc7a5\uc5d0 \uc2e4\ud328\ud588\uc2b5\ub2c8\ub2e4. \uc785\ub825\uc744 \uc720\uc9c0\ud558\uace0 \uc7ac\uc2dc\ub3c4\ud558\uc138\uc694.'},status_code=503)

from .routes import make_router
app.include_router(make_router(current,require))
