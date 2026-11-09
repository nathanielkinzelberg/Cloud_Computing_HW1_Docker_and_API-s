import os
import secrets
import string
import requests
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
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
