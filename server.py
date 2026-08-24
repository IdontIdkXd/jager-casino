# server.py
#
# Простой бэкенд для JAGER Mini App: настоящая регистрация (имя + пароль) и
# админ-панель, которая видит ВСЕХ игроков со всех устройств — то, что просил
# Илья. Без этого сервера мини-апп физически не может видеть чужие данные,
# потому что HTML-файл на телефоне пользователя не имеет доступа к чужим
# localStorage — это ограничение браузера, а не мини-аппа.
#
# ЧТО ЭТО ДАЁТ:
#   - Игрок один раз вводит имя + пароль в приложении -> получает токен.
#   - Каждые несколько секунд и при переходе в игру приложение шлёт "heartbeat":
#     кто сейчас онлайн и в какую игру играет прямо сейчас.
#   - После каждого раунда приложение синхронизирует баланс и статистику на сервер.
#   - Админ-панель внутри мини-аппа (и страница /admin в браузере) читает эти
#     данные с сервера — значит ты видишь ВСЕХ игроков, кто онлайн, кто во что
#     играет, у кого сколько побед, и топ по балансу — в реальном времени.
#
# ЗАПУСК ЛОКАЛЬНО (для проверки):
#   pip install fastapi uvicorn --break-system-packages
#   python server.py
#   Откроется на http://localhost:8000  (админка: http://localhost:8000/admin)
#
# ЗАПУСК В БОЮ (чтобы Mini App из Telegram мог достучаться):
#   Тебе нужен обычный хостинг с публичным HTTPS-адресом (Telegram требует HTTPS
#   для Mini App). Варианты попроще: Railway, Render, VPS + Caddy/nginx.
#   Дальше просто пропиши этот адрес в API_BASE_URL внутри HTML-файла мини-аппа.
#
# ВАЖНО: пароль игрока хранится хэшированным (не в открытом виде).
# ВАЖНО: поменяй ADMIN_PASSWORD ниже перед запуском в бою.

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ==================== НАСТРОЙКИ — ПРАВЬ ПОД СЕБЯ ====================

ADMIN_PASSWORD = os.environ.get("JAGER_ADMIN_PASSWORD", "jager-admin-2026")  # <-- обязательно поменяй
DB_PATH = Path(__file__).parent / "jager.db"
ONLINE_THRESHOLD_SEC = 90  # игрок считается "онлайн", если heartbeat был меньше 90 сек назад

app = FastAPI(title="JAGER backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # мини-апп открывается из Telegram WebView — origin разный, поэтому *
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== БАЗА ДАННЫХ ====================


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            tg_id TEXT,
            balance INTEGER NOT NULL DEFAULT 10000,
            games INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            total_win INTEGER NOT NULL DEFAULT 0,
            total_lose INTEGER NOT NULL DEFAULT 0,
            total_wagered INTEGER NOT NULL DEFAULT 0,
            best_win INTEGER NOT NULL DEFAULT 0,
            activity TEXT NOT NULL DEFAULT 'В меню',
            last_seen REAL NOT NULL DEFAULT 0,
            banned_until REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


init_db()


def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


PASSWORD_SALT = "jager-static-salt-v1"  # для демо достаточно; в бою лучше уникальную соль на пользователя


# ==================== МОДЕЛИ ЗАПРОСОВ ====================


class RegisterIn(BaseModel):
    name: str
    password: str
    tg_id: Optional[str] = None


class ActivityIn(BaseModel):
    token: str
    activity: str


class SyncIn(BaseModel):
    token: str
    balance: int
    games: int = 0
    wins: int = 0
    losses: int = 0
    total_win: int = 0
    total_lose: int = 0
    total_wagered: int = 0
    best_win: int = 0


def find_by_token(conn: sqlite3.Connection, token: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM players WHERE token = ?", (token,)).fetchone()


# ==================== РЕГИСТРАЦИЯ / ВХОД ====================
# Один и тот же эндпоинт: если имя уже занято — проверяем пароль и логиним,
# если имени ещё нет — создаём нового игрока. Так проще для мини-регистрации
# "имя + пароль", которую просил Илья.


@app.post("/api/register")
def register(body: RegisterIn):
    name = body.name.strip()
    if not (2 <= len(name) <= 24):
        raise HTTPException(400, "Имя должно быть от 2 до 24 символов")
    if not (4 <= len(body.password) <= 64):
        raise HTTPException(400, "Пароль должен быть от 4 до 64 символов")

    conn = get_db()
    row = conn.execute("SELECT * FROM players WHERE name = ?", (name,)).fetchone()
    pw_hash = hash_password(body.password, PASSWORD_SALT)

    if row:
        if row["password_hash"] != pw_hash:
            conn.close()
            raise HTTPException(401, "Неверный пароль для этого имени")
        banned, note = _ban_status(row)
        conn.close()
        if banned:
            raise HTTPException(403, f"Доступ заблокирован: {note}")
        return _player_payload(row)

    token = secrets.token_hex(20)
    conn.execute(
        "INSERT INTO players (name, password_hash, token, tg_id, balance, activity, last_seen, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (name, pw_hash, token, body.tg_id, 10000, "В меню", time.time(), time.time()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM players WHERE token = ?", (token,)).fetchone()
    conn.close()
    return _player_payload(row)


def _ban_status(row: sqlite3.Row) -> tuple[bool, str]:
    until = row["banned_until"]
    if until == -1:
        return True, "забанен навсегда"
    if until and until > time.time():
        minutes_left = int((until - time.time()) / 60) + 1
        return True, f"забанен ещё на {minutes_left} мин."
    return False, ""


def _player_payload(row: sqlite3.Row) -> dict:
    return {
        "token": row["token"],
        "name": row["name"],
        "balance": row["balance"],
        "games": row["games"],
        "wins": row["wins"],
        "losses": row["losses"],
        "total_win": row["total_win"],
        "total_lose": row["total_lose"],
        "total_wagered": row["total_wagered"],
        "best_win": row["best_win"],
    }


# ==================== HEARTBEAT / АКТИВНОСТЬ ====================


@app.post("/api/activity")
def set_activity(body: ActivityIn):
    conn = get_db()
    row = find_by_token(conn, body.token)
    if not row:
        conn.close()
        raise HTTPException(401, "Неизвестный токен, нужна повторная регистрация/вход")
    conn.execute(
        "UPDATE players SET activity = ?, last_seen = ? WHERE token = ?",
        (body.activity[:64], time.time(), body.token),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


# ==================== СИНХРОНИЗАЦИЯ БАЛАНСА / СТАТИСТИКИ ====================


@app.post("/api/sync")
def sync(body: SyncIn):
    conn = get_db()
    row = find_by_token(conn, body.token)
    if not row:
        conn.close()
        raise HTTPException(401, "Неизвестный токен, нужна повторная регистрация/вход")
    conn.execute(
        """UPDATE players SET balance=?, games=?, wins=?, losses=?, total_win=?, total_lose=?,
           total_wagered=?, best_win=?, last_seen=? WHERE token=?""",
        (
            body.balance, body.games, body.wins, body.losses, body.total_win,
            body.total_lose, body.total_wagered, body.best_win, time.time(), body.token,
        ),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


# ==================== АДМИНКА (API) ====================


def _check_admin(password: str) -> None:
    if password != ADMIN_PASSWORD:
        raise HTTPException(401, "Неверный пароль администратора")


@app.get("/api/admin/players")
def admin_players(password: str = Query(...), sort: str = Query("recent")):
    _check_admin(password)
    conn = get_db()
    rows = conn.execute("SELECT * FROM players").fetchall()
    conn.close()
    now = time.time()
    players = []
    for r in rows:
        online = (now - r["last_seen"]) < ONLINE_THRESHOLD_SEC if r["last_seen"] else False
        banned, ban_note = _ban_status(r)
        players.append({
            "name": r["name"],
            "tg_id": r["tg_id"],
            "balance": r["balance"],
            "games": r["games"],
            "wins": r["wins"],
            "losses": r["losses"],
            "total_wagered": r["total_wagered"],
            "best_win": r["best_win"],
            "activity": r["activity"] if online else "Не в сети",
            "online": online,
            "last_seen": r["last_seen"],
            "banned": banned,
            "ban_note": ban_note,
        })
    if sort == "balance":
        players.sort(key=lambda p: p["balance"], reverse=True)
    elif sort == "games":
        players.sort(key=lambda p: p["games"], reverse=True)
    else:
        players.sort(key=lambda p: p["last_seen"], reverse=True)
    return {
        "total_players": len(players),
        "online_now": sum(1 for p in players if p["online"]),
        "total_games": sum(p["games"] for p in players),
        "total_wagered": sum(p["total_wagered"] for p in players),
        "players": players,
    }


class AdminGiveIn(BaseModel):
    password: str
    name: str
    amount: int


@app.post("/api/admin/give")
def admin_give(body: AdminGiveIn):
    _check_admin(body.password)
    conn = get_db()
    row = conn.execute("SELECT * FROM players WHERE name = ?", (body.name,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Игрок не найден")
    new_balance = max(0, row["balance"] + body.amount)
    conn.execute("UPDATE players SET balance = ? WHERE name = ?", (new_balance, body.name))
    conn.commit()
    conn.close()
    return {"ok": True, "balance": new_balance}


class AdminBanIn(BaseModel):
    password: str
    name: str
    minutes: int  # 0 = навсегда, -1 = снять бан


@app.post("/api/admin/ban")
def admin_ban(body: AdminBanIn):
    _check_admin(body.password)
    conn = get_db()
    row = conn.execute("SELECT * FROM players WHERE name = ?", (body.name,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Игрок не найден")
    until = 0 if body.minutes == -1 else (-1 if body.minutes == 0 else time.time() + body.minutes * 60)
    conn.execute("UPDATE players SET banned_until = ? WHERE name = ?", (until, body.name))
    conn.commit()
    conn.close()
    return {"ok": True}


# ==================== ПРОСТАЯ HTML-СТРАНИЦА АДМИНКИ ====================
# Отдельная от мини-аппа страница — удобно открыть прямо в браузере на компьютере
# или на телефоне вне Telegram, если нужно быстро глянуть, что происходит.

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JAGER — админ</title>
<style>
  body{font-family:sans-serif;background:#0a0716;color:#f4f0ff;margin:0;padding:16px;}
  h1{font-size:18px;}
  input{background:#1e1440;border:1px solid #2c1f57;color:#fff;padding:8px;border-radius:8px;margin-bottom:10px;width:220px;}
  button{background:#8b5cf6;color:#fff;border:none;padding:8px 14px;border-radius:8px;cursor:pointer;}
  table{width:100%;border-collapse:collapse;margin-top:14px;font-size:13px;}
  th,td{padding:6px 8px;border-bottom:1px solid #2c1f57;text-align:left;}
  th{color:#9186b8;font-size:11px;text-transform:uppercase;}
  .online{color:#3ddc84;}
  .kpis{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap;}
  .kpi{background:#170f30;border:1px solid #2c1f57;border-radius:10px;padding:10px 14px;}
  .kpi b{display:block;font-size:16px;}
</style></head>
<body>
  <h1>⚙️ JAGER — Админ-панель (реальные данные со всех устройств)</h1>
  <div>
    <input id="pw" type="password" placeholder="Пароль администратора">
    <button onclick="load()">Войти</button>
  </div>
  <div class="kpis" id="kpis"></div>
  <table id="tbl"><thead><tr>
    <th>Игрок</th><th>Сейчас делает</th><th>Баланс</th><th>Игр</th><th>Побед</th><th>Ставки</th><th>Статус</th>
  </tr></thead><tbody id="tbody"></tbody></table>
<script>
async function load(){
  const pw = document.getElementById('pw').value;
  const res = await fetch('/api/admin/players?password='+encodeURIComponent(pw)+'&sort=recent');
  if(!res.ok){ alert('Неверный пароль'); return; }
  const data = await res.json();
  document.getElementById('kpis').innerHTML =
    kpi('Игроков всего', data.total_players) + kpi('Онлайн сейчас', data.online_now) +
    kpi('Игр сыграно', data.total_games) + kpi('Сумма ставок', data.total_wagered);
  document.getElementById('tbody').innerHTML = data.players.map(p =>
    '<tr><td>'+(p.online?'<span class="online">●</span> ':'○ ')+p.name+'</td>'+
    '<td>'+p.activity+'</td><td>◆'+p.balance+'</td><td>'+p.games+'</td><td>'+p.wins+'</td>'+
    '<td>◆'+p.total_wagered+'</td><td>'+(p.banned?('🚫 '+p.ban_note):'✅')+'</td></tr>'
  ).join('');
  setTimeout(load, 5000); // авто-обновление раз в 5 сек
}
function kpi(label, val){ return '<div class="kpi"><span>'+label+'</span><b>'+val+'</b></div>'; }
</script>
</body></html>
"""


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return ADMIN_HTML


@app.get("/")
def root():
    return {"status": "ok", "service": "jager-backend"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
