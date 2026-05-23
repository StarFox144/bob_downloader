# YouTube / TikTok Downloader Bot

Простий Telegram-бот для завантаження аудіо та відео з YouTube і TikTok за допомогою `yt-dlp`.

## Встановлення

1. Створіть віртуальне середовище Python:
   ```bash
   python -m venv venv
   venv\Scripts\activate
   ```
2. Встановіть залежності:
   ```bash
   pip install -r requirements.txt
   ```
3. Встановіть змінну оточення з токеном Telegram бота:
   - Windows PowerShell:
     ```powershell
     $env:TELEGRAM_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
     ```
   - Linux / macOS:
     ```bash
     export TELEGRAM_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"
     ```

## Як створити бота в Telegram

1. Відкрийте Telegram і знайдіть акаунт @BotFather.
2. Надішліть `/newbot` та вкажіть ім'я бота.
3. Вкажіть унікальний username, що закінчується на `bot`.
4. Отримайте токен від BotFather і збережіть його у змінній `TELEGRAM_TOKEN`.

## Запуск

```bash
python bot.py
```

## Використання

- `/audio <URL>` — завантажити аудіо
- `/video <URL>` — завантажити відео
- Надішліть пряме посилання на YouTube або TikTok, і бот запропонує кнопки для вибору формату.

## Рекомендована краща структура бота

- Використовуйте `BotFather` для створення токена.
- Працюйте у віртуальному середовищі `venv`.
- Якщо хочете постійно працюючого бота, запустіть `bot.py` на сервері або VPS.
- Для більш стабільної роботи у production можна перейти на Webhook замість polling.
- Переконайтесь, що `ffmpeg` встановлено та доступно в PATH, якщо ви завантажуєте аудіо.

## Примітки

- Бот надсилає файли до 49 МБ. Якщо файл більший, він поверне помилку.
- Працює з YouTube та TikTok посиланнями, які підтримує `yt-dlp`.
- Якщо бот не може завантажити посилання, спробуйте оновити `yt-dlp` або перевірити формат URL.
