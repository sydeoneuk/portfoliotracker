# Trading 212 Portfolio Dashboard

A self-hosted portfolio tracker that syncs your [Trading 212](https://www.trading212.com) account data and presents it in a rich dashboard — positions, dividends, transactions, pie analysis and more.

![Python](https://img.shields.io/badge/Python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.11x-green)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-blue)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED)

---

## Features

- **Portfolio overview** — current positions with market value, unrealised P&L, cost basis and FX conversion to GBP
- **Pie analysis** — filter positions by T212 pie; compare holdings across pies
- **Dividends** — full dividend payment history with per-share amounts, month/account filters and a bar chart of monthly income
- **Transactions** — order history and cash transaction log
- **Analysis page** — country and sector/type donut charts with breakdowns; forward dividend yield; FCF and EPS dividend coverage
- **Metadata enrichment** — sector, industry, country and company descriptions sourced from yfinance, FMP and Claude
- **Multi-user** — each user has isolated data; supports Google and Microsoft OAuth login
- **Scheduled sync** — trigger a full sync from the Settings page; incremental updates on subsequent runs

---

## Architecture

```
Browser → Caddy (TLS) → FastAPI (Python) → PostgreSQL
                              ↕
                      Trading 212 REST API
                      yfinance / FMP / Claude (enrichment)
```

| Component | Technology |
|-----------|-----------|
| Web framework | FastAPI + Jinja2 templates |
| UI | Bootstrap 5 + Chart.js + DataTables |
| Database | PostgreSQL 16 (SQLAlchemy ORM, Alembic migrations) |
| Auth | OAuth2 via Google / Microsoft (authlib) |
| Reverse proxy | Caddy 2 (auto TLS via Let's Encrypt) |
| Infrastructure | AWS EC2 + Terraform |
| CI/CD | GitHub Actions |

---

## Running Locally with Docker Compose

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Docker + Docker Compose plugin)
- A Trading 212 API key — generate one in the T212 app under **Settings → API (Beta)**
- A Google or Microsoft OAuth app for authentication (see [OAuth setup](#oauth-setup))

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

# Session security — generate with:
# python -c "import secrets; print(secrets.token_hex(32))"
SESSION_SECRET=your-generated-secret

# Encryption key for stored API keys — generate with:
# python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
ENCRYPTION_KEY=your-generated-fernet-key

# OAuth — at least one provider required
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
# MICROSOFT_CLIENT_ID=...
# MICROSOFT_CLIENT_SECRET=...

# Optional enrichment APIs
# FMP_API_KEY=...
# ANTHROPIC_API_KEY=...
```

### 3. Start the stack

```bash
docker compose up -d
```

The app will be available at **http://localhost:8001**.

On first run, Alembic migrations run automatically and create the database schema.

### 4. Log in and configure

1. Open http://localhost:8001 and sign in with Google or Microsoft
2. Go to **Settings** and paste your Trading 212 API key
3. Click **Sync Now** — the first sync fetches your full history and may take a few minutes

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

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → **APIs & Services → Credentials**
2. Create an **OAuth 2.0 Client ID** (Web application)
3. Add authorised redirect URI: `http://localhost:8001/auth/google/callback`
4. Copy the client ID and secret into `.env`

### Microsoft

1. Go to [portal.azure.com](https://portal.azure.com) → **Azure Active Directory → App registrations**
2. New registration → add redirect URI: `http://localhost:8001/auth/microsoft/callback`
3. Create a client secret under **Certificates & secrets**
4. Copy the Application (client) ID and secret into `.env`

> When deploying to production, add your production domain callback URLs alongside the localhost ones — a single OAuth app supports multiple redirect URIs.

---

## Deploying to AWS

The `terraform/` directory contains infrastructure-as-code for deploying to AWS EC2 with a static IP and automatic HTTPS via Caddy + Let's Encrypt.

### Architecture

```
Internet → Route 53 (DNS) → Elastic IP → EC2 (t3.small)
                                              └─ Docker Compose
                                                   ├─ Caddy (ports 80/443, auto TLS)
                                                   ├─ FastAPI app
                                                   └─ PostgreSQL
```

### Prerequisites

- [Terraform](https://developer.hashicorp.com/terraform/install) ≥ 1.6
- [AWS CLI](https://aws.amazon.com/cli/) configured with an IAM Identity Center profile
- A domain with a Route 53 hosted zone
- An SSH key pair

### One-time manual setup

#### 1. AWS IAM Identity Center

1. Enable IAM Identity Center in the AWS console
2. Create a user and a permission set with `AmazonEC2FullAccess` + `AmazonRoute53FullAccess`
3. Assign the user to your account with that permission set
4. Configure the AWS CLI: `aws configure sso --profile trading212`

#### 2. Domain and Route 53

1. In Route 53, create a hosted zone for your subdomain (e.g. `t212.example.com`)
2. Copy the **Zone ID** from the console
3. At your domain registrar, add NS records for the subdomain pointing to the four Route 53 nameservers shown in the hosted zone

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

Populate with the same variables as local, plus:

```env
DOMAIN_NAME=t212.example.com
POSTGRES_PASSWORD=strong-production-password
SESSION_SECRET=<generated>
ENCRYPTION_KEY=<generated>
# OAuth, API keys etc.
```

Then start the app:

```bash
sudo systemctl start trading212
```

### GitHub Actions deployment

Every push to `main` automatically deploys via SSH. Add these repository secrets under **Settings → Secrets → Actions**:

| Secret | Value |
|--------|-------|
| `EC2_HOST` | Elastic IP from `terraform output elastic_ip` |
| `EC2_USER` | `ec2-user` |
| `EC2_SSH_KEY` | Contents of `~/.ssh/trading212_deploy` (private key) |
| `EC2_SSH_PASSPHRASE` | SSH key passphrase (if set) |

Update OAuth app registrations to add your production callback URLs:
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
| `POSTGRES_PASSWORD` | ✅ | Database password |
| `SESSION_SECRET` | ✅ | Flask/Starlette session signing key (32-byte hex) |
| `ENCRYPTION_KEY` | ✅ | Fernet key for encrypting stored API keys |
| `GOOGLE_CLIENT_ID` | One of these | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | One of these | Google OAuth client secret |
| `MICROSOFT_CLIENT_ID` | One of these | Microsoft OAuth client ID |
| `MICROSOFT_CLIENT_SECRET` | One of these | Microsoft OAuth client secret |
| `DOMAIN_NAME` | Production | Domain name, e.g. `t212.example.com` |
| `FMP_API_KEY` | Optional | Financial Modeling Prep API key (dividend enrichment) |
| `ANTHROPIC_API_KEY` | Optional | Claude API key (description enrichment fallback) |
| `POSTGRES_DB` | Optional | Database name (default: `trading212`) |
| `POSTGRES_USER` | Optional | Database user (default: `trading212`) |
| `HTTPS_ONLY` | Optional | Set `true` in production (default: `false`) |

---

## Database Migrations

Migrations run automatically on startup. To run manually:

```bash
# Local
docker compose exec web alembic upgrade head

# Production
docker compose -f docker-compose.prod.yml exec web alembic upgrade head
```

---

## License

APACHE 2.0