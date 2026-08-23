import os
from pathlib import Path
from dotenv import load_dotenv
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)
import hashlib
import secrets
import time
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

# --- КОНФИГУРАЦИЯ ---
# Строку подключения берём из переменной окружения DATABASE_URL (её зададим в Render)
DATABASE_URL=os.environ.get("DATABASE_URL")
ADMIN_PASSWORD = "jager-admin-2026"
APP_SALT = "jager_super_secret_salt_2026_change_this_in_prod"

if not DATABASE_URL:
    raise RuntimeError("❌ Переменная окружения DATABASE_URL не задана! Добавь её в настройках Render.")

app = FastAPI(title="JAGER Casino API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://jagerbot.netlify.app",
        "http://localhost:3000",
        "http://localhost:5500",
        "https://jagernaytbot-casino-koli.onrender.com"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=3600,
)

# --- МОДЕЛИ ---
class RegisterRequest(BaseModel):
    name: str
    password: str
    tg_id: Optional[str] = None

class ActivityRequest(BaseModel):
    token: str
    activity: str

class SyncRequest(BaseModel):
    token: str
    balance: int
    games: int
    wins: int
    losses: int
    total_win: int
    total_lose: int
    total_wagered: int
    best_win: int

class AdminGiveRequest(BaseModel):
    password: str
    name: str
    amount: int

class AdminBanRequest(BaseModel):
    password: str
    name: str
    minutes: int

# --- РАБОТА С БД ---
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    """Создаёт таблицу users, если её ещё нет"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            tg_id TEXT UNIQUE,
            name TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            token TEXT,
            balance INTEGER DEFAULT 5000,
            games INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            total_win INTEGER DEFAULT 0,
            total_lose INTEGER DEFAULT 0,
            total_wagered INTEGER DEFAULT 0,
            best_win INTEGER DEFAULT 0,
            last_activity INTEGER DEFAULT 0,
            activity_text TEXT DEFAULT 'В меню',
            banned_until INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    cur.close()
    conn.close()

def hash_password(password: str) -> str:
    return hashlib.sha256((password + APP_SALT).encode()).hexdigest()

@app.on_event("startup")
def startup_event():
    init_db()
    print("✅ PostgreSQL подключена. Таблица users проверена/создана.")

# --- ЭНДПОИНТЫ ---

@app.post("/api/register")
def register(req: RegisterRequest):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE name = %s", (req.name,))
    if cur.fetchone():
        cur.close(); conn.close()
        raise HTTPException(status_code=400, detail="Имя уже занято")

    token = secrets.token_urlsafe(32)
    pwd_hash = hash_password(req.password)
    cur.execute('''
        INSERT INTO users (tg_id, name, password_hash, token, balance)
        VALUES (%s, %s, %s, %s, 5000)
        RETURNING *
    ''', (req.tg_id, req.name, pwd_hash, token))
    user = cur.fetchone()
    conn.commit()
    cur.close(); conn.close()

    return {
        "token": user["token"], "name": user["name"], "balance": user["balance"],
        "games": user["games"], "wins": user["wins"], "losses": user["losses"],
        "total_win": user["total_win"], "total_lose": user["total_lose"],
        "total_wagered": user["total_wagered"], "best_win": user["best_win"]
    }

@app.post("/api/activity")
def activity(req: ActivityRequest):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET last_activity = %s, activity_text = %s WHERE token = %s",
        (int(time.time()), req.activity, req.token)
    )
    conn.commit()
    cur.close(); conn.close()
    return {"status": "ok"}

@app.post("/api/sync")
def sync(req: SyncRequest):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        UPDATE users SET
            balance=%s, games=%s, wins=%s, losses=%s,
            total_win=%s, total_lose=%s, total_wagered=%s, best_win=%s,
            last_activity=%s
        WHERE token=%s
    ''', (req.balance, req.games, req.wins, req.losses, req.total_win,
          req.total_lose, req.total_wagered, req.best_win, int(time.time()), req.token))
    conn.commit()
    cur.close(); conn.close()
    return {"status": "ok"}

@app.get("/api/admin/players")
def get_players(password: str, sort: str = "recent"):
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Неверный пароль админа")

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(id) AS c, COALESCE(SUM(games),0) AS g, COALESCE(SUM(total_wagered),0) AS w FROM users")
    totals = cur.fetchone()
    total_players = totals["c"] or 0
    total_games = totals["g"] or 0
    total_wagered = totals["w"] or 0

    now = int(time.time())
    cur.execute("SELECT COUNT(id) AS c FROM users WHERE last_activity > %s", (now - 90,))
    online_now = cur.fetchone()["c"] or 0

    order_by = "last_activity DESC"
    if sort == "balance": order_by = "balance DESC"
    elif sort == "games": order_by = "games DESC"

    cur.execute(f"SELECT name, balance, games, wins, last_activity, activity_text, banned_until FROM users ORDER BY {order_by} LIMIT 100")
    rows = cur.fetchall()
    cur.close(); conn.close()

    players = []
    for row in rows:
        is_banned = row["banned_until"] > now
        ban_note = ""
        if is_banned:
            if row["banned_until"] > now + 31536000:
                ban_note = "Навсегда"
            else:
                ban_note = f"До {time.strftime('%d.%m %H:%M', time.localtime(row['banned_until']))}"
        players.append({
            "name": row["name"],
            "online": row["last_activity"] > now - 90,
            "activity": row["activity_text"],
            "balance": row["balance"],
            "games": row["games"],
            "wins": row["wins"],
            "banned": is_banned,
            "ban_note": ban_note
        })

    return {
        "total_players": total_players, "online_now": online_now,
        "total_games": total_games, "total_wagered": total_wagered,
        "players": players
    }

@app.post("/api/admin/give")
def admin_give(req: AdminGiveRequest):
    if req.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Неверный пароль админа")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT balance FROM users WHERE name = %s", (req.name,))
    user = cur.fetchone()
    if not user:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="Игрок не найден")
    new_balance = user["balance"] + req.amount
    cur.execute("UPDATE users SET balance = %s WHERE name = %s", (new_balance, req.name))
    conn.commit()
    cur.close(); conn.close()
    return {"status": "ok", "balance": new_balance}

@app.post("/api/admin/ban")
def admin_ban(req: AdminBanRequest):
    if req.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Неверный пароль админа")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE name = %s", (req.name,))
    if not cur.fetchone():
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="Игрок не найден")
    ban_until = int(time.time()) + (req.minutes * 60) if req.minutes > 0 else 9999999999
    cur.execute("UPDATE users SET banned_until = %s WHERE name = %s", (ban_until, req.name))
    conn.commit()
    cur.close(); conn.close()
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))