#!/usr/bin/env python3
"""Start the Academic Timetable System (MongoDB edition)."""
import sys, os, argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))

parser = argparse.ArgumentParser(description='Academic Timetable System (MongoDB)')
parser.add_argument('--prod',  action='store_true', help='Bind to all interfaces')
parser.add_argument('--port',  type=int, default=5000, help='Port (default: 5000)')
parser.add_argument('--debug', action='store_true', help='Debug/reload mode')
parser.add_argument('--mongo', default='mongodb://localhost:27017', help='MongoDB URI')
parser.add_argument('--db',    default='timetable_db', help='MongoDB database name')
args = parser.parse_args()

os.environ['MONGO_URI'] = args.mongo
os.environ['MONGO_DB']  = args.db
os.environ['HOST']       = '0.0.0.0' if args.prod else '127.0.0.1'
os.environ['PORT']       = str(args.port)
os.environ['DEBUG']      = 'true' if args.debug else 'false'

host  = os.environ['HOST']
port  = args.port
debug = args.debug

from database import DatabaseConnectionError, init_db
try:
    init_db()
except DatabaseConnectionError as exc:
    print("\n" + "=" * 62)
    print("Academic Timetable System startup failed")
    print("=" * 62)
    print(str(exc))
    print("=" * 62 + "\n")
    raise SystemExit(1)

print(
    "\n"
    + "=" * 62
    + f"\nAcademic Timetable System (MongoDB Edition)\n"
    + f"URL       : http://{'0.0.0.0' if args.prod else 'localhost'}:{port}\n"
    + f"MongoDB   : {args.mongo}\n"
    + f"Database  : {args.db}\n"
    + "Coordinator: admin@college.edu / admin123\n"
    + "=" * 62
    + "\n"
)

from app import app
app.run(host=host, port=port, debug=debug)
