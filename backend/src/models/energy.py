"""电量对账封账领域模型。

两条数据流（电表累计读数 / 片段起止）各自带确认水位；账期封账后原账冻结，
迟到更正进入修订账。所有金额以 Double 落库、amount_exact 保留 Decimal 文本，
分摊恒等式由 energy_engine 保证。
"""
from sqlalchemy import String, Double, ForeignKey, Boolean, JSON, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class EnergyPeriod(Base):
    __tablename__ = 'energy_periods'
    id: Mapped[int] = mapped_column(primary_key=True)
    period_key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    start_ts: Mapped[float] = mapped_column(Double)
    end_ts: Mapped[float] = mapped_column(Double)
    max_gap_seconds: Mapped[float | None] = mapped_column(Double, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default='open')  # open/blocked/closed/revised
    meter_watermark_ts: Mapped[float | None] = mapped_column(Double, nullable=True)
    segment_watermark_ts: Mapped[float | None] = mapped_column(Double, nullable=True)
    created_at: Mapped[float] = mapped_column(Double)
    closed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    closed_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)


class EnergyMeterReading(Base):
    __tablename__ = 'energy_meter_readings'
    id: Mapped[int] = mapped_column(primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey('energy_periods.id'))
    meter_serial: Mapped[str] = mapped_column(String(64))
    reading_ts: Mapped[float] = mapped_column(Double)
    reading_value: Mapped[float] = mapped_column(Double)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    source_ref: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[float] = mapped_column(Double)


class EnergySegment(Base):
    __tablename__ = 'energy_segments'
    id: Mapped[int] = mapped_column(primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey('energy_periods.id'))
    segment_ref: Mapped[str] = mapped_column(String(80))
    label: Mapped[str] = mapped_column(String(160))
    start_ts: Mapped[float] = mapped_column(Double)
    end_ts: Mapped[float] = mapped_column(Double)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    source_ref: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[float] = mapped_column(Double)


class EnergyCloseBook(Base):
    """封账原账：一个账期至多一本，封账后不可变。"""
    __tablename__ = 'energy_close_books'
    id: Mapped[int] = mapped_column(primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey('energy_periods.id'), unique=True)
    status: Mapped[str] = mapped_column(String(20), default='closed')
    totals: Mapped[dict] = mapped_column(JSON, default=dict)
    identity_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    meter_watermark_ts: Mapped[float] = mapped_column(Double)
    segment_watermark_ts: Mapped[float] = mapped_column(Double)
    checksum: Mapped[str] = mapped_column(String(64))
    briefing_filename: Mapped[str | None] = mapped_column(String(200), nullable=True)
    closed_at: Mapped[float] = mapped_column(Double)
    closed_by: Mapped[int] = mapped_column(ForeignKey('users.id'))


class EnergyEntry(Base):
    """分摊明细：book_id 指向原账，revision_id 指向修订账，二者互斥。"""
    __tablename__ = 'energy_entries'
    id: Mapped[int] = mapped_column(primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey('energy_periods.id'))
    book_id: Mapped[int | None] = mapped_column(ForeignKey('energy_close_books.id'), nullable=True)
    revision_id: Mapped[int | None] = mapped_column(ForeignKey('energy_revisions.id'), nullable=True)
    segment_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    meter_serial: Mapped[str] = mapped_column(String(64))
    start_ts: Mapped[float] = mapped_column(Double)
    end_ts: Mapped[float] = mapped_column(Double)
    overlap_seconds: Mapped[float] = mapped_column(Double, default=0.0)
    amount: Mapped[float] = mapped_column(Double, default=0.0)
    amount_exact: Mapped[str] = mapped_column(String(40), default='0')
    is_unassigned: Mapped[bool] = mapped_column(Boolean, default=False)
    unassigned_reason: Mapped[str | None] = mapped_column(String(60), nullable=True)


class EnergyAnomaly(Base):
    __tablename__ = 'energy_anomalies'
    id: Mapped[int] = mapped_column(primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey('energy_periods.id'))
    book_id: Mapped[int | None] = mapped_column(ForeignKey('energy_close_books.id'), nullable=True)
    revision_id: Mapped[int | None] = mapped_column(ForeignKey('energy_revisions.id'), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    scope: Mapped[str] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(String(300))
    ref: Mapped[dict] = mapped_column(JSON, default=dict)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Double)


class EnergyRevision(Base):
    """封账后迟到更正另开的修订账；cursor 记录断点，中断后从已记位置恢复。"""
    __tablename__ = 'energy_revisions'
    id: Mapped[int] = mapped_column(primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey('energy_periods.id'))
    book_id: Mapped[int] = mapped_column(ForeignKey('energy_close_books.id'))
    reason: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default='in_progress')  # in_progress/completed/failed
    cursor: Mapped[dict] = mapped_column(JSON, default=dict)
    totals: Mapped[dict] = mapped_column(JSON, default=dict)
    diff: Mapped[dict] = mapped_column(JSON, default=dict)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    briefing_filename: Mapped[str | None] = mapped_column(String(200), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
    completed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    error: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class EnergyCorrection(Base):
    """封账后迟到更正的暂存行；processed 标记支持修订中断后从断点恢复。"""
    __tablename__ = 'energy_corrections'
    id: Mapped[int] = mapped_column(primary_key=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey('energy_revisions.id'))
    stream: Mapped[str] = mapped_column(String(20))  # meter/segment
    idempotency_key: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Double)
    __table_args__ = (UniqueConstraint('revision_id', 'idempotency_key', name='uq_energy_corr_rev_key'),)


class EnergyIdempotency(Base):
    """通用幂等记录：补传/封账/修订重试不重复记耗电。"""
    __tablename__ = 'energy_idempotency'
    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    ref_id: Mapped[int | None] = mapped_column(Double, nullable=True)
    created_at: Mapped[float] = mapped_column(Double)
    __table_args__ = (UniqueConstraint('scope', 'idempotency_key', name='uq_energy_idem_scope_key'),
                      Index('ix_energy_idem_scope', 'scope'))
