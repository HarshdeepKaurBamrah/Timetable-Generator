#!/usr/bin/env python3
"""Setup helper — installs requirements and checks MongoDB connectivity."""
import subprocess, sys, os

print("Installing requirements...")
subprocess.check_call([sys.executable, "-m", "pip", "install", "-r",
                       os.path.join(os.path.dirname(__file__), "requirements.txt")])

print("\nChecking MongoDB connectivity...")
mongo_uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
try:
    from pymongo import MongoClient
    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=3000)
    client.admin.command("ping")
    print(f"✓ MongoDB connected at {mongo_uri}")
except Exception as e:
    print(f"✗ MongoDB not reachable at {mongo_uri}")
    print(f"  Error: {e}")
    print("  Start MongoDB: mongod --dbpath /data/db")
    print("  Or set MONGO_URI env variable for a remote instance")
    sys.exit(1)

print("\nInitialising database...")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
from database import init_db
init_db()
print("✓ Database ready")
print("\nRun: python run.py")
