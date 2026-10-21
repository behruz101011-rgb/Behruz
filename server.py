import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, WebAppInfo
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DEV_MODE = os.getenv("DEV_MODE") == "1"
DATABASE_URL = os.getenv("DATABASE_URL", "")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MAX_ENERGY = 200
REGEN_PER_SEC = 1.0
MAX_TAPS_PER_REQUEST = 100
MAX_MULTITAP_LEVEL = 10
INIT_DATA_MAX_AGE = 24 * 3600

WEBAPP_URL = (os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
WEBHOOK_SECRET = hashlib.sha256(("wh:" + BOT_TOKEN).encode()).hexdigest()[:32]
WEBHOOK_PATH = "/webhook/" + WEBHOOK_SECRET

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()


@dp.message(CommandStart())
async def start_cmd(message: Message):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🎮 O'ynash", web_app=WebAppInfo(url=WEBAPP_URL))]]
    )
    await message.answer(
        f"Salom, {message.from_user.first_name}! 👋\nTangani bosing va coin yig'ing!",
        reply_markup=kb,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    if bot and WEBAPP_URL.startswith("https://"):
        await bot.set_webhook(WEBAPP_URL + WEBHOOK_PATH, secret_token=WEBHOOK_SECRET)
    yield
    if bot:
        await bot.session.close()
    pool.close()


app = FastAPI(lifespan=lifespan)


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str = Header(default="")):
    if not bot or not hmac.compare_digest(x_telegram_bot_api_secret_token, WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}


if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment o'zgaruvchisi o'rnatilmagan")

pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=5,
    max_lifetime=300,
    kwargs={"row_factory": dict_row, "prepare_threshold": None},
    check=ConnectionPool.check_connection,
    open=True,
)


def init_db():
    with pool.connection() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id BIGINT PRIMARY KEY,
                first_name TEXT,
                coins BIGINT NOT NULL DEFAULT 0,
                energy DOUBLE PRECISION NOT NULL,
                energy_ts DOUBLE PRECISION NOT NULL,
                multitap INTEGER NOT NULL DEFAULT 0,
                created_at DOUBLE PRECISION NOT NULL
            )"""
        )


init_db()


def verify_init_data(init_data: str) -> dict:
    if DEV_MODE and init_data == "dev":
        return {"id": 1, "first_name": "Dev"}
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash")
        check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received_hash):
            raise ValueError("bad hash")
        if time.time() - int(pairs["auth_date"]) > INIT_DATA_MAX_AGE:
            raise ValueError("expired")
        return json.loads(pairs["user"])
    except Exception:
        raise HTTPException(status_code=401, detail="Avtorizatsiya xatosi")


def multitap_cost(level: int) -> int:
    return 500 * (2 ** level)


def load_user(conn, tg_user: dict):
    now = time.time()
    conn.execute(
        "INSERT INTO users (id, first_name, coins, energy, energy_ts, multitap, created_at)"
        " VALUES (%s, %s, 0, %s, %s, 0, %s) ON CONFLICT (id) DO NOTHING",
        (tg_user["id"], tg_user.get("first_name", ""), MAX_ENERGY, now, now),
    )
    row = conn.execute("SELECT * FROM users WHERE id=%s FOR UPDATE", (tg_user["id"],)).fetchone()
    energy = min(MAX_ENERGY, row["energy"] + (now - row["energy_ts"]) * REGEN_PER_SEC)
    return row, energy, now


def state(row, coins: int, energy: float, multitap: int) -> dict:
    return {
        "name": row["first_name"],
        "coins": coins,
        "energy": int(energy),
        "max_energy": MAX_ENERGY,
        "regen_per_sec": REGEN_PER_SEC,
        "tap_value": 1 + multitap,
        "multitap_level": multitap,
        "multitap_cost": multitap_cost(multitap) if multitap < MAX_MULTITAP_LEVEL else None,
    }


class TapBody(BaseModel):
    taps: int


@app.get("/api/me")
def me(x_init_data: str = Header(default="")):
    tg_user = verify_init_data(x_init_data)
    with pool.connection() as conn:
        row, energy, now = load_user(conn, tg_user)
        conn.execute(
            "UPDATE users SET energy=%s, energy_ts=%s WHERE id=%s", (energy, now, row["id"])
        )
        return state(row, row["coins"], energy, row["multitap"])


@app.post("/api/tap")
def tap(body: TapBody, x_init_data: str = Header(default="")):
    tg_user = verify_init_data(x_init_data)
    with pool.connection() as conn:
        row, energy, now = load_user(conn, tg_user)
        taps = max(0, min(body.taps, MAX_TAPS_PER_REQUEST, int(energy)))
        coins = row["coins"] + taps * (1 + row["multitap"])
        energy -= taps
        conn.execute(
            "UPDATE users SET coins=%s, energy=%s, energy_ts=%s WHERE id=%s",
            (coins, energy, now, row["id"]),
        )
        return state(row, coins, energy, row["multitap"])


@app.post("/api/boost/multitap")
def buy_multitap(x_init_data: str = Header(default="")):
    tg_user = verify_init_data(x_init_data)
    with pool.connection() as conn:
        row, energy, now = load_user(conn, tg_user)
        level = row["multitap"]
        if level >= MAX_MULTITAP_LEVEL:
            raise HTTPException(400, "Maksimal darajaga yetgansiz")
        cost = multitap_cost(level)
        if row["coins"] < cost:
            raise HTTPException(400, "Coin yetarli emas")
        coins = row["coins"] - cost
        conn.execute(
            "UPDATE users SET coins=%s, multitap=%s, energy=%s, energy_ts=%s WHERE id=%s",
            (coins, level + 1, energy, now, row["id"]),
        )
        return state(row, coins, energy, level + 1)


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))
