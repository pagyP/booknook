import sys
import os

# Add the parent directory to the Python path so tests can import app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Set test database URI BEFORE importing app (required since SQLite fallback removed)
os.environ['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///test.db'

import pytest
from app import app, db, limiter


# Disable rate limiter globally for all tests
limiter.enabled = False


@pytest.fixture
def reset_app_state():
    """Reset app state before and after each test to ensure complete isolation."""
    with app.app_context():
        # Clear any existing database
        db.session.remove()
        try:
            db.drop_all()
        except:
            pass
    
    # Set fresh test configuration for this test
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///test.db'
    
    # Create fresh database for this test
    with app.app_context():
        db.drop_all()
        db.create_all()
    
    yield
    
    # Clean up after test
    with app.app_context():
        db.session.remove()
        db.drop_all()
    
    # Clean up test database file
    if os.path.exists('test.db'):
        os.remove('test.db')
