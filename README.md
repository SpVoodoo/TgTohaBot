# TgTohaBot

Телеграм-бот в стиле "Зек-водолаз", который считает, сколько раз за день написали ключевую фразу:

`Пахан, Тоху опять обидели`

Бот:
- ведет дневной счетчик по чатам;
- сохраняет дневной итог в SQLite;
- умеет показывать статистику за день и за месяц;
- отправляет PNG-график по дням месяца;
- генерирует смешную историю через ИИ-команду.

## Команды

- `/start` - справка
- `/help` - справка
- `/today` - сколько раз Тоху обидели сегодня
- `/month` - сколько раз Тоху обидели за текущий месяц
- `/chart` - график по дням за текущий месяц (файл PNG)
- `/story [тема]` - история от ИИ в стиле Зека-водолаза
- `/zona` - случайная зек-цитата
- `/toha` - случайный статус Тохи
- `/gazy [сек]` - запуск раунда "газы" (по умолчанию 30 сек, диапазон 10-300)
- `/mask` - отметить, что выжил в раунде "газы"

## Режим "Газы"

- `/gazy` запускает отсчет.
- Участником считается тот, кто во время раунда написал любое сообщение, или команды `/gazy`/`/mask`.
- Чтобы выжить, нужно успеть ввести `/mask` до конца таймера.
- После окончания бот публикует список выживших и умерших.
- Если в отсчете никто не участвовал, бот сообщит об этом отдельно.

## Стек

- Python 3.11+
- `python-telegram-bot` (polling + job queue)
- SQLite (`toha_counter.db`)
- `matplotlib` (графики)
- `httpx` (запросы к OpenRouter)
- `python-dotenv` (`.env`)

## Установка локально

1. Клонировать репозиторий:
```bash
git clone git@github.com:SpVoodoo/TgTohaBot.git
cd TgTohaBot
```

2. Создать виртуальное окружение и поставить зависимости:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

3. Создать `.env`:
```env
BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
BOT_TIMEZONE=Asia/Yekaterinburg
OPENROUTER_API_KEY=YOUR_OPENROUTER_KEY
STORY_MODEL=stepfun/step-3.5-flash:free
```

4. Запустить:
```bash
python main.py
```

## Деплой на Ubuntu (systemd)

### 1) Установка зависимостей
```bash
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip git
```

### 2) Клонирование и установка
```bash
cd /home/bot
git clone git@github.com:SpVoodoo/TgTohaBot.git
cd TgTohaBot
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

### 3) Настройка `.env`
```bash
nano /home/bot/TgTohaBot/.env
```

### 4) systemd unit
Создай файл `/etc/systemd/system/tgbot.service`:

```ini
[Unit]
Description=Toha Counter Telegram Bot
After=network.target

[Service]
Type=simple
User=bot
WorkingDirectory=/home/bot/TgTohaBot
ExecStart=/home/bot/TgTohaBot/.venv/bin/python /home/bot/TgTohaBot/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Применить:
```bash
systemctl daemon-reload
systemctl enable tgbot
systemctl start tgbot
systemctl status tgbot --no-pager -l
```

Логи:
```bash
journalctl -u tgbot -f
```

## Обновление бота на сервере

Если изменил код и запушил в GitHub:

```bash
cd /home/bot/TgTohaBot
git pull
source .venv/bin/activate
pip install -r requirements.txt
```

Потом перезапуск:
```bash
systemctl restart tgbot
systemctl status tgbot --no-pager -l
```

## База данных

Файл базы: `toha_counter.db` (SQLite).

Таблицы:
- `current_counts` - текущий дневной счет;
- `daily_stats` - сохраненные дневные итоги.

## Бэкап БД (рекомендуется)

```bash
mkdir -p /home/bot/TgTohaBot/backups
cp /home/bot/TgTohaBot/toha_counter.db /home/bot/TgTohaBot/backups/toha_counter_$(date +%F).db
```

## Важные замечания

- Не коммить `.env` в git.
- Если токен бота где-то засветился, перевыпусти его через `@BotFather`.
- Для работы счетчика в группах может понадобиться выключить privacy mode в `@BotFather`:
  - `Bot Settings` -> `Group Privacy` -> `Turn off`.
