# Account Recovery Using Recovery Codes

## Overview

Reading Nook supports account recovery using **recovery codes**. This is a password-less recovery method that doesn't require email access or external services. Each code is single-use and cryptographically secure.

## How It Works

### Registration
1. When you create a new account, 8 recovery codes are automatically generated
2. Recovery codes appear on a single-use page immediately after registration
3. You must save these codes in a secure location (password manager, encrypted note, printed copy, etc.)
4. **Important**: These codes are only shown once. If you lose them, you'll need all 8 to be regenerated

### Password Recovery
1. Go to the login page and click "Recover your account"
2. Enter your email address and one of your saved recovery codes
3. If valid, you'll be able to create a new password
4. The recovery code is marked as used and cannot be reused

### Code Format
- Recovery codes use format: `XXXX-XXXX-XXXX` (14 alphanumeric characters: A-Z and 2-7, separated by hyphens)
- Example: `ADR6-SVTG-FH5E`
- All 8 codes are valid for recovery - use any one you have

## Security Details

### Code Generation
```python
# 8 codes generated per user during registration
# Each code: 12 base32 characters (A-Z, 2-7) formatted as XXXX-XXXX-XXXX
# Entropy: 32^12 ≈ 1.2 * 10^18 combinations (exceeds NIST 2^50 minimum)
# Generation: secrets.token_bytes(9) for cryptographic randomness
```

### Code Storage
- Recovery codes are **hashed using Argon2** before storage (same as passwords)
- Plain-text codes are only shown once after registration
- Hashes cannot be reversed to retrieve original codes
- Even database breaches cannot expose usable codes

### Code Verification
- When you attempt recovery, your entered code is hashed and compared against stored hashes
- If hash matches, the recovery code is marked as `used=True`
- Used codes cannot be reused
- All authentication logs include recovery attempts (for security monitoring)

### Single-Use Enforcement
```python
# After successful recovery:
recovery_code.used = True
recovery_code.used_at = datetime.now(timezone.utc)
```

## Database Schema

```sql
CREATE TABLE recovery_code (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL FOREIGN KEY,
    code_hash VARCHAR(255) NOT NULL,  -- Argon2 hash
    used BOOLEAN DEFAULT FALSE,
    created_at DATETIME,
    used_at DATETIME
);
```

## Where to Store Recovery Codes

**Recommended:**
1. **Password Manager** (Bitwarden, 1Password, KeePass, Dashlane, etc.)
   - Encrypted, searchable, backed up, accessible from anywhere
   - Best option for most users

2. **Encrypted Notes**
   - Apple Notes with password protection
   - Google Keep (not end-to-end encrypted)
   - Joplin with E2E encryption
   - OneNote with encryption

3. **Printed Copy**
   - Physical backup in a safe location
   - Safe deposit box, home safe, etc.
   - Write down with clear labeling

**NOT Recommended:**
- Unencrypted text files on your computer
- Email drafts or forwarded messages
- Public cloud storage (Dropbox, Google Drive without encryption)
- Sticky notes or unsecured locations

## Regenerating Recovery Codes

Currently, recovery codes cannot be regenerated through the UI. To regenerate:

1. Contact support with verification
2. Or reset password using existing recovery code, then the new password generates new codes on next session

(Feature for self-service regeneration coming in future updates)

## API Routes

### View Recovery Codes (Post-Registration)
```
GET /recovery-codes
  - Displays recovery codes immediately after registration
  - Codes stored in session (single-use, then cleared)
```

### Initiate Recovery
```
GET /forgot-password
  - Display recovery form (email + recovery code)

POST /forgot-password
  - Verify email and recovery code
  - Generates secure, time-limited reset token (cryptographically signed)
  - Redirects to password reset with token parameter
  - Returns error if code is invalid/used
  - Rate limited to 5 attempts per 15 minutes per IP
```

### Reset Password (Secure Token-Based)
```
GET /reset-password?token=<signed_token>
  - Display password reset form (after code verification)
  - Token is time-limited (15 minutes) and cryptographically signed
  - Token contains encrypted user ID and code ID (unguessable)

POST /reset-password
  - Accepts token in form data or URL parameter
  - Set new password
  - Mark recovery code as used (single-use enforcement)
  - Redirect to login
  - Returns error if token is invalid or expired
```

### Security Features
- **Unguessable Tokens**: Uses `URLSafeTimedSerializer` for cryptographic signing
- **Time-Limited**: Tokens expire after 15 minutes
- **No Predictable IDs**: User ID and code ID are encrypted within the token
- **Single-Use Enforcement**: Recovery code marked as used after reset
- **Integrity Verification**: Token signature prevents tampering
- **No Sensitive Data in Logs**: IDs not exposed in browser history or server logs

## Test Coverage

Recovery code feature includes 6 comprehensive tests:

1. **test_recovery_codes_generated_on_registration**
   - Verifies codes are auto-generated after signup

2. **test_recovery_codes_stored_in_database**
   - Verifies codes are hashed and stored securely
   - Confirms 8 codes per user

3. **test_forgot_password_page_loads**
   - Verifies recovery form is accessible

4. **test_password_reset_with_valid_recovery_code**
   - Tests complete recovery workflow

5. **test_invalid_recovery_code_rejected**
   - Verifies invalid codes are rejected

6. **test_recovery_code_must_match_email**
   - Verifies codes only work for their associated account

All tests pass with 100% pass rate.

## Security Logging

Recovery attempts are logged for security auditing:

```
INFO:    Recovery code verified: {username}
WARNING: Invalid recovery code attempt for user: {username}
WARNING: Recovery attempt with no available codes for user: {username}
WARNING: Recovery attempt for non-existent email: {email}
```

No passwords or plain-text codes are ever logged.

## Future Enhancements

Possible improvements (not yet implemented):

1. **Self-Service Code Regeneration**
   - Users can regenerate 8 new codes via UI
   - After password change, offer option to generate new codes

2. **Backup Codes Display**
   - Show codes on account settings page
   - Allow download as encrypted PDF
   - Display count of unused codes

3. **Recovery Code Export**
   - Export codes with encryption
   - QR code display for backup apps

4. **Email Notification**
   - Notify user when recovery code is used
   - Alert if multiple failed recovery attempts

5. **Rate Limiting**
   - Limit recovery attempts per email
   - Prevent brute force attacks on recovery codes

## Troubleshooting

### "Invalid recovery code"
- Verify code format is exactly: `XXXX-XXXX-XXXX` (14 alphanumeric characters)
- Check that you're using the correct email address
- Recovery codes are case-sensitive: use exact case
- If code was already used, it cannot be reused

### "No available recovery codes"
- All 8 codes have been used
- Contact support to regenerate codes
- (Self-service regeneration coming soon)

### Lost all recovery codes
- Contact support with account verification
- Admin can regenerate codes
- Implement additional verification methods (identity verification)

## Compliance

- **GDPR**: Recovery codes stored in user's account database
- **PCI DSS**: No payment card data involved
- **HIPAA**: If health data stored, recovery process complies with audit requirements
- **SOC 2**: Secure code generation, hashing, and audit logging

## References

- [OWASP: Account Recovery Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Account_Recovery_Cheat_Sheet.html)
- [Argon2 Password Hashing](https://argon2-cffi.readthedocs.io/)
- [Python secrets module (cryptographic random)](https://docs.python.org/3/library/secrets.html)
