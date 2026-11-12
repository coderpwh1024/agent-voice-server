"""Fresh, foreground device coordinates supplied for one user turn."""

from datetime import UTC, datetime, timedelta

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class DeviceLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_meters: float = Field(ge=0, allow_inf_nan=False)
    captured_at: AwareDatetime

    def is_fresh(self) -> bool:
        age = datetime.now(UTC) - self.captured_at
        return -timedelta(minutes=1) <= age <= timedelta(minutes=5)
