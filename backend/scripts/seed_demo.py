import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import (User, Dataset, Job, Report, Delivery, Audit,
                        EnergyPeriod, EnergyMeterReading, EnergySegment)
from src.services.business import metadata, seed_records, analyze
from src.services.reports import deliver
from src.services import energy as energy_service
from src.utils.security import hash_password
from src.utils.serialization import checksum
def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        if not db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name'])):
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        db.commit()
        seed_energy_demo(db)
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,Audit,EnergyPeriod,EnergyMeterReading,EnergySegment]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts

def seed_energy_demo(db):
    """幂等演示电量账期：两路乱序读数 + 双水位 + 封账，固定口径可复查。"""
    from src.schemas.energy import PeriodInput, IngestInput, WatermarkInput
    key='P-2026W1'
    if db.scalar(select(EnergyPeriod).where(EnergyPeriod.period_key==key)):
        return
    admin=db.scalar(select(User).where(User.role=='admin'))
    p=energy_service.create_period(db, PeriodInput(
        period_key=key, name='第1周电量对账（演示）', start_ts=0, end_ts=100, max_gap_seconds=60))
    # 表计累计读数（故意乱序提交，服务端按时间排序）
    meter=[
        {'meter_serial':'M1','reading_ts':40,'reading_value':135,'idempotency_key':'m4'},
        {'meter_serial':'M1','reading_ts':0,'reading_value':100,'idempotency_key':'m1'},
        {'meter_serial':'M1','reading_ts':20,'reading_value':130,'idempotency_key':'m3'},
        {'meter_serial':'M1','reading_ts':10,'reading_value':110,'idempotency_key':'m2'},
    ]
    segs=[
        {'segment_ref':'S-A','label':'A 原动作','start_ts':0,'end_ts':20,'idempotency_key':'g1'},
        {'segment_ref':'S-B','label':'B 改动作','start_ts':20,'end_ts':40,'idempotency_key':'g2'},
    ]
    energy_service.ingest(db, p.id, IngestInput(stream='meter', items=meter), admin)
    energy_service.ingest(db, p.id, IngestInput(stream='segment', items=segs), admin)
    energy_service.confirm_watermark(db, p.id, WatermarkInput(stream='meter', watermark_ts=100), admin)
    energy_service.confirm_watermark(db, p.id, WatermarkInput(stream='segment', watermark_ts=100), admin)
    db.commit()
    energy_service.close_period(db, p.id, admin)
    db.commit()
    db.add(Audit(actor='seed',action='demo.energy',detail={'period':p.id},at=time.time()))
    db.commit()

if __name__=='__main__':seed()
