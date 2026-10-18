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
git clone https://github.com/jkotosmos/liherwdc.git /opt/operon
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
* **MongoDB сайта** (нужен адрес старой базы):
  `docker run --rm mongo:7 mongodump --uri="СТАРЫЙ_MONGODB_URI" --archive | docker compose exec -T mongo mongorestore --archive --drop`
* **Бот:** содержимое `/data` с Amvera (раздел «Данные») → в `data/bot/`.
  `OPERON_TOKEN_KEY` в `bot.env` — прежний, иначе входы Google не расшифруются.

После переноса — остановить проекты на Amvera (иначе два бота с одним токеном).

## 4. Проверка и обновление
* Сайт: `https://kb.…`, бот: `/check` в Telegram, журнал: `docker compose logs -f backend bot`.
* Обновление всего: `sh setup.sh` (подтянет код бота и сайта и пересоберёт).
* Обновить только бота: `docker compose up -d --build bot` — сайт не перезапускается.
* Только сайт: `docker compose up -d --build backend frontend`.
