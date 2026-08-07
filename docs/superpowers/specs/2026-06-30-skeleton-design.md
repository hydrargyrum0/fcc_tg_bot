# FCC Telegram Bot — Skeleton Design

**Date:** 2026-06-30
**Scope:** Initial skeleton — foundation only, no feature logic yet

---

## Overview

A Telegram bot for server management (DevOps control center) targeting Turkmenistan infrastructure. The skeleton establishes the project structure, database, permission system, and team management — everything features will be built on top of.

---

## Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12 |
| Telegram framework | aiogram v3 |
| Database | PostgreSQL |
| ORM + migrations | SQLAlchemy 2.0 + Alembic |
| Session/FSM state | Redis |
| Local dev / deploy | Docker Compose |

---

## User Model

Two roles only:

| Role | Permissions |
|------|-------------|
| **Superadmin** | Create teams, add/remove members, see all teams and servers |
| **Member** | Manage own team's resources (servers, deployments, etc.) |

Rules:
- Only superadmin can create teams
- Members of different teams are isolated — they cannot see each other's data
- Superadmin can be attached to a team
- The first user to start the bot and register as superadmin gets the role (one-time setup)

---

## Database Schema

### `users`
| Column | Type | Notes |
|--------|------|-------|
| id | BIGINT PK | Telegram user ID |
| username | VARCHAR | Telegram @username |
| full_name | VARCHAR | Display name |
| role | ENUM | `superadmin` / `member` |
| created_at | TIMESTAMP | |

### `teams`
| Column | Type | Notes |
|--------|------|-------|
| id | SERIAL PK | |
| name | VARCHAR | Team name, unique |
| created_by | BIGINT FK → users.id | |
| created_at | TIMESTAMP | |

### `team_members`
| Column | Type | Notes |
|--------|------|-------|
| team_id | INT FK → teams.id | |
| user_id | BIGINT FK → users.id | |
| joined_at | TIMESTAMP | |

---

## Bot Functionality (Skeleton)

### Superadmin commands
- `/start` — register / welcome
- `/newteam <name>` — create a new team
- `/addmember <team_id> <@username or user_id>` — add user to team
- `/removemember <team_id> <user_id>` — remove user from team
- `/teams` — list all teams with member counts

### Member commands
- `/start` — register / welcome
- `/myteam` — show own team info and members

### Access control
- Every handler goes through `RoleMiddleware` — checks user role before executing
- Unregistered users get a friendly message directing them to contact superadmin

---

## Project Structure

```
fcc_tg_bot/
├── bot/
│   ├── handlers/
│   │   ├── common.py       # /start, fallback
│   │   ├── superadmin.py   # team management
│   │   └── member.py       # /myteam
│   ├── middlewares/
│   │   └── role.py         # RoleMiddleware
│   └── keyboards/
│       └── inline.py       # reusable inline keyboards
├── db/
│   ├── models/
│   │   ├── base.py
│   │   ├── user.py
│   │   ├── team.py
│   │   └── team_member.py
│   ├── session.py          # async engine + session factory
│   └── migrations/         # Alembic
├── services/
│   ├── user_service.py
│   └── team_service.py
├── config.py               # pydantic-settings, reads .env
├── main.py                 # bot startup
├── .env.example
└── docker-compose.yml
```

---

## Configuration (.env)

```
BOT_TOKEN=
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/fcc_bot
REDIS_URL=redis://localhost:6379/0
SUPERADMIN_IDS=123456789   # comma-separated Telegram IDs of superadmins
```

---

## Docker Compose (local + prod)

Services:
- `bot` — the Python application
- `postgres` — PostgreSQL 16
- `redis` — Redis 7

---

## What is NOT in scope for skeleton

- Server registration and management
- Deployments (SSH / kubectl / cloud)
- Monitoring and alerts
- Cloud provider integrations
- Celery task queue (added when async operations are needed)

These are added in subsequent iterations once the skeleton is running.

---

## Success Criteria

- [ ] Bot starts and responds to `/start`
- [ ] First superadmin can register via `.env` config
- [ ] Superadmin can create teams and add members
- [ ] Members can view their team
- [ ] Non-registered users are blocked with a message
- [ ] Runs locally via `docker compose up`
