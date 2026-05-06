# Academic Timetable System — MongoDB Edition

A full-stack academic timetable generator with MongoDB database backend.

## What's New vs Original

- **MongoDB database** — replaces SQLite; connects to any MongoDB instance
- **Named Batches** — each batch in a division now has a custom name (e.g. "Batch Alpha", "Group B1") instead of auto-generated IDs. Names are visible in the UI, timetable, and Excel export.
- **Batch Management API** — rename/resize batches after creation (`PUT /api/batches/<id>`, `DELETE /api/batches/<id>`, `POST /api/divisions/<did>/batches`)
- **Health endpoint** reports MongoDB connectivity

## Prerequisites

- Python 3.9+
- MongoDB 5.0+ (running locally or any Atlas/remote URI)
- pip packages: `flask pymongo openpyxl tabulate`

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Make sure MongoDB is running
# Local: mongod --dbpath /data/db
# Or use MongoDB Atlas URI

# 3. Start the server
python run.py

# With custom MongoDB URI:
python run.py --mongo "mongodb://user:pass@host:27017" --db timetable_db

# Production mode (all interfaces):
python run.py --prod --port 5000
```

Open http://localhost:5000

## Docker Quick Start

```bash
# Build and start the app + MongoDB
docker compose up --build
```

Open http://localhost:5000

Useful commands:

```bash
# Stop containers
docker compose down

# Stop and remove the MongoDB data volume
docker compose down -v

# Rebuild after code changes
docker compose up --build
```

If you want to run the app container against some other MongoDB instance, update `MONGO_URI` in `docker-compose.yml` or pass it with `docker run`.
The Compose setup starts with a fresh MongoDB volume; the app will create the default coordinator account automatically. If you need the existing dump in `backup/timetable_db`, restore it separately after startup.

## Default Login

| Role        | Email                 | Password   |
|-------------|-----------------------|------------|
| Coordinator | admin@college.edu     | admin123   |

## Named Batches

When creating a division you can specify custom batch names:

```
Division: "CSE Year 3"   Batches: 3
  Batch 1 → "Batch Alpha"
  Batch 2 → "Batch Beta"
  Batch 3 → "Batch Gamma"
```

Batch names appear in:
- Division cards (coloured badges)
- Batch Teachers assignment dropdown
- Batch Teachers assignment table
- Excel export (lab cells show batch names)

### Batch API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/batches` | All batches (optional `?division=X`) |
| PUT | `/api/batches/<id>` | Rename or resize a batch |
| DELETE | `/api/batches/<id>` | Delete a batch (if no active slots) |
| POST | `/api/divisions/<did>/batches` | Add a new batch to a division |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGO_DB` | `timetable_db` | Database name |
| `SECRET_KEY` | (built-in) | Flask session secret |
| `PORT` | `5000` | HTTP port |

## MongoDB Collections

| Collection | Description |
|------------|-------------|
| `users` | Login accounts |
| `teachers` | Faculty profiles |
| `rooms` | Lecture + lab rooms |
| `subjects` | Subjects with lab/tutorial config |
| `divisions` | Class sections |
| `batches` | **Named** batches per division |
| `batch_teachers` | Per-batch lab teacher overrides |
| `timetable_slots` | Generated schedule |
| `notifications` | Absence/claim notifications |
| `change_log` | Audit trail |
| `config` | System config (periods, days, etc.) |
| `sequences` | Auto-increment counters |
