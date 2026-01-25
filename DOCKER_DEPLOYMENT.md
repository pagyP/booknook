# Docker Production Deployment Guide

## Deployment Options

### Option A: Local Build (Development)
For local development or building images yourself:
```bash
docker compose up --build
```

### Option B: Pre-built Image from GitHub Container Registry (Production)
For production deployments using pre-built images from GHCR:
```bash
docker compose -f docker-compose.prod.yml up
```

This uses `ghcr.io/pagyp/readingnook:latest` (or specify a version tag).

---

## Quick Start

### 1. Prepare Environment
```bash
# Create production environment file
cp .env.production.example .env.production

# Edit with strong values
nano .env.production
```

Generate strong secrets:
```bash
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))"
python3 -c "import secrets; print('DB_PASSWORD=' + secrets.token_urlsafe(32))"
```

### 2. Build and Start Containers
```bash
# Build images
docker compose build

# Start services (detached)
docker compose up -d

# Check logs
docker compose logs -f web
docker compose logs -f db
```

### 3. Verify Deployment
```bash
# Check services are running
docker compose ps

# Test the app (app runs on port 8000)
curl http://localhost:8000

# Access the app through your reverse proxy
# Browser: http://your-domain.com (via nginx/caddy/etc)
```

### 4. Database Initialization
Database is automatically initialized on first run. To manually initialize:
```bash
docker compose exec web python -c "from app import app, db; app.app_context().push(); db.create_all()"
```

## Production Checklist

### Security
- [x] Strong SECRET_KEY generated
- [x] Strong DATABASE password set
- [x] SESSION_COOKIE_SECURE=True
- [x] FLASK_DEBUG=False
- [x] Non-root user in Docker
- [ ] HTTPS/SSL configured at reverse proxy level
- [ ] Reverse proxy (nginx, caddy, etc.) configured externally
- [ ] Firewall rules configured (port 8000 internal only, 80/443 at proxy)
- [ ] Regular backups of PostgreSQL database

### Monitoring & Maintenance
```bash
# View logs
docker compose logs -f

# View web app logs
docker compose logs -f web

# View database logs
docker compose logs -f db

# Backup database
docker compose exec db pg_dump -U readingnook readingnook > backup.sql

# Restore database
docker compose exec -T db psql -U readingnook readingnook < backup.sql

# Scale workers (edit docker compose.yml gunicorn command)
# --workers 8  # for high traffic

# Stop services
docker compose down

# Remove everything (careful!)
docker compose down -v
```

## GitHub Container Registry (GHCR) Images

### Available Image Tags
Images are automatically built and pushed to `ghcr.io/pagyp/readingnook` by GitHub Actions:
- `latest` - Latest build from main branch
- `main` - Current main branch (same as latest)
- `v*` - Semantic version tags (e.g., `v1.0.0`, `v1.0`, `v1`)

### Using Specific Versions
In `docker-compose.prod.yml`, specify a version:
```yaml
web:
  image: ghcr.io/pagyp/readingnook:v1.0.0  # Specific version
  # or
  image: ghcr.io/pagyp/readingnook:latest   # Always latest
```

### Image Builds
Images are built automatically on:
- **Push to main branch** → tagged as `latest` and `main`
- **Git tags** (e.g., `v1.0.0`) → tagged as `1.0.0`, `1.0`, and updated `latest`
- **Pull requests** → built but not pushed

## Reverse Proxy Configuration

The app runs on port 8000. Configure your external reverse proxy (nginx, caddy, Apache, etc.) to:

1. Listen on ports 80 (HTTP) and 443 (HTTPS)
2. Forward requests to `http://localhost:8000` or your app server IP
3. Handle SSL/TLS termination
4. Add security headers (X-Frame-Options, X-Content-Type-Options, etc.)

**Example nginx upstream block:**
```nginx
upstream readingnook {
    server localhost:8000;
}

server {
    listen 80;
    server_name your-domain.com;
    
    location / {
        proxy_pass http://readingnook;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Scaling & Performance

### Increase Workers
Edit docker compose.yml, change gunicorn command:
```bash
gunicorn --bind 0.0.0.0:8000 --workers 8 --timeout 60 ...
```

### Database Optimization
```bash
# Connect to database
docker compose exec db psql -U readingnook readingnook

# Create indexes for better performance
CREATE INDEX idx_book_user_id ON book(user_id);
CREATE INDEX idx_book_date_read ON book(date_read);
```

## Troubleshooting

### App won't start
```bash
docker compose logs web
```

### Database connection error
```bash
# Check database is healthy
docker compose ps
# Should show db as "Up"

# Test connection
docker compose exec db psql -U readingnook -c "SELECT 1"
```

### High memory usage
```bash
# Reduce workers in docker compose.yml
--workers 2

# Or limit memory in docker compose.yml:
# mem_limit: 512m
```

### Need to run migrations
```bash
docker compose exec web python migrate.py
```

## Backups

### Database Backup
```bash
# Full backup
docker compose exec db pg_dump -U readingnook readingnook > backup-$(date +%Y%m%d).sql

# Automated daily backup
# Add to crontab:
0 2 * * * docker compose -f /path/to/docker compose.yml exec -T db pg_dump -U readingnook readingnook > /backups/readingnook-$(date +\%Y\%m\%d).sql
```

### Volume Backup
```bash
# Backup PostgreSQL volume
docker run --rm -v readingnook_postgres_data:/data -v $(pwd):/backup \
  alpine tar czf /backup/postgres-backup.tar.gz /data
```

## Production URLs

- **App**: http://your-domain.com
- **Admin/Database**: Only accessible internally

## Security Notes

1. **Never commit .env.production to git** - it's in .gitignore
2. **Rotate SECRET_KEY** if compromised
3. **Keep Docker images updated** - `docker compose pull`
4. **Monitor logs** for suspicious activity
5. **Use strong passwords** for database
6. **Enable HTTPS** in production (not optional!)
7. **Set up firewall** to limit access

## Architecture

```
Client Request
     ↓
[External Reverse Proxy: nginx/caddy/etc.]
     ↓ (HTTP :8000)
[Gunicorn App Container]
     ↓
[PostgreSQL Container]
```

- **App**: Runs on port 8000 inside container (not exposed to internet)
- **Database**: Internal network, not exposed
- **HTTPS**: Handled by your external reverse proxy
- **Static files**: Served through Gunicorn (consider reverse proxy caching)
