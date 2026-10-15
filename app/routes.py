import json, sqlite3, zipfile
from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import FileResponse
from .core import connect, password_hash, DB_LOCK
from .domain import assignment_ids, assignment_view, live, equipment, audit, fail, now, period, status, findings, inspect_guard, can_inspect, idempotent, remember
from .models import EquipmentIn, EquipmentUpdate, Delete, Version, Assignment, UserIn, UserUpdate, InspectionIn, FindingAction, Restore
from .backup import snapshot, restore


def make_router(current,require):
    router=APIRouter(prefix='/api')

    @router.get('/equipment')
    def list_equipment(request:Request,year:int=Query(2026,ge=2000,le=2100),quarter:int=Query(1,ge=1,le=4),deleted:bool=False,user=Depends(current)):
        with connect(request.app.state.data) as db:
            live(db,user)
            if deleted and user['role']!='owner':fail(403,'denied')
            rows=[dict(r) for r in db.execute('SELECT * FROM equipment WHERE '+('deleted_at IS NOT NULL' if deleted else 'deleted_at IS NULL')+' ORDER BY code')]
            for row in rows:
                row.update(status(db,row['id'],year,quarter))
                a=db.execute('SELECT a.*,u.username,u.display_name,u.active FROM assignments a LEFT JOIN users u ON u.id=a.assignee_id WHERE equipment_id=? AND year=? AND quarter=?',(row['id'],year,quarter)).fetchone()
                row['assignment']=assignment_view(db,a)
            return rows

    @router.post('/equipment',status_code=201)
    def create(body:EquipmentIn,request:Request,user=Depends(require('owner','admin'))):
        with connect(request.app.state.data) as db:
            live(db,user,('owner','admin'))
            if not db.execute('SELECT 1 FROM layouts WHERE id=? AND active=1',(body.layout_id,)).fetchone():fail(422,'invalid')
            try:
                cur=db.execute('INSERT INTO equipment(code,type,subtype,name,location_text,layout_id,x,y) VALUES(?,?,?,?,?,?,?,?)',tuple(body.model_dump().values()))
            except sqlite3.IntegrityError:fail(409,'\uc124\ube44\ubc88\ud638\uac00 \uc911\ubcf5\ub418\uc5c8\uc2b5\ub2c8\ub2e4.')
            row=equipment(db,cur.lastrowid);audit(db,user,'create','equipment',row['id'],after=row)
            return row

    @router.get('/equipment/{eid}')
    def detail(eid:int,request:Request,year:int=Query(2026,ge=2000,le=2100),quarter:int=Query(1,ge=1,le=4),user=Depends(current)):
        with connect(request.app.state.data) as db:
            live(db,user);e=equipment(db,eid,deleted=user['role']=='owner')
            row=db.execute('SELECT i.*,u.username AS inspector FROM inspections i LEFT JOIN users u ON u.id=i.inspector_id WHERE equipment_id=? AND year=? AND quarter=?',(eid,year,quarter)).fetchone()
            template_id=row['template_id'] if row else db.execute('SELECT id FROM checklist_templates WHERE type=? ORDER BY version DESC LIMIT 1',(e['type'],)).fetchone()[0]
            items=[dict(i) for i in db.execute('SELECT item_code,label,applicability FROM checklist_items WHERE template_id=? ORDER BY CAST(item_code AS INTEGER)',(template_id,))]
            for item in items:item['applicable']=item['applicability']=='all' or item['applicability']==e['subtype']
            results=[dict(i) for i in db.execute('SELECT * FROM inspection_results WHERE inspection_id=?',(row['id'],))] if row else []
            quarters=[]
            for q in range(1,5):
                a=db.execute('SELECT a.*,u.username,u.display_name,u.active FROM assignments a LEFT JOIN users u ON u.id=a.assignee_id WHERE equipment_id=? AND year=? AND quarter=?',(eid,year,q)).fetchone()
                quarters.append({'quarter':q,'assignment':assignment_view(db,a),**status(db,eid,year,q)})
            return {'equipment':e,'inspection':dict(row) if row else None,'items':items,'results':results,'findings':findings(db,eid,year,quarter),'quarters':quarters,'can_inspect':not e['deleted_at'] and can_inspect(db,user,eid,year,quarter),**status(db,eid,year,quarter)}

    @router.patch('/equipment/{eid}')
    def update(eid:int,body:EquipmentUpdate,request:Request,user=Depends(require('owner','admin'))):
        with connect(request.app.state.data) as db:
            live(db,user,('owner','admin'));old=equipment(db,eid)
            if old['version']!=body.version:fail(409,'conflict')
            if (old['type']!=body.type or old['subtype']!=body.subtype) and db.execute('SELECT 1 FROM inspections WHERE equipment_id=?',(eid,)).fetchone():fail(409,'\uc810\uac80 \uc774\ub825\uc774 \uc788\ub294 \uc124\ube44\uc758 \uc720\ud615\uc740 \ubcc0\uacbd\ud560 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4.')
            if old['layout_id']!=body.layout_id:fail(422,'invalid')
            try:
                db.execute('UPDATE equipment SET code=?,type=?,subtype=?,name=?,location_text=?,layout_id=?,x=?,y=?,version=version+1,updated_at=? WHERE id=?',(*body.model_dump(exclude={'version'}).values(),now(),eid))
            except sqlite3.IntegrityError:fail(409,'\uc124\ube44\ubc88\ud638\uac00 \uc911\ubcf5\ub418\uc5c8\uc2b5\ub2c8\ub2e4.')
            new=equipment(db,eid);audit(db,user,'update','equipment',eid,old,new);return new

    @router.delete('/equipment/{eid}')
    def delete(eid:int,body:Delete,request:Request,user=Depends(require('owner'))):
        with connect(request.app.state.data) as db:
            live(db,user,('owner',));old=equipment(db,eid)
            if old['version']!=body.version:fail(409,'conflict')
            db.execute('UPDATE equipment SET deleted_at=?,deleted_by=?,delete_reason=?,version=version+1 WHERE id=?',(now(),user['user_id'],body.reason,eid))
            audit(db,user,'delete','equipment',eid,old,equipment(db,eid,True));return {'ok':True}

    @router.post('/equipment/{eid}/restore')
    def restore_equipment(eid:int,body:Version,request:Request,user=Depends(require('owner'))):
        with connect(request.app.state.data) as db:
            live(db,user,('owner',));old=equipment(db,eid,True)
            if old['version']!=body.version or not old['deleted_at']:fail(409,'conflict')
            db.execute('UPDATE equipment SET deleted_at=NULL,deleted_by=NULL,delete_reason=NULL,version=version+1 WHERE id=?',(eid,))
            new=equipment(db,eid);audit(db,user,'restore','equipment',eid,old,new);return new

    @router.get('/assignees')
    def assignees(request:Request,user=Depends(require('owner','admin'))):
        with connect(request.app.state.data) as db:
            live(db,user,('owner','admin'))
            return [dict(r) for r in db.execute("SELECT id,username,display_name,role FROM users WHERE active=1 AND role IN ('owner','admin','inspector') ORDER BY username")]

    @router.put('/equipment/{eid}/assignments/{year}/{quarter}')
    def assign(eid:int,year:int,quarter:int,body:Assignment,request:Request,user=Depends(require('owner','admin'))):
        if not 2000<=year<=2100 or not 1<=quarter<=4:fail(422,'invalid')
        with connect(request.app.state.data) as db:
            live(db,user,('owner','admin'));equipment(db,eid)
            old=db.execute('SELECT * FROM assignments WHERE equipment_id=? AND year=? AND quarter=?',(eid,year,quarter)).fetchone()
            if (old['version'] if old else 0)!=body.version:fail(409,'conflict')
            ids=body.assignee_ids if body.assignee_ids is not None else ([body.assignee_id] if body.assignee_id is not None else [])
            for uid in ids:
                if not db.execute("SELECT 1 FROM users WHERE id=? AND active=1 AND role IN ('owner','admin','inspector')",(uid,)).fetchone():fail(422,'assignee')
            before=assignment_view(db,old)
            first=ids[0] if ids else None
            if old:db.execute('UPDATE assignments SET assignee_id=?,assignee_ids_json=?,version=version+1 WHERE id=?',(first,json.dumps(ids),old['id']))
            elif ids:db.execute('INSERT INTO assignments(equipment_id,year,quarter,assignee_id,assignee_ids_json) VALUES(?,?,?,?,?)',(eid,year,quarter,first,json.dumps(ids)))
            new=db.execute('SELECT * FROM assignments WHERE equipment_id=? AND year=? AND quarter=?',(eid,year,quarter)).fetchone()
            after=assignment_view(db,new)
            audit(db,user,'assign','equipment',eid,before,{'year':year,'quarter':quarter,'assignment':after})
            return after if after else {'version':0,'assignee_id':None,'assignee_ids':[],'assignees':[]}

    @router.get('/users')
    def users(request:Request,user=Depends(require('owner'))):
        with connect(request.app.state.data) as db:
            live(db,user,('owner',));return [dict(r) for r in db.execute('SELECT id,username,display_name,role,active,version,created_at FROM users ORDER BY id')]

    @router.post('/users',status_code=201)
    def create_user(body:UserIn,request:Request,user=Depends(require('owner'))):
        with connect(request.app.state.data) as db:
            live(db,user,('owner',))
            try:cur=db.execute('INSERT INTO users(username,display_name,password_hash,role) VALUES(?,?,?,?)',(body.username,body.display_name,password_hash(body.password),body.role))
            except sqlite3.IntegrityError:fail(409,'\uc544\uc774\ub514\uac00 \uc911\ubcf5\ub418\uc5c8\uc2b5\ub2c8\ub2e4.')
            public={'id':cur.lastrowid,'username':body.username,'display_name':body.display_name,'role':body.role,'active':1,'version':1}
            audit(db,user,'create','user',cur.lastrowid,after=public);return public

    @router.patch('/users/{uid}')
    def update_user(uid:int,body:UserUpdate,request:Request,user=Depends(require('owner'))):
        with connect(request.app.state.data) as db:
            live(db,user,('owner',));row=db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
            if not row:fail(404,'invalid')
            if row['version']!=body.version:fail(409,'conflict')
            if (row['role']=='owner' and (body.role!='owner' or not body.active)) or (row['role']!='owner' and body.role=='owner'):fail(422,'owner')
            changed=body.role!=row['role'] or bool(body.active)!=bool(row['active']) or body.password is not None
            db.execute('UPDATE users SET display_name=?,role=?,active=?,password_hash=?,auth_version=auth_version+?,version=version+1 WHERE id=?',(body.display_name,body.role,int(body.active),password_hash(body.password) if body.password else row['password_hash'],int(changed),uid))
            if changed:db.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
            public={'id':uid,'username':row['username'],'display_name':body.display_name,'role':body.role,'active':body.active,'version':body.version+1}
            old={k:row[k] for k in ('id','username','display_name','role','active','version')}
            audit(db,user,'update','user',uid,old,{**public,'password_reset':body.password is not None});return public

    @router.put('/equipment/{eid}/inspections/{year}/{quarter}')
    def save_inspection(eid:int,year:int,quarter:int,body:InspectionIn,request:Request,user=Depends(current)):
        if not 2000<=year<=2100 or not 1<=quarter<=4:fail(422,'invalid')
        with connect(request.app.state.data) as db:
            inspect_guard(db,user,eid,year,quarter)
            route=f'inspection/{eid}/{year}/{quarter}'
            digest,prior=idempotent(db,user,body.request_id,route,body.model_dump())
            if prior is not None:return prior
            e=equipment(db,eid)
            old=db.execute('SELECT * FROM inspections WHERE equipment_id=? AND year=? AND quarter=?',(eid,year,quarter)).fetchone()
            if old and old['submitted_at']:fail(409,'submitted')
            if (old['version'] if old else 0)!=body.version:fail(409,'conflict')
            tid=old['template_id'] if old else db.execute('SELECT id FROM checklist_templates WHERE type=? ORDER BY version DESC LIMIT 1',(e['type'],)).fetchone()[0]
            template={r['item_code']:dict(r) for r in db.execute('SELECT * FROM checklist_items WHERE template_id=?',(tid,))}
            items={i.item_code:i for i in body.items}
            if len(items)!=len(body.items) or not set(items)<=set(template):fail(422,'invalid')
            for code,item in items.items():
                if item.result=='deficient' and not item.comment:fail(422,'comment')
                if item.result=='na' and not item.not_applicable_reason:fail(422,'comment')
                applicable=template[code]['applicability'] in ('all',e['subtype'])
                if not applicable and item.result not in (None,'na'):fail(422,'invalid')
            if body.submit:
                if set(items)!=set(template) or any(i.result is None for i in items.values()):fail(422,'all_items')
                latest=db.execute('SELECT MAX(year*4+quarter) FROM inspections WHERE equipment_id=? AND submitted_at IS NOT NULL',(eid,)).fetchone()[0]
                if latest and latest>period(year,quarter):fail(409,'chronology')
                latest_event=db.execute('SELECT MAX(e.year*4+e.quarter) FROM finding_events e JOIN findings f ON f.id=e.finding_id WHERE f.equipment_id=?',(eid,)).fetchone()[0]
                if latest_event and latest_event>period(year,quarter):fail(409,'chronology')
            if old:
                iid=old['id'];db.execute('UPDATE inspections SET inspector_id=?,version=version+1 WHERE id=?',(user['user_id'],iid))
                db.execute('DELETE FROM inspection_results WHERE inspection_id=?',(iid,))
            else:
                iid=db.execute('INSERT INTO inspections(equipment_id,year,quarter,inspector_id,template_id) VALUES(?,?,?,?,?)',(eid,year,quarter,user['user_id'],tid)).lastrowid
            for item in body.items:
                db.execute('INSERT INTO inspection_results(inspection_id,item_code,result,comment,not_applicable_reason) VALUES(?,?,?,?,?)',(iid,item.item_code,item.result,item.comment,item.not_applicable_reason))
            if body.submit:
                for item in body.items:
                    if item.result!='deficient':continue
                    opened=db.execute("SELECT f.id FROM findings f JOIN inspections i ON i.id=f.source_inspection_id WHERE f.equipment_id=? AND f.item_code=? AND f.status='open'",(eid,item.item_code)).fetchone()
                    if opened:
                        fid=opened['id'];event='observed'
                        db.execute('UPDATE findings SET version=version+1 WHERE id=?',(fid,))
                        latest_event=db.execute('SELECT MAX(year*4+quarter) FROM finding_events WHERE finding_id=?',(fid,)).fetchone()[0]
                        if latest_event and latest_event>period(year,quarter):fail(409,'chronology')
                    else:
                        fid=db.execute('INSERT INTO findings(equipment_id,source_inspection_id,item_code,description) VALUES(?,?,?,?)',(eid,iid,item.item_code,item.comment)).lastrowid;event='create'
                    db.execute('INSERT INTO finding_events(finding_id,event_type,actor_id,occurred_at,details,year,quarter) VALUES(?,?,?,?,?,?,?)',(fid,event,user['user_id'],now(),item.comment,year,quarter))
                db.execute('UPDATE inspections SET submitted_at=? WHERE id=?',(now(),iid))
                submitted_state=status(db,eid,year,quarter)['status']
                db.execute('UPDATE inspections SET status_at_submission=? WHERE id=?',(submitted_state,iid))
            row=dict(db.execute('SELECT * FROM inspections WHERE id=?',(iid,)).fetchone())
            audit(db,user,'submit' if body.submit else 'draft','equipment',eid,dict(old) if old else None,{'inspection':row,'items':[i.model_dump() for i in body.items]})
            response={'inspection':row,**status(db,eid,year,quarter)}
            remember(db,user,body.request_id,route,digest,response)
            return response

    @router.post('/findings/{fid}/actions')
    def finding_action(fid:int,body:FindingAction,request:Request,user=Depends(current)):
        with connect(request.app.state.data) as db:
            row=db.execute('SELECT f.*,i.year AS source_year,i.quarter AS source_quarter FROM findings f JOIN inspections i ON i.id=f.source_inspection_id WHERE f.id=?',(fid,)).fetchone()
            if not row:fail(404,'invalid')
            inspect_guard(db,user,row['equipment_id'],body.year,body.quarter)
            if row['version']!=body.version:fail(409,'conflict')
            last=db.execute('SELECT MAX(year*4+quarter) FROM finding_events WHERE finding_id=?',(fid,)).fetchone()[0]
            if period(body.year,body.quarter)<max(last or 0,period(row['source_year'],row['source_quarter'])):fail(409,'chronology')
            desired='resolved' if body.action=='resolve' else 'open'
            if row['status']==desired:fail(409,'conflict')
            if desired=='resolved':
                db.execute('UPDATE findings SET status=?,resolved_at=?,resolved_by=?,resolution_comment=?,resolved_year=?,resolved_quarter=?,version=version+1 WHERE id=?',(desired,now(),user['user_id'],body.comment,body.year,body.quarter,fid))
            else:
                if db.execute("SELECT 1 FROM findings WHERE equipment_id=? AND item_code=? AND status='open' AND id!=?",(row['equipment_id'],row['item_code'],fid)).fetchone():fail(409,'conflict')
                db.execute('UPDATE findings SET status=?,resolved_at=NULL,resolved_by=NULL,resolution_comment=NULL,resolved_year=NULL,resolved_quarter=NULL,version=version+1 WHERE id=?',(desired,fid))
            db.execute('INSERT INTO finding_events(finding_id,event_type,actor_id,occurred_at,details,year,quarter) VALUES(?,?,?,?,?,?,?)',(fid,body.action,user['user_id'],now(),body.comment,body.year,body.quarter))
            new=dict(db.execute('SELECT * FROM findings WHERE id=?',(fid,)).fetchone())
            audit(db,user,body.action,'equipment',row['equipment_id'],dict(row),new)
            return {'finding':new,**status(db,row['equipment_id'],body.year,body.quarter)}

    @router.get('/equipment/{eid}/history')
    def history(eid:int,request:Request,user=Depends(current)):
        with connect(request.app.state.data) as db:
            live(db,user);equipment(db,eid,True)
            inspections=[]
            for row in db.execute('SELECT i.*,u.username AS inspector FROM inspections i LEFT JOIN users u ON u.id=i.inspector_id WHERE equipment_id=? ORDER BY year DESC,quarter DESC',(eid,)):
                item=dict(row);item['results']=[dict(r) for r in db.execute('SELECT r.*,c.label FROM inspection_results r JOIN checklist_items c ON c.template_id=? AND c.item_code=r.item_code WHERE inspection_id=?',(row['template_id'],row['id']))];inspections.append(item)
            logs=[dict(r) for r in db.execute("SELECT a.*,u.username AS actor FROM audit_logs a LEFT JOIN users u ON u.id=a.actor_id WHERE entity_type='equipment' AND entity_id=? ORDER BY a.id DESC",(eid,))]
            return {'inspections':inspections,'audit':logs}

    @router.get('/backups')
    def backups(request:Request,user=Depends(require('owner'))):
        with connect(request.app.state.data) as db:live(db,user,('owner',))
        return [{'name':p.name,'size':p.stat().st_size,'modified':p.stat().st_mtime} for p in sorted((request.app.state.data/'backups').glob('*.zip'),reverse=True)]

    @router.post('/backups')
    def backup(request:Request,user=Depends(require('owner'))):
        with DB_LOCK:
            with connect(request.app.state.data) as db:live(db,user,('owner',))
            name=snapshot(request.app.state.data)
            with connect(request.app.state.data) as db:audit(db,user,'backup','backup',None,after={'name':name})
            return {'name':name}

    @router.get('/backups/{name}/download')
    def download(name:str,request:Request,user=Depends(require('owner'))):
        with connect(request.app.state.data) as db:live(db,user,('owner',))
        from pathlib import Path
        if Path(name).name!=name or not name.endswith('.zip'):fail(422,'invalid')
        path=request.app.state.data/'backups'/name
        if not path.is_file():fail(404,'invalid')
        return FileResponse(path,filename=name,media_type='application/zip')

    @router.post('/backups/{name}/restore')
    def restore_backup(name:str,body:Restore,request:Request,user=Depends(require('owner'))):
        with DB_LOCK:
            with connect(request.app.state.data) as db:live(db,user,('owner',))
            request.app.state.maintenance=True
            try:
                safety=restore(request.app.state.data,name,user)
                return {'ok':True,'pre_restore':safety,'login_required':True}
            except (ValueError,sqlite3.DatabaseError,KeyError,json.JSONDecodeError,zipfile.BadZipFile) as error:
                fail(422,'\ubcf5\uad6c\ubcf8 \uac80\uc99d\uc5d0 \uc2e4\ud328\ud588\uc2b5\ub2c8\ub2e4. \uae30\uc874 \ub370\uc774\ud130\ub294 \ubcf4\uc874\ub429\ub2c8\ub2e4.')
            finally:request.app.state.maintenance=False

    return router
