from pydantic import BaseModel, Field
from typing import List

class StartRequest(BaseModel):
    zone: int = Field(..., ge=1)
    duration: int = Field(..., ge=1)
    time_unit: str = "Minuten"

class QueueAddRequest(BaseModel):
    zone: int = Field(..., ge=1)
    duration: int = Field(..., ge=1)
    time_unit: str = "Minuten"

class ScheduleAddRequest(BaseModel):
    zone: int = Field(..., ge=0)  # 0 = alle Ventile
    weekdays: List[int] = Field(..., min_length=1)
    start_times: List[str] = Field(..., min_length=1)
    duration_s: int = Field(..., ge=1)
    repeat: bool = True
    time_unit: str = "Minuten"

class ParallelModeRequest(BaseModel):
    enabled: bool
