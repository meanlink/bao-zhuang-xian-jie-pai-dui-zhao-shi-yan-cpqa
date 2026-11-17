import hashlib, json
def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
def dataset_dict(d):
    return {'id':d.id, 'name':d.name, 'source':d.source, 'rows':len(d.records), 'checksum':d.checksum, 'created_at':d.created_at}
def job_dict(j):
    return {k:getattr(j,k) for k in ['id','name','dataset_id','params','status','result','error','approved','created_at','finished_at']}
def report_dict(r):
    return {k:getattr(r,k) for k in ['id','name','job_id','destination','interval_seconds','next_run','enabled']}
def delivery_dict(d):
    return {k:getattr(d,k) for k in ['id','report_id','created_at','checksum','filename','destination']}

def energy_period_dict(p):
    return {k:getattr(p,k) for k in ['id','period_key','name','start_ts','end_ts','max_gap_seconds',
            'status','meter_watermark_ts','segment_watermark_ts','created_at','closed_at','snapshot']}
def energy_book_dict(b):
    return {k:getattr(b,k) for k in ['id','period_id','status','totals','identity_ok',
            'meter_watermark_ts','segment_watermark_ts','checksum','briefing_filename','closed_at','closed_by']}
def energy_revision_dict(r):
    return {k:getattr(r,k) for k in ['id','period_id','book_id','reason','status','cursor','totals',
            'diff','checksum','briefing_filename','created_at','completed_at','error']}
def energy_anomaly_dict(a):
    return {k:getattr(a,k) for k in ['id','period_id','book_id','revision_id','kind','scope','message','ref','resolved','created_at']}
def energy_entry_dict(e):
    return {k:getattr(e,k) for k in ['id','period_id','book_id','revision_id','segment_ref','meter_serial',
            'start_ts','end_ts','overlap_seconds','amount','amount_exact','is_unassigned','unassigned_reason']}
