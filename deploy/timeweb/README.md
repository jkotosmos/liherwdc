# Вся система OPERON на одном сервере (Timeweb Cloud)

Бот ассистента, сайт базы знаний (фронтенд + бэкенд), MongoDB, Qdrant («Квант»)
и HTTPS — одной командой. Код сайта скачивается из
`github.com/skullmare/ai-knowledge-base-backend` и `-frontend`, сами эти
репозитории не меняются.

## 1. Сервер и домены
* Timeweb Cloud → облачный сервер Ubuntu 24.04, от 4 ГБ памяти (сборка фронтенда
  и MongoDB требовательны), диск от 40 ГБ.
* Два домена (поддомена) с A-записью на IP сервера, например
  `kb.operonai.ru` — сайт, `bot.operonai.ru` — бот.

## 2. Установка
```sh
git clone -b main https://github.com/jkotosmos/liherwdc.git /opt/operon
cd /opt/operon/deploy/timeweb
cp .env.example .env               # домены и ключ нового Qdrant
cp backend.env.example backend.env # настройки сайта (админ, JWT, Yandex, почта, RouterAI)
cp bot.env.example bot.env         # настройки бота (как на Amvera)
nano .env backend.env bot.env      # заполнить
sh setup.sh
```
Боту сайта (`TG_BOT_TOKEN`) и боту OPERON (`TELEGRAM_BOT_TOKEN`) нужны **разные**
токены — `setup.sh` это проверит.

## 3. Перенос данных с Amvera
* **«Квант» (Qdrant):**
  `docker compose run --rm -v "$PWD/migrate_qdrant.py:/m.py:ro" bot python /m.py https://qdrantdb-operon123.amvera.io СТАРЫЙ_КЛЮЧ`
* **MongoDB сайта** (нужен адрес старой базы; СТАРАЯ_БАЗА — имя базы в конце этого адреса,
  после последнего `/`; без имени — `test`):
  `docker run --rm mongo:7 mongodump --uri="СТАРЫЙ_MONGODB_URI" --archive | docker compose exec -T mongo mongorestore --archive --drop --nsFrom='СТАРАЯ_БАЗА.*' --nsTo='operon.*'`
  Без `--nsFrom/--nsTo` данные лягут в базу со старым именем, а сайт и бот читают `operon`.
* **Бот:** содержимое `/data` с Amvera (раздел «Данные») → в `data/bot/`.
  `OPERON_TOKEN_KEY` в `bot.env` — прежний, иначе входы Google не расшифруются.

После переноса — остановить проекты на Amvera (иначе два бота с одним токеном).

## Права к «Кванту»
Бот читает из MongoDB сайта роль человека (по его Telegram ID) и ищет в «Кванте» только
документы этой роли — как бот самого сайта. Поэтому каждому, кто пользуется ботом OPERON,
на сайте нужна роль: человек один раз регистрируется в боте сайта (делится телефоном),
администратор назначает роль в разделе пользователей агента. Без роли бот честно отвечает,
что документы недоступны. Роль по умолчанию для всех без роли — `QDRANT_ROLES=<ID роли>`
в `bot.env`. Ваша роль видна в `/check` → «Права в Кванте».

Даты документов (когда менялся файл в Google Drive, когда обновлялась тема) бот берёт
оттуда же и указывает в ответе.

## 4. Проверка и обновление
* Сайт: `https://kb.…`, бот: `/check` в Telegram, журнал: `docker compose logs -f backend bot`.
* Обновление всего: `sh setup.sh` (подтянет код бота и сайта и пересоберёт).
* Обновить только бота: `docker compose up -d --build bot` — сайт не перезапускается.
* Только сайт: `docker compose up -d --build backend frontend`.
