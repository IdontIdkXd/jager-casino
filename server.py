from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import uvicorn
import time
import sqlite3
import threading
from fastapi.middleware.cors import CORSMiddleware
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"], 
    allow_headers=["*"], 
)
# --- НАСТРОЙКИ ---
ADMIN_PASSWORD = "jager-admin-2026"
START_BALANCE = 5000
DB_NAME = "jager_database.db"

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row # Чтобы обращаться к колонкам по имени
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    # Создаем таблицу игроков, если её нет
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
            banned INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

# Инициализируем базу при старте
init_db()

# --- МОДЕЛИ ЗАПРОСОВ ---
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

class GiveRequest(AdminAuth):
    name: str
    amount: int

class BanRequest(AdminAuth):
    name: str
    minutes: int

# --- API ДЛЯ ИГРЫ ---

@app.post("/api/register")
async def register(req: RegisterRequest):
    conn = get_db()
    cursor = conn.cursor()
    
    # Проверяем, есть ли игрок
    cursor.execute("SELECT * FROM players WHERE name = ?", (req.name,))
    player = cursor.fetchone()
    
    if player:
        # Игрок существует - просто возвращаем данные
        conn.close()
        return {
            "token": player["name"], 
            "name": player["name"], 
            "balance": player["balance"]
        }
    
    # Создаем нового игрока в базе
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
    
    # Обновляем данные игрока в базе
    # Используем MAX, чтобы не потерять прогресс, если запросы придут не по порядку
    cursor.execute('''
        UPDATE players SET 
            balance = MAX(balance, ?),
            games = MAX(games, ?),
            wins = MAX(wins, ?),
            losses = MAX(losses, ?),
            total_win = MAX(total_win, ?),
            total_lose = MAX(total_lose, ?),
            total_wagered = MAX(total_wagered, ?),
            best_win = MAX(best_win, ?),
            last_seen = ?
        WHERE name = ?
    ''', (
        req.balance, req.games, req.wins, req.losses,
        req.total_win, req.total_lose, req.total_wagered,
        req.best_win, time.time(), req.token
    ))
    
    conn.commit()
    conn.close()
    return {"status": "ok"}

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

# --- АДМИН ПАНЕЛЬ API ---

@app.get("/api/admin/players")
async def get_players(password: str, sort: str = "recent"):
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Неверный пароль")
        
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Получаем всех игроков
    cursor.execute("SELECT * FROM players")
    rows = cursor.fetchall()
    
    plist = []
    total_games = 0
    total_wagered = 0
    
    for row in rows:
        is_online = (time.time() - row["last_seen"]) < 120 # Онлайн если был активен 2 мин назад
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
    
    # Сортировка
    if sort == "balance":
        plist.sort(key=lambda x: x["balance"], reverse=True)
    elif sort == "games":
        plist.sort(key=lambda x: x["games"], reverse=True)
        
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
    if req.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Неверный пароль")
        
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM players WHERE name = ?", (req.name,))
    player = cursor.fetchone()
    
    if not player:
        conn.close()
        raise HTTPException(status_code=404, detail="Игрок не найден")
    
    new_balance = player["balance"] + req.amount
    cursor.execute("UPDATE players SET balance = ? WHERE name = ?", (new_balance, req.name))
    conn.commit()
    conn.close()
    
    return {"status": "ok", "balance": new_balance}

@app.post("/api/admin/ban")
async def admin_ban(req: BanRequest):
    if req.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Неверный пароль")
        
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE players SET banned = 1 WHERE name = ?", (req.name,))
    conn.commit()
    conn.close()
    
    return {"status": "banned"}

if __name__ == "__main__":
    print("🚀 Сервер запускается... База данных: jager_database.db")
    uvicorn.run(app, host="0.0.0.0", port=8000)