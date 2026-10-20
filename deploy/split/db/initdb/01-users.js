// Пользователи базы сайта — создаются один раз, при первом запуске MongoDB
// (когда data/mongo пуст). Пароли приходят из .env сервера баз.
const operon = db.getSiblingDB("operon");

operon.createUser({
  user: "operon_app",
  pwd: process.env.MONGO_APP_PASSWORD,
  roles: [{ role: "readWrite", db: "operon" }],
});

operon.createUser({
  user: "operon_bot",
  pwd: process.env.MONGO_BOT_PASSWORD,
  roles: [{ role: "read", db: "operon" }],
});

print("Пользователи operon_app (чтение и запись) и operon_bot (только чтение) созданы");
