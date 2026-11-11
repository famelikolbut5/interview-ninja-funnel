# Interview Ninja Funnel

Telegram-бот для регистрации на вебинар Interview Ninja. Mini App показывает старты, регистрации, конверсию и отправки.

`/start` → приветствие → контакт → два поста через 10 и 60 секунд от регистрации.

aiogram 3 · FastAPI · PostgreSQL 18 · Docker Compose · Loguru

## Запуск

Нужен запущенный Docker Desktop.

```powershell
git clone https://github.com/famelikolbut5/interview-ninja-funnel.git
cd interview-ninja-funnel
Copy-Item .env.example .env
```

Заполните `BOT_TOKEN`, числовой `ADMIN_USER_ID` и два разных пароля `POSTGRES_PASSWORD`, `APP_DB_PASSWORD`.

Запустите HTTPS-туннель и скопируйте адрес из его журнала в `MINI_APP_URL`.

```powershell
docker compose --profile tunnel up -d tunnel
docker compose logs tunnel
```

Запустите приложение.

```powershell
docker compose up --build -d
```

Откройте бота, отправьте `/start` и нажмите "Поделиться номером". Статистика доступна владельцу через меню бота или `/stats`. Для проверки фактических показателей выберите "Мои данные", если включено демо.

Повторный контакт обновляет номер без повторной регистрации и рассылки. Неотправленные задачи продолжаются после перезапуска.

## Тесты

```powershell
docker compose --env-file .env.test --profile test run --build --rm tests
docker compose --env-file .env.test --profile test run --rm tests mypy --cache-dir=/tmp/mypy-cache
```

Тесты работают с отдельным PostgreSQL и тестовыми реквизитами из `.env.test`.

## Управление

```powershell
docker compose ps
docker compose logs -f --tail 100 bot web
docker compose stop
```

Логи сохраняются в `logs/`. Ротация по 5 МБ, три архива каждого журнала. Для ошибок записываются место сбоя, тип, `job_id` или `request_id`.

## Настройка

Тексты, изображения, задержки и UTM находятся в `funnel.yaml`.
После изменения выполните `docker compose up --build -d`. Уже поставленные задачи сохраняют свои тексты и сроки.

Рекламная ссылка `https://t.me/<bot_username>?start=ninja_channel`. Источники задаются в `sources`. Сохраняется первый переход.

SQL-миграции в `launch_funnel/migrations` применяются перед запуском приложения. Регистрация, телефон и расписание сохраняются одной транзакцией.
