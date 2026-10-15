import json, hashlib
from datetime import datetime, timezone
from fastapi import HTTPException

MESSAGES={
 'denied':'\uad8c\ud55c\uc774 \uc5c6\uc2b5\ub2c8\ub2e4.',
 'conflict':'\ub2e4\ub978 \uc0ac\uc6a9\uc790\uac00 \ubcc0\uacbd\ud588\uc2b5\ub2c8\ub2e4. \uc785\ub825\uc744 \ubcf4\uc874\ud55c \ud6c4 \ub2e4\uc2dc \uc870\ud68c\ud558\uc138\uc694.',
 'missing':'\uc124\ube44\ub97c \ucc3e\uc744 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4.',
 'invalid':'\uc785\ub825\uac12\uc744 \ud655\uc778\ud558\uc138\uc694.',
 'submitted':'\uc81c\ucd9c\ub41c \uc810\uac80\ud45c\ub294 \uc218\uc815\ud560 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4. \uc870\uce58\ub294 \ubbf8\ube44\uc0ac\ud56d\uc5d0 \ubcc4\ub3c4 \uae30\ub85d\ud558\uc138\uc694.',
 'chronology':'\uae30\uc874 \uc810\uac80/\uc870\uce58\ubcf4\ub2e4 \uc774\uc804 \ubd84\uae30\ub85c \uc81c\ucd9c\ud560 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4.',
 'comment':'\ubbf8\ube44 \ucf54\uba58\ud2b8 \ub610\ub294 \ud574\ub2f9\uc5c6\uc74c \uc0ac\uc720\ub97c \uc785\ub825\ud558\uc138\uc694.',
 'all_items':'\ubaa8\ub4e0 \uc810\uac80\ud56d\ubaa9\uc744 \uc785\ub825\ud558\uc138\uc694.',
 'owner':'\ucd5c\uace0\uad00\ub9ac\uc790 \uacc4\uc815\uc744 \ube44\ud65c\uc131\ud654\ud558\uac70\ub098 \uad8c\ud55c\uc744 \ubcc0\uacbd\ud560 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4.',
 'assignee':'\ud65c\uc131 \uc810\uac80\uad8c\ud55c \uacc4\uc815\uc744 \uc120\ud0dd\ud558\uc138\uc694.',
}
def fail(code,key):raise HTTPException(code,MESSAGES.get(key,key))
def now():return datetime.now(timezone.utc).isoformat()
def period(year,quarter):return year*4+quarter

def audit(db,user,action,kind,entity_id,before=None,after=None):
    db.execute('INSERT INTO audit_logs(actor_id,action,entity_type,entity_id,before_json,after_json,occurred_at) VALUES(?,?,?,?,?,?,?)',(user['user_id'],action,kind,entity_id,json.dumps(before,ensure_ascii=False) if before is not None else None,json.dumps(after,ensure_ascii=False) if after is not None else None,now()))

def live(db,user,roles=None):
    row=db.execute('SELECT * FROM users WHERE id=?',(user['user_id'],)).fetchone()
    session=db.execute('SELECT 1 FROM sessions WHERE token_hash=? AND user_id=?',(user['token_hash'],user['user_id'])).fetchone()
    if not session or not row or not row['active'] or row['auth_version']!=user['auth_version']:
        fail(401,'denied')
    if roles and row['role'] not in roles:fail(403,'denied')
    return row

def equipment(db,eid,deleted=False):
    row=db.execute('SELECT * FROM equipment WHERE id=?',(eid,)).fetchone()
    if not row or (row['deleted_at'] and not deleted):fail(404,'missing')
    return dict(row)

def can_inspect(db,user,eid,year,quarter):
    row=live(db,user)
    if row['role'] in ('owner','admin'):return True
    assignment=db.execute('SELECT * FROM assignments WHERE equipment_id=? AND year=? AND quarter=?',(eid,year,quarter)).fetchone()
    return row['role']=='inspector' and row['id'] in assignment_ids(assignment)

def inspect_guard(db,user,eid,year,quarter):
    equipment(db,eid)
    if not can_inspect(db,user,eid,year,quarter):fail(403,'denied')

def findings(db,eid,year,quarter):
    rows=db.execute('SELECT f.*,i.year AS source_year,i.quarter AS source_quarter FROM findings f JOIN inspections i ON i.id=f.source_inspection_id WHERE f.equipment_id=? AND i.year*4+i.quarter<=? ORDER BY f.id',(eid,period(year,quarter))).fetchall()
    output=[]
    for row in rows:
        item=dict(row)
        events=[dict(e) for e in db.execute('SELECT e.*,u.username AS actor FROM finding_events e JOIN users u ON u.id=e.actor_id WHERE finding_id=? ORDER BY e.id',(row['id'],))]
        state='open'
        for event in events:
            if event['year'] is not None and period(event['year'],event['quarter'])<=period(year,quarter):
                state='resolved' if event['event_type']=='resolve' else 'open'
        item['period_status']=state;item['events']=events
        item['carried']=period(row['source_year'],row['source_quarter'])<period(year,quarter)
        output.append(item)
    return output

def status(db,eid,year,quarter):
    row=db.execute('SELECT * FROM inspections WHERE equipment_id=? AND year=? AND quarter=?',(eid,year,quarter)).fetchone()
    fs=findings(db,eid,year,quarter)
    count=sum(f['period_status']=='open' for f in fs)
    state='uninspected' if not row or not row['submitted_at'] else 'incomplete' if count else 'complete'
    return {'status':state,'unresolved':count,'carried_unresolved':sum(f['period_status']=='open' and f['carried'] for f in fs),'draft':bool(row and not row['submitted_at'])}

def idempotent(db,user,key,route,body):
    digest=hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    row=db.execute('SELECT * FROM requests WHERE id=?',(key,)).fetchone()
    if row:
        if row['user_id']!=user['user_id'] or row['route']!=route or row['body_hash']!=digest:fail(409,'conflict')
        return digest,json.loads(row['response_json'])
    return digest,None

def remember(db,user,key,route,digest,response):
    db.execute('INSERT INTO requests(id,user_id,route,body_hash,response_json) VALUES(?,?,?,?,?)',(key,user['user_id'],route,digest,json.dumps(response)))


def assignment_ids(row):
    if not row:return []
    data=dict(row)
    if data.get('assignee_ids_json') is not None:return json.loads(data['assignee_ids_json'])
    return [data['assignee_id']] if data.get('assignee_id') is not None else []

def assignment_view(db,row):
    if not row:return None
    data=dict(row);ids=assignment_ids(row);data['assignee_ids']=ids
    data['assignees']=[dict(u) for uid in ids for u in db.execute('SELECT id,username,display_name,active FROM users WHERE id=?',(uid,))]
    return data
