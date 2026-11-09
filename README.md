# HW1 — Gym Appointment REST API

REST API for scheduling gym exercise appointments. Built with Flask, runs in a single Docker container.

## Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/register` | Register a user, receive user-id and API-key |
| GET | `/register` | Admin only — list all users |
| POST | `/validate/{user-id}` | Check if a user-id + API-key pair is valid |
| POST | `/exercises` | Admin only — add an exercise (fetches data from API Ninjas) |
| GET | `/exercises` | List all exercises (filterable by type, muscle-group, difficulty) |
| GET | `/exercises/{exercise-code}` | Get a specific exercise |
| DELETE | `/exercises/{exercise-code}` | Admin only — remove an exercise |
| POST | `/schedule/{user-id}/{exercise-code}` | Book an appointment |
| GET | `/schedule/{user-id}/{appt-id}` | Get an appointment |
| PUT | `/schedule/{user-id}/{appt-id}` | Update an appointment |
| DELETE | `/schedule/{user-id}/{appt-id}` | Cancel an appointment |
| GET | `/schedule` | Admin only — list all appointments |

## Running

```bash
cp .env.example .env
# Add your API Ninjas key to .env

docker build -t gym-api .
docker run --env-file .env -p 5001:5001 gym-api
```

## Notes

- All requests (except `POST /register` and `POST /validate`) require an `X-Api-Key` header
- Data is in-memory only — resets on every restart
- Appointments must be dated between November 15 and December 15, 2026
- A user can have at most 3 active appointments
