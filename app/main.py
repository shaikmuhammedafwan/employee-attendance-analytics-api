
"""
Employee Attendance & Analytics API
Run: uvicorn app.main:app --port 8000
Environment: MONGO_URI, MONGO_DB
"""
import os
import re
import calendar
import json
from bson import json_util
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
from pymongo.collection import ReturnDocument

load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

if not MONGO_URI:
    raise RuntimeError("MONGO_URI is missing. Check your .env file.")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}


# ---------------------------------------------------------------------------
# Application and indexes
# ---------------------------------------------------------------------------

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

def explain_json(command):
    result = db.command(command)
    return json.loads(json_util.dumps(result))




def create_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employee_department_code",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)],
        name="ix_employee_joined_department",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
    )
    
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    )

    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", ASCENDING)],
        name="ix_attendance_status_date",
    )


@app.on_event("startup")
def startup():
    create_indexes()



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fail(status_code: int, message: str):
    raise HTTPException(status_code=status_code, detail=message)


def round_half_up(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def epoch_ms_to_utc(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(422, "Timestamp must be an integer in epoch milliseconds")
    if not 100000000000 <= value <= 4102444800000:
        fail(422, "Timestamp is outside the supported range")
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def utc_to_epoch_ms(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC).replace(microsecond=0)
    return int(value.timestamp()) * 1000


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def as_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST).replace(microsecond=0)


def api_value(value):
    """Convert MongoDB datetimes to API epoch milliseconds recursively."""
    if isinstance(value, datetime):
        return utc_to_epoch_ms(value)
    if isinstance(value, dict):
        return {k: api_value(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [api_value(v) for v in value]
    return value


def parse_date(value: str, field_name="date") -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return parsed
    except (ValueError, TypeError):
        fail(422, f"{field_name} must use YYYY-MM-DD format")


def parse_month(value: str) -> tuple[int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}", value):
        fail(422, "month must use YYYY-MM format")
    year, month = map(int, value.split("-"))
    if not 1 <= month <= 12:
        fail(422, "month must use YYYY-MM format")
    return year, month


def month_dates(month: str):
    year, mon = parse_month(month)
    first = date(year, mon, 1)
    last = date(year, mon, calendar.monthrange(year, mon)[1])
    return first, last


def working_days(start: date, end: date) -> int:
    if end < start:
        return 0
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    local = as_ist(punch_in)
    hour, minute = map(int, shift_start.split(":"))
    start = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    seconds = (local - start).total_seconds()
    if seconds <= 600:
        return 0
    return max(0, int(seconds // 60))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (as_utc(punch_out) - as_utc(punch_in)).total_seconds()
    return round_half_up(seconds / 3600, 2)


def shift_end_datetime(attendance_date: str, shift_start: str,
                       shift_end: str) -> datetime:
    day = parse_date(attendance_date)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    end_day = day + timedelta(days=1) if (eh, em) <= (sh, sm) else day
    return datetime(
        end_day.year, end_day.month, end_day.day, eh, em, tzinfo=IST
    )


def compute_overtime(punch_out: datetime, shift_end: str,
                     attendance_date: str, shift_start: str) -> int:
    end = shift_end_datetime(attendance_date, shift_start, shift_end)
    seconds = (as_ist(punch_out) - end).total_seconds()
    minutes = int(seconds // 60)
    return minutes if minutes >= 30 else 0


def attendance_date_for(punch_in: datetime, emp: dict) -> str:
    local = as_ist(punch_in)
    sh, sm = map(int, emp["shift_start"].split(":"))
    eh, em = map(int, emp["shift_end"].split(":"))
    day = local.date()
    if (eh, em) <= (sh, sm) and (local.hour, local.minute) < (eh, em):
        day -= timedelta(days=1)
    return day.isoformat()


def derive_fields(doc: dict, emp: dict):
    pin = doc.get("punch_in")
    pout = doc.get("punch_out")

    if pin is None or pout is None:
        doc["work_hours"] = None
        doc["overtime_minutes"] = 0
        doc["half_day"] = False
    else:
        doc["work_hours"] = compute_work_hours(pin, pout)
        doc["overtime_minutes"] = compute_overtime(
            pout, emp["shift_end"], doc["date"], emp["shift_start"]
        )
        doc["half_day"] = doc["work_hours"] < 4.50

    doc["late_minutes"] = (
        compute_late_minutes(pin, emp["shift_start"])
        if pin is not None and doc["status"] in PRESENCE else 0
    )
    return doc


def valid_status(status):
    if status not in STATUSES:
        fail(422, "Invalid attendance status")


def get_employee(emp_code):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        fail(404, "Employee not found")
    return emp


def get_attendance(emp_code, day):
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if not record:
        fail(404, "Attendance record not found")
    return record


def clean_attendance(doc):
    doc.setdefault("history", [])
    doc.setdefault("half_day", False)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    return api_value(doc)


def pagination(page, page_size):
    if page < 1 or not 1 <= page_size <= 100:
        fail(422, "page must be >= 1 and page_size must be between 1 and 100")
    return (page - 1) * page_size


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=1, max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: date

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Invalid email address")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift_time(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Time must use HH:MM")
        return value

    @field_validator("shift_end")
    @classmethod
    def different_shift_times(cls, value, info):
        if info.data.get("shift_start") == value:
            raise ValueError("shift_start and shift_end must differ")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None:
            epoch_ms_to_utc(value)
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        if value not in PRESENCE:
            raise ValueError("Punch-in status must be PRESENT, WFH, or ON_DUTY")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)

    @field_validator("punched_at")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None:
            epoch_ms_to_utc(value)
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[int] = Field(default=None, strict=True)
    punch_out: Optional[int] = Field(default=None, strict=True)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        if value is not None and value not in STATUSES:
            raise ValueError("Invalid attendance status")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_timestamp(cls, value):
        if value is not None:
            epoch_ms_to_utc(value)
        return value


# ---------------------------------------------------------------------------
# Health and employees
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["joined_on"] = body.joined_on.isoformat()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        fail(409, "emp_code already exists")
    return api_value(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    skip = pagination(page, page_size)
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    )
    return api_value({
        "items": items, "total": total, "page": page, "page_size": page_size
    })


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = epoch_ms_to_utc(body.punched_at) if body.punched_at is not None \
        else datetime.now(UTC).replace(microsecond=0)
    ts = as_utc(ts)
    day = attendance_date_for(ts, emp)

    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        fail(409, "Attendance already exists for this employee and date")
    return clean_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    get_employee(body.emp_code)
    punched_at = epoch_ms_to_utc(body.punched_at) if body.punched_at is not None \
        else datetime.now(UTC).replace(microsecond=0)
    punched_at = as_utc(punched_at)

    record = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "punch_in": {"$ne": None, "$lte": punched_at},
    }, sort=[("punch_in", DESCENDING)])

    if record is None:
        fail(404, "No matching punch-in found")
    if record.get("punch_out") is not None:
        fail(409, "Already punched out")

    punch_in_at = as_utc(record["punch_in"])

    if punched_at <= punch_in_at:
        fail(422, "punch_out must be after punch_in")
    if punched_at > punch_in_at + timedelta(hours=24):
        fail(422, "punch_out must be within 24 hours of punch_in")

    emp = get_employee(body.emp_code)
    derived = derive_fields({
        **record, "punch_out": punched_at
    }, emp)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": punched_at,
            "work_hours": derived["work_hours"],
            "late_minutes": derived["late_minutes"],
            "overtime_minutes": derived["overtime_minutes"],
            "half_day": derived["half_day"],
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        fail(409, "Attendance was updated by another request")
    return clean_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    skip = pagination(page, page_size)
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None:
        parse_date(date_from, "date_from")
    if date_to is not None:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        fail(422, "date_from must be <= date_to")
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        if status not in STATUSES:
            fail(422, "Invalid status")
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    items = list(
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip).limit(page_size)
    )
    return {
        "items": [clean_attendance(d) for d in items],
        "total": total, "page": page, "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{attendance_day}")
def regularize_attendance(emp_code: str, attendance_day: str,
                          body: RegularizeIn):
    parse_date(attendance_day)
    emp = get_employee(emp_code)
    old = get_attendance(emp_code, attendance_day)

    new_status = body.status if body.status is not None else old["status"]
    new_pin = old.get("punch_in")
    new_pout = old.get("punch_out")

    if body.punch_in is not None:
        new_pin = epoch_ms_to_utc(body.punch_in)
    if body.punch_out is not None:
        new_pout = epoch_ms_to_utc(body.punch_out)

    if new_status in {"ABSENT", "LEAVE"}:
        if (body.punch_in is not None or body.punch_out is not None):
            fail(422, "ABSENT and LEAVE cannot be supplied with punch times")
        new_pin, new_pout = None, None
    elif new_pin is None:
        fail(422, "Presence status requires punch_in")

    if new_pin is not None:
        if as_ist(new_pin).date().isoformat() != attendance_day:
            # Overnight punch-ins before shift_end belong to the previous shift day.
            if attendance_date_for(new_pin, emp) != attendance_day:
                fail(422, "punch_in does not belong to this attendance date")
    if new_pout is not None:
        if new_pin is None or new_pout <= new_pin:
            fail(422, "punch_out must be after punch_in")
        if new_pout > new_pin + timedelta(hours=24):
            fail(422, "punch_out must be within 24 hours of punch_in")

    changes = {}
    candidates = {
        "status": new_status,
        "punch_in": as_utc(new_pin) if new_pin is not None else None,
        "punch_out": as_utc(new_pout) if new_pout is not None else None,
    }
    for key, value in candidates.items():
        if old.get(key) != value:
            changes[key] = value

    if not changes:
        fail(422, "No changes supplied")

    candidate = {**old, **candidates}
    candidate = derive_fields(candidate, emp)
    for key in ("work_hours", "late_minutes", "overtime_minutes", "half_day"):
        if old.get(key, False if key == "half_day" else 0) != candidate.get(key):
            changes[key] = candidate.get(key)

    history_entry = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Conditional update prevents silently overwriting a concurrent correction.
    filter_doc = {
        "_id": old["_id"],
        "status": old["status"],
        "punch_in": old.get("punch_in"),
        "punch_out": old.get("punch_out"),
    }
    updated = db.attendance_logs.find_one_and_update(
        filter_doc,
        {
            "$set": {
                "status": candidate["status"],
                "punch_in": candidate.get("punch_in"),
                "punch_out": candidate.get("punch_out"),
                "work_hours": candidate.get("work_hours"),
                "late_minutes": candidate.get("late_minutes", 0),
                "overtime_minutes": candidate.get("overtime_minutes", 0),
                "half_day": candidate.get("half_day", False),
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        fail(409, "Attendance changed concurrently; retry the correction")
    return clean_attendance(updated)


# ---------------------------------------------------------------------------
# Employee monthly analytics
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")

def employee_monthly(emp_code: str, month: str = Query(...)):
    emp = get_employee(emp_code)
    first, last = month_dates(month)
    start_day = max(first, parse_date(emp["joined_on"], "joined_on"))
    days = working_days(start_day, last)

    records = list(db.attendance_logs.aggregate([
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": start_day.isoformat(),
                    "$lte": last.isoformat(),
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE)]},
                                    {
                                        "$gte": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            2,
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            6,
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
                "work_hours": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE)]},
                                    {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                ]
                            },
                            "$work_hours",
                            0,
                        ]
                    }
                },
            }
        },
    ]))

    stats = records[0] if records else {}
    present_days = stats.get("present_days", 0.0)
    total_work_hours = stats.get("work_hours", 0.0)

    attendance_pct = (
        round_half_up(present_days / days * 100, 2) if days else None
    )

    return {
        "emp_code": emp_code,
        "name": emp.get("name"),
        "department": emp.get("department"),
        "month": month,
        "working_days": days,
        "present_days": present_days,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "late_minutes": stats.get("late_minutes", 0),
        "overtime_minutes": stats.get("overtime_minutes", 0),
        "work_hours": round_half_up(total_work_hours, 2),
        "attendance_pct": attendance_pct,
    }


# ---------------------------------------------------------------------------
# Department summary analytics
# ---------------------------------------------------------------------------

@app.get("/analytics/departments/summary")

def department_summary(month: str = Query(...),
                       department: Optional[str] = None):
    first, last = month_dates(month)
    end_s = last.isoformat()

    employee_match = {"joined_on": {"$lte": end_s}}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$gte": ["$date", first.isoformat()]},
                                    {"$lte": ["$date", end_s]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", list(PRESENCE)]},
                                                {
                                                    "$gte": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$date"
                                                                }
                                                            }
                                                        },
                                                        2,
                                                    ]
                                                },
                                                {
                                                    "$lte": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$date"
                                                                }
                                                            }
                                                        },
                                                        6,
                                                    ]
                                                },
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", list(PRESENCE)]},
                                                {
                                                    "$ne": [
                                                        {"$ifNull": ["$work_hours", None]},
                                                        None,
                                                    ]
                                                },
                                            ]
                                        },
                                        "$work_hours",
                                        0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", list(PRESENCE)]},
                                                {
                                                    "$ne": [
                                                        {"$ifNull": ["$work_hours", None]},
                                                        None,
                                                    ]
                                                },
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_minutes": {
                                "$sum": {"$ifNull": ["$late_minutes", 0]}
                            },
                            "leave_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "LEAVE"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "ON_DUTY"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "stats",
            }
        },
        {
            "$set": {
                "stats": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$stats", 0]},
                        {
                            "present_days": 0,
                            "work_hours_sum": 0,
                            "work_hours_count": 0,
                            "late_count": 0,
                            "late_minutes": 0,
                            "leave_count": 0,
                            "on_duty_count": 0,
                        },
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": "$stats.present_days",
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$stats.work_hours_count", 0]},
                        {
                            "$round": [
                                {
                                    "$divide": [
                                        "$stats.work_hours_sum",
                                        "$stats.work_hours_count",
                                    ]
                                },
                                2,
                            ]
                        },
                        None,
                    ]
                },
                "late_count": "$stats.late_count",
                "late_minutes": "$stats.late_minutes",
                "leave_count": "$stats.leave_count",
                "on_duty_count": "$stats.on_duty_count",
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))

    for item in items:
        item["month"] = month

    return {"items": items}



# ---------------------------------------------------------------------------
# Late leaderboard
# ---------------------------------------------------------------------------

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(default=10, ge=1, le=100),
    department: Optional[str] = None,
):
    first, last = month_dates(month)
    match = {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
             "late_minutes": {"$gt": 0}}

    pipeline = [
        {"$match": match},
        {"$group": {
            "_id": "$emp_code",
            "late_minutes": {"$sum": "$late_minutes"},
        }},
        {"$sort": {"late_minutes": -1, "_id": 1}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "late_minutes": 1,
            "rank": 1,
        }},
    ])
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


# ---------------------------------------------------------------------------
# Department trend
# ---------------------------------------------------------------------------

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")
    if end < start:
        fail(422, "to must be on or after from")
    if (end - start).days + 1 > 92:
        fail(422, "Date range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}):
        fail(404, "Department not found")

    # Daily attendance aggregation; $densify fills missing calendar dates.
    start_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)
    end_dt = datetime(end.year, end.month, end.day, tzinfo=UTC)
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$addFields": {
            "dateDate": {"$dateFromString": {"dateString": "$date"}},
            "isPresent": {"$in": ["$status", list(PRESENCE)]},
        }},
        {"$group": {
            "_id": "$dateDate",
            "present_count": {"$sum": {
                "$cond": [
                    "$isPresent",
                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                    0,
                ]
            }},
            "late_count": {"$sum": {
                "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
            }},
        }},
        {"$project": {
            "_id": 0, "dateDate": "$_id",
            "present_count": 1, "late_count": 1,
        }},
        {"$unionWith": {
            "pipeline": [
                {"$documents": [{
                    "dateDate": start_dt,
                    "present_count": 0,
                    "late_count": 0,
                }]}
            ]
        }},
        {"$group": {
            "_id": "$dateDate",
            "present_count": {"$max": "$present_count"},
            "late_count": {"$max": "$late_count"},
        }},
        {"$project": {
            "_id": 0,
            "dateDate": "$_id",
            "present_count": 1,
            "late_count": 1,
        }},
        {"$densify": {
            "field": "dateDate",
            "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt + timedelta(days=1)]},
        }},
        {"$set": {
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "date": {"$dateToString": {
                "format": "%Y-%m-%d", "date": "$dateDate", "timezone": "UTC"
            }},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"department": department}},
                {"$match": {"$expr": {"$lte": ["$joined_on", "$$day"]}}},
                {"$count": "headcount"},
            ],
            "as": "hc",
        }},
        {"$set": {
            "headcount": {"$ifNull": [
                {"$arrayElemAt": ["$hc.headcount", 0]}, 0
            ]},
            
"working_day": {"$and": [
    {"$gte": [{"$dayOfWeek": "$dateDate"}, 2]},
    {"$lte": [{"$dayOfWeek": "$dateDate"}, 6]}
]},

        }},
        {"$set": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$working_day", {"$gt": ["$headcount", 0]}]},
                    {"$round": [
                        {"$multiply": [
                            {"$divide": ["$present_count", "$headcount"]}, 100
                        ]}, 4
                    ]},
                    None,
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"dateDate": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]},
                }
            },
        }},
        {"$sort": {"dateDate": 1}},
        {"$project": {
            "_id": 0, "date": 1, "working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "attendance_rate": 1,
            "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]},
        }},
    ]
    items = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    return {"department": department, "from": from_date, "to": to_date,
            "items": items}


# ---------------------------------------------------------------------------
# Explain plans
# ---------------------------------------------------------------------------

EXPLAIN_ENDPOINTS = {
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    limit: int = Query(default=10, ge=1, le=50),
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
):
    if endpoint not in EXPLAIN_ENDPOINTS:
        fail(422, "Unknown explain endpoint")

    if endpoint == "attendance_list":
        skip = pagination(page, page_size)
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from:
            parse_date(date_from, "date_from")
            query.setdefault("date", {})["$gte"] = date_from
        if date_to:
            parse_date(date_to, "date_to")
            query.setdefault("date", {})["$lte"] = date_to
        cursor = db.attendance_logs.find(query).sort(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        ).skip(skip).limit(page_size)
        return explain_json({
            "explain": {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            fail(422, "emp_code and month are required")
        first, last = month_dates(month)
        get_employee(emp_code)
        pipeline = [
            {"$match": {
                "emp_code": emp_code,
                "date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
            }},
            {"$sort": {"date": 1}},
        ]
        return explain_json({
            "explain": {"aggregate": "attendance_logs",
                        "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        })

    
    if endpoint == "department_summary":
        if not month:
            fail(422, "month is required")

        first, last = month_dates(month)
        end_s = last.isoformat()

        employee_match = {"joined_on": {"$lte": end_s}}
        if department is not None:
            employee_match["department"] = department

        pipeline = [
            {"$match": employee_match},
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "emp_codes": {"$push": "$emp_code"},
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"codes": "$emp_codes"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$in": ["$emp_code", "$$codes"]},
                                        {"$gte": ["$date", first.isoformat()]},
                                        {"$lte": ["$date", end_s]},
                                    ]
                                }
                            }
                        },
                        {
                            "$group": {
                                "_id": None,
                                "present_days": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$status", list(PRESENCE)]},
                                                    {
                                                        "$gte": [
                                                            {
                                                                "$dayOfWeek": {
                                                                    "$dateFromString": {
                                                                        "dateString": "$date"
                                                                    }
                                                                }
                                                            },
                                                            2,
                                                        ]
                                                    },
                                                    {
                                                        "$lte": [
                                                            {
                                                                "$dayOfWeek": {
                                                                    "$dateFromString": {
                                                                        "dateString": "$date"
                                                                    }
                                                                }
                                                            },
                                                            6,
                                                        ]
                                                    },
                                                ]
                                            },
                                            {
                                                "$cond": [
                                                    {"$eq": ["$half_day", True]},
                                                    0.5,
                                                    1,
                                                ]
                                            },
                                            0,
                                        ]
                                    }
                                },
                                "work_hours_sum": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$status", list(PRESENCE)]},
                                                    {
                                                        "$ne": [
                                                            {"$ifNull": ["$work_hours", None]},
                                                            None,
                                                        ]
                                                    },
                                                ]
                                            },
                                            "$work_hours",
                                            0,
                                        ]
                                    }
                                },
                                "work_hours_count": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$and": [
                                                    {"$in": ["$status", list(PRESENCE)]},
                                                    {
                                                        "$ne": [
                                                            {"$ifNull": ["$work_hours", None]},
                                                            None,
                                                        ]
                                                    },
                                                ]
                                            },
                                            1,
                                            0,
                                        ]
                                    }
                                },
                                "late_count": {
                                    "$sum": {
                                        "$cond": [
                                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                            1,
                                            0,
                                        ]
                                    }
                                },
                                "late_minutes": {
                                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                                },
                                "leave_count": {
                                    "$sum": {
                                        "$cond": [
                                            {"$eq": ["$status", "LEAVE"]},
                                            1,
                                            0,
                                        ]
                                    }
                                },
                                "on_duty_count": {
                                    "$sum": {
                                        "$cond": [
                                            {"$eq": ["$status", "ON_DUTY"]},
                                            1,
                                            0,
                                        ]
                                    }
                                },
                            }
                        },
                    ],
                    "as": "stats",
                }
            },
            {
                "$set": {
                    "stats": {
                        "$ifNull": [
                            {"$arrayElemAt": ["$stats", 0]},
                            {
                                "present_days": 0,
                                "work_hours_sum": 0,
                                "work_hours_count": 0,
                                "late_count": 0,
                                "late_minutes": 0,
                                "leave_count": 0,
                                "on_duty_count": 0,
                            },
                        ]
                    }
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": 1,
                    "present_days": "$stats.present_days",
                    "avg_work_hours": {
                        "$cond": [
                            {"$gt": ["$stats.work_hours_count", 0]},
                            {
                                "$round": [
                                    {
                                        "$divide": [
                                            "$stats.work_hours_sum",
                                            "$stats.work_hours_count",
                                        ]
                                    },
                                    2,
                                ]
                            },
                            None,
                        ]
                    },
                    "late_count": "$stats.late_count",
                    "late_minutes": "$stats.late_minutes",
                    "leave_count": "$stats.leave_count",
                    "on_duty_count": "$stats.on_duty_count",
                }
            },
            {"$sort": {"department": 1}},
        ]

        return explain_json({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
    
    
    if endpoint == "department_trend":
        if not month:
            fail(422, "month is required")

        first, last = month_dates(month)

        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": first.isoformat(),
                        "$lte": last.isoformat(),
                    }
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", "Present"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$late_minutes", 0]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {"$sort": {"_id": 1}},
        ]

        return explain_json({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

    fail(422, "Unsupported explain endpoint")
