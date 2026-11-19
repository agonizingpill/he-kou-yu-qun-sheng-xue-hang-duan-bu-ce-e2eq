import random
from datetime import datetime,timezone
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .models import BlindSheet,BlindSheetItem,SchoolAnalysis,SchoolReview,Transect,Cruise,User,Audit,Echo
from .common import get,dump,cruise_lock

ISSUED='issued'
SUBMITTED='submitted'

def _accuracy(correct,total):
    return (correct/total) if total else None

def summary(row,redact=False):
    value=dump(row);value.pop('request_key',None)
    value['fish_accuracy']=_accuracy(row.fish_correct,row.fish_total)
    value['noise_accuracy']=_accuracy(row.noise_correct,row.noise_total)
    if redact:
        # 交卷前连两类样本数与得分都不下发，避免从汇总反推卷内构成。
        for key in ('fish_total','fish_correct','fish_accuracy','noise_total','noise_correct','noise_accuracy'):value[key]=None
    return value

def _items(db,sheet_id,redact):
    rows=list(db.scalars(select(BlindSheetItem).where(BlindSheetItem.sheet_id==sheet_id).order_by(BlindSheetItem.order_index)))
    echoes={e.id:e for e in db.scalars(select(Echo).where(Echo.id.in_([r.echo_id for r in rows])))}
    items=[]
    for row in rows:
        e=echoes.get(row.echo_id)
        item={'id':row.id,'sheet_id':row.sheet_id,'order_index':row.order_index,'along_m':e.along_m if e else None,'depth_m':e.depth_m if e else None,'strength_db':e.strength_db if e else None,'answer':row.answer}
        if redact:
            # 交卷前值班员只看到题目与自己的作答，预测与得分一律不下发。
            item['correct']=None
        else:
            item.update(echo_id=row.echo_id,predicted_label=row.predicted_label,correct=row.correct)
        items.append(item)
    return items

def detail(db,id,user):
    row=get(db,BlindSheet,id)
    if user.role!='supervisor' and row.assignee_id!=user.id:raise HTTPException(403,'仅受卷值班员或调查主管可查看该盲判卷')
    redact=user.role!='supervisor' and row.status==ISSUED
    return {**summary(row,redact),'items':_items(db,row.id,redact)}

def _find_by_key(db,key):return db.scalar(select(BlindSheet).where(BlindSheet.request_key==key))

def issue(db,data,user):
    existing=_find_by_key(db,data.request_key)
    if existing:
        if existing.analysis_id!=data.analysis_id or existing.assignee_id!=data.assignee_id:raise HTTPException(409,'发卷请求号已被其他盲判卷占用')
        return detail(db,existing.id,user)
    # 先取识别结果所属航段，再按Cruise根锁→子记录的既有锁序加锁，避免与其他现场写入互锁。
    peek=get(db,SchoolAnalysis,data.analysis_id);t=get(db,Transect,peek.transect_id);cruise_lock(db,t.cruise_id)
    analysis=get(db,SchoolAnalysis,data.analysis_id,True)
    if analysis.status not in ('done','reviewed'):raise HTTPException(409,'仅已完成的识别结果可发盲判卷')
    if analysis.data_version!=t.data_version:raise HTTPException(409,'识别结果不是最新回波版本，请重新识别后再发卷')
    if not analysis.assignments:raise HTTPException(409,'识别结果尚无逐点标签，稍后重试')
    assignee=get(db,User,data.assignee_id)
    if assignee.role!='operator':raise HTTPException(409,'盲判卷只能发给值班调查员')
    live=db.scalar(select(BlindSheet).where(BlindSheet.analysis_id==analysis.id,BlindSheet.assignee_id==assignee.id))
    if live:
        if live.status==ISSUED:return detail(db,live.id,user)
        raise HTTPException(409,'该识别结果已向此值班员发过卷并按发卷时预测落账，不重复发卷')
    assignments=analysis.assignments or []
    fish=[p for p in assignments if int(p.get('label',-1))>=0]
    noise=[p for p in assignments if int(p.get('label',-1))<0]
    if not fish or not noise:raise HTTPException(409,'当前识别结果需同时含鱼群与噪点样本才能发盲判卷')
    rng=random.Random(f'{analysis.id}:{assignee.id}:{data.request_key}')
    picked=rng.sample(fish,min(data.per_class,len(fish)))+rng.sample(noise,min(data.per_class,len(noise)))
    rng.shuffle(picked)
    fish_total=sum(1 for p in picked if int(p['label'])>=0)
    row=BlindSheet(cruise_id=t.cruise_id,analysis_id=analysis.id,data_version=analysis.data_version,assignee_id=assignee.id,issued_by=user.id,request_key=data.request_key,status=ISSUED,per_class=data.per_class,fish_total=fish_total,fish_correct=0,noise_total=len(picked)-fish_total,noise_correct=0)
    db.add(row)
    try:
        db.flush()
        for index,p in enumerate(picked):
            db.add(BlindSheetItem(sheet_id=row.id,echo_id=int(p['echo_id']),order_index=index,predicted_label=int(p['label'])))
        db.flush()
        db.add(Audit(actor=user.username,action='发值班盲判卷',detail=str(row.id)))
        db.commit()
    except IntegrityError:
        # 并发重试：只保留先落账的卷，按请求号或识别+受卷人取回同一份。
        db.rollback();existing=_find_by_key(db,data.request_key) or db.scalar(select(BlindSheet).where(BlindSheet.analysis_id==analysis.id,BlindSheet.assignee_id==assignee.id))
        if existing:return detail(db,existing.id,user)
        raise
    return detail(db,row.id,user)

def submit(db,id,data,user):
    # 与发卷保持同一锁序：先Cruise根锁，再锁盲判卷本身。
    peek=get(db,BlindSheet,id);analysis=get(db,SchoolAnalysis,peek.analysis_id);t=get(db,Transect,analysis.transect_id);get(db,Cruise,t.cruise_id,True)
    row=get(db,BlindSheet,id,True)
    if row.assignee_id!=user.id:raise HTTPException(403,'仅受卷值班员本人可交卷')
    if row.status!=ISSUED:raise HTTPException(409,'该盲判卷已由先落账的答卷提交，重复答卷不再收录')
    items=list(db.scalars(select(BlindSheetItem).where(BlindSheetItem.sheet_id==row.id)))
    answers={a.item_id:a.answer for a in data.answers}
    if set(answers)!={item.id for item in items}:raise HTTPException(409,'需逐题作答全部盲判样本')
    fish_total=fish_correct=noise_total=noise_correct=0
    for item in items:
        predicted_fish=item.predicted_label>=0
        actual_fish=answers[item.id]=='fish'
        item.answer=answers[item.id];item.correct=predicted_fish==actual_fish
        if predicted_fish:
            fish_total+=1;fish_correct+=int(item.correct)
        else:
            noise_total+=1;noise_correct+=int(item.correct)
    row.fish_total,row.fish_correct=fish_total,fish_correct
    row.noise_total,row.noise_correct=noise_total,noise_correct
    row.status=SUBMITTED;row.submitted_at=datetime.now(timezone.utc)
    db.add(Audit(actor=user.username,action='提交值班盲判卷',detail=str(row.id)))
    db.commit()
    return detail(db,row.id,user)

def blinded_transects(db,user):
    if user.role=='supervisor':return set()
    return set(db.scalars(select(SchoolAnalysis.transect_id).join(BlindSheet,BlindSheet.analysis_id==SchoolAnalysis.id).where(BlindSheet.assignee_id==user.id,BlindSheet.status==ISSUED)))

def workspace_scope(db,user,data):
    """任何读入口统一脱敏：未交卷值班员看不到卷内样本所属航段的识别标签。"""
    redacted=blinded_transects(db,user)
    if redacted:
        for analysis in data.get('school_analyses',[]):
            if analysis['transect_id'] in redacted:
                # 卷内航段交卷前连聚合指标和逐点标签都不下发。
                analysis['assignments']=[];analysis['metrics']={}
        if any(t['id'] in redacted for r in data.get('survey_releases',[]) for t in [layer.get('transect',{}) for layer in r.get('snapshot',{}).get('layers',[])]):
            data['survey_releases']=[]
    if user.role=='supervisor':
        data['blind_sheets']=[summary(r) for r in db.scalars(select(BlindSheet).order_by(BlindSheet.id))]
        data['blind_sheet_items']=[item for r in data['blind_sheets'] for item in _items(db,r['id'],False)]
    else:
        sheets=[r for r in db.scalars(select(BlindSheet).where(BlindSheet.assignee_id==user.id).order_by(BlindSheet.id))]
        data['blind_sheets']=[summary(r,r.status==ISSUED) for r in sheets]
        data['blind_sheet_items']=[item for r in sheets for item in _items(db,r.id,r.status==ISSUED)]
    data['users']=[{'id':u.id,'username':u.username,'role':u.role} for u in db.scalars(select(User).order_by(User.id))]
    data['me']={'id':user.id,'username':user.username,'role':user.role}
    return data

def release_visible(db,user,release):
    if user.role=='supervisor':return True
    redacted=blinded_transects(db,user)
    if not redacted:return True
    return not any(layer['transect']['id'] in redacted for layer in release.snapshot.get('layers',[]))
