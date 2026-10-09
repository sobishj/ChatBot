"""A tiny fake hospital booking API for demonstrating API actions.

Run it on the host:

    uvicorn demo.mock_booking_api:app --port 8100

Then, in on-premise mode, point a client's API actions at http://host.docker.internal:8100
with header authentication ``X-API-Key`` = ``demo-key`` (or set MOCK_API_KEY) and add the
"appointment booking" template. Data lives in memory and resets when the server restarts.
"""

from __future__ import annotations

import os
import secrets
from datetime import date

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

API_KEY = os.environ.get("MOCK_API_KEY", "demo-key")

app = FastAPI(title="Mock booking API", version="1.0")

DEPARTMENTS = [
    {"id": 1, "name": "Cardiology"},
    {"id": 2, "name": "Orthopaedics"},
    {"id": 3, "name": "ENT"},
]
DOCTORS = [
    {"id": 1, "name": "Dr. Anil Kumar", "department_id": 1},
    {"id": 2, "name": "Dr. Meera Nair", "department_id": 1},
    {"id": 3, "name": "Dr. Joseph Mathew", "department_id": 2},
    {"id": 4, "name": "Dr. Fathima Rahman", "department_id": 3},
]
TIMES = ["09:00", "09:30", "10:00", "10:30", "11:00", "14:00", "14:30", "15:00"]
APPOINTMENTS: dict[str, dict[str, object]] = {}


def require_key(x_api_key: str | None = Header(default=None)) -> None:
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Missing or wrong X-API-Key.")


def _doctor(doctor_id: int) -> dict[str, object]:
    doctor = next((d for d in DOCTORS if d["id"] == doctor_id), None)
    if doctor is None:
        raise HTTPException(status_code=404, detail=f"No doctor with id {doctor_id}.")
    return doctor


def _free(doctor_id: int, day: date) -> list[str]:
    taken = {a["time"] for a in APPOINTMENTS.values() if a["doctor_id"] == doctor_id and a["date"] == day.isoformat()}
    # Doctors don't work on Sundays.
    return [] if day.weekday() == 6 else [t for t in TIMES if t not in taken]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/departments", dependencies=[Depends(require_key)])
def departments() -> list[dict[str, object]]:
    return DEPARTMENTS


@app.get("/doctors", dependencies=[Depends(require_key)])
def doctors(department_id: int | None = None) -> list[dict[str, object]]:
    return [d for d in DOCTORS if department_id is None or d["department_id"] == department_id]


@app.get("/slots", dependencies=[Depends(require_key)])
def slots(doctor_id: int, date: date) -> dict[str, object]:  # noqa: A002 - the API's parameter name
    doctor = _doctor(doctor_id)
    return {"doctor": doctor["name"], "date": date.isoformat(), "free": _free(doctor_id, date)}


class BookingIn(BaseModel):
    doctor_id: int
    date: date
    time: str = Field(pattern=r"^\d{2}:\d{2}$")
    patient_name: str = Field(min_length=1, max_length=200)
    phone: str = Field(pattern=r"^\+?\d{6,15}$")


@app.post("/appointments", status_code=201, dependencies=[Depends(require_key)])
def book(body: BookingIn) -> dict[str, object]:
    doctor = _doctor(body.doctor_id)
    if body.time not in _free(body.doctor_id, body.date):
        raise HTTPException(status_code=409, detail=f"{body.time} on {body.date} is not free for {doctor['name']}.")
    booking_id = f"BK-{secrets.randbelow(900000) + 100000}"
    APPOINTMENTS[booking_id] = {"doctor_id": body.doctor_id, "date": body.date.isoformat(), "time": body.time, "patient_name": body.patient_name}
    return {"booking_id": booking_id, "doctor": doctor["name"], "date": body.date.isoformat(), "time": body.time, "patient_name": body.patient_name}


@app.delete("/appointments/{appointment_id}", dependencies=[Depends(require_key)])
def cancel(appointment_id: str) -> dict[str, object]:
    if APPOINTMENTS.pop(appointment_id, None) is None:
        raise HTTPException(status_code=404, detail=f"No appointment {appointment_id}.")
    return {"cancelled": appointment_id}
