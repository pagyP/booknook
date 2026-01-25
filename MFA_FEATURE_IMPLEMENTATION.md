# Multi-Factor Authentication (MFA) Feature Implementation

## Overview

The Reading Nook app now includes **optional Multi-Factor Authentication (MFA)** using Time-based One-Time Passwords (TOTP). Users can enable MFA to secure their accounts with an authenticator app (Google Authenticator, Authy, Microsoft Authenticator, etc.).

## Key Features

- ✅ **TOTP-based MFA**: Use authenticator apps for 6-digit codes
- ✅ **Device Trust**: Mark devices as "trusted" for 30 days to reduce MFA friction
- ✅ **Recovery Code Integration**: Use existing recovery codes to regain access if MFA app is lost
- ✅ **Encrypted TOTP Secrets**: Secrets are encrypted in the database using your password
- ✅ **Optional**: Users can choose to enable or disable MFA anytime
- ✅ **Zero Cookies**: Device trust uses stateless fingerprinting (User-Agent + IP hash)

## What's New

### Database Models
- **User Model** - Added MFA fields:
  - `mfa_enabled` (Boolean): Whether MFA is active
  - `mfa_secret_encrypted` (String): Encrypted TOTP secret
  - `mfa_last_authenticated` (DateTime): Last successful MFA verification

- **TrustedDevice Model** - New model for managing trusted devices:
  - Device fingerprinting (SHA256 hash of User-Agent + IP)
  - Optional user-provided device names
  - 30-day expiration with automatic cleanup
  - User can revoke devices anytime

### Forms
- **MFASetupForm**: TOTP code (6 digits) + password verification
- **MFAVerifyForm**: TOTP code OR recovery code + device trust checkbox

### Routes
- `GET/POST /mfa/setup` - Display QR code and enable MFA
- `POST /mfa/disable` - Disable MFA on account
- `GET /mfa/trusted-devices` - View and manage trusted devices
- `POST /mfa/trusted-devices/<id>/revoke` - Revoke individual device
- `Enhanced /login` - Support for TOTP/recovery code verification
- `Enhanced /settings` - Display MFA status and management options

### How It Works

#### Enabling MFA
1. User goes to Settings → Multi-Factor Authentication → Enable MFA
2. App generates a TOTP secret and displays a QR code
3. User scans QR code with authenticator app
4. User enters 6-digit code from app + their password to verify
5. MFA is enabled
6. **Important Note**: Recovery codes are generated and displayed once during initial account registration (not during MFA setup). Users should have saved them securely as they cannot be viewed again. These codes can be used for MFA bypass if the authenticator app is lost.

#### Logging In with MFA
1. User enters email + password
2. App checks if device is already trusted (skip MFA if yes)
3. If device not trusted:
   - User enters 6-digit code from authenticator app
   - OR enters a recovery code if they lost their app
   - Optional: Check "Trust this device for 30 days"
4. Login complete

#### Device Trust
- Fingerprint: SHA256 hash of (User-Agent + IP address)
- Expires after 30 days
- If IP changes on mobile/VPN, user will need to re-verify once
- Users can manually revoke devices from settings

#### Using Recovery Codes During MFA
- If user loses their authenticator app, they can enter a recovery code
- Recovery code immediately disables MFA
- User sees warning: "Recovery code used. MFA has been disabled. Please set up MFA again to secure your account."
- User must re-enable MFA after recovery

### Security Implementation

#### TOTP Secret Encryption
- Uses AES-128 in CBC mode with HMAC-SHA256 authentication (Fernet)
- Key derived from user's password using PBKDF2HMAC:
  - 480,000 iterations (OWASP recommended)
  - SHA256 hashing algorithm
- Even if database is compromised, attacker needs password to access TOTP secret

#### Password Protection
- MFA setup requires password verification
- Ensures only account owner can enable MFA
- MFA disable requires POST (prevents accidental clicks)

#### Rate Limiting
- `/mfa/setup`: 10 per hour
- `/mfa/disable`: 5 per hour
- `/login` with MFA: 5 per minute (existing limit)

#### Audit Logging
- All MFA actions logged (enable, disable, device trust, etc.)
- Tracks which user performed action and when

## Database Migration

### Fresh Database Setup (New Deployments)

For **new deployments with no existing data**:
- `db.create_all()` in `init_db.py` automatically creates the `user` table with MFA columns
- `TrustedDevice` table is created automatically
- No manual steps needed

### Existing Database Setup (Migration Required)

⚠️ **Important**: `db.create_all()` **only creates tables that don't exist**. It **does NOT alter existing tables** to add new columns.

For **existing deployments with data**, you **MUST** manually add the MFA columns using one of these approaches:

#### Option 1: Zero-Downtime Migration (Recommended for Production)

Add the columns to the existing PostgreSQL database without losing data or downtime:

```bash
docker exec readingnook_db psql -U readingnook -d readingnook -c "
ALTER TABLE \"user\" ADD COLUMN mfa_enabled BOOLEAN DEFAULT FALSE;
ALTER TABLE \"user\" ADD COLUMN mfa_secret_encrypted VARCHAR(255);
ALTER TABLE \"user\" ADD COLUMN mfa_last_authenticated TIMESTAMP;
"
```

Then create the `trusted_device` table:

```bash
docker exec readingnook_db psql -U readingnook -d readingnook << 'EOF'
CREATE TABLE trusted_device (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES "user"(id),
    device_fingerprint VARCHAR(255) NOT NULL,
    device_name VARCHAR(255),
    user_agent VARCHAR(500),
    ip_address VARCHAR(45),
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP NOT NULL,
    last_used_at TIMESTAMP
);

CREATE INDEX idx_user_fingerprint ON trusted_device(user_id, device_fingerprint);
EOF
```

#### Option 2: Clean Restart (if downtime is acceptable)

If you prefer a fresh start without data:

```bash
docker compose down -v
docker compose up
```

This removes the old database and creates a new one with correct schema. **All previous data will be lost.** Only use this if you don't need to preserve existing user accounts or book data.

### Error If Migration Not Done

If you don't run the migration, the app will fail with:

```
psycopg.errors.UndefinedColumn: column "user".mfa_enabled does not exist
```

This error occurs when users try to log in, as the login route attempts to access `user.mfa_enabled`.

## Testing MFA Locally

### Prerequisites
- Fresh Docker container (or migrated database)
- Authenticator app installed (Google Authenticator, Authy, etc.)

### Steps

1. **Register a new account**
   - Go to http://localhost:5000/register
   - Create user with email and password
   - Save recovery codes

2. **Enable MFA**
   - Log in to your account
   - Go to Settings
   - Click "Enable MFA"
   - Scan QR code with authenticator app
   - Enter 6-digit code from app + your password
   - Verify and save settings

3. **Test MFA on Login**
   - Log out
   - Log in with email + password
   - Should see MFA verification prompt
   - Enter 6-digit code from authenticator
   - Option to trust device
   - Should log in successfully

4. **Test Device Trust**
   - Log out again
   - Log in with same email + password
   - Should skip MFA (device is trusted for 30 days)
   - Verify in Settings → Trusted Devices

5. **Test Recovery Code Usage**
   - Go to Settings → Trusted Devices
   - Click "Revoke All Devices" or manually revoke from device list
   - Log out
   - Log in with email + password
   - Use recovery code instead of TOTP (codes saved from registration)
   - Should log in and see warning about MFA disabled
   - MFA should be disabled in Settings

6. **Disable MFA (Optional)**
   - If you want to test disabling MFA:
   - Go to Settings
   - Click "Disable MFA" in the MFA section
   - Enter your password to confirm
   - Future logins won't require MFA

## Architecture Notes

### Why No Cookies for Device Trust?

This implementation avoids browser cookies entirely:
- Device fingerprint is calculated fresh on each login
- Stateless design works with load-balanced deployments
- No session management complexity
- Trade-off: IP changes will reset trust (acceptable for personal app)

### Why Encrypt TOTP Secrets?

- Database could be compromised via SQL injection
- Password hash alone isn't enough for TOTP decryption
- Using plaintext password + PBKDF2HMAC provides strong encryption
- Key derivation ensures same password always derives same key
- Prevents TOTP bypass even if both password and TOTP secret are stolen

### Recovery Code Design

- Reuses existing recovery codes (no duplicate system)
- Using recovery code disables MFA (prevents account lockout)
- Users must re-enable MFA after recovery
- Encourages security-conscious behavior

## Known Limitations

- IP-based device fingerprinting: Users on mobile/VPN may see IP changes and lose device trust
- TOTP requires manual entry: No push notification or one-click verification
- Recovery codes are one-time use: After using one, it's consumed

## Future Enhancements

- [ ] Push-based MFA (e.g., Duo Security)
- [ ] SMS fallback for TOTP
- [ ] WebAuthn/FIDO2 support
- [ ] Backup codes regeneration
- [ ] Trusted device duration customization
