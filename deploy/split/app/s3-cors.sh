#!/bin/sh
# Загрузка файлов на сайте базы знаний: проверка хранилища и CORS бакета.
#
# Файл с компьютера браузер кладёт прямо в Yandex Object Storage (по
# подписанным ссылкам), минуя сервер. Хранилище пускает такие запросы только
# с адресов из CORS-политики бакета. После переезда сайта на новый домен там
# остаётся старый адрес (Amvera) — загрузка падает с «Что-то пошло не так»,
# а импорт из Google Drive (идёт через сервер) продолжает работать.
#
# Скрипт работает внутри контейнера backend теми же ключами, что и сайт:
#   1) проверяет, что ключи и имя бакета заданы и бакет доступен;
#   2) пробует начать и отменить загрузку — как это делает сайт;
#   3) показывает текущую CORS-политику;
#   4) с флагом --apply записывает новую: домен сайта (KB_DOMAIN) и
#      дополнительные домены из аргументов; PUT/GET/HEAD, заголовок ETag.
#
# Запуск из /opt/operon/deploy/split/app:
#   sh s3-cors.sh                          # только проверить
#   sh s3-cors.sh --apply                  # прописать домен сайта
#   sh s3-cors.sh --apply operonai.ru      # и ещё домены
# Ничего, кроме CORS-политики бакета, не меняет. Секреты не печатает.
set -eu
cd "$(dirname "$0")"

APPLY=0
EXTRA=""
for arg in "$@"; do
    case "$arg" in
        --apply) APPLY=1 ;;
        *) EXTRA="$EXTRA $arg" ;;
    esac
done

docker compose exec -T -e APPLY="$APPLY" -e EXTRA_DOMAINS="$EXTRA" backend node - <<'NODE'
const {
    S3Client, HeadBucketCommand, GetBucketCorsCommand, PutBucketCorsCommand,
    CreateMultipartUploadCommand, AbortMultipartUploadCommand,
} = require('/app/node_modules/@aws-sdk/client-s3');

const say = (mark, text) => console.log(`${mark} ${text}`);
const bucket = (process.env.BUCKET_NAME || '').trim();
const keyId = (process.env.YANDEX_ACCESS_KEY_ID || '').trim();
const secret = (process.env.YANDEX_SECRET_ACCESS_KEY || '').trim();
const main = (process.env.MAIN_DOMAIN || '').trim().replace(/^https?:\/\//, '').replace(/\/+$/, '');
const extra = (process.env.EXTRA_DOMAINS || '').split(/\s+/).filter(Boolean)
    .map((d) => d.replace(/^https?:\/\//, '').replace(/\/+$/, ''));

const why = (e) => `${e.name || 'Error'}: ${e.message}`.slice(0, 200);

(async () => {
    let ok = true;
    if (!keyId || !secret || !bucket) {
        say('✗', `Не заданы в backend.env: ${[
            !keyId && 'YANDEX_ACCESS_KEY_ID', !secret && 'YANDEX_SECRET_ACCESS_KEY', !bucket && 'BUCKET_NAME',
        ].filter(Boolean).join(', ')}. Возьмите их из переменных старого бэкенда (Amvera) и перезапустите: docker compose up -d backend`);
        process.exit(1);
    }
    say('✓', `Ключи заданы, бакет: ${bucket}`);

    const s3 = new S3Client({
        region: 'ru-central1',
        endpoint: 'https://storage.yandexcloud.net',
        credentials: { accessKeyId: keyId, secretAccessKey: secret },
    });

    try {
        await s3.send(new HeadBucketCommand({ Bucket: bucket }));
        say('✓', 'Бакет доступен');
    } catch (e) {
        say('✗', `Бакет недоступен этими ключами — ${why(e)}`);
        process.exit(1);
    }

    try {
        const created = await s3.send(new CreateMultipartUploadCommand({
            Bucket: bucket, Key: `_operon_check/${Date.now()}.txt`, ContentType: 'text/plain',
        }));
        await s3.send(new AbortMultipartUploadCommand({ Bucket: bucket, Key: created.Key, UploadId: created.UploadId }));
        say('✓', 'Права на загрузку есть (тестовая загрузка начата и отменена)');
    } catch (e) {
        ok = false;
        say('✗', `Нет права загружать файлы — ${why(e)}. Сервисному аккаунту ключа нужна роль storage.editor на бакет.`);
    }

    let rules = [];
    try {
        rules = (await s3.send(new GetBucketCorsCommand({ Bucket: bucket }))).CORSRules || [];
    } catch (e) {
        if (!/NoSuchCORSConfiguration/i.test(`${e.name} ${e.message}`)) say('!', `CORS не прочитать — ${why(e)}`);
    }
    const allowed = rules.flatMap((r) => r.AllowedOrigins || []);
    console.log(`  Сейчас CORS разрешает: ${allowed.length ? allowed.join(', ') : 'никого (политики нет)'}`);

    const wanted = [...new Set([main, ...extra].filter(Boolean))].map((d) => `https://${d}`);
    if (!wanted.length) {
        say('✗', 'Не знаю домен сайта: MAIN_DOMAIN пуст (KB_DOMAIN в .env). Укажите домен аргументом.');
        process.exit(1);
    }
    const putOk = (origin) => rules.some((r) => (r.AllowedOrigins || []).some((o) => o === '*' || o === origin)
        && (r.AllowedMethods || []).includes('PUT')
        && (r.ExposeHeaders || []).some((h) => h.toLowerCase() === 'etag'));
    const missing = wanted.filter((o) => !putOk(o));
    if (!missing.length) {
        say('✓', `CORS в порядке для ${wanted.join(', ')}`);
    } else if (process.env.APPLY !== '1') {
        ok = false;
        say('✗', `Загрузка с ${missing.join(', ')} заблокирована CORS бакета. Исправить: sh s3-cors.sh --apply${extra.length ? ' ' + extra.join(' ') : ''}`);
    } else {
        // Старые правила сохраняем (например, для прежнего адреса), добавляем своё.
        const origins = [...new Set([...allowed.filter((o) => o !== '*'), ...wanted])];
        const rule = {
            AllowedOrigins: origins,
            AllowedMethods: ['GET', 'PUT', 'HEAD'],
            AllowedHeaders: ['*'],
            ExposeHeaders: ['ETag'],
            MaxAgeSeconds: 3600,
        };
        try {
            await s3.send(new PutBucketCorsCommand({ Bucket: bucket, CORSConfiguration: { CORSRules: [rule] } }));
            say('✓', `CORS записан: ${origins.join(', ')} — PUT/GET/HEAD, ETag. Обновите страницу сайта и загрузите файл снова.`);
        } catch (e) {
            ok = false;
            say('✗', `Не удалось записать CORS этими ключами — ${why(e)}.`);
            console.log('  Сделайте вручную: консоль Yandex Cloud → Object Storage → бакет → CORS → правило:');
            console.log(`  Origin: ${origins.join(', ')}; методы: PUT, GET, HEAD; заголовки: *; Expose headers: ETag`);
        }
    }
    process.exit(ok ? 0 : 1);
})();
NODE
