"""电量对账的请求体定义（pydantic 严格校验，服务端兜底再校验）。"""
from pydantic import BaseModel, Field


class PeriodInput(BaseModel):
    period_key: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_\-\.]+$')
    name: str = Field(min_length=2, max_length=160)
    start_ts: float
    end_ts: float
    max_gap_seconds: float | None = Field(default=None, gt=0)


class MeterReadingInput(BaseModel):
    meter_serial: str = Field(min_length=1, max_length=64)
    reading_ts: float
    reading_value: float = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)
    source_ref: str | None = Field(default=None, max_length=160)


class SegmentInput(BaseModel):
    segment_ref: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=160)
    start_ts: float
    end_ts: float
    idempotency_key: str = Field(min_length=1, max_length=128)
    source_ref: str | None = Field(default=None, max_length=160)


class IngestInput(BaseModel):
    stream: str = Field(pattern=r'^(meter|segment)$')
    items: list[dict] = Field(min_length=1, max_length=2000)


class WatermarkInput(BaseModel):
    stream: str = Field(pattern=r'^(meter|segment)$')
    watermark_ts: float


class RevisionInput(BaseModel):
    reason: str = Field(min_length=2, max_length=300)
    idempotency_key: str = Field(min_length=1, max_length=128)


class CorrectionInput(BaseModel):
    stream: str = Field(pattern=r'^(meter|segment)$')
    idempotency_key: str = Field(min_length=1, max_length=128)
    payload: dict


class EnergyApproveInput(BaseModel):
    job_id: int
    period_id: int
