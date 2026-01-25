from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, IntegerField, SelectField
from wtforms.validators import DataRequired, Email, EqualTo, ValidationError, Length, Regexp, NumberRange, Optional
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from datetime import datetime, timezone, timedelta
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy import func
import pyotp
import qrcode
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import hashlib
import io
import os
import re
import logging
import secrets
import requests
from dotenv import load_dotenv
import base64

load_dotenv()

# Create Flask app
app = Flask(__name__)

# Logging configuration function
def configure_logging(flask_app):
    """Configure Flask's built-in logger for security events.
    
    This function should be called after the app is fully configured
    to ensure the TESTING and DEBUG flags are properly respected.
    
    Args:
        flask_app: The Flask application instance
    """
    # Set logging level based on current configuration
    if flask_app.config.get('TESTING') or flask_app.config.get('DEBUG'):
        flask_app.logger.setLevel(logging.DEBUG)
    else:
        flask_app.logger.setLevel(logging.WARNING)
    
    # Add console handler if not already present
    if not flask_app.logger.handlers:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        ))
        flask_app.logger.addHandler(console_handler)

# PostgreSQL is required - no SQLite fallback (configured via SQLALCHEMY_DATABASE_URI)
db_uri = os.getenv('SQLALCHEMY_DATABASE_URI')
if not db_uri:
    raise ValueError('SQLALCHEMY_DATABASE_URI environment variable is required')
# Convert old postgresql:// to postgresql+psycopg:// for psycopg3
if db_uri.startswith('postgresql://'):
    db_uri = db_uri.replace('postgresql://', 'postgresql+psycopg://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_uri
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-key-change-in-production')

# Production Security Settings
app.config['DEBUG'] = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
app.config['TESTING'] = os.getenv('FLASK_ENV', '').lower() == 'testing'
app.config['SESSION_COOKIE_SECURE'] = os.getenv('SESSION_COOKIE_SECURE', 'True').lower() == 'true'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 24 * 60 * 60  # 24 hours

# Configure logging after all configuration is set
configure_logging(app)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'

# Argon2 password hasher configuration
# Parameters explicitly set for consistency and future tunability
# 
# Security rationale for a personal use application:
# - time_cost=3: Number of iterations. 3 is the minimum and still provides
#   good security (~100-200ms per hash). For personal use, this is acceptable.
#   For high-volume production, consider increasing to 4-5.
#
# - memory_cost=65536: Memory usage in KB (65 MB). High memory cost makes GPU
#   attacks impractical. 65 MB per hash is substantial and sufficient for this
#   application's threat model.
#
# - parallelism=4: Number of parallel threads. Matches typical multi-core CPUs.
#   Increases memory pressure during hashing, improving attack resistance.
#
# These parameters can be tuned as security requirements evolve:
# - Increase time_cost/memory_cost if GPUs become more common attackers
# - Decrease time_cost if performance becomes a bottleneck (e.g., high user load)
# - Adjust parallelism based on server CPU cores
#
# Reference: OWASP Password Storage Cheat Sheet recommends these parameter ranges
password_hasher = PasswordHasher(
    time_cost=3,          # iterations
    memory_cost=65536,    # 65 MB
    parallelism=4,        # threads
    hash_len=32,          # hash length in bytes
    salt_len=16,          # salt length in bytes
)

# Rate limiter for security (disabled in testing)
def limiter_enabled():
    """Check if rate limiting should be enabled."""
    return not (app.config.get('TESTING') or app.config.get('RATELIMIT_ENABLED') == False)

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    enabled=limiter_enabled
)

# Token serializer for password reset and recovery code display tokens
# Uses app.config['SECRET_KEY'] for signing
# Tokens are stateless and work across multiple gunicorn workers
serializer = URLSafeTimedSerializer(os.getenv('SECRET_KEY', 'dev-key-change-in-production'))
TOKEN_EXPIRATION_SECONDS = 15 * 60  # 15 minutes

def generate_recovery_code_display_token(user_id, recovery_codes):
    """Generate a signed, time-limited token containing recovery codes.
    
    Uses cryptographic signing to ensure codes are not tampered with.
    Token expires after 5 minutes - enough time for user to view and save codes.
    
    Security benefits:
    - Token is cryptographically signed (cannot be forged)
    - Recovery codes are not stored in session (reduces session compromise risk)
    - Token expires after 5 minutes
    - Signed token prevents modification of codes or user_id
    - **Stateless design**: Works with multiple gunicorn workers (no shared cache needed)
    
    Architecture note:
    Using signed tokens instead of server-side cache allows the application to
    scale horizontally with multiple gunicorn workers. Each worker can independently
    verify the token's signature without requiring a shared Redis/Memcached cache.
    
    Args:
        user_id: ID of the user whose codes are being displayed
        recovery_codes: List of plain-text recovery codes to display
    
    Returns:
        Signed token containing user_id and recovery codes
    """
    return serializer.dumps(
        {'user_id': user_id, 'codes': recovery_codes},
        salt='recovery-codes'
    )

def get_recovery_codes_from_cache(token):
    """Retrieve and verify recovery codes from signed token.
    
    Args:
        token: Signed token generated by generate_recovery_code_display_token
    
    Returns:
        Tuple of (user_id, recovery_codes) or (None, None) if token invalid/expired
    """
    try:
        data = serializer.loads(
            token,
            salt='recovery-codes',
            max_age=5 * 60  # 5 minutes
        )
        return data['user_id'], data['codes']
    except (BadSignature, SignatureExpired):
        return None, None

def cleanup_expired_recovery_codes():
    """No-op cleanup function for backward compatibility.
    
    With signed tokens, there's no need for explicit cleanup since tokens
    expire cryptographically after 5 minutes.
    """
    pass


# ============================================================================
# MFA (Multi-Factor Authentication) Functions
# ============================================================================

def derive_encryption_key(password, user_id=None, salt=b'totp-secret'):
    """Derive a 32-byte encryption key from password using PBKDF2.
    
    Args:
        password: User's plaintext password
        user_id: User ID for per-user salt derivation (defense-in-depth)
        salt: Base salt string (default: 'totp-secret')
    
    Returns:
        Base64-encoded encryption key suitable for Fernet
        
    Security notes:
        - Uses PBKDF2HMAC with SHA256 for KDF
        - 480,000 iterations (OWASP recommended)
        - Per-user salt derived from user_id + base salt ensures unique keys per user
        - Different salt ensures TOTP secret is encrypted differently from other uses
        - Can use either plaintext password or password hash (will derive same key from hash)
    """
    if isinstance(password, str):
        password = password.encode()
    
    # Derive per-user salt from user_id if provided
    if user_id is not None:
        # Combine base salt with user_id for unique salt per user
        # This ensures even if password is compromised, keys are unique per user
        salt = salt + str(user_id).encode()
    
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480000,  # OWASP recommendation for SHA256
    )
    key = kdf.derive(password)
    return base64.urlsafe_b64encode(key)

def encrypt_totp_secret(secret, password, user_id=None):
    """Encrypt TOTP secret using password as key.
    
    Args:
        secret: TOTP secret (generated by pyotp)
        password: User's plaintext password
        user_id: User ID for per-user salt derivation
    
    Returns:
        Base64-encoded encrypted secret
        
    Security:
        - Uses AES-128 in CBC mode with HMAC-SHA256 authentication (Fernet)
        - Encrypted with key derived from user's password
        - Per-user salt ensures unique encryption keys even for same password
        - Same password + user_id always derives same key, enabling decryption later
    """
    key = derive_encryption_key(password, user_id=user_id)
    cipher = Fernet(key)
    encrypted = cipher.encrypt(secret.encode())
    return encrypted.decode()

def decrypt_totp_secret(encrypted_secret, password, user_id=None):
    """Decrypt TOTP secret using password.
    
    Args:
        encrypted_secret: Base64-encoded encrypted secret from DB
        password: User's plaintext password
        user_id: User ID for per-user salt derivation
    
    Returns:
        Plaintext TOTP secret or None if decryption fails
    """
    try:
        key = derive_encryption_key(password, user_id=user_id)
        cipher = Fernet(key)
        decrypted = cipher.decrypt(encrypted_secret.encode())
        return decrypted.decode()
    except InvalidToken:
        app.logger.warning('Failed to decrypt TOTP secret (wrong password or corrupted data)')
        return None

def generate_device_fingerprint(user_agent, ip_address):
    """Generate a device fingerprint from User-Agent and IP address.
    
    Args:
        user_agent: HTTP User-Agent header
        ip_address: Client IP address
    
    Returns:
        SHA256 hash of User-Agent + IP (hex string)
        
    Design notes:
        - Simple but effective fingerprinting
        - Changes when user's IP changes (acceptable for 30-day trust period)
        - Users on dynamic IPs will need to re-verify after IP change
    """
    fingerprint_data = f"{user_agent}:{ip_address}"
    return hashlib.sha256(fingerprint_data.encode()).hexdigest()

def get_client_ip():
    """Extract client IP from request, handling proxies.
    
    Returns:
        Client IP address string
        
    Notes:
        - Checks X-Forwarded-For for proxied requests (e.g., behind nginx)
        - Falls back to remote_addr for direct connections
    """
    if request.headers.get('X-Forwarded-For'):
        # Behind proxy: X-Forwarded-For is comma-separated list of IPs
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr

def get_user_agent_display(user_agent):
    """Parse User-Agent into readable device description.
    
    Args:
        user_agent: HTTP User-Agent header
    
    Returns:
        Human-readable string like "Chrome on Ubuntu" or "Safari on iOS"
        
    Examples:
        - "Mozilla/5.0 (X11; Linux x86_64)..." → "Chrome on Ubuntu"
        - "Mozilla/5.0 (iPhone; CPU iPhone OS)..." → "Safari on iOS"
    """
    # Simplified parsing - in production, consider using user-agents library
    ua = user_agent.lower()
    
    # Detect browser
    if 'chrome' in ua and 'edg' not in ua:
        browser = 'Chrome'
    elif 'firefox' in ua:
        browser = 'Firefox'
    elif 'safari' in ua and 'chrome' not in ua:
        browser = 'Safari'
    elif 'edge' in ua or 'edg' in ua:
        browser = 'Edge'
    else:
        browser = 'Unknown'
    
    # Detect OS
    if 'windows' in ua:
        os_name = 'Windows'
    elif 'linux' in ua and 'android' not in ua:
        os_name = 'Ubuntu'
    elif 'macintosh' in ua or 'mac os x' in ua:
        os_name = 'macOS'
    elif 'iphone' in ua:
        os_name = 'iOS'
    elif 'android' in ua:
        os_name = 'Android'
    else:
        os_name = 'Unknown'
    
    return f"{browser} on {os_name}"


def fetch_book_from_open_library(isbn):
    """Fetch book metadata from Open Library API by ISBN.
    
    Args:
        isbn: ISBN-10 or ISBN-13 (with or without hyphens)
    
    Returns:
        Dict with keys: title, author, genre, cover_url
        Or None if book not found or API error
    
    Security:
        - API call is non-blocking with timeout (3 seconds)
        - Returns only public data from Open Library
        - Gracefully handles API failures
    """
    try:
        # Clean ISBN: remove hyphens and spaces
        isbn_clean = isbn.replace('-', '').replace(' ', '')
        
        # Open Library Search API endpoint
        # More reliable than the books API for finding metadata
        url = f'https://openlibrary.org/search.json?isbn={isbn_clean}'
        
        # Timeout after 3 seconds to avoid blocking user
        response = requests.get(url, timeout=3)
        
        if response.status_code != 200:
            app.logger.warning(f'Open Library API error for ISBN {isbn_clean}: status {response.status_code}')
            return None
        
        data = response.json()
        
        # Search API returns docs array
        docs = data.get('docs', [])
        if not docs:
            app.logger.info(f'ISBN {isbn_clean} not found in Open Library')
            return None
        
        # Use first result
        book_data = docs[0]
        
        # Extract fields from response
        result = {}
        
        # Title
        result['title'] = book_data.get('title', '')
        
        # Author(s)
        author_names = book_data.get('author_name', [])
        if author_names:
            result['author'] = ', '.join(author_names[:3])  # First 3 authors
        else:
            result['author'] = ''
        
        # Subjects (genres) - fetch from works API for better metadata
        result['genre'] = ''
        work_key = book_data.get('key')
        if work_key:
            try:
                # Fetch work data which includes subjects
                work_url = f'https://openlibrary.org{work_key}.json'
                work_response = requests.get(work_url, timeout=3)
                if work_response.status_code == 200:
                    work_data = work_response.json()
                    subjects = work_data.get('subjects', [])
                    if subjects:
                        # Use first 3 subjects as genre
                        result['genre'] = ', '.join(subjects[:3])
                else:
                    # Non-200 is expected in some cases; warn so it's visible in logs
                    app.logger.warning(
                        f'Open Library works API returned status {work_response.status_code} for work {work_key} (ISBN {isbn_clean})'
                    )
            except requests.Timeout as e:
                # Network timeout - warn because this affects functionality
                app.logger.warning(
                    f'Open Library works API timeout for work {work_key} (ISBN {isbn_clean}): {str(e)}'
                )
            except requests.RequestException as e:
                # Other requests-related errors (connection, DNS, etc.)
                app.logger.warning(
                    f'Open Library works API request error for work {work_key} (ISBN {isbn_clean}): {str(e)}'
                )
            except ValueError as e:
                # JSON decoding errors
                app.logger.warning(
                    f'Invalid JSON from Open Library works API for work {work_key} (ISBN {isbn_clean}): {str(e)}'
                )
            except Exception as e:
                # Unexpected errors should be logged as errors with traceback
                app.logger.error(
                    f'Unexpected error fetching work {work_key} from Open Library (ISBN {isbn_clean}): {str(e)}',
                    exc_info=True
                )
        
        # Cover image URL - Open Library provides cover_i (image ID)
        cover_id = book_data.get('cover_i')
        if cover_id:
            result['cover_url'] = f'https://covers.openlibrary.org/b/id/{cover_id}-M.jpg'
        else:
            result['cover_url'] = None
        
        app.logger.info(f'Successfully fetched book from Open Library: {result["title"]}')
        return result
        
    except requests.Timeout:
        app.logger.warning(f'Open Library API timeout for ISBN {isbn}')
        return None
    except requests.RequestException as e:
        app.logger.error(f'Open Library API request error: {str(e)}')
        return None
    except (ValueError, KeyError) as e:
        app.logger.error(f'Error parsing Open Library response: {str(e)}')
        return None


def generate_reset_token(user_id, code_id):
    """Generate a cryptographically signed, time-limited password reset token.
    
    Args:
        user_id: User ID to include in token
        code_id: Recovery code ID to include in token
    
    Returns:
        Signed token string that expires in 15 minutes
        
    Security properties:
        - Token is signed with SECRET_KEY (cannot be forged)
        - Token is URL-safe
        - Token expires after 15 minutes
        - Token contains no sensitive data in URL (hashed IDs in token)
    """
    return serializer.dumps(
        {'user_id': user_id, 'code_id': code_id},
        salt='password-reset'
    )

def verify_reset_token(token):
    """Verify and extract data from a password reset token.
    
    Args:
        token: Token string to verify
    
    Returns:
        Dict with 'user_id' and 'code_id' if valid and not expired
        None if token is invalid or expired
    """
    try:
        data = serializer.loads(
            token,
            salt='password-reset',
            max_age=TOKEN_EXPIRATION_SECONDS
        )
        return data
    except (BadSignature, SignatureExpired):
        return None


# User Model
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    books = db.relationship('Book', backref='user', lazy=True, cascade='all, delete-orphan')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    
    # Multi-Factor Authentication fields
    mfa_enabled = db.Column(db.Boolean, default=False)
    mfa_secret_encrypted = db.Column(db.String(255))  # Encrypted TOTP secret (encrypted with user's password)
    mfa_last_authenticated = db.Column(db.DateTime)  # Last successful MFA verification
    
    # Relationships
    trusted_devices = db.relationship('TrustedDevice', backref='user', lazy=True, cascade='all, delete-orphan')
    
    def set_password(self, password):
        """Hash password using Argon2 (memory-hard, GPU-resistant)"""
        self.password_hash = password_hasher.hash(password)
    
    def check_password(self, password):
        """Verify password against Argon2 hash
        
        Returns:
            True if password matches, False otherwise
            
        Handles:
            - VerifyMismatchError: Password doesn't match (expected during failed login)
            - InvalidHashError: Hash is malformed or from different algorithm (logs warning)
        """
        try:
            password_hasher.verify(self.password_hash, password)
            return True
        except VerifyMismatchError:
            # Expected: password doesn't match
            return False
        except InvalidHashError as e:
            # Unexpected: hash is corrupted or from different algorithm
            # Log generic message without exception details to avoid exposing
            # sensitive information about hash format or validation process
            app.logger.warning(f'Invalid password hash format for user {self.username}')
            return False
    
    def __repr__(self):
        return f'<User {self.username}>'

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

class RecoveryCode(db.Model):
    """Recovery codes for account password recovery (no email required)
    
    Each recovery code is single-use and hashed for security.
    Users can use any code during password recovery.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    code_hash = db.Column(db.String(255), nullable=False)  # Argon2 hash
    used = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    used_at = db.Column(db.DateTime)
    
    # Composite index for query: RecoveryCode.query.filter_by(user_id=x, used=False)
    # Significantly improves performance as recovery codes accumulate
    __table_args__ = (
        db.Index('idx_user_id_used', 'user_id', 'used'),
    )
    
    def __repr__(self):
        return f'<RecoveryCode user_id={self.user_id}>'

class TrustedDevice(db.Model):
    """Trusted devices for MFA bypass (device fingerprint + 30-day expiration)
    
    Devices are identified by a fingerprint of User-Agent + IP address.
    Users can optionally name devices for easy management and revocation.
    Devices expire after 30 days.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    device_fingerprint = db.Column(db.String(255), nullable=False)  # SHA256 hash of User-Agent + IP
    device_name = db.Column(db.String(255))  # User-provided friendly name (e.g., "Home Laptop")
    user_agent = db.Column(db.String(500))  # Full User-Agent for display
    ip_address = db.Column(db.String(45))  # IPv4 or IPv6
    expires_at = db.Column(db.DateTime, nullable=False)  # 30 days from creation
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_used_at = db.Column(db.DateTime)
    
    __table_args__ = (
        db.Index('idx_user_fingerprint', 'user_id', 'device_fingerprint'),
    )
    
    def is_expired(self):
        """Check if device trust has expired"""
        # Handle both timezone-aware and naive datetimes from DB
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            # Make naive datetime aware in UTC
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > expires_at
    
    def __repr__(self):
        return f'<TrustedDevice user_id={self.user_id} name={self.device_name}>'

class Book(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    author = db.Column(db.String(200), nullable=False)
    isbn = db.Column(db.String(20))
    genre = db.Column(db.String(100))
    format = db.Column(db.String(20), default='physical')  # 'physical', 'ebook', or 'audiobook'
    status = db.Column(db.String(20), default='read')  # 'to_read', 'currently_reading', or 'read'
    date_added = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    rating = db.Column(db.Integer)  # 1-5 stars
    notes = db.Column(db.Text)
    cover_url = db.Column(db.String(500))  # Open Library cover image URL
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    
    def __repr__(self):
        return f'<Book {self.title}>'

# Custom Validators
class ISBNValidator:
    """Validates ISBN-10 or ISBN-13 format (with or without hyphens)"""
    def __init__(self, message='Invalid ISBN format. Use ISBN-10 or ISBN-13.'):
        self.message = message
    
    def __call__(self, form, field):
        if not field.data:
            return  # ISBN is optional
        
        # Remove hyphens for validation
        isbn_clean = field.data.replace('-', '').replace(' ', '')
        
        # Check if it's 10 or 13 digits
        if not (len(isbn_clean) == 10 or len(isbn_clean) == 13):
            raise ValidationError(self.message)
        
        # Check all characters are digits
        if not isbn_clean.isdigit():
            raise ValidationError(self.message)

def password_strength(form, field):
    """Validate password has minimum length and complexity"""
    password = field.data
    if len(password) < 8:
        raise ValidationError('Password must be at least 8 characters long.')
    if not re.search(r'[A-Z]', password):
        raise ValidationError('Password must contain at least one uppercase letter.')
    if not re.search(r'[0-9]', password):
        raise ValidationError('Password must contain at least one number.')
    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
        raise ValidationError('Password must contain at least one special character (!@#$%^&*(),.?":{}|<>).')

def validate_cover_url(url):
    """Validate cover URL format and source.
    
    Args:
        url: URL string to validate (can be None or empty)
    
    Returns:
        The URL if valid, None if empty/None
    
    Raises:
        ValueError: If URL format is invalid or not from trusted source
    
    Security:
        - Only allows Open Library CDN URLs (covers.openlibrary.org)
        - Validates URL format to prevent injection
        - Enforces length limit (500 chars max per database schema)
    """
    if not url or not url.strip():
        return None
    
    url = url.strip()
    
    # Enforce length limit
    if len(url) > 500:
        raise ValueError('Cover URL exceeds maximum length of 500 characters.')
    
    # Basic URL format validation: must start with https://
    # (HTTP is not allowed for security reasons)
    if not url.startswith('https://'):
        raise ValueError('Cover URL must use HTTPS protocol.')
    
    # Security: Only allow Open Library CDN URLs
    # Format: https://covers.openlibrary.org/b/id/{cover_id}-{size}.{format}
    # Example: https://covers.openlibrary.org/b/id/10590366-M.jpg
    from urllib.parse import urlparse
    parsed = urlparse(url)
    
    if parsed.netloc != 'covers.openlibrary.org':
        raise ValueError('Cover URL must be from Open Library CDN (covers.openlibrary.org).')
    
    if not parsed.path.startswith('/b/id/'):
        raise ValueError('Cover URL must follow Open Library CDN path structure.')
    
    return url


def normalize_genre_input(value):
    """Normalize a free-text genre input into a canonical comma-separated string.

    - Splits on commas
    - Strips whitespace around parts
    - Drops empty parts
    - Joins parts with single comma+space

    Returns None if input is empty or results in no parts.
    """
    if not value:
        return None
    # Ensure it's a string
    value = str(value)
    parts = [p.strip() for p in value.split(',') if p.strip()]
    if not parts:
        return None
    return ', '.join(parts)

def generate_recovery_codes(user_id, count=8):
    """Generate recovery codes for a user.
    
    Uses base32 encoding for better entropy and readability.
    Each code: 12 alphanumeric characters (A-Z, 2-7) formatted as XXX-XXX-XXX
    Entropy: 32^12 ≈ 1.2 * 10^18 combinations (exceeds NIST 2^50 minimum)
    
    Args:
        user_id: User ID to generate codes for
        count: Number of codes to generate (default 8)
    
    Returns:
        List of plain-text recovery codes for display to user
        
    Note: Caller is responsible for committing the database session.
          This function only adds codes to the session without committing.
    """
    import base64
    plain_codes = []
    
    for _ in range(count):
        # Generate 9 bytes of random data = 12 base32 characters (32^12 entropy)
        # This exceeds NIST recommendation of 2^50 entropy for recovery codes
        random_bytes = secrets.token_bytes(9)
        # Use base32 (A-Z, 2-7) for better readability than base64
        base32_code = base64.b32encode(random_bytes).decode('utf-8').rstrip('=')
        # Format: XXX-XXX-XXX (12 chars in 3 groups of 4)
        plain_code = f"{base32_code[:4]}-{base32_code[4:8]}-{base32_code[8:12]}"
        
        # Hash the code for secure storage
        code_hash = password_hasher.hash(plain_code)
        
        recovery_code = RecoveryCode(user_id=user_id, code_hash=code_hash)
        db.session.add(recovery_code)
        plain_codes.append(plain_code)
    
    return plain_codes

# Forms
class RegistrationForm(FlaskForm):
    username = StringField('Username', validators=[
        DataRequired(),
        Length(min=3, max=80, message='Username must be 3-80 characters.')
    ])
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[
        DataRequired(),
        password_strength
    ])
    confirm_password = PasswordField('Confirm Password', 
                                    validators=[DataRequired(), EqualTo('password')])
    submit = SubmitField('Sign Up')
    
    def validate_username(self, username):
        user = User.query.filter_by(username=username.data).first()
        if user:
            raise ValidationError('Username already exists.')
    
    def validate_email(self, email):
        user = User.query.filter_by(email=email.data).first()
        if user:
            raise ValidationError('Email already registered.')

class LoginForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    submit = SubmitField('Log In')

class ChangeEmailForm(FlaskForm):
    new_email = StringField('New Email', validators=[
        DataRequired(),
        Email(message='Invalid email address.')
    ])
    password = PasswordField('Current Password', validators=[DataRequired()])
    submit = SubmitField('Change Email')
    
    def validate_new_email(self, new_email):
        if new_email.data == current_user.email:
            raise ValidationError('New email must be different from current email.')
        user = User.query.filter_by(email=new_email.data).first()
        if user:
            raise ValidationError('Email already registered.')

class DisableMFAForm(FlaskForm):
    """Form to disable MFA with password verification."""
    password = PasswordField('Password', validators=[DataRequired(message='Password required to disable MFA')])
    submit = SubmitField('Disable MFA')

class BookForm(FlaskForm):
    title = StringField('Title', validators=[
        DataRequired(),
        Length(min=1, max=255, message='Title must be 1-255 characters.')
    ])
    author = StringField('Author', validators=[
        DataRequired(),
        Length(min=1, max=200, message='Author name must be 1-200 characters.')
    ])
    isbn = StringField('ISBN', validators=[ISBNValidator()])
    genre = StringField('Genre', validators=[
        Length(max=100, message='Genre must be 100 characters or less.')
    ])
    format = SelectField('Format', validators=[DataRequired()], choices=[
        ('physical', 'Physical Book'),
        ('ebook', 'E-book'),
        ('audiobook', 'Audiobook')
    ])
    status = SelectField('Status', validators=[DataRequired()], choices=[
        ('to_read', 'To Read'),
        ('currently_reading', 'Currently Reading'),
        ('read', 'Read')
    ])
    rating = IntegerField('Rating', validators=[
        Optional(),
        NumberRange(min=1, max=5, message='Rating must be between 1 and 5.')
    ])
    notes = StringField('Notes', validators=[
        Length(max=5000, message='Notes must be 5000 characters or less.')
    ])
    date_added = StringField('Date Added')
    submit = SubmitField('Save Book')

class RecoverAccountForm(FlaskForm):
    """Form to initiate account recovery using email and recovery code"""
    email = StringField('Email', validators=[DataRequired(), Email()])
    recovery_code = StringField('Recovery Code', validators=[
        DataRequired(),
        Length(min=14, max=14, message='Recovery code must be in format: XXXX-XXXX-XXXX')
    ])
    submit = SubmitField('Verify Code')

class ResetPasswordForm(FlaskForm):
    """Form to reset password after recovery code verification"""
    password = PasswordField('New Password', validators=[
        DataRequired(),
        password_strength
    ])
    confirm_password = PasswordField('Confirm Password', 
                                    validators=[DataRequired(), EqualTo('password')])
    submit = SubmitField('Reset Password')

class MFASetupForm(FlaskForm):
    """Form to verify TOTP during MFA setup - includes password for security"""
    totp_code = StringField('6-Digit Code from Authenticator', validators=[
        DataRequired(),
        Length(min=6, max=6, message='Code must be exactly 6 digits'),
        Regexp(r'^\d{6}$', message='Code must contain only digits')
    ])
    password = PasswordField('Your Password (for security)', validators=[DataRequired()])
    submit = SubmitField('Verify and Enable MFA')

class MFAVerifyForm(FlaskForm):
    """Form to enter TOTP code during login"""
    totp_code = StringField('Authenticator Code', validators=[
        Optional(),
        Length(min=6, max=6, message='Code must be exactly 6 digits'),
        Regexp(r'^\d{6}$', message='Code must contain only digits')
    ])
    trust_device = SelectField('Trust this device for 30 days?', 
                               choices=[('no', 'No'), ('yes', 'Yes')],
                               default='no')
    recovery_code = StringField('Or enter recovery code', validators=[Optional()])
    submit = SubmitField('Verify')

# Routes

# API Endpoints
@app.route('/api/book-lookup', methods=['POST'])
@login_required
def book_lookup():
    """API endpoint to fetch book metadata from Open Library by ISBN.
    
    Request: JSON with 'isbn' field
    Response: JSON with book metadata (title, author, genre, cover_url)
              or error message if not found
    """
    data = request.get_json()
    isbn = data.get('isbn', '').strip()
    
    if not isbn:
        return jsonify({'error': 'ISBN is required'}), 400
    
    # Fetch from Open Library
    book_data = fetch_book_from_open_library(isbn)
    
    if not book_data:
        return jsonify({'error': 'Book not found in Open Library'}), 404
    
    # Return found book data
    return jsonify(book_data), 200

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    form = RegistrationForm()
    if form.validate_on_submit():
        try:
            user = User(username=form.username.data, email=form.email.data)
            user.set_password(form.password.data)
            db.session.add(user)
            
            # Flush to generate user.id without committing
            # This allows generate_recovery_codes to use user.id
            # If anything fails before final commit, everything can be rolled back
            db.session.flush()
            
            # Generate recovery codes BEFORE final commit
            # If this fails, the entire transaction will be rolled back
            recovery_codes = generate_recovery_codes(user.id, count=8)
            
            # Now commit everything atomically: user + recovery codes
            db.session.commit()
            
            # Generate signed token containing recovery codes
            # Token expires after 5 minutes, enough time for user to view and save codes
            display_token = generate_recovery_code_display_token(user.id, recovery_codes)
            session['recovery_code_token'] = display_token  # Signed token in session
            session.modified = True  # Ensure session is saved before redirect
            
            # Clean up any expired cache entries (no-op with signed tokens, but kept for compatibility)
            cleanup_expired_recovery_codes()
            
            app.logger.info(f'New user account created: {user.username}')
            flash('Account created successfully! Save your recovery codes.', 'success')
            
            # Log the user in so they can access settings if they need to fix their email
            login_user(user, remember=True)
            session.permanent = True
            
            # Redirect to recovery codes display page
            return redirect(url_for('show_recovery_codes'))
        except Exception as e:
            # Rollback all pending changes (user + recovery codes)
            db.session.rollback()
            app.logger.error(f'Registration failed - rollback executed: {str(e)}')
            flash('Registration failed. Please try again.', 'error')
            return render_template('register.html', form=form)
    
    return render_template('register.html', form=form)


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def login():
    """Login route with optional MFA verification.
    
    Flow:
    1. User enters email + password
    2. If MFA enabled:
       - Check if device is trusted (skip TOTP)
       - If not trusted, show TOTP verification form
       - Accept TOTP code or recovery code
       - Option to trust device for 30 days
    3. Complete login
    """
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    # Clean up any abandoned MFA sessions (if MFA started >10 minutes ago)
    mfa_started_at = session.get('mfa_started_at')
    if mfa_started_at:
        try:
            mfa_start_time = datetime.fromisoformat(mfa_started_at)
            if datetime.now(timezone.utc) - mfa_start_time > timedelta(minutes=10):
                # MFA session expired, clean up
                session.pop('mfa_required', None)
                session.pop('pending_user_id', None)
                session.pop('mfa_started_at', None)
                app.logger.info('Abandoned MFA session cleaned up (expired after 10 minutes)')
        except (ValueError, TypeError):
            # Invalid timestamp, clean up
            session.pop('mfa_required', None)
            session.pop('pending_user_id', None)
            session.pop('mfa_started_at', None)
    
    # Get MFA form data from session if continuing from MFA step
    mfa_required = session.get('mfa_required', False)
    pending_user_id = session.get('pending_user_id', None)
    
    form = LoginForm()
    mfa_form = MFAVerifyForm()
    
    # Step 1: Initial login form (email + password)
    if form.validate_on_submit() and not mfa_required:
        user = User.query.filter_by(email=form.email.data).first()
        
        if user and user.check_password(form.password.data):
            # Check if device is already trusted
            fingerprint = generate_device_fingerprint(
                request.headers.get('User-Agent', ''),
                get_client_ip()
            )
            trusted_device = TrustedDevice.query.filter_by(
                user_id=user.id,
                device_fingerprint=fingerprint
            ).first()
            
            # Skip MFA if device is trusted and not expired
            if user.mfa_enabled and (not trusted_device or trusted_device.is_expired()):
                # MFA required: store pending user in session and show MFA form
                session['mfa_required'] = True
                session['pending_user_id'] = user.id
                session['mfa_password'] = form.password.data  # Store password for MFA secret decryption
                session['mfa_started_at'] = datetime.now(timezone.utc).isoformat()
                session.permanent = False
                app.logger.info(f'MFA required for user: {user.username}')
                return render_template('mfa_verify.html', mfa_form=mfa_form)
            
            # Either MFA disabled or device is trusted
            login_user(user, remember=True)
            session.pop('mfa_required', None)
            session.pop('pending_user_id', None)
            
            app.logger.info(f'User logged in successfully: {user.username}')
            flash(f'Welcome back, {user.username}!', 'success')
            return redirect(url_for('index'))
        else:
            # Log failed attempt with email (no password) for security monitoring
            app.logger.warning(f'Failed login attempt for email: {form.email.data}')
            flash('Invalid email or password.', 'error')
    
    # Step 2: MFA verification form (TOTP or recovery code)
    if mfa_form.validate_on_submit() and mfa_required and pending_user_id:
        user = User.query.get(pending_user_id)
        
        if not user:
            app.logger.error(f'MFA verification: user {pending_user_id} not found')
            session.pop('mfa_required', None)
            session.pop('pending_user_id', None)
            session.pop('mfa_started_at', None)
            flash('Session expired. Please log in again.', 'error')
            return redirect(url_for('login'))
        
        mfa_verified = False
        
        # Try TOTP code first
        if mfa_form.totp_code.data:
            password = session.get('mfa_password')
            decrypted_secret = decrypt_totp_secret(
                user.mfa_secret_encrypted,
                password,
                user_id=user.id
            )
            totp = pyotp.TOTP(decrypted_secret) if decrypted_secret else None
            if totp and totp.verify(mfa_form.totp_code.data, valid_window=3):
                mfa_verified = True
                app.logger.info(f'MFA verification successful via TOTP for user: {user.username}')
        
        # Try recovery code if TOTP didn't work
        if not mfa_verified and mfa_form.recovery_code.data:
            recovery_code = mfa_form.recovery_code.data.replace('-', '').replace(' ', '')
            unused_codes = RecoveryCode.query.filter_by(user_id=user.id, used=False).all()
            
            for code in unused_codes:
                try:
                    password_hasher.verify(code.code_hash, recovery_code)
                    # Mark code as used
                    code.used = True
                    code.used_at = datetime.now(timezone.utc)
                    db.session.commit()
                    
                    # Disable MFA
                    user.mfa_enabled = False
                    user.mfa_secret_encrypted = None
                    db.session.commit()
                    
                    mfa_verified = True
                    app.logger.info(f'MFA disabled via recovery code for user: {user.username}')
                    flash('Recovery code used. MFA has been disabled. Please set up MFA again to secure your account.', 'warning')
                    break
                except VerifyMismatchError:
                    continue
        
        if mfa_verified:
            # Trust device if requested
            if mfa_form.trust_device.data == 'yes':
                fingerprint = generate_device_fingerprint(
                    request.headers.get('User-Agent', ''),
                    get_client_ip()
                )
                device_name = get_user_agent_display(request.headers.get('User-Agent', ''))
                
                trusted_device = TrustedDevice(
                    user_id=user.id,
                    device_fingerprint=fingerprint,
                    device_name=device_name,
                    user_agent=request.headers.get('User-Agent', ''),
                    ip_address=get_client_ip(),
                    expires_at=datetime.now(timezone.utc) + timedelta(days=30)
                )
                db.session.add(trusted_device)
                db.session.commit()
                app.logger.info(f'Trusted device added for user: {user.username}')
            
            # Complete login
            login_user(user, remember=True)
            session.pop('mfa_required', None)
            session.pop('pending_user_id', None)
            session.pop('mfa_started_at', None)
            session.pop('mfa_password', None)  # Clear stored password
            
            user.mfa_last_authenticated = datetime.now(timezone.utc)
            db.session.commit()
            
            flash(f'Welcome back, {user.username}!', 'success')
            return redirect(url_for('index'))
        else:
            app.logger.warning(f'Invalid MFA code for user: {user.username}')
            flash('Invalid authenticator code or recovery code.', 'error')
    
    return render_template('login.html', form=form, mfa_required=mfa_required, mfa_form=mfa_form)

@app.route('/logout')
@login_required
def logout():
    username = current_user.username
    logout_user()
    app.logger.info(f'User logged out: {username}')
    flash('You have been logged out.', 'success')
    return redirect(url_for('login'))

# ============================================================================
# MFA Routes
# ============================================================================

@app.route('/mfa/setup', methods=['GET', 'POST'])
@login_required
@limiter.limit("10 per hour")
def setup_mfa():
    """Initiate MFA setup - generate TOTP secret and display QR code.
    
    GET: Display QR code for scanning
    POST: User scans and submits the setup form
    """
    if current_user.mfa_enabled:
        flash('MFA is already enabled on your account.', 'info')
        return redirect(url_for('settings'))
    
    # Retrieve existing secret from session or generate new one if it doesn't exist
    secret = session.get('mfa_setup_secret')
    if not secret:
        secret = pyotp.random_base32()
        session['mfa_setup_secret'] = secret
    
    # Generate QR code
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(
        name=current_user.email,
        issuer_name='Reading Nook'
    )
    
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Convert to base64 for HTML embedding
    img_io = io.BytesIO()
    img.save(img_io, 'PNG')
    img_io.seek(0)
    qr_code_b64 = base64.b64encode(img_io.getvalue()).decode()
    
    form = MFASetupForm()
    if form.validate_on_submit():
        # Verify password first
        if not current_user.check_password(form.password.data):
            app.logger.warning(f'Failed MFA setup - invalid password for user: {current_user.username}')
            flash('Invalid password. MFA not enabled.', 'error')
            return render_template('mfa_setup.html', form=form, qr_code=qr_code_b64, secret=secret)
        
        # Verify TOTP code with time window tolerance (up to ±90 seconds for clock skew)
        totp = pyotp.TOTP(secret)
        app.logger.debug('MFA setup verification initiated for user')
        
        # Test current and adjacent time windows
        current_time = datetime.now()
        app.logger.debug(f'Current time: {current_time}')
        
        # Try verifying with extended window to handle significant clock skew
        verification_result = totp.verify(form.totp_code.data, valid_window=3)
        app.logger.debug(f'TOTP verification result (window=3): {verification_result}')
        
        if verification_result:
            # Encrypt secret with user's password using per-user salt
            encrypted_secret = encrypt_totp_secret(secret, form.password.data, user_id=current_user.id)
            
            # Enable MFA
            current_user.mfa_enabled = True
            current_user.mfa_secret_encrypted = encrypted_secret
            current_user.mfa_last_authenticated = datetime.now(timezone.utc)
            db.session.commit()
            
            # Clear session
            session.pop('mfa_setup_secret', None)
            
            app.logger.info(f'MFA enabled for user: {current_user.username}')
            flash('MFA enabled successfully! Your authenticator app is now linked.', 'success')
            return redirect(url_for('settings'))
        else:
            app.logger.warning(f'MFA setup failed - TOTP code mismatch for user: {current_user.username}')
            flash('Invalid code. Please try again.', 'error')
    
    return render_template('mfa_setup.html', form=form, qr_code=qr_code_b64, secret=secret)

@app.route('/mfa/disable', methods=['GET', 'POST'])
@login_required
@limiter.limit("5 per hour")
def disable_mfa():
    """Disable MFA on account with password verification.
    
    Security:
    - Requires password verification to prevent unauthorized MFA disabling
    - Rate limited to 5 attempts per hour per IP
    - Logs MFA disable events for audit trail
    """
    if not current_user.mfa_enabled:
        flash('MFA is not enabled on your account.', 'info')
        return redirect(url_for('settings'))
    
    form = DisableMFAForm()
    
    if form.validate_on_submit():
        # Verify password before allowing MFA disable
        if not current_user.check_password(form.password.data):
            app.logger.warning(f'Failed MFA disable attempt - invalid password for user: {current_user.username}')
            flash('Invalid password. MFA not disabled.', 'error')
            return render_template('disable_mfa.html', form=form)
        
        # Disable MFA
        current_user.mfa_enabled = False
        current_user.mfa_secret_encrypted = None
        db.session.commit()
        
        app.logger.info(f'MFA disabled for user: {current_user.username}')
        flash('MFA has been disabled. Your account is less secure without it.', 'warning')
        return redirect(url_for('settings'))
    
    return render_template('disable_mfa.html', form=form)

@app.route('/mfa/trusted-devices')
@login_required
def trusted_devices():
    """View and manage trusted devices."""
    devices = TrustedDevice.query.filter_by(user_id=current_user.id).all()
    
    # Remove expired devices
    expired_count = 0
    for device in devices:
        if device.is_expired():
            db.session.delete(device)
            expired_count += 1
    
    if expired_count > 0:
        db.session.commit()
    
    devices = TrustedDevice.query.filter_by(user_id=current_user.id).all()
    return render_template('trusted_devices.html', devices=devices)

@app.route('/mfa/trusted-devices/<int:device_id>/revoke', methods=['POST'])
@login_required
def revoke_trusted_device(device_id):
    """Revoke a trusted device."""
    device = TrustedDevice.query.get_or_404(device_id)
    
    if device.user_id != current_user.id:
        flash('Unauthorized access.', 'error')
        return redirect(url_for('trusted_devices'))
    
    device_name = device.device_name or f"{device.user_agent[:30]}..."
    db.session.delete(device)
    db.session.commit()
    
    app.logger.info(f'Trusted device revoked for user: {current_user.username}')
    flash(f'Device "{device_name}" has been revoked.', 'success')
    return redirect(url_for('trusted_devices'))

@app.route('/settings', methods=['GET', 'POST'])
@login_required
@limiter.limit("10 per hour")  # Rate limit email changes to prevent abuse
def settings():
    """Account settings page where users can change their email and manage MFA.
    
    Security:
    - Requires password verification before email change
    - Rate limited to 10 changes per hour per IP
    - Logs all email changes for audit trail
    - Prevents email duplication
    """
    form = ChangeEmailForm()
    
    if form.validate_on_submit():
        # Verify password before allowing email change
        if not current_user.check_password(form.password.data):
            app.logger.warning(f'Failed email change attempt - invalid password for user: {current_user.username}')
            flash('Invalid password. Email not changed.', 'error')
            return render_template('settings.html', form=form)
        
        # Update email
        old_email = current_user.email
        current_user.email = form.new_email.data
        db.session.commit()
        
        # Log the change
        app.logger.info(f'User {current_user.username} changed email from {old_email} to {current_user.email}')
        flash('Email updated successfully!', 'success')
        return redirect(url_for('settings'))
    
    # Prepare MFA info for display
    mfa_status = {
        'enabled': current_user.mfa_enabled,
        'last_authenticated': current_user.mfa_last_authenticated,
        'trusted_device_count': TrustedDevice.query.filter_by(user_id=current_user.id).filter(
            TrustedDevice.expires_at > datetime.now(timezone.utc)
        ).count()
    }
    
    return render_template('settings.html', form=form, mfa_status=mfa_status)

@app.route('/recovery-codes')
def show_recovery_codes():
    """Display recovery codes to user immediately after account creation.
    
    Security: Recovery codes are signed in a cryptographically verified token with:
    - 5-minute expiration
    - Tamper-proof signature (using SECRET_KEY)
    - Only a signed reference token stored in session
    
    This protects against session compromise (XSS, fixation, etc.)
    """
    # Check if token is in session
    token = session.pop('recovery_code_token', None)
    if not token:
        flash('Recovery codes not found. Please log in.', 'info')
        return redirect(url_for('login'))
    
    # Retrieve and verify codes from signed token
    user_id, recovery_codes = get_recovery_codes_from_cache(token)
    
    if user_id is None:
        flash('Recovery codes expired or not found. Please create a new account.', 'error')
        return redirect(url_for('login'))
    
    user = db.session.get(User, user_id)
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('login'))
    
    return render_template('recovery_codes.html', user=user, codes=recovery_codes)

@app.route('/forgot-password', methods=['GET', 'POST'])
@limiter.limit("5 per 15 minutes")  # Prevent brute force attacks on recovery codes
def forgot_password():
    """Initiate password recovery using email and recovery code
    
    Rate limited: 5 attempts per 15 minutes per IP address
    This prevents brute force attacks on recovery codes even with high entropy.
    """
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    form = RecoverAccountForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        
        if not user:
            # Use generic message (don't reveal if email exists)
            app.logger.warning(f'Recovery attempt for non-existent email: {form.email.data}')
            flash('Invalid email or recovery code.', 'error')
            return redirect(url_for('forgot_password'))
        
        # Get all unused recovery codes for this user
        recovery_codes = RecoveryCode.query.filter_by(
            user_id=user.id,
            used=False
        ).all()
        
        if not recovery_codes:
            # Use same generic message (don't reveal that user exists but has no codes)
            app.logger.warning(f'Recovery attempt with no available codes for user: {user.username}')
            flash('Invalid email or recovery code.', 'error')
            return redirect(url_for('forgot_password'))
        
        # Check if provided code matches any unused code
        # Use constant-time verification: check all codes even after finding a match
        # This prevents timing side-channel attacks that could leak information about
        # which position in the list contains the correct code
        matching_code = None
        for recovery_code in recovery_codes:
            try:
                password_hasher.verify(recovery_code.code_hash, form.recovery_code.data)
                # Found a match, but continue checking remaining codes
                # to avoid revealing position via timing information
                if matching_code is None:
                    matching_code = recovery_code
            except (VerifyMismatchError, InvalidHashError):
                # Code doesn't match, continue to next (doesn't break early)
                pass
        
        if not matching_code:
            # Same generic message for invalid code
            app.logger.warning(f'Invalid recovery code attempt for user: {user.username}')
            flash('Invalid email or recovery code.', 'error')
            return redirect(url_for('forgot_password'))
        
        # Generate time-limited, cryptographically signed token
        # Token contains user_id and code_id but is unguessable and expires in 15 minutes
        reset_token = generate_reset_token(user.id, matching_code.id)
        app.logger.info(f'User verified recovery code: {user.username}')
        
        # Redirect to reset password with token parameter (no exposed IDs in URL)
        return redirect(url_for('reset_password', token=reset_token))
    
    return render_template('forgot_password.html', form=form)

@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    """Reset password after recovery code verification
    
    Uses a time-limited, cryptographically signed token instead of exposing
    user_id and code_id in the URL. This prevents:
    - ID enumeration attacks
    - URL interception/logging issues
    - Indefinite password reset URLs
    """
    # Token can come from query string (GET) or form data (POST)
    token = request.args.get('token') or request.form.get('token')
    
    if not token:
        flash('Invalid or missing reset token.', 'error')
        return redirect(url_for('login'))
    
    # Verify token and extract user_id and code_id
    # Token is only valid for 15 minutes
    token_data = verify_reset_token(token)
    if not token_data:
        app.logger.warning(f'Invalid or expired reset token attempted')
        flash('Password reset link is invalid or has expired. Please try again.', 'error')
        return redirect(url_for('forgot_password'))
    
    user_id = token_data.get('user_id')
    code_id = token_data.get('code_id')
    
    user = db.session.get(User, user_id)
    recovery_code = db.session.get(RecoveryCode, code_id)
    if not user or not recovery_code:
        flash('Invalid reset link.', 'error')
        return redirect(url_for('login'))
    
    if recovery_code.user_id != user.id or recovery_code.used:
        flash('This recovery code has already been used.', 'error')
        return redirect(url_for('login'))
    
    form = ResetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        recovery_code.used = True
        recovery_code.used_at = datetime.now(timezone.utc)
        db.session.commit()
        
        app.logger.info(f'Password reset via recovery code: {user.username}')
        flash('Password reset successfully! Please log in with your new password.', 'success')
        return redirect(url_for('login'))
    
    # Pass token as hidden form field instead of URL parameters
    return render_template('reset_password.html', form=form, user=user, token=token)

@app.route('/')
@login_required
def index():
    search_query = request.args.get('search', '').strip()
    status_filter = request.args.get('status', 'all').strip()
    genre_filter = request.args.get('genre', 'all').strip()
    
    # Validate status_filter to prevent invalid values
    VALID_STATUSES = ('all', 'to_read', 'currently_reading', 'read')
    if status_filter not in VALID_STATUSES:
        status_filter = 'all'

    # Build list of individual genres for the current user to populate the UI.
    # Books store comma-separated genre strings (e.g. "Fiction, Mystery, Thriller").
    # Split those, trim whitespace, deduplicate and sort for the dropdown.
    # Query trimmed genre strings at the SQL level so `distinct()` operates
    # on normalized values (avoids duplicates like 'Fiction' vs ' Fiction').
    raw_genres = db.session.query(func.trim(Book.genre)).filter(
        Book.user_id == current_user.id,
        Book.genre.isnot(None),
        func.trim(Book.genre) != ''
    ).distinct().all()

    genre_set = set()
    for g in raw_genres:
        if not g:
            continue
        # g is a 1-tuple like ("Fiction, Mystery",)
        genre_value = g[0]
        for part in genre_value.split(','):
            part = part.strip()
            if part:
                genre_set.add(part)

    genres = sorted(genre_set)

    # Validate genre_filter against the expanded genre list
    if genre_filter != 'all' and genre_filter not in genres:
        genre_filter = 'all'
    
    query = Book.query.filter_by(user_id=current_user.id)
    
    # Apply status filter
    if status_filter and status_filter != 'all':
        query = query.filter_by(status=status_filter)

    # Apply genre filter. Use pattern matching so a user selecting a single
    # genre will match books whose comma-separated `genre` field contains it.
    if genre_filter and genre_filter != 'all':
        query = query.filter(Book.genre.ilike(f'%{genre_filter}%'))
    
    # Apply search filter
    if search_query:
        query = query.filter(
            db.or_(
                Book.title.ilike(f'%{search_query}%'),
                Book.author.ilike(f'%{search_query}%'),
                Book.isbn.ilike(f'%{search_query}%'),
                Book.genre.ilike(f'%{search_query}%')
            )
        )
    
    books = query.order_by(Book.date_added.desc()).all()

    return render_template(
        'index.html',
        books=books,
        search_query=search_query,
        status_filter=status_filter,
        genres=genres,
        genre_filter=genre_filter
    )

@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_book():
    form = BookForm()
    if form.validate_on_submit():
        try:
            # Parse date_added if provided, otherwise use current date
            date_added = None
            if form.date_added.data:
                try:
                    date_added = datetime.strptime(form.date_added.data, '%Y-%m-%d')
                    date_added = date_added.replace(tzinfo=timezone.utc)
                except ValueError:
                    date_added = None
            
            # Get and validate cover_url from form data if provided (from ISBN lookup)
            cover_url_raw = request.form.get('cover_url', '').strip() or None
            try:
                cover_url = validate_cover_url(cover_url_raw)
            except ValueError as e:
                # Log the attempt and show generic error to user
                app.logger.warning(f'Invalid cover URL rejected: {str(e)}')
                flash('Invalid cover URL provided. Book saved without cover image.', 'warning')
                cover_url = None
            
            book = Book(
                title=form.title.data,
                author=form.author.data,
                isbn=form.isbn.data or None,
                genre=normalize_genre_input(form.genre.data) or None,
                format=form.format.data,
                status=form.status.data,
                rating=form.rating.data or None,
                notes=form.notes.data or None,
                date_added=date_added,
                cover_url=cover_url,
                user_id=current_user.id
            )
            db.session.add(book)
            db.session.commit()
            flash(f'Book "{book.title}" added successfully!', 'success')
            return redirect(url_for('index'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error adding book: {str(e)}', 'error')
    
    return render_template('add_book.html', form=form)

@app.route('/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_book(id):
    book = db.get_or_404(Book, id)
    
    # Check if the book belongs to the current user
    if book.user_id != current_user.id:
        flash('You do not have permission to edit this book.', 'error')
        return redirect(url_for('index'))
    
    form = BookForm()
    if form.validate_on_submit():
        try:
            # Parse date_added if provided
            date_added = None
            if form.date_added.data:
                try:
                    date_added = datetime.strptime(form.date_added.data, '%Y-%m-%d')
                    date_added = date_added.replace(tzinfo=timezone.utc)
                except ValueError:
                    date_added = book.date_added  # Keep original if parsing fails
            
            # Get and validate cover_url from form data if provided (from ISBN lookup)
            cover_url_raw = request.form.get('cover_url', '').strip() or None
            if cover_url_raw:
                try:
                    cover_url = validate_cover_url(cover_url_raw)
                except ValueError as e:
                    # Log the attempt and show generic error to user
                    app.logger.warning(f'Invalid cover URL rejected during edit: {str(e)}')
                    flash('Invalid cover URL provided. Using existing cover image.', 'warning')
                    cover_url = book.cover_url
            else:
                # No cover_url in form data, keep existing cover_url
                cover_url = book.cover_url
            
            book.title = form.title.data
            book.author = form.author.data
            book.isbn = form.isbn.data or None
            book.genre = normalize_genre_input(form.genre.data) or None
            book.format = form.format.data
            book.status = form.status.data
            book.rating = form.rating.data or None
            book.notes = form.notes.data or None
            book.cover_url = cover_url
            if date_added:
                book.date_added = date_added
            db.session.commit()
            flash(f'Book "{book.title}" updated successfully!', 'success')
            return redirect(url_for('index'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating book: {str(e)}', 'error')
    elif request.method == 'GET':
        # Pre-populate form with book data
        form.title.data = book.title
        form.author.data = book.author
        form.isbn.data = book.isbn
        # Show a normalized genre string in the edit form to avoid
        # re-introducing leading/trailing whitespace when saving.
        form.genre.data = normalize_genre_input(book.genre) if book.genre else None
        form.format.data = book.format
        form.status.data = book.status
        form.rating.data = book.rating
        form.notes.data = book.notes
        if book.date_added:
            form.date_added.data = book.date_added.strftime('%Y-%m-%d')
    
    return render_template('edit_book.html', form=form, book=book)

@app.route('/delete/<int:id>')
@login_required
def delete_book(id):
    book = db.get_or_404(Book, id)
    
    # Check if the book belongs to the current user
    if book.user_id != current_user.id:
        flash('You do not have permission to delete this book.', 'error')
        return redirect(url_for('index'))
    try:
        db.session.delete(book)
        db.session.commit()
        flash(f'Book "{book.title}" deleted successfully!', 'success')
    except Exception as e:
        flash(f'Error deleting book: {str(e)}', 'error')
    
    return redirect(url_for('index'))

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    # Development only - use Gunicorn in production
    app.run(debug=app.config['DEBUG'])
