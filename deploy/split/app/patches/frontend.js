// Правки фронтенда сайта базы знаний при сборке (docker-compose.yml → frontend).
// Код сайта — в отдельном репозитории; здесь две точечные замены.
//
// 1. Загрузка файла с компьютера: браузер отправлял куски файла прямо в Yandex
//    Object Storage — на чужой домен, и всё зависело от CORS бакета, видимости
//    заголовка ETag и того, пускает ли сеть пользователя к storage.yandexcloud.net.
//    Теперь кусок идёт на свой домен (/s3/<хост хранилища>/…), а Caddy пересылает
//    его в хранилище без изменений: подпись ссылки остаётся верной, CORS не нужен.
//
// 2. Сообщение об ошибке: при сбое загрузки сайт показывал «Что-то пошло не так»
//    вместо настоящей причины (обработчик не читал текст обычной ошибки).
//
// Запуск: node frontend.js <папка исходников фронтенда>. Не нашёл место для
// замены — сборка останавливается с объяснением, а не собирается без правки.
const fs = require('fs');
const path = require('path');

const root = process.argv[2] || '.';

function patch(file, from, to, why) {
    const full = path.join(root, file);
    const text = fs.readFileSync(full, 'utf8');
    if (text.includes(to)) {
        console.log(`[operon-patch] ${file}: уже исправлено (${why})`);
        return;
    }
    if (!text.includes(from)) {
        console.error(`[operon-patch] ${file}: не найдено место для правки (${why}). ` +
            'Код сайта изменился — обновите deploy/split/app/patches/frontend.js.');
        process.exit(1);
    }
    fs.writeFileSync(full, text.replace(from, to));
    console.log(`[operon-patch] ${file}: ${why}`);
}

patch(
    'src/services/file.js',
    "const response = await fetch(url, { method: 'PUT', body: blob, signal })",
    "const viaSite = (u) => { const p = new URL(u); return `/s3/${p.host}${p.pathname}${p.search}` }\n" +
    "  const response = await fetch(viaSite(url), { method: 'PUT', body: blob, signal })",
    'куски файла идут через свой домен (/s3/), без CORS',
);

patch(
    'src/utils/handleError.js',
    "return 'Что-то пошло не так'",
    "return err?.message || 'Что-то пошло не так'",
    'показывается настоящая причина ошибки',
);
