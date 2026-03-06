import logging
import os
import random
import re
import sqlite3
import textwrap
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from matplotlib import pyplot as plt
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

KEY_PHRASE_PATTERN = re.compile(
    r"\bпахан\b[\s,;:!?.-]*\bтоху\b[\s,;:!?.-]*\bопять\b[\s,;:!?.-]*\bобидели\b",
    re.IGNORECASE,
)
DB_PATH = Path("toha_counter.db")
DEFAULT_TIMEZONE = "Europe/Moscow"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_STORY_MODEL = "stepfun/step-3.5-flash:free"

ZEK_QUOTES = [
    "Зек-водолаз докладывает: глубина обид сегодня зашкаливает.",
    "Всплыл с зоны: Тоху опять зацепили за жабры.",
    "На дне все стабильно: Тоха в протоколе обиженных.",
    "Пахан, гидрокостюм порвался, но счетчик держу.",
    "Водолазный этап пройден, обиды Тохи учтены по форме.",
]

TOHA_STATUS = [
    "Тоха держится, но морально на мели.",
    "Тоха в норме, но обида уже на горизонте.",
    "Тоха ушел в себя, обещал вернуться с реваншем.",
    "Тоха требует адвоката и чай с сухарями.",
    "Тоха бурчит, но статистика честная.",
]


@dataclass
class CounterDB:
    db_path: Path

    def __post_init__(self) -> None:
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS current_counts (
                    chat_id INTEGER PRIMARY KEY,
                    day TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS daily_stats (
                    chat_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, day)
                );
                """
            )
            conn.commit()

    def _archive_previous_if_needed(self, conn: sqlite3.Connection, chat_id: int, today: str) -> None:
        row = conn.execute(
            "SELECT day, count FROM current_counts WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        if row is None:
            return

        if row["day"] == today:
            return

        conn.execute(
            """
            INSERT INTO daily_stats(chat_id, day, count)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id, day) DO UPDATE SET
            count = excluded.count
            """,
            (chat_id, row["day"], row["count"]),
        )
        conn.execute(
            "UPDATE current_counts SET day = ?, count = 0 WHERE chat_id = ?",
            (today, chat_id),
        )

    def add_occurrences(self, chat_id: int, occurrences: int, today: str) -> int:
        with closing(self._connect()) as conn:
            self._archive_previous_if_needed(conn, chat_id, today)
            conn.execute(
                """
                INSERT INTO current_counts(chat_id, day, count)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                day = excluded.day,
                count = current_counts.count + excluded.count
                """,
                (chat_id, today, occurrences),
            )
            conn.commit()
            row = conn.execute(
                "SELECT count FROM current_counts WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            return int(row["count"]) if row else occurrences

    def get_today_count(self, chat_id: int, today: str) -> int:
        with closing(self._connect()) as conn:
            self._archive_previous_if_needed(conn, chat_id, today)
            row = conn.execute(
                "SELECT count FROM current_counts WHERE chat_id = ? AND day = ?",
                (chat_id, today),
            ).fetchone()
            conn.commit()
            return int(row["count"]) if row else 0

    def get_month_total(self, chat_id: int, day_in_month: date, today_iso: str) -> int:
        month_start = day_in_month.replace(day=1).isoformat()
        month_end = (
            (day_in_month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        ).isoformat()
        with closing(self._connect()) as conn:
            self._archive_previous_if_needed(conn, chat_id, today_iso)
            committed = conn.execute(
                """
                SELECT COALESCE(SUM(count), 0) AS total
                FROM daily_stats
                WHERE chat_id = ? AND day BETWEEN ? AND ?
                """,
                (chat_id, month_start, month_end),
            ).fetchone()["total"]
            current = conn.execute(
                "SELECT count FROM current_counts WHERE chat_id = ? AND day = ?",
                (chat_id, today_iso),
            ).fetchone()
            conn.commit()
            return int(committed) + (int(current["count"]) if current else 0)

    def get_daily_points(self, chat_id: int, start_day: date, end_day: date, today_iso: str) -> list[tuple[date, int]]:
        with closing(self._connect()) as conn:
            self._archive_previous_if_needed(conn, chat_id, today_iso)
            stats_rows = conn.execute(
                """
                SELECT day, count
                FROM daily_stats
                WHERE chat_id = ? AND day BETWEEN ? AND ?
                ORDER BY day
                """,
                (chat_id, start_day.isoformat(), end_day.isoformat()),
            ).fetchall()
            current = conn.execute(
                "SELECT day, count FROM current_counts WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            conn.commit()

        points: dict[date, int] = {}
        for row in stats_rows:
            points[date.fromisoformat(row["day"])] = int(row["count"])
        if current:
            current_day = date.fromisoformat(current["day"])
            if start_day <= current_day <= end_day:
                points[current_day] = int(current["count"])

        out: list[tuple[date, int]] = []
        cursor = start_day
        while cursor <= end_day:
            out.append((cursor, points.get(cursor, 0)))
            cursor += timedelta(days=1)
        return out

    def flush_all_previous_days(self, today: str) -> None:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT chat_id, day, count FROM current_counts").fetchall()
            for row in rows:
                if row["day"] >= today:
                    continue
                conn.execute(
                    """
                    INSERT INTO daily_stats(chat_id, day, count)
                    VALUES (?, ?, ?)
                    ON CONFLICT(chat_id, day) DO UPDATE SET
                    count = excluded.count
                    """,
                    (row["chat_id"], row["day"], row["count"]),
                )
                conn.execute(
                    "UPDATE current_counts SET day = ?, count = 0 WHERE chat_id = ?",
                    (today, row["chat_id"]),
                )
            conn.commit()


def now_local(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


def month_bounds(d: date) -> tuple[date, date]:
    start = d.replace(day=1)
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    end = next_month - timedelta(days=1)
    return start, end


def make_month_chart(points: list[tuple[date, int]], tz_name: str) -> BytesIO:
    x = [p[0].day for p in points]
    y = [p[1] for p in points]

    fig, ax = plt.subplots(figsize=(10, 4.5), dpi=130)
    ax.plot(x, y, color="#1f6d4f", linewidth=2.2, marker="o", markersize=4)
    ax.fill_between(x, y, color="#66c2a4", alpha=0.25)
    ax.set_title("Сколько раз обидели Тоху по дням", fontsize=13, fontweight="bold")
    ax.set_xlabel("День месяца")
    ax.set_ylabel("Количество")
    ax.grid(alpha=0.25, linestyle="--")
    ax.set_xticks(x[:: max(1, len(x) // 12)])
    ax.text(
        0.99,
        0.02,
        f"Часовой пояс: {tz_name}",
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=8,
        alpha=0.65,
    )
    fig.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    buf.name = "toha_month_chart.png"
    return buf


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Зек-водолаз на связи.\n"
        "Слежу за фразой: «Пахан, Тоху опять обидели».\n\n"
        "Команды:\n"
        "/today - сколько раз за сегодня\n"
        "/month - сколько раз за месяц\n"
        "/chart - график по дням за текущий месяц\n"
        "/story [тема] - история от ИИ\n"
        "/zona - случайная мудрость с глубин\n"
        "/toha - состояние Тохи\n"
        "/help - подсказка"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: CounterDB = context.bot_data["db"]
    tz: ZoneInfo = context.bot_data["tz"]
    today_iso = now_local(tz).date().isoformat()
    count = db.get_today_count(update.effective_chat.id, today_iso)
    await update.message.reply_text(f"За сегодня Тоху обидели {count} раз(а).")


async def month_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: CounterDB = context.bot_data["db"]
    tz: ZoneInfo = context.bot_data["tz"]
    now_dt = now_local(tz)
    total = db.get_month_total(update.effective_chat.id, now_dt.date(), now_dt.date().isoformat())
    await update.message.reply_text(f"За {now_dt.strftime('%m.%Y')} Тоху обидели {total} раз(а).")


async def chart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: CounterDB = context.bot_data["db"]
    tz: ZoneInfo = context.bot_data["tz"]
    today = now_local(tz).date()
    start_day, end_day = month_bounds(today)
    points = db.get_daily_points(update.effective_chat.id, start_day, end_day, today.isoformat())
    image = make_month_chart(points, str(tz))
    await update.message.reply_document(
        document=image,
        filename="toha_month_chart.png",
        caption="График обид Тохи за текущий месяц.",
    )


async def zona_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(random.choice(ZEK_QUOTES))


async def toha_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(random.choice(TOHA_STATUS))


async def story_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    api_key: str | None = context.bot_data.get("openrouter_api_key")
    model: str = context.bot_data.get("story_model", DEFAULT_STORY_MODEL)

    if not api_key:
        await update.message.reply_text("OPENROUTER_API_KEY не задан в .env")
        return

    topic = " ".join(context.args).strip() if context.args else "как Тоху опять обидели на глубине"
    prompt = textwrap.dedent(
        f"""
        Напиши короткую смешную историю (5-8 предложений) от лица "Зека-водолаза".
        Стиль: дворовый юмор, без жести.
        Обязательно упомяни Тоху и тему "его опять обидели".
        Тема: {topic}
        """
    ).strip()

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Ты пишешь короткие юмористические истории на русском языке.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.9,
        "max_tokens": 500,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=40) as client:
            response = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        await update.message.reply_text(
            f"Сервис истории временно недоступен (HTTP {exc.response.status_code})."
        )
        return
    except Exception:
        await update.message.reply_text("Не получилось получить историю. Попробуй позже.")
        return

    try:
        choice = data["choices"][0]
        story = choice["message"]["content"].strip()
        finish_reason = choice.get("finish_reason")
    except (KeyError, IndexError, TypeError):
        await update.message.reply_text("ИИ вернул пустой или непонятный ответ.")
        return

    # If provider truncated response, request continuation and finish the story.
    if finish_reason == "length" or not re.search(r'[.!?…"]\s*$', story):
        continuation_payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "Ты пишешь короткие юмористические истории на русском языке.",
                },
                {
                    "role": "user",
                    "content": (
                        "Продолжи и закончи историю без повтора начала, 2-4 предложения:\n\n"
                        f"{story}"
                    ),
                },
            ],
            "temperature": 0.8,
            "max_tokens": 220,
        }
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                cont_response = await client.post(
                    OPENROUTER_URL, json=continuation_payload, headers=headers
                )
                cont_response.raise_for_status()
                cont_data = cont_response.json()
            continuation = cont_data["choices"][0]["message"]["content"].strip()
            if continuation:
                story = f"{story}\n\n{continuation}"
        except Exception:
            pass

    if len(story) > 3900:
        story = story[:3900] + "..."
    await update.message.reply_text(story)


async def track_phrase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    raw_text = update.message.text or update.message.caption
    if raw_text is None:
        return

    normalized_text = raw_text.lower().replace("ё", "е")
    matches = KEY_PHRASE_PATTERN.findall(normalized_text)
    if not matches:
        return

    db: CounterDB = context.bot_data["db"]
    tz: ZoneInfo = context.bot_data["tz"]
    today_iso = now_local(tz).date().isoformat()
    new_count = db.add_occurrences(update.effective_chat.id, len(matches), today_iso)
    await update.message.reply_text(
        f"Зафиксировано: +{len(matches)}.\n"
        f"Текущий дневной счет: {new_count}."
    )


async def daily_rollover(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: CounterDB = context.bot_data["db"]
    tz: ZoneInfo = context.bot_data["tz"]
    today_iso = now_local(tz).date().isoformat()
    db.flush_all_previous_days(today_iso)
    logging.info("Daily rollover completed for %s", today_iso)


def main() -> None:
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не найден BOT_TOKEN в .env")

    tz_name = os.getenv("BOT_TIMEZONE", DEFAULT_TIMEZONE)
    tz = ZoneInfo(tz_name)
    openrouter_api_key = os.getenv("OPENROUTER_API_KEY")
    story_model = os.getenv("STORY_MODEL", DEFAULT_STORY_MODEL)

    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    db = CounterDB(DB_PATH)

    application = Application.builder().token(token).build()
    application.bot_data["db"] = db
    application.bot_data["tz"] = tz
    application.bot_data["openrouter_api_key"] = openrouter_api_key
    application.bot_data["story_model"] = story_model

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("today", today_cmd))
    application.add_handler(CommandHandler("month", month_cmd))
    application.add_handler(CommandHandler("chart", chart_cmd))
    application.add_handler(CommandHandler("story", story_cmd))
    application.add_handler(CommandHandler("zona", zona_cmd))
    application.add_handler(CommandHandler("toha", toha_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, track_phrase))
    application.add_handler(MessageHandler(filters.CAPTION & ~filters.COMMAND, track_phrase))

    application.job_queue.run_daily(
        daily_rollover,
        time=time(hour=0, minute=0, second=5, tzinfo=tz),
        name="daily_rollover",
    )

    logging.info("Bot started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
