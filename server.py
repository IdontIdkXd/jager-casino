import os
import time
import sqlite3
import secrets
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import uvicorn

app = FastAPI()

# CORS для всех (для Telegram WebApp и сайта)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- НАСТРОЙКИ ---
ADMIN_PASSWORD = "jager-admin-2026" # Смени на свой!
START_BALANCE = 5000
DB_NAME = "jager_database.db"

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS players (
            name TEXT PRIMARY KEY,
            password TEXT,
            tg_id TEXT,
            balance INTEGER DEFAULT 0,
            games INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            total_win INTEGER DEFAULT 0,
            total_lose INTEGER DEFAULT 0,
            total_wagered INTEGER DEFAULT 0,
            best_win INTEGER DEFAULT 0,
            last_seen REAL DEFAULT 0,
            activity TEXT DEFAULT 'В меню',
            banned INTEGER DEFAULT 0,
            pending_credits INTEGER DEFAULT 0
        )
    ''')
    # Миграция: добавляем колонку, если её нет
    try:
        cursor.execute("ALTER TABLE players ADD COLUMN pending_credits INTEGER DEFAULT 0")
        conn.commit()
    except:
        pass
    conn.close()

init_db()

# --- МОДЕЛИ ---
class RegisterRequest(BaseModel):
    name: str
    password: str
    tg_id: Optional[str] = None

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

class AdminAuth(BaseModel):
    password: str

class GiveRequest(BaseModel):
    admin_token: str # Используем токен вместо пароля в теле для безопасности
    name: str
    amount: int

class BanRequest(BaseModel):
    admin_token: str
    name: str
    minutes: int

# Хранилище админ-сессий
admin_sessions = {}

def verify_admin(token: str):
    exp = admin_sessions.get(token)
    if not exp or exp < time.time():
        return False
    return True

# --- API ИГРОКА ---

@app.post("/api/register")
async def register(req: RegisterRequest):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM players WHERE name = ?", (req.name,))
    player = cursor.fetchone()
    
    if player:
        if player["password"] != req.password:
            conn.close()
            raise HTTPException(status_code=401, detail="Неверный пароль")
        conn.close()
        return {"token": player["name"], "name": player["name"], "balance": player["balance"]}
    
    cursor.execute('''
        INSERT INTO players (name, password, tg_id, balance, last_seen)
        VALUES (?, ?, ?, ?, ?)
    ''', (req.name, req.password, req.tg_id, START_BALANCE, time.time()))
    conn.commit()
    conn.close()
    return {"token": req.name, "name": req.name, "balance": START_BALANCE}

@app.post("/api/sync")
async def sync(req: SyncRequest):
    conn = get_db()
    cursor = conn.cursor()
    
    # Получаем текущие pending_credits (начисления админа)
    cursor.execute("SELECT pending_credits, balance FROM players WHERE name = ?", (req.token,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Игрок не найден")
        
    pending = row["pending_credits"] or 0
    server_balance = row["balance"]
    
    # Новый баланс = то что прислал клиент + то что начислил админ
    # Но мы не хотим перезаписывать серверный баланс клиентским, если клиент "отстал"
    # Лучшая стратегия для казино: Сервер хранит правду. Клиент присылает изменения игр.
    # Для упрощения: принимаем баланс клиента, но ПЛЮСУЕМ pending_credits сверху.
    new_balance = req.balance + pending
    
    cursor.execute('''
        UPDATE players SET 
            balance = ?,
            games = MAX(games, ?),
            wins = MAX(wins, ?),
            losses = MAX(losses, ?),
            total_win = MAX(total_win, ?),
            total_lose = MAX(total_lose, ?),
            total_wagered = MAX(total_wagered, ?),
            best_win = MAX(best_win, ?),
            pending_credits = 0,
            last_seen = ?
        WHERE name = ?
    ''', (new_balance, req.games, req.wins, req.losses,
          req.total_win, req.total_lose, req.total_wagered,
          req.best_win, time.time(), req.token))
    
    conn.commit()
    conn.close()
    return {"status": "ok", "balance": new_balance, "credited": pending}

@app.post("/api/activity")
async def activity(req: dict):
    token = req.get("token")
    act = req.get("activity")
    if token:
        conn = get_db()
        conn.execute("UPDATE players SET activity = ?, last_seen = ? WHERE name = ?", (act, time.time(), token))
        conn.commit()
        conn.close()
    return {"status": "ok"}

# --- АДМИНКА ---

@app.post("/api/admin/login")
async def admin_login(req: AdminAuth):
    if req.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Неверный пароль")
    token = secrets.token_urlsafe(32)
    admin_sessions[token] = time.time() + 3600 * 24 # 24 часа
    return {"token": token}

@app.get("/api/admin/players")
async def get_players(admin_token: str, sort: str = "recent"):
    if not verify_admin(admin_token):
        raise HTTPException(status_code=403, detail="Не авторизован")
        
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM players")
    rows = cursor.fetchall()
    
    plist = []
    total_games = 0
    total_wagered = 0
    
    for row in rows:
        is_online = (time.time() - row["last_seen"]) < 120
        p = {
            "name": row["name"],
            "balance": row["balance"],
            "games": row["games"],
            "wins": row["wins"],
            "online": is_online,
            "activity": row["activity"],
            "banned": bool(row["banned"])
        }
        plist.append(p)
        total_games += row["games"]
        total_wagered += row["total_wagered"]
    
    if sort == "balance": plist.sort(key=lambda x: x["balance"], reverse=True)
    elif sort == "games": plist.sort(key=lambda x: x["games"], reverse=True)
        
    conn.close()
    return {
        "players": plist,
        "total_players": len(plist),
        "online_now": sum(1 for p in plist if p["online"]),
        "total_games": total_games,
        "total_wagered": total_wagered
    }

@app.post("/api/admin/give")
async def admin_give(req: GiveRequest):
    if not verify_admin(req.admin_token):
        raise HTTPException(status_code=403, detail="Не авторизован")
        
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT pending_credits FROM players WHERE name = ?", (req.name,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Игрок не найден")
    
    # Добавляем в pending, чтобы игрок получил при следующем sync
    new_pending = (row["pending_credits"] or 0) + req.amount
    cursor.execute("UPDATE players SET pending_credits = ? WHERE name = ?", (new_pending, req.name))
    conn.commit()
    conn.close()
    
    return {"status": "ok", "message": "Фишки будут начислены при следующей синхронизации игрока"}

@app.post("/api/admin/ban")
async def admin_ban(req: BanRequest):
    if not verify_admin(req.admin_token):
        raise HTTPException(status_code=403, detail="Не авторизован")
    conn = get_db()
    conn.execute("UPDATE players SET banned = 1 WHERE name = ?", (req.name,))
    conn.commit()
    conn.close()
    return {"status": "banned"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)