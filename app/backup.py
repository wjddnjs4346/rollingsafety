import os, json, sqlite3, zipfile, hashlib, tempfile, shutil, secrets, logging
from pathlib import Path, PurePosixPath
from datetime import datetime, timedelta, timezone
from .core import DB_LOCK, connect

LOGGER=logging.getLogger('rolling_safety')

def acquire_server_lock(path):
    handle=open(path/'.server.lock','a+b')
    handle.seek(0);handle.write(b'1');handle.flush();handle.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError('Another server is using DATA_DIR. Run exactly one worker.')
    return handle

def snapshot(path,kind='manual'):
    with DB_LOCK, tempfile.TemporaryDirectory(dir=path) as tmp:
        tmp=Path(tmp);dbfile=tmp/'safety.db'
        with connect(path) as source:
            target=sqlite3.connect(dbfile)
            try:source.backup(target)
            finally:target.close()
        files={'safety.db':dbfile}
        for file in (path/'layouts').rglob('*'):
            if file.is_file():files[file.relative_to(path).as_posix()]=file
        manifest={'format':2,'created_at':datetime.now(timezone.utc).isoformat(),'files':{name:hashlib.sha256(file.read_bytes()).hexdigest() for name,file in files.items()}}
        name=kind+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'_'+secrets.token_hex(4)+'.zip'
        target=path/'backups'/name;partial=target.with_suffix('.partial')
        try:
            with zipfile.ZipFile(partial,'w',zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('manifest.json',json.dumps(manifest))
                for name_in,file in files.items():archive.write(file,name_in)
            partial.replace(target)
        finally:partial.unlink(missing_ok=True)
        return target.name

def unpack_validate(archive_path,target):
    with zipfile.ZipFile(archive_path) as archive:
        infos=archive.infolist()
        if len(infos)>1000 or sum(i.file_size for i in infos)>512*1024*1024:raise ValueError('Backup exceeds size limit')
        if len({i.filename for i in infos})!=len(infos):raise ValueError('Duplicate archive entries')
        manifest=json.loads(archive.read('manifest.json'))
        if manifest.get('format')!=2 or not isinstance(manifest.get('files'),dict):raise ValueError('Unsupported backup format')
        if set(archive.namelist()) != set(manifest['files'])|{'manifest.json'}:raise ValueError('Unexpected archive contents')
        for name,digest in manifest['files'].items():
            parts=PurePosixPath(name)
            if parts.is_absolute() or '..' in parts.parts or '\\' in name or ':' in name or not (name=='safety.db' or name.startswith('layouts/')):raise ValueError('Unsafe backup path')
            data=archive.read(name)
            if hashlib.sha256(data).hexdigest()!=digest:raise ValueError('Backup checksum mismatch')
            file=target/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(data)
    db=sqlite3.connect(target/'safety.db')
    try:
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('Invalid database integrity')
        if db.execute('PRAGMA user_version').fetchone()[0]!=2:raise ValueError('Unsupported database schema')
        expected={'users','sessions','login_attempts','layouts','equipment','assignments','checklist_templates','checklist_items','inspections','inspection_results','findings','finding_events','audit_logs','requests'}
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not expected<=tables:raise ValueError('Missing database tables')
        if not db.execute("SELECT 1 FROM users WHERE role='owner' AND active=1").fetchone():raise ValueError('Backup has no active owner')
        for original,image in db.execute('SELECT original_path,image_path FROM layouts'):
            for value in (original,image):
                file=(target/value).resolve()
                if not file.is_relative_to((target/'layouts').resolve()) or not file.is_file():raise ValueError('Missing or unsafe layout reference')
    finally:db.close()
    return manifest

def apply_snapshot(path,source_dir):
    # All application DB connections are closed before this exclusive operation.
    source=sqlite3.connect(source_dir/'safety.db')
    target=sqlite3.connect(path/'safety.db')
    try:source.backup(target)
    finally:source.close();target.close()
    old=path/'layouts.previous';new=path/'layouts.next'
    shutil.rmtree(old,ignore_errors=True);shutil.rmtree(new,ignore_errors=True)
    shutil.copytree(source_dir/'layouts',new)
    (path/'layouts').rename(old)
    try:new.rename(path/'layouts')
    except BaseException:
        old.rename(path/'layouts');raise
    shutil.rmtree(old)

def restore(path,name,user):
    if Path(name).name!=name or not name.endswith('.zip'):raise ValueError('Invalid backup name')
    archive=path/'backups'/name
    if not archive.is_file():raise ValueError('Backup not found')
    with DB_LOCK, tempfile.TemporaryDirectory(dir=path) as tmp:
        tmp=Path(tmp);incoming=tmp/'incoming';incoming.mkdir()
        unpack_validate(archive,incoming)
        # Keep the owner identity fixed; restored credentials are those in the backup.
        db=sqlite3.connect(incoming/'safety.db')
        try:
            owner=db.execute('SELECT username FROM users WHERE id=? AND role=? AND active=1',(user['user_id'],'owner')).fetchone()
            if not owner or owner[0]!=user['username']:raise ValueError('Owner identity mismatch')
        finally:db.close()
        safety_name=snapshot(path,'pre_restore')
        safety=tmp/'safety';safety.mkdir();unpack_validate(path/'backups'/safety_name,safety)
        try:
            apply_snapshot(path,incoming)
            with connect(path) as db:
                db.execute('DELETE FROM sessions');db.execute('DELETE FROM login_attempts')
                from .domain import audit
                audit(db,user,'restore','backup',None,after={'backup':name,'pre_restore':safety_name})
        except BaseException:
            apply_snapshot(path,safety)
            raise
        return safety_name

def daily_backup(path):
    # UTC+09:00 has no DST; avoid a Windows tzdata dependency.
    today=datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    stamp=path/'logs/last_auto_backup.txt'
    if stamp.exists() and stamp.read_text().strip()==today:return
    name=snapshot(path,'auto')
    stamp.write_text(today)
    cutoff=datetime.now(timezone.utc).timestamp()-30*86400
    for file in (path/'backups').glob('auto_*.zip'):
        if file.stat().st_mtime<cutoff:file.unlink()
    LOGGER.info('Automatic backup completed: %s',name)
