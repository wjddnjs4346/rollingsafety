import os, sqlite3, secrets, hashlib, hmac, shutil, threading, json
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
DB_LOCK = threading.RLock()

ROLES = ('owner', 'admin', 'inspector', 'viewer')

def storage():
    value = os.environ.get('DATA_DIR', '').strip()
    if not value:
        raise RuntimeError('DATA_DIR must be configured; no fallback database is allowed.')
    if value.startswith(('\\\\', '//')):
        raise RuntimeError('Network shares are not supported for the live database.')
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeError('DATA_DIR must be an absolute local path.')
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ('.write-test-' + secrets.token_hex(8))
    try:
        probe.write_bytes(b'test')
    finally:
        probe.unlink(missing_ok=True)
    if shutil.disk_usage(path).free < 50 * 1024 * 1024:
        raise RuntimeError('At least 50 MB of free storage is required.')
    for name in ('layouts', 'backups', 'logs'):
        (path / name).mkdir(exist_ok=True)
    return path

@contextmanager
def connect(path):
    with DB_LOCK:
        db = sqlite3.connect(path / 'safety.db', timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=10000')
        try:
            with db:
                yield db
        finally:
            db.close()


def initialize(path):
    with connect(path) as db:
        db.execute('PRAGMA journal_mode=WAL')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version > 2:
            raise RuntimeError('Database schema is newer than this application.')
        if version == 0:
            db.executescript((ROOT / 'app' / 'schema.sql').read_text())
            db.execute('PRAGMA user_version=1')
            version = 1
    if version == 1:
        # Consistent safety snapshot before non-destructive migration.
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        with connect(path) as source_db:
            target_db = sqlite3.connect(path / 'backups' / ('pre_migration_' + stamp + '.sqlite'))
            try:
                source_db.backup(target_db)
            finally:
                target_db.close()
            sql = (ROOT / 'app/migrations/002.sql').read_text()
            source_db.executescript('BEGIN;\n' + sql + '\nPRAGMA user_version=2;\nCOMMIT;')
    with connect(path) as db:
        templates = json.loads((ROOT / 'app/checklists.json').read_text(encoding='utf-8'))
        for kind, items in templates.items():
            db.execute('INSERT OR IGNORE INTO checklist_templates(type,version) VALUES(?,1)', (kind,))
            template_id = db.execute('SELECT id FROM checklist_templates WHERE type=? AND version=1', (kind,)).fetchone()[0]
            for item in items:
                db.execute('INSERT OR IGNORE INTO checklist_items(template_id,item_code,label,applicability) VALUES(?,?,?,?)', (template_id,item['code'],item['label'],item['applicability']))
    source = ROOT / 'assets' / 'LAYOUT.pdf'
    target = path / 'layouts' / 'LAYOUT.pdf'
    if not target.exists():
        if not source.exists():
            raise RuntimeError('LAYOUT.pdf is missing.')
        shutil.copy2(source, target)
    image = path / 'layouts' / 'layout.png'
    if not image.exists() and (ROOT/'assets/layout.png').exists():
        shutil.copy2(ROOT/'assets/layout.png',image)
    if not image.exists():
        import fitz
        with fitz.open(target) as doc:
            if len(doc) != 1:
                raise RuntimeError('Expected a one-page plant layout.')
            pix = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            temp = image.with_name('layout.tmp.png')
            pix.save(temp)
            temp.replace(image)
    with connect(path) as db:
        db.execute('INSERT OR IGNORE INTO layouts(id,version,original_path,image_path,active) VALUES(1,1,?,?,1)', ('layouts/LAYOUT.pdf','layouts/layout.png'))
        from PIL import Image
        with Image.open(image) as pix:
            db.execute('UPDATE layouts SET width=?,height=? WHERE id=1',pix.size)

def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + ':' + digest.hex()

def verify(password, encoded):
    try:
        salt, digest = encoded.split(':')
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(actual.hex(), digest)
    except (ValueError, TypeError):
        return False

def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()
