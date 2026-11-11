import os
import re
import secrets
import string
import requests
from datetime import datetime, date
from dotenv import load_dotenv
from functools import wraps
from flask import Flask, request, jsonify

app = Flask(__name__)
load_dotenv()
API_NINJAS_KEY = os.getenv("API_NINJAS_KEY")

# ---------------------------------------------------------------------------
# In-memory stores
# ---------------------------------------------------------------------------

# users: { user_id (int) -> { "name": str, "api_key": str } }
users = {}

# name_index: { name (str, lowercase) -> user_id } for fast duplicate checks
name_index = {}

# api_key_index: { api_key (str) -> user_id } for fast key lookups
api_key_index = {}

ADMIN_ID = 0
ADMIN_NAME = "admin"
ADMIN_KEY = "ytngh52@gb&&ubt8"

_next_user_id = 1  # regular users start at 1


def _bootstrap_admin():
    users[ADMIN_ID] = {"name": ADMIN_NAME, "api_key": ADMIN_KEY}
    name_index[ADMIN_NAME] = ADMIN_ID
    api_key_index[ADMIN_KEY] = ADMIN_ID


_bootstrap_admin()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _generate_api_key():
    alphabet = string.ascii_letters + string.digits
    while True:
        key = "".join(secrets.choice(alphabet) for _ in range(16))
        if key not in api_key_index:
            return key


def _resolve_caller(req):
    """Return (user_id, is_admin) or None if the key is missing/invalid."""
    key = req.headers.get("X-Api-Key")
    if not key:
        return None
    user_id = api_key_index.get(key)
    if user_id is None:
        return None
    return user_id, user_id == ADMIN_ID


def require_auth(f):
    """Decorator: validates X-Api-Key and injects caller_id + is_admin."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        result = _resolve_caller(request)
        if result is None:
            return jsonify({"error": "Unauthorized"}), 401
        caller_id, is_admin = result
        return f(*args, caller_id=caller_id, is_admin=is_admin, **kwargs)
    return wrapper


def require_admin(f):
    """Decorator: validates X-Api-Key and requires admin privileges."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        result = _resolve_caller(request)
        if result is None:
            return jsonify({"error": "Unauthorized"}), 401
        caller_id, is_admin = result
        if not is_admin:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, caller_id=caller_id, is_admin=is_admin, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# API Ninjas helper
# ---------------------------------------------------------------------------

def _lookup_exercise(name):
    response = requests.get(
        "https://api.api-ninjas.com/v1/exercises",
        headers={"X-Api-Key": API_NINJAS_KEY},
        params={"name": name}
    )
    if response.status_code != 200:
        return None, 502

    results = response.json()
    for exercise in results:
        if exercise["name"].lower() == name.lower():
            return {
                "type": exercise["type"],
                "muscle-group": exercise["muscle"],
                "difficulty": exercise["difficulty"]
            }, 200

    return None, 422


# ---------------------------------------------------------------------------
# Exercise store
# ---------------------------------------------------------------------------

# exercises: { exercise_code (str) -> { name, type, muscle-group, difficulty } }
exercises = {}

# prefix_count: { 3-letter prefix (str) -> count } for exercise-code generation
prefix_count = {}


def _generate_exercise_code(name):
    prefix = name[:3].lower() if len(name) >= 3 else name.lower().ljust(3, "a")
    count = prefix_count.get(prefix, 0) + 1
    prefix_count[prefix] = count
    return f"{prefix}{count}"


# ---------------------------------------------------------------------------
# Scheduling data model
# ---------------------------------------------------------------------------

VALID_PERIODS = ("morning", "afternoon", "evening")
SCHEDULE_WINDOW_START = date(2026, 11, 15)
SCHEDULE_WINDOW_END = date(2026, 12, 15)
MAX_ACTIVE_APPOINTMENTS = 3

DATE_FORMAT_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")

# schedule_slots: { (exercise_code, date_str, period) -> user_id }
# Global slot ledger: at most one appointment may exist per slot.
schedule_slots = {}

# user_appointments: { user_id -> { appt_id -> {appt-id, exercise-code, date, period} } }
user_appointments = {}

# _next_appt_id: { user_id -> next id } — appt-ids are per-user, start at 1,
# and are never reused so a deleted appointment's id stays dead.
_next_appt_id = {}


def _parse_date(date_str):
    """Return a date for a strict DD/MM/YYYY string, or None if malformed."""
    if not isinstance(date_str, str) or not DATE_FORMAT_RE.match(date_str):
        return None
    try:
        return datetime.strptime(date_str, "%d/%m/%Y").date()
    except ValueError:
        return None


def _validate_booking_input(date_str, period):
    """Validate date + period syntax/semantics.

    Returns (None, None) when valid, otherwise (error_body, status):
      400 — malformed date or unrecognised period value
      422 — well-formed date outside the booking window
    """
    parsed = _parse_date(date_str)
    if parsed is None:
        return {"error": "Bad Request"}, 400
    if period is not None and period not in VALID_PERIODS:
        return {"error": "Bad Request"}, 400
    if not (SCHEDULE_WINDOW_START <= parsed <= SCHEDULE_WINDOW_END):
        return {"error": "Date outside booking window"}, 422
    return None, None


def _find_booking_conflict(user_id, exercise_code, date_str, period, ignore_appt_id=None):
    """Apply the three scheduling rules for a candidate (user, exercise, date, period).

    Returns an error string when the booking must be rejected (always a 422),
    or None when the slot can be booked. `ignore_appt_id` lets PUT exclude the
    appointment being replaced from the checks.
    """
    # Rule 1 — global slot: one appointment per (exercise, date, period)
    holder = schedule_slots.get((exercise_code, date_str, period))
    if holder is not None:
        appts = user_appointments.get(holder, {})
        held_by_ignored = any(
            aid == ignore_appt_id
            and a["exercise-code"] == exercise_code
            and a["date"] == date_str
            and a["period"] == period
            for aid, a in appts.items()
        ) if holder == user_id else False
        if not held_by_ignored:
            return "slot taken"

    # Rule 2 — one exercise per user per date+period
    for aid, appt in user_appointments.get(user_id, {}).items():
        if aid == ignore_appt_id:
            continue
        if appt["date"] == date_str and appt["period"] == period:
            return "user already booked in this period"

    # Rule 3 — at most 3 active appointments per user
    active = user_appointments.get(user_id, {})
    count = len(active) - (1 if ignore_appt_id in active else 0)
    if count >= MAX_ACTIVE_APPOINTMENTS:
        return "appointment limit reached"

    return None


def _book_appointment(user_id, exercise_code, date_str, period, appt_id=None):
    """Record an appointment in both stores and return its record.

    Assumes validation and conflict checks already passed. When `appt_id` is
    given (PUT rebooking) it is reused; otherwise the next per-user id is drawn.
    """
    if appt_id is None:
        appt_id = _next_appt_id.get(user_id, 1)
        _next_appt_id[user_id] = appt_id + 1

    record = {
        "appt-id": appt_id,
        "exercise-code": exercise_code,
        "date": date_str,
        "period": period,
    }
    user_appointments.setdefault(user_id, {})[appt_id] = record
    schedule_slots[(exercise_code, date_str, period)] = user_id
    return record


def _cancel_appointment(user_id, appt_id):
    """Remove an appointment from both stores; return it, or None if absent."""
    appt = user_appointments.get(user_id, {}).pop(appt_id, None)
    if appt is None:
        return None
    schedule_slots.pop((appt["exercise-code"], appt["date"], appt["period"]), None)
    return appt


# ---------------------------------------------------------------------------
# 3. /exercises and /exercises/{exercise-code}
# ---------------------------------------------------------------------------

VALID_EXERCISE_FILTERS = {"type", "muscle-group", "difficulty"}


@app.route("/exercises", methods=["POST"])
@require_admin
def add_exercise(caller_id, is_admin):
    data = request.get_json(silent=True)
    if not data or "name" not in data or not isinstance(data["name"], str):
        return jsonify({"error": "Bad Request"}), 400

    name = data["name"]
    details, status = _lookup_exercise(name)
    if status != 200:
        return jsonify({"error": "Exercise not found" if status == 422 else "Bad Gateway"}), status

    code = _generate_exercise_code(name)
    exercises[code] = {"name": name, "exercise-code": code, **details}

    return jsonify(exercises[code]), 201


@app.route("/exercises", methods=["GET"])
@require_auth
def list_exercises(caller_id, is_admin):
    params = request.args
    for key in params:
        if key not in VALID_EXERCISE_FILTERS:
            return jsonify({"error": "Bad Request"}), 400

    result = list(exercises.values())
    for key, value in params.items():
        result = [e for e in result if e.get(key) == value]

    return jsonify(result), 200


@app.route("/exercises/<exercise_code>", methods=["GET"])
@require_auth
def get_exercise(exercise_code, caller_id, is_admin):
    exercise = exercises.get(exercise_code)
    if exercise is None:
        return jsonify({"error": "Not Found"}), 404
    return jsonify(exercise), 200


@app.route("/exercises/<exercise_code>", methods=["DELETE"])
@require_admin
def delete_exercise(exercise_code, caller_id, is_admin):
    if exercise_code not in exercises:
        return jsonify({"error": "Not Found"}), 404
    del exercises[exercise_code]
    return "", 204


# ---------------------------------------------------------------------------
# 1. /register
# ---------------------------------------------------------------------------

@app.route("/register", methods=["POST"])
def register():
    data = request.get_json(silent=True)
    if not data or "name" not in data or not isinstance(data["name"], str):
        return jsonify({"error": "Bad Request"}), 400

    name = data["name"]
    if name.lower() in name_index:
        return jsonify({"error": "User already registered"}), 422

    global _next_user_id
    api_key = _generate_api_key()
    user_id = _next_user_id
    _next_user_id += 1

    users[user_id] = {"name": name, "api_key": api_key}
    name_index[name.lower()] = user_id
    api_key_index[api_key] = user_id

    return jsonify({"name": name, "user-id": user_id, "API-key": api_key}), 201


@app.route("/register", methods=["GET"])
@require_admin
def list_users(caller_id, is_admin):
    result = [
        {"name": u["name"], "user-id": uid, "API-key": u["api_key"]}
        for uid, u in users.items()
    ]
    return jsonify(result), 200


# ---------------------------------------------------------------------------
# 2. /validate/{user-id}
# ---------------------------------------------------------------------------

@app.route("/validate/<int:user_id>", methods=["POST"])
def validate(user_id):
    data = request.get_json(silent=True)
    if not data or "API-key" not in data or not isinstance(data["API-key"], str):
        return jsonify({"error": "Bad Request"}), 400

    user = users.get(user_id)
    if user is None or user["api_key"] != data["API-key"]:
        return jsonify({"error": "Unauthorized"}), 401

    return jsonify({"privileged": user_id == ADMIN_ID}), 200


# ---------------------------------------------------------------------------
# 4. /schedule/{user-id}/{exercise-code}
# ---------------------------------------------------------------------------

@app.route("/schedule/<int:user_id>/<exercise_code>", methods=["POST"])
@require_auth
def book_schedule(user_id, exercise_code, caller_id, is_admin):
    if caller_id != user_id:
        return jsonify({"error": "Unauthorized"}), 401
    if exercise_code not in exercises:
        return jsonify({"error": "Not Found"}), 404

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not set(data) <= {"date", "period"}:
        return jsonify({"error": "Bad Request"}), 400
    if "date" not in data or not isinstance(data["date"], str):
        return jsonify({"error": "Bad Request"}), 400

    date_str = data["date"]
    period = data.get("period")
    error, status = _validate_booking_input(date_str, period)
    if error is not None:
        return jsonify(error), status

    if period is None:
        period = next(
            (p for p in VALID_PERIODS
             if _find_booking_conflict(user_id, exercise_code, date_str, p) is None),
            None,
        )
        if period is None:
            return jsonify({"availability": False, "appt-id": 0}), 422

    if _find_booking_conflict(user_id, exercise_code, date_str, period) is not None:
        return jsonify({"availability": False, "appt-id": 0}), 422

    record = _book_appointment(user_id, exercise_code, date_str, period)
    return jsonify({
        "appt-id": record["appt-id"],
        "user-id": user_id,
        "availability": True,
        "exercise-code": exercise_code,
        "date": date_str,
        "period": period,
    }), 201


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
