from enum import Enum
class Role(str, Enum):
    admin = 'admin'
    analyst = 'analyst'
    viewer = 'viewer'
class JobStatus(str, Enum):
    queued = 'queued'
    running = 'running'
    completed = 'completed'
    failed = 'failed'

# ---- 电量对账封账 ----
class EnergyStream(str, Enum):
    meter = 'meter'        # 电表累计读数
    segment = 'segment'    # 实验片段起止时间
class EnergyCloseStatus(str, Enum):
    open_period = 'open'            # 账期开放，可继续补传
    closed = 'closed'               # 已封账，原账与简报冻结
    blocked = 'blocked'             # 存在隔离异常，拒绝结案
    revised = 'revised'             # 已有修订账（原账仍保留）
class EnergyRevisionStatus(str, Enum):
    in_progress = 'in_progress'     # 修订进行中，可从断点恢复
    completed = 'completed'         # 修订完成
    failed = 'failed'
class EnergyAnomaly(str, Enum):
    meter_reset = 'meter_reset'                 # 换表回零/读数下降
    duplicate_timestamp = 'duplicate_timestamp'  # 同表同时间戳
    reading_gap = 'reading_gap'                 # 读数缺口
    segment_overlap = 'segment_overlap'         # 片段交叠
    bad_segment = 'bad_segment'                 # 片段时长非法
