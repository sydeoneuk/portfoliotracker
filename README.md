# Trading 212 Portfolio Dashboard

A self-hosted portfolio tracker that syncs your [Trading 212](https://www.trading212.com) data into a multi-user web app with portfolio views, pie-aware analysis, dividends, transactions, instrument enrichment, and daily portfolio history.

![Python](https://img.shields.io/badge/Python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.11x-green)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-blue)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED)

---

## Features

- **Portfolio overview** - current positions with market value, unrealised P&L, cost basis, dividend totals, and FX conversion to GBP
- **Pie-aware filtering** - filter the portfolio and analysis views by Trading 212 pie and by account (`Trading`, `ISA`, or combined)
- **Portfolio history** - each sync stores a daily portfolio snapshot; the Analysis page charts portfolio value over time and respects the current pie/account filters
- **Dividends** - dividend payment history with month/account filters, monthly income charts, and holding-level breakdowns
- **Transactions and orders** - open orders, historical orders, and cash transaction history
- **Analysis page** - portfolio total over time, geographic split, sector/type split, and dividends by holding
- **Instrument enrichment** - sector, industry, country, description, EPS, FCF/share, and OpenFIGI fields from yfinance, FMP, Claude, and OpenFIGI
- **Multi-user auth** - isolated per-user data with Google and Microsoft OAuth login
- **Encrypted credentials** - Trading 212 API credentials are stored per user and encrypted at rest
- **Scheduled background jobs** - optional automatic daily sync at `03:00 UTC`, plus nightly catalogue enrichment at `04:00 UTC`
- **Admin tools** - optional admin access for user/sync visibility and catalogue maintenance

---

## Architecture

```text
Browser -> Caddy (TLS) -> FastAPI (Python) -> PostgreSQL
                             |
                             +-> Trading 212 REST API
                             +-> yfinance
                             +-> Financial Modeling Prep
                             +-> OpenFIGI
                             +-> Anthropic Claude
```

| Component | Technology |
|-----------|------------|
| Web framework | FastAPI + Jinja2 templates |
| UI | Bootstrap 5 + Chart.js |
| Database | PostgreSQL 16 (SQLAlchemy ORM, Alembic migrations) |
| Auth | OAuth2 via Google / Microsoft (`authlib`) |
| Background jobs | APScheduler |
| Reverse proxy | Caddy 2 |
| Infrastructure | AWS EC2 + Terraform |
| CI/CD | GitHub Actions |

---

## Running Locally with Docker Compose

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Docker + Docker Compose plugin)
- A Google or Microsoft OAuth app for login
- Trading 212 API credentials for the account(s) you want to sync
  - The web app supports separate `Trading` and `ISA` credentials
  - These are entered in the app's **Settings** page after login

### 1. Clone the repository

```bash
git clone https://github.com/sydeoneuk/portfoliotracker.git
cd YOUR_REPO
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in the required values:

```env
# PostgreSQL
POSTGRES_PASSWORD=choose-a-strong-password

# Session security - generate with:
# python -c "import secrets; print(secrets.token_hex(32))"
SESSION_SECRET=your-generated-secret

# Encryption key for stored API credentials - generate with:
# python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
ENCRYPTION_KEY=your-generated-fernet-key

# OAuth - at least one provider required
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
# MICROSOFT_CLIENT_ID=...
# MICROSOFT_CLIENT_SECRET=...

# OAuth callback base URL
APP_BASE_URL=http://localhost:8001

# Optional enrichment APIs
# FMP_API_KEY=...
# OPENFIGI_API_KEY=...
# ANTHROPIC_API_KEY=...

# Optional admin users
# ADMIN_EMAILS=you@example.com

# Optional production-only toggle
# HTTPS_ONLY=false
```

Notes:

- `SESSION_SECRET` and `ENCRYPTION_KEY` are mandatory. The app refuses to start if they are left on insecure default placeholders.
- `T212_API_KEY` / `T212_API_SECRET` environment variables still exist for the legacy `scripts/sync.py` path, but the web app stores per-user Trading 212 credentials from the Settings page.

### 3. Start the stack

```bash
docker compose up -d
```

The app will be available at **http://localhost:8001**.

On first run, Alembic migrations run automatically and create the database schema.

### 4. Log in and configure

1. Open `http://localhost:8001` and sign in with Google or Microsoft
2. Go to **Settings**
3. Paste your Trading 212 API credentials for `Trading`, `ISA`, or both
4. Click **Sync Now**
5. Optionally enable **Automatic Daily Sync**

The first sync may take a few minutes while positions, pies, orders, transactions, dividends, and the first portfolio-history snapshots are populated.

### Useful local commands

```bash
# View logs
docker compose logs -f web

# Run database migrations manually
docker compose exec web alembic upgrade head

# Stop the stack
docker compose down

# Stop and remove all data (including the database volume)
docker compose down -v
```

---

## OAuth Setup

You need at least one OAuth provider configured.

### Google

1. Go to [console.cloud.google.com](https://console.cloud.google.com) -> **APIs & Services -> Credentials**
2. Create an **OAuth 2.0 Client ID** (Web application)
3. Add redirect URI: `http://localhost:8001/auth/google/callback`
4. Copy the client ID and secret into `.env`

### Microsoft

1. Go to [portal.azure.com](https://portal.azure.com) -> **Azure Active Directory -> App registrations**
2. Create a new app registration with redirect URI: `http://localhost:8001/auth/microsoft/callback`
3. Create a client secret under **Certificates & secrets**
4. Copy the client ID and secret into `.env`

When deploying to production, add your production callback URLs alongside the localhost ones.

---

## Deploying to AWS

The `terraform/` directory contains infrastructure-as-code for deploying to AWS EC2 with a static IP and automatic HTTPS via Caddy + Let's Encrypt.

### Deployment architecture

```text
Internet -> Route 53 (DNS) -> Elastic IP -> EC2 (t3.small)
                                         |
                                         +-> Docker Compose
                                             +-> Caddy (ports 80/443, auto TLS)
                                             +-> FastAPI app
                                             +-> PostgreSQL
```

### Prerequisites

- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.6
- [AWS CLI](https://aws.amazon.com/cli/) configured with an IAM Identity Center profile
- A domain with a Route 53 hosted zone
- An SSH key pair

### One-time manual setup

#### 1. AWS IAM Identity Center

1. Enable IAM Identity Center in AWS
2. Create a user and a permission set with `AmazonEC2FullAccess` and `AmazonRoute53FullAccess`
3. Assign the user to your account
4. Configure the AWS CLI: `aws configure sso --profile trading212`

#### 2. Domain and Route 53

1. Create a hosted zone for your subdomain (for example `t212.example.com`)
2. Copy the hosted zone ID
3. Point your registrar's NS records for that subdomain at the Route 53 nameservers

#### 3. SSH key pair

```bash
ssh-keygen -t ed25519 -f ~/.ssh/trading212_deploy -C "trading212-deploy"
```

#### 4. Terraform variables

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your values
```

#### 5. Provision infrastructure

```bash
aws sso login --profile trading212
terraform init
terraform apply
```

Note the `elastic_ip` from the outputs.

#### 6. Create the `.env` file on the server

```bash
ssh -i ~/.ssh/trading212_deploy ec2-user@<ELASTIC_IP>
nano /opt/trading212/.env
```

Populate it with the same variables as local, plus:

```env
DOMAIN_NAME=t212.example.com
APP_BASE_URL=https://t212.example.com
POSTGRES_PASSWORD=strong-production-password
SESSION_SECRET=<generated>
ENCRYPTION_KEY=<generated>
HTTPS_ONLY=true
# OAuth / enrichment / admin variables as needed
```

Then start the app:

```bash
sudo systemctl start trading212
```

### GitHub Actions deployment

Every push to `main` automatically deploys via SSH. Add these repository secrets under **Settings -> Secrets -> Actions**:

| Secret | Value |
|--------|-------|
| `EC2_HOST` | Elastic IP from `terraform output elastic_ip` |
| `EC2_USER` | `ec2-user` |
| `EC2_SSH_KEY` | Contents of `~/.ssh/trading212_deploy` (private key) |
| `EC2_SSH_PASSPHRASE` | SSH key passphrase (if set) |

Update OAuth app registrations to include:

- `https://t212.example.com/auth/google/callback`
- `https://t212.example.com/auth/microsoft/callback`

### Manual deploy

```bash
ssh -i ~/.ssh/trading212_deploy ec2-user@<ELASTIC_IP>
cd /opt/trading212
git pull origin main
docker compose -f docker-compose.prod.yml up -d --build
```

---

## Environment Variables Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `POSTGRES_PASSWORD` | Yes | Database password |
| `POSTGRES_DB` | Optional | Database name (default: `trading212`) |
| `POSTGRES_USER` | Optional | Database user (default: `trading212`) |
| `POSTGRES_HOST` | Optional | Database host |
| `POSTGRES_PORT` | Optional | Database port |
| `SESSION_SECRET` | Yes | Starlette session signing key |
| `ENCRYPTION_KEY` | Yes | Fernet key for encrypting stored Trading 212 credentials |
| `GOOGLE_CLIENT_ID` | One provider required | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | One provider required | Google OAuth client secret |
| `MICROSOFT_CLIENT_ID` | One provider required | Microsoft OAuth client ID |
| `MICROSOFT_CLIENT_SECRET` | One provider required | Microsoft OAuth client secret |
| `APP_BASE_URL` | Yes | Base URL used for OAuth callbacks |
| `HTTPS_ONLY` | Optional | Set `true` behind HTTPS in production |
| `ADMIN_EMAILS` | Optional | Comma-separated list of admin email addresses |
| `FMP_API_KEY` | Optional | Financial Modeling Prep key for dividend/date enrichment |
| `OPENFIGI_API_KEY` | Optional | OpenFIGI key for FIGI/MIC/security-type enrichment |
| `ANTHROPIC_API_KEY` | Optional | Claude key for description fallback |
| `DOMAIN_NAME` | Production | Domain name used by the production stack |
| `T212_API_KEY` | Legacy optional | Legacy single-user sync script credential |
| `T212_API_SECRET` | Legacy optional | Legacy single-user sync script secret |
| `T212_ISA_API_KEY` | Legacy optional | Legacy ISA sync script credential |
| `T212_ISA_API_SECRET` | Legacy optional | Legacy ISA sync script secret |

---

## Database Migrations

Migrations run automatically on startup. To run them manually:

```bash
# Local
docker compose exec web alembic upgrade head

# Production
docker compose -f docker-compose.prod.yml exec web alembic upgrade head
```

The current schema includes daily portfolio snapshots used by the Analysis page's portfolio-history chart.

---

## License

APACHE 2.0
