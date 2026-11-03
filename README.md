# Video Downloader Bot

Telegram-бот принимает ссылки на **общедоступные** видео TikTok, YouTube и Pinterest. Supabase Edge Function сохраняет запросы в таблицу `video_bot_requests`; этот GitHub Actions worker периодически загружает видео и отправляет его пользователю в Telegram.

Скачивайте только видео, которые вы имеете право сохранять. Бот не обходит приватность, DRM или другие ограничения доступа.

## Как это работает

1. Пользователь отправляет ссылку боту.
2. Supabase записывает запрос со статусом `queued`.
3. GitHub Actions запускает worker каждые 5 минут (также можно запустить вручную).
4. Worker обрабатывает до 3 запросов за запуск и отправляет MP4 в тот же Telegram-чат.
5. Статус запроса становится `sent` или `failed`.

**Ограничение:** для отправки этим способом файл должен быть не больше 49 МБ. Большие файлы получают понятное сообщение об ошибке. Запуск по расписанию GitHub Actions может задерживаться.

## Настройка секретов GitHub

Открой репозиторий → **Settings → Secrets and variables → Actions → New repository secret**. Добавь три секрета:

| Имя секрета | Значение |
|---|---|
| `SUPABASE_URL` | URL проекта Supabase |
| `SUPABASE_SERVICE_ROLE_KEY` | серверный ключ Supabase (`service_role` / secret key), не публикуй его |
| `TELEGRAM_BOT_TOKEN` | токен Telegram-бота |

Значения уже добавленных секретов Supabase можно скопировать из **Supabase → Project Settings → Edge Functions → Secrets**. Сам токен и ключ никуда не отправляй и не добавляй в код.

## Запуск и проверка

- Вкладка **Actions** → **Process video requests** → **Run workflow** — ручной запуск.
- После успешного запуска отправь боту ссылку на общедоступное видео, которое разрешено скачивать.
- Worker проверяет очередь раз в 5 минут. Запросы можно посмотреть в таблице `public.video_bot_requests`.

## Локальный запуск (необязательно)

Нужны Python 3.12 и FFmpeg. Установи зависимости:

```bash
python -m pip install -r requirements.txt
```

Задай переменные окружения `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` и `TELEGRAM_BOT_TOKEN`, затем выполни:

```bash
python worker.py
```
