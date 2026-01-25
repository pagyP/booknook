#!/usr/bin/env python3
"""
Initialize the database with all required tables.
This script is run on container startup to ensure the database schema exists.
"""

from app import app, db

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        print("Database initialized successfully")
