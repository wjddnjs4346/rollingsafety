"""Streamlit UI backed by a loopback-only Python API and persistent SQLite."""
import os,uuid,base64
from datetime import datetime, timezone, timedelta
import httpx
import streamlit as st
import streamlit.components.v1 as components
from pathlib import Path
ROOT=Path(__file__).resolve().parent
TYPES={'extinguisher':'소화기','hydrant':'소화전','manifold':'매니폴더'}
STATES={'uninspected':'미점검','incomplete':'미완료','complete':'완료'}
ROLES={'owner':'최고관리자','admin':'지정관리자','inspector':'점검담당자','viewer':'조회자'}
st.set_page_config(page_title='중형압연 안전설비',layout='wide')
map_widget=components.declare_component('safety_map',path=str(ROOT/'map_component'))
class APIError(Exception):pass

@st.cache_resource
def runtime(config):
 from cloud_runtime import CloudRuntime
 return CloudRuntime(dict(config))

def api(path,method='GET',body=None,raw=False):
 try:r=runtime(tuple(sorted(CLOUD_CONFIG.items()))).request(path,method,body,st.session_state.get('cookie',''),st.session_state.get('csrf',''))
 except Exception as e:
  import logging
  logging.getLogger('rolling_cloud').error('Cloud operation failed: %s',type(e).__name__)
  raise APIError('저장소 연결 또는 저장에 실패했습니다. 입력을 유지하고 설정/로그를 확인한 뒤 재시도하세요.') from e
 if not r.is_success:
  if r.status_code==401:st.session_state.pop('user',None)
  try:msg=r.json().get('detail','Request failed')
  except ValueError:msg='Request failed'
  raise APIError(f'{r.status_code}: {msg}')
 if path=='/api/login' and r.cookies.get('session'):
  st.session_state.cookie='session='+r.cookies.get('session')
 return r.content if raw else r.json()

def save(path,method,body):
 try:
  api(path,method,body);st.session_state.flash='저장했습니다.';st.rerun()
 except APIError as e:st.error(str(e))

def logout():
 try:api('/api/logout','POST')
 except APIError:pass
 st.session_state.clear();st.rerun()

def editor(rows,image):
 mode=st.radio('설비 관리',['등록','수정'],horizontal=True)
 e=None
 if mode=='수정':
  if not rows:st.info('수정할 설비가 없습니다.');return
  eid=st.selectbox('수정 설비',[e['id'] for e in rows],format_func=lambda i:next(e['code'] for e in rows if e['id']==i))
  e=api(f'/api/equipment/{eid}?year={year}&quarter={quarter}')['equipment']
 token=f'{kind}-{e["id"] if e else "new"}'
 event=map_widget(image=image,markers=rows,edit=True,key='editor-map-'+token,default=None)
 if event and event.get('event')=='position' and st.session_state.get('posnonce')!=event['nonce']:
  st.session_state['x-'+token]=float(event['x']);st.session_state['y-'+token]=float(event['y']);st.session_state.posnonce=event['nonce']
 st.caption('지도 터치로 좌표를 지정하거나 X/Y를 입력하세요. 현장 확인 없이 위치를 추정하지 마세요.')
 with st.form('editor-'+token):
  code=st.text_input('설비번호',value=e['code'] if e else '',max_chars=80)
  name=st.text_input('명칭',value=e['name'] if e else '',max_chars=120)
  location=st.text_input('위치 설명',value=e['location_text'] if e else '',max_chars=300)
  subtype=st.selectbox('소화전 구분',['indoor','outdoor'],index=1 if e and e.get('subtype')=='outdoor' else 0,format_func=lambda v:'옥내' if v=='indoor' else '옥외') if kind=='hydrant' else None
  x=st.number_input('X (0~1)',0.,1.,value=float(e['x']) if e else 0.,step=.001,key='x-'+token)
  y=st.number_input('Y (0~1)',0.,1.,value=float(e['y']) if e else 0.,step=.001,key='y-'+token)
  confirm=st.checkbox('현장 위치를 확인했습니다.')
  if st.form_submit_button('설비 저장'):
   if not confirm:st.error('위치 확인이 필요합니다.')
   else:
    body=dict(code=code,name=name,type=kind,subtype=subtype,location_text=location,x=x,y=y,layout_id=1)
    if e:body['version']=e['version']
    save(f'/api/equipment/{e["id"]}' if e else '/api/equipment','PATCH' if e else 'POST',body)

def location_assignees(eid):
 d=api(f'/api/equipment/{eid}?year={year}&quarter={quarter}');e=d['equipment']
 st.subheader('위치별 담당자: '+e['code']+' / '+e['name'])
 st.write('위치: '+e['location_text'])
 st.caption(f'{year}년 · 아래에서 분기별 담당자를 여러 명 지정할 수 있습니다. 점검 입력은 지도 / 점검 탭에서 진행하세요.')
 if not manager:st.info('담당자 조회만 가능합니다. 배정 변경은 최고관리자 또는 지정관리자가 할 수 있습니다.')
 users=api('/api/assignees') if manager else []
 for q in d['quarters']:
  a=q.get('assignment') or {};n=q['quarter'];st.write(f'{n}분기: '+(', '.join(u.get('display_name') or u['username'] for u in a.get('assignees',[])) or '미지정')+' / '+STATES[q['status']])
  if manager:
   current=a.get('assignees',[])
   options={u['id']:u for u in current}
   options.update({u['id']:u for u in users})
   choices=list(options)
   with st.form(f'assign-{eid}-{year}-{n}'):
    uids=st.multiselect('담당자 (여러 명 선택)',choices,default=[i for i in a.get('assignee_ids',[]) if i in choices],format_func=lambda i:(options[i]['display_name'] or options[i]['username'])+(' (비활성: 선택 해제 필요)' if not options[i].get('active',True) else ''))
    st.caption('지정된 담당자 모두 점검할 수 있습니다. 점검표는 설비·분기당 하나를 공동 사용하며, 실제 저장/제출자는 별도로 기록됩니다.')
    if st.form_submit_button('배정 저장'):save(f'/api/equipment/{eid}/assignments/{year}/{n}','PUT',dict(assignee_ids=uids,version=a.get('version',0)))

def detail(eid):
 d=api(f'/api/equipment/{eid}?year={year}&quarter={quarter}');e=d['equipment'];inspection=d.get('inspection') or {}
 st.subheader(e['code']+' / '+e['name']);st.write(e['location_text']);st.info(STATES[d['status']])

 writable=d['can_inspect'] and not inspection.get('submitted_at');results={v['item_code']:v for v in d['results']}
 prefix=f'check-{eid}-{year}-{quarter}-{inspection.get("version",0)}'
 st.subheader('분기 점검')
 with st.form(prefix,clear_on_submit=False):
  items=[]
  for item in d['items']:
   code=item['item_code'];old=results.get(code,{});st.markdown('**'+item['label']+'**')
   if not item['applicable']:st.caption('이 소화전 유형에서 해당없음. 사유를 기록하세요.')
   choices=[None,'good','deficient','na']
   result=st.selectbox('판정',choices,index=choices.index(old.get('result')),format_func=lambda v:{None:'미입력','good':'양호','deficient':'미비','na':'해당없음'}[v],key=prefix+code+'r',disabled=not writable)
   comment=st.text_area('미비 코멘트',value=old.get('comment',''),key=prefix+code+'c',disabled=not writable,max_chars=4000)
   reason=st.text_input('해당없음 사유',value=old.get('not_applicable_reason',''),key=prefix+code+'n',disabled=not writable,max_chars=2000)
   items.append(dict(item_code=code,result=result,comment=comment,not_applicable_reason=reason))
  draft=st.form_submit_button('임시저장',disabled=not writable);submit=st.form_submit_button('점검 제출',disabled=not writable,type='primary')
  if draft or submit:
   fingerprint=repr((prefix,items,submit));pending=st.session_state.get('pending',{})
   if pending.get('fingerprint')!=fingerprint:pending={'fingerprint':fingerprint,'id':str(uuid.uuid4())};st.session_state.pending=pending
   save(f'/api/equipment/{eid}/inspections/{year}/{quarter}','PUT',dict(version=inspection.get('version',0),submit=submit,request_id=pending['id'],items=items))
 st.subheader('미비사항 / 이월 / 조치')
 for f in d['findings']:
  with st.expander(f['description']+' / '+('조치 완료' if f['status']=='resolved' else '미해결')):
   st.write(f.get('resolution_comment') or '');st.json(f.get('events',[]),expanded=False)
   if d['can_inspect']:
    with st.form(f'finding-{f["id"]}-{f["version"]}-{year}-{quarter}'):
     comment=st.text_area('조치/재개방 사유',max_chars=4000);action='reopen' if f['status']=='resolved' else 'resolve'
     if st.form_submit_button('재개방' if action=='reopen' else '조치 완료'):save(f'/api/findings/{f["id"]}/actions','POST',dict(version=f['version'],action=action,year=year,quarter=quarter,comment=comment))
 with st.expander('점검 / 변경 이력'):st.json(api(f'/api/equipment/{eid}/history'))
 if owner:
  with st.expander('설비 삭제'):
   with st.form(f'delete-{eid}'):
    reason=st.text_input('삭제 사유');confirm=st.checkbox('삭제 확인. 이력은 보존됩니다.')
    if st.form_submit_button('삭제') and confirm:save(f'/api/equipment/{eid}','DELETE',dict(version=e['version'],reason=reason))

def administration():
 users=api('/api/users');st.subheader('계정 / 백업 관리')
 with st.expander('계정 추가'):
  with st.form('new-user'):
   username=st.text_input('아이디');display=st.text_input('표시명');password=st.text_input('비밀번호 (12자 이상)',type='password');role=st.selectbox('권한',['admin','inspector','viewer'],format_func=lambda v:ROLES[v])
   if st.form_submit_button('계정 생성'):save('/api/users','POST',dict(username=username,display_name=display,password=password,role=role))
 for u in users:
  with st.expander(u['username']+' / '+ROLES[u['role']]):
   with st.form(f'user-{u["id"]}-{u["version"]}'):
    display=st.text_input('표시명',value=u['display_name']);role=st.selectbox('권한',list(ROLES),index=list(ROLES).index(u['role']),format_func=lambda v:ROLES[v]);active=st.checkbox('활성',value=bool(u['active']));password=st.text_input('새 비밀번호 (비우면 유지)',type='password')
    if st.form_submit_button('계정 저장'):save(f'/api/users/{u["id"]}','PATCH',dict(version=u['version'],display_name=display,role=role,active=active,password=password or None))
 if st.button('수동 백업 생성'):save('/api/backups','POST',None)
 for b in api('/api/backups'):
  with st.expander(b['name']):
   if st.button('다운로드 준비',key='prep-'+b['name']):st.download_button('백업 다운로드',api('/api/backups/'+b['name']+'/download',raw=True),file_name=b['name'],mime='application/zip',key='dl-'+b['name'])
   with st.form('restore-'+b['name']):
    st.warning('계정과 데이터가 백업 시점으로 돌아가고 모든 세션이 종료됩니다.')
    confirm=st.text_input('RESTORE 입력')
    if st.form_submit_button('복구') and confirm=='RESTORE':
     try:api('/api/backups/'+b['name']+'/restore','POST',{'confirmation':'RESTORE'});logout()
     except APIError as e:st.error(str(e))
 with st.expander('삭제 설비 복원'):
  for e in api(f'/api/equipment?year={year}&quarter={quarter}&deleted=true'):
   st.write(e['code']+' / '+e['delete_reason'])
   if st.button('복원',key='restore-eq-'+str(e['id'])):save(f'/api/equipment/{e["id"]}/restore','POST',dict(version=e['version']))

try:
 CLOUD_CONFIG=dict(st.secrets.get('cloud',{}))
except Exception:
 CLOUD_CONFIG={}
if not CLOUD_CONFIG:
 st.title('Community Cloud 설정')
 st.info('코드는 바로 실행됐습니다. Create app > Advanced settings > Secrets에 관리자와 저장소 설정을 넣으면 로그인 화면이 열립니다.')
 st.code('[cloud]\nstorage_mode = "postgres"\ndatabase_url = "postgresql://USER:PASSWORD@HOST:5432/DB?sslmode=require"\nadmin_username = "your_admin"\nadmin_password = "CHANGE_TO_A_UNIQUE_12_PLUS_PASSWORD"',language='toml')
 st.warning('실제 운영 기록은 외부 PostgreSQL 연결이 필요합니다. 화면 확인만 하려면 storage_mode="demo"로 설정할 수 있으나 기록은 재시작 시 사라질 수 있습니다.')
 st.stop()
if CLOUD_CONFIG.get('storage_mode','postgres')=='demo':
 st.warning('체험 모드: 데이터는 임시 저장입니다. 실제 점검 기록을 입력하지 마세요.')
try:
 runtime(tuple(sorted(CLOUD_CONFIG.items())))
except Exception as e:
 st.error('초기화 실패: Secrets의 관리자 설정과 PostgreSQL 연결/테이블 권한을 확인하세요. 상세 비밀정보는 표시하지 않습니다.')
 st.stop()
st.title('중형압연공장 안전설비 관리');st.caption('Streamlit / 종류별 시트 지도 / 분기 점검')
if 'user' not in st.session_state:
 with st.form('login',clear_on_submit=True):
  username=st.text_input('아이디');password=st.text_input('비밀번호',type='password')
  if st.form_submit_button('로그인',type='primary'):
   try:r=api('/api/login','POST',dict(username=username,password=password));st.session_state.csrf=r['csrf'];st.session_state.user=api('/api/me');st.rerun()
   except APIError as e:st.error(str(e))
 st.info('Secrets에 지정한 관리자 계정으로 로그인하세요. 공개 기본 계정은 없습니다.');st.stop()
try:
 user=api('/api/me');owner=user['role']=='owner';manager=user['role'] in ('owner','admin')
 with st.sidebar:
  st.write(user['username']+' / '+ROLES[user['role']])
  if st.button('로그아웃'):logout()
  year=int(st.number_input('연도',2000,2100,datetime.now(timezone(timedelta(hours=9))).year));quarter=st.selectbox('분기',[1,2,3,4],index=(datetime.now(timezone(timedelta(hours=9))).month-1)//3)
  if st.button('새로고침'):st.rerun()
 if st.session_state.get('flash'):st.success(st.session_state.pop('flash'))
 kind=st.radio('설비 시트',list(TYPES),format_func=lambda v:TYPES[v],horizontal=True,key='sheet')
 pages=['지도 / 점검','위치별 담당자']+(['설비 등록 / 수정'] if manager else [])+(['관리'] if owner else [])
 page=st.radio('화면',pages,horizontal=True,key='page_navigation')
 st.caption('폼 입력 후 저장하고 시트·설비·분기를 전환하세요. 저장 전 화면 전환 시 입력이 사라질 수 있습니다.')
 rows=[e for e in api(f'/api/equipment?year={year}&quarter={quarter}') if e['type']==kind]
 if page=='관리':administration()
 else:
  image=base64.b64encode(api('/api/layout',raw=True)).decode('ascii')
  if page=='설비 등록 / 수정':editor(rows,image)
  else:
   query=st.text_input('설비 검색');mine=st.checkbox('내 담당만');state=st.selectbox('상태 필터',['all']+list(STATES),format_func=lambda v:'전체' if v=='all' else STATES[v])
   rows=[e for e in rows if (not query or query.lower() in (e['code']+' '+e['name']+' '+e['location_text']).lower()) and (not mine or user['id'] in (e.get('assignment') or {}).get('assignee_ids',[])) and (state=='all' or e['status']==state)]
   for col,v in zip(st.columns(3),STATES):col.metric(STATES[v],sum(e['status']==v for e in rows))
   event=map_widget(image=image,markers=rows,edit=False,key=f'map-{page}-{kind}-{year}-{quarter}',default=None)
   ids=[e['id'] for e in rows];key=f'selected-{kind}-{year}-{quarter}'
   if st.session_state.get(key) not in ids:st.session_state.pop(key,None)
   if event and event.get('event')=='select' and event.get('id') in ids and st.session_state.get('mapnonce')!=event['nonce']:st.session_state[key]=event['id'];st.session_state.mapnonce=event['nonce']
   if rows:
    remembered=f'location-memory-{kind}-{year}-{quarter}'
    if key not in st.session_state and st.session_state.get(remembered) in ids:st.session_state[key]=st.session_state[remembered]
    eid=st.selectbox('설비 선택',ids,key=key,format_func=lambda i:next(e['code']+' / '+e['name'] for e in rows if e['id']==i))
    st.session_state[remembered]=eid
    if page=='위치별 담당자':location_assignees(eid)
    else:
     st.caption('담당자 배정은 상단의 위치별 담당자 탭에서 설정하세요.')
     detail(eid)
   else:st.info('선택한 시트에 설비가 없습니다.')
 st.caption('완료는 점검/조치 기록 상태이며 안전성 인증이 아닙니다. 현장 안전절차를 준수하세요.')
except APIError as e:
 st.error(str(e))
 if 'user' not in st.session_state and st.button('로그인 화면으로'):st.rerun()
