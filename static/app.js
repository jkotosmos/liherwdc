"use strict";

const el = (id) => document.getElementById(id);
const chat = el("chat");
const messages = el("messages");
const input = el("input");
const sendBtn = el("send");
const welcome = el("welcome");

let sessionId = null;
let busy = false;

/* ------------------------------------------------------------------ */
/* Telegram Mini App: вход по подписи Telegram, токен — в заголовке.   */
/* В веб-версии Telegram страница живёт во фрейме, и куки туда не      */
/* доходят, поэтому все запросы несут Authorization: Bearer.           */
/* ------------------------------------------------------------------ */

const tg = window.Telegram && window.Telegram.WebApp;
const inTelegram = Boolean(tg && tg.initData);
let authToken = null;
const nativeFetch = window.fetch.bind(window);

window.fetch = (url, options = {}) => {
  if (authToken) {
    options = { ...options, headers: { ...(options.headers || {}), Authorization: `Bearer ${authToken}` } };
  }
  return nativeFetch(url, options);
};

async function telegramLogin() {
  tg.ready();
  tg.expand();
  if (tg.colorScheme) document.documentElement.dataset.theme = tg.colorScheme;
  const response = await nativeFetch("/api/telegram/auth", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ init_data: tg.initData }),
  });
  let payload = {};
  try { payload = await response.json(); } catch { /* без тела */ }
  if (!response.ok) {
    el("model-line").textContent = payload.detail || `Вход не выполнен (HTTP ${response.status})`;
    input.disabled = true;
    sendBtn.disabled = true;
    return false;
  }
  authToken = payload.token;
  return true;
}

/* ------------------------------------------------------------------ */
/* Разметка ответа: экранируем всё, затем размечаем ограниченный набор */
/* конструкций Markdown. Никакого innerHTML из сырого текста модели.   */
/* ------------------------------------------------------------------ */

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function inline(text) {
  return text
    .replace(/`([^`\n]+)`/g, (_, code) => `<code>${code}</code>`)
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
    )
    .replace(
      /(^|[\s(])((?:https?:\/\/)[^\s<)]+[^\s<).,;:])/g,
      '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>'
    );
}

function renderTable(rows) {
  const cells = (row) =>
    row
      .replace(/^\s*\|/, "")
      .replace(/\|\s*$/, "")
      .split("|")
      .map((c) => c.trim());
  const head = cells(rows[0]);
  const body = rows.slice(2).map(cells);
  const th = head.map((c) => `<th>${inline(c)}</th>`).join("");
  const tr = body
    .map((row) => `<tr>${row.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`)
    .join("");
  return `<table><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table>`;
}

function renderMarkdown(raw) {
  const blocks = [];
  const source = escapeHtml(raw).replace(/\r\n/g, "\n");

  // Блоки кода выносим отдельно, чтобы их содержимое не размечалось.
  const fences = [];
  const withoutFences = source.replace(/```[^\n]*\n([\s\S]*?)```/g, (_, code) => {
    fences.push(code.replace(/\n$/, ""));
    return ` FENCE${fences.length - 1} `;
  });

  const lines = withoutFences.split("\n");
  let i = 0;

  const isTableRow = (line) => /^\s*\|.*\|\s*$/.test(line);
  const isTableSep = (line) => /^\s*\|[\s:|-]+\|\s*$/.test(line);

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { i++; continue; }

    const fence = line.match(/^ FENCE(\d+) $/);
    if (fence) {
      blocks.push(`<pre><code>${fences[Number(fence[1])]}</code></pre>`);
      i++;
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = Math.min(heading[1].length + 1, 4);
      blocks.push(`<h${level}>${inline(heading[2].trim())}</h${level}>`);
      i++;
      continue;
    }

    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { blocks.push("<hr>"); i++; continue; }

    if (isTableRow(line) && isTableSep(lines[i + 1] || "")) {
      const rows = [];
      while (i < lines.length && isTableRow(lines[i])) rows.push(lines[i++]);
      blocks.push(renderTable(rows));
      continue;
    }

    // Цитата: символ «>» к этому моменту уже экранирован в &gt;.
    if (/^\s*&gt;\s?/.test(line)) {
      const quote = [];
      while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) {
        quote.push(lines[i++].replace(/^\s*&gt;\s?/, ""));
      }
      blocks.push(`<blockquote>${inline(quote.join(" "))}</blockquote>`);
      continue;
    }

    const bullet = /^\s*[-*•]\s+(.*)$/;
    const numbered = /^\s*\d+[.)]\s+(.*)$/;
    if (bullet.test(line) || numbered.test(line)) {
      const ordered = numbered.test(line);
      const pattern = ordered ? numbered : bullet;
      const items = [];
      while (i < lines.length && pattern.test(lines[i])) {
        items.push(`<li>${inline(lines[i].match(pattern)[1].trim())}</li>`);
        i++;
        // Продолжение пункта на следующей строке без маркера.
        while (
          i < lines.length &&
          lines[i].trim() &&
          !pattern.test(lines[i]) &&
          /^\s{2,}\S/.test(lines[i])
        ) {
          items[items.length - 1] = items[items.length - 1].replace(
            /<\/li>$/,
            ` ${inline(lines[i].trim())}</li>`
          );
          i++;
        }
      }
      const tag = ordered ? "ol" : "ul";
      blocks.push(`<${tag}>${items.join("")}</${tag}>`);
      continue;
    }

    const paragraph = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^(#{1,6}\s|\s*[-*•]\s|\s*\d+[.)]\s|\s*&gt;| FENCE)/.test(lines[i]) &&
      !isTableRow(lines[i])
    ) {
      paragraph.push(lines[i++]);
    }
    if (paragraph.length) blocks.push(`<p>${inline(paragraph.join(" ").trim())}</p>`);
    else i++;
  }

  return blocks.join("");
}

/* ------------------------------------------------------------------ */
/* Элементы диалога                                                    */
/* ------------------------------------------------------------------ */

function scrollDown() {
  requestAnimationFrame(() => { chat.scrollTop = chat.scrollHeight; });
}

function hideWelcome() { welcome.classList.add("hidden"); }

function addUserMessage(text) {
  const node = document.createElement("div");
  node.className = "msg user";
  node.innerHTML = `<div class="msg-role">Вы</div><div class="bubble"></div>`;
  node.querySelector(".bubble").textContent = text;
  messages.appendChild(node);
  scrollDown();
}

function createAssistantTurn() {
  const node = document.createElement("div");
  node.className = "msg assistant";
  node.innerHTML = `<div class="msg-role">Ассистент</div>
    <div class="activity"></div>
    <div class="bubble"></div>`;
  messages.appendChild(node);

  const activity = node.querySelector(".activity");
  const bubble = node.querySelector(".bubble");
  const chips = new Map();
  let text = "";

  // Индикатор «Анализирую» снимается, как только пошёл текст или новый инструмент.
  function clearStatusChips() {
    for (const [key, chip] of chips) {
      if (key.startsWith("__status_")) {
        chip.remove();
        chips.delete(key);
      }
    }
  }

  return {
    node,
    appendText(chunk) {
      if (!text) clearStatusChips();
      text += chunk;
      bubble.innerHTML = renderMarkdown(text) + '<span class="cursor"></span>';
      scrollDown();
    },
    finishText() {
      clearStatusChips();
      bubble.innerHTML = text ? renderMarkdown(text) : "";
      if (!text) bubble.remove();
    },
    hasText: () => text.length > 0,
    startTool(name, label) {
      if (!name.startsWith("__status_")) clearStatusChips();
      const chip = document.createElement("div");
      chip.className = "chip running";
      chip.innerHTML = `<span class="spark"></span><span class="chip-text"></span>`;
      chip.querySelector(".chip-text").textContent = `${label}…`;
      activity.appendChild(chip);
      chips.set(name, chip);
      scrollDown();
    },
    endTool(name, ok, summary) {
      const chip = chips.get(name) || activity.lastElementChild;
      if (!chip) return;
      chip.className = `chip ${ok ? "done" : "fail"}`;
      const current = chip.querySelector(".chip-text").textContent.replace(/…$/, "");
      chip.querySelector(".chip-text").textContent = summary ? `${current} — ${summary}` : current;
      chips.delete(name);
    },
    declineTool(summary) {
      const chip = document.createElement("div");
      chip.className = "chip declined";
      chip.innerHTML = `<span class="spark"></span><span class="chip-text"></span>`;
      chip.querySelector(".chip-text").textContent = summary;
      activity.appendChild(chip);
    },
    notice(kind, message) {
      const box = document.createElement("div");
      box.className = `notice ${kind}`;
      box.textContent = message;
      node.appendChild(box);
      scrollDown();
    },
    mount(element) {
      node.appendChild(element);
      scrollDown();
    },
  };
}

/* ------------------------------------------------------------------ */
/* Карточка подтверждения                                              */
/* ------------------------------------------------------------------ */

function buildConfirmation(actions, expiresInMinutes, onSubmit) {
  const wrapper = document.createElement("div");
  const expiries = [];
  const cards = actions.map((action) => {
    const card = document.createElement("div");
    card.className = "confirm";

    const rows = Object.entries(action.details || {})
      .map(([key, value]) => {
        const row = document.createElement("div");
        row.className = "confirm-row";
        const k = document.createElement("div");
        k.className = "confirm-key";
        k.textContent = key;
        const v = document.createElement("div");
        v.className = "confirm-val";
        v.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
        row.append(k, v);
        return row;
      });

    const head = document.createElement("div");
    head.className = "confirm-head";
    head.innerHTML = `<span class="confirm-badge">требуется подтверждение</span>`;
    const title = document.createElement("span");
    title.className = "confirm-title";
    title.textContent = action.title || action.name;
    head.prepend(title);

    const summary = document.createElement("div");
    summary.className = "confirm-summary";
    summary.textContent = action.summary || "";

    const details = document.createElement("div");
    details.className = "confirm-details";
    rows.forEach((row) => details.appendChild(row));

    const actionsRow = document.createElement("div");
    actionsRow.className = "confirm-actions";
    const approve = document.createElement("button");
    approve.className = "btn btn-approve";
    approve.type = "button";
    approve.textContent = "Подтвердить";
    const reject = document.createElement("button");
    reject.className = "btn btn-reject";
    reject.type = "button";
    reject.textContent = "Отклонить";
    const comment = document.createElement("input");
    comment.className = "confirm-comment";
    comment.type = "text";
    comment.placeholder = "Комментарий при отклонении (необязательно)";
    actionsRow.append(approve, reject, comment);

    card.append(head, summary);
    if (rows.length) card.appendChild(details);
    card.appendChild(actionsRow);

    let decision = null;
    const settle = (value) => {
      decision = value;
      card.classList.add("resolved");
      actionsRow.innerHTML = "";
      const verdict = document.createElement("span");
      verdict.className = `confirm-verdict ${value === "approve" ? "approved" : "rejected"}`;
      verdict.textContent =
        value === "approve" ? "✓ Подтверждено — выполняю" : "✕ Отклонено — действие не выполнено";
      actionsRow.appendChild(verdict);
      if (value === "reject" && comment.value.trim()) {
        const note = document.createElement("span");
        note.style.color = "var(--text-dim)";
        note.style.fontSize = "13px";
        note.textContent = `Комментарий: ${comment.value.trim()}`;
        actionsRow.appendChild(note);
      }
      maybeSubmit();
    };

    approve.addEventListener("click", () => settle("approve"));
    reject.addEventListener("click", () => settle("reject"));

    // Молчание — это отказ, и отказ наступает по времени. Пользователь должен
    // видеть срок, а не обнаруживать его постфактум.
    expiries.push(() => {
      if (decision !== null) return;
      card.classList.add("resolved");
      actionsRow.innerHTML = "";
      const verdict = document.createElement("span");
      verdict.className = "confirm-verdict rejected";
      verdict.textContent = "⌛ Время вышло — действие отменено";
      actionsRow.appendChild(verdict);
    });

    wrapper.appendChild(card);
    return {
      action,
      get decision() { return decision; },
      get comment() { return comment.value.trim(); },
    };
  });

  function maybeSubmit() {
    if (cards.some((c) => c.decision === null)) return;
    if (countdown) clearInterval(countdown);
    onSubmit(
      cards.map((c) => ({
        tool_use_id: c.action.tool_use_id,
        decision: c.decision,
        comment: c.comment,
      }))
    );
  }

  let countdown = null;
  if (expiresInMinutes > 0) {
    const deadline = Date.now() + expiresInMinutes * 60000;
    const note = document.createElement("div");
    note.className = "confirm-deadline";
    wrapper.appendChild(note);

    const tick = () => {
      const left = Math.max(0, Math.round((deadline - Date.now()) / 60000));
      if (left > 0) {
        note.textContent = `Без ответа действие отменяется автоматически (осталось ~${left} мин).`;
        return;
      }
      clearInterval(countdown);
      countdown = null;
      note.textContent = "Срок ответа истёк — неподтверждённые действия отменены.";
      expiries.forEach((expire) => expire());
    };
    tick();
    countdown = setInterval(tick, 15000);
  }

  return wrapper;
}

/* ------------------------------------------------------------------ */
/* Транспорт: POST + чтение SSE-потока                                 */
/* ------------------------------------------------------------------ */

async function streamRequest(url, body, turn) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const payload = await response.json();
      if (payload.detail) detail = payload.detail;
    } catch { /* тело без JSON — оставляем код статуса */ }
    turn.notice("error", detail);
    turn.finishText();
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let split;
    while ((split = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      const line = frame.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue;
      try {
        handleEvent(JSON.parse(line.slice(6)), turn);
      } catch (err) {
        console.error("Не удалось разобрать событие", err, line);
      }
    }
  }
  turn.finishText();
}

function handleEvent(event, turn) {
  switch (event.type) {
    case "session":
      sessionId = event.session_id;
      break;
    case "text_delta":
      turn.appendText(event.text);
      break;
    case "status":
      turn.startTool(`__status_${event.state}`, event.message);
      break;
    case "tool_start":
      turn.startTool(event.name, event.activity || event.name);
      break;
    case "tool_end":
      turn.endTool(event.name, event.ok, event.summary);
      break;
    case "tool_declined":
      turn.declineTool(event.summary);
      break;
    case "warning":
      turn.notice("warn", event.message);
      break;
    case "error":
      turn.notice("error", event.message);
      break;
    case "confirmation_required":
      turn.finishText();
      turn.mount(
        buildConfirmation(event.actions, event.expires_in_minutes || 0, (decisions) => {
          const next = createAssistantTurn();
          setBusy(true);
          streamRequest("/api/confirm", { session_id: sessionId, decisions }, next)
            .catch((err) => next.notice("error", `Сбой соединения: ${err.message}`))
            .finally(() => setBusy(false));
        })
      );
      break;
    case "done":
      break;
    default:
      break;
  }
}

/* ------------------------------------------------------------------ */
/* Управление вводом                                                   */
/* ------------------------------------------------------------------ */

function setBusy(value) {
  busy = value;
  sendBtn.disabled = value;
  input.disabled = value;
  if (!value) input.focus();
}

async function send() {
  const text = input.value.trim();
  if (!text || busy) return;

  hideWelcome();
  addUserMessage(text);
  input.value = "";
  input.style.height = "auto";
  setBusy(true);

  const turn = createAssistantTurn();
  try {
    await streamRequest("/api/chat", { session_id: sessionId, message: text }, turn);
  } catch (err) {
    turn.notice("error", `Сбой соединения с сервером: ${err.message}`);
    turn.finishText();
  } finally {
    setBusy(false);
  }
}

sendBtn.addEventListener("click", send);

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    send();
  }
});

input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 190)}px`;
});

el("suggestions").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-prompt]");
  if (!button || busy) return;
  input.value = button.dataset.prompt;
  send();
});

el("new-chat").addEventListener("click", async () => {
  if (busy) return;
  await fetch("/api/session/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  }).catch(() => {});
  sessionId = null;
  messages.innerHTML = "";
  welcome.classList.remove("hidden");
  input.focus();
});

/* ------------------------------------------------------------------ */
/* Индикаторы состояния                                                */
/* ------------------------------------------------------------------ */

function pill(label, state) {
  return `<span class="pill ${state}"><span class="dot"></span>${label}</span>`;
}

async function loadStatus() {
  try {
    const response = await fetch("/api/status");
    if (response.status === 401 && !inTelegram) {
      window.location.href = "/login";
      return;
    }
    const status = await response.json();
    el("org-name").textContent = status.org;
    document.title = `Ассистент ${status.org}`;
    el("model-line").textContent = `${status.model} · ${status.timezone}`;

    const kb = status.knowledge_base;
    const parts = [
      kb.documents
        ? pill(`База знаний: ${kb.documents}`, "ok")
        : pill("База знаний пуста", "off"),
      status.google.connected
        ? pill(status.google.account_hint || "Google подключён", "ok")
        : pill("Google не подключён", "off"),
      status.web_search ? pill("Интернет", "ok") : pill("Интернет выкл.", "off"),
    ];
    el("indicators").innerHTML = parts.join("");
  } catch {
    el("model-line").textContent = "сервер недоступен";
  }
}

/* ------------------------------------------------------------------ */
/* Модель и баланс                                                     */
/* ------------------------------------------------------------------ */

let catalog = [];
let currency = "₽";
let currentModel = "";

const money = (value, digits = 2) =>
  value === undefined || value === null
    ? "—"
    : `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: digits, minimumFractionDigits: Math.min(digits, 2) }).format(value)} ${currency}`;

const price = (value) => (value === null || value === undefined ? "?" : money(value, value < 1 ? 4 : 2));

function facts(target, rows) {
  target.innerHTML = rows
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(String(value))}</dd>`)
    .join("");
}

function showView(view) {
  document.querySelectorAll(".tab").forEach((tab) => {
    const active = tab.dataset.view === view;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".view-chat").forEach((node) => node.classList.toggle("hidden", view !== "chat"));
  el("settings").classList.toggle("hidden", view !== "settings");
  if (view === "settings") loadSettings();
}

el("tabs").addEventListener("click", (event) => {
  const tab = event.target.closest(".tab");
  if (tab) showView(tab.dataset.view);
});

function renderCurrent(model) {
  const info = catalog.find((m) => m.id === currentModel) || model;
  el("current-name").textContent = (info && info.name) || currentModel || "не выбрана";
  el("current-id").textContent = currentModel;
  facts(el("current-facts"), info ? [
    ["Вход, 1 млн токенов", price(info.price_in)],
    ["Выход, 1 млн токенов", price(info.price_out)],
    ["Контекст", info.context_length ? `${new Intl.NumberFormat("ru-RU").format(info.context_length)} токенов` : null],
  ] : []);
}

function renderModels() {
  const query = el("model-search").value.trim().toLowerCase();
  const sort = el("model-sort").value;
  let list = catalog.filter((m) => !query || m.name.toLowerCase().includes(query) || m.id.toLowerCase().includes(query));
  if (sort === "price") {
    const cost = (m) => (m.price_in ?? Infinity) + (m.price_out ?? Infinity);
    list = [...list].sort((a, b) => cost(a) - cost(b));
  }
  el("model-list").innerHTML = list.slice(0, 200).map((m) => `
    <li class="model-item${m.id === currentModel ? " selected" : ""}" data-id="${escapeHtml(m.id)}">
      <span class="m-name">${escapeHtml(m.name)}${m.tools === null ? ' <span class="m-flag">инструменты не подтверждены</span>' : ""}</span>
      <span class="m-price">${escapeHtml(price(m.price_in))}<br>${escapeHtml(price(m.price_out))}</span>
      <span class="m-id">${escapeHtml(m.id)}</span>
    </li>`).join("") || '<li class="card-note">Ничего не найдено.</li>';
}

el("model-search").addEventListener("input", renderModels);
el("model-sort").addEventListener("change", renderModels);

function ask(text) {
  // Переключение модели меняет работу бота для всех — только после согласия.
  if (tg && tg.showConfirm && inTelegram) return new Promise((resolve) => tg.showConfirm(text, resolve));
  return Promise.resolve(window.confirm(text));
}

el("model-list").addEventListener("click", async (event) => {
  const item = event.target.closest(".model-item");
  if (!item || item.dataset.id === currentModel) return;
  const model = catalog.find((m) => m.id === item.dataset.id);
  const agreed = await ask(`Переключить ассистента на «${model ? model.name : item.dataset.id}»? Это действует и в чате бота.`);
  if (!agreed) return;
  const who = inTelegram && tg.initDataUnsafe && tg.initDataUnsafe.user ? String(tg.initDataUnsafe.user.id) : "веб";
  const response = await fetch("/api/model", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Operon-User": who },
    body: JSON.stringify({ model: item.dataset.id }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    el("balance-note").textContent = payload.detail || `Не удалось переключить (HTTP ${response.status})`;
    el("balance-note").classList.add("error");
    return;
  }
  currentModel = payload.current.model;
  renderCurrent();
  renderModels();
  loadStatus();
  if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
});

async function loadSettings() {
  const note = el("balance-note");
  note.classList.remove("error");
  note.textContent = "Загружаю…";
  try {
    const [billing, models] = await Promise.all([
      fetch("/api/billing").then((r) => r.json()),
      fetch("/api/models").then((r) => r.json()),
    ]);
    currentModel = (billing.current && billing.current.model) || "";
    if (!billing.available) {
      el("balance-value").textContent = "—";
      note.textContent = "Баланс и каталог доступны при работе через RouterAI (ROUTERAI_API_KEY).";
      renderCurrent();
      return;
    }
    currency = billing.currency || currency;
    el("balance-value").textContent = money(billing.balance);
    facts(el("balance-facts"), [
      ["Потрачено ключом всего", billing.key_usage !== undefined ? money(billing.key_usage) : null],
      ["За месяц", billing.key_usage_monthly !== undefined ? money(billing.key_usage_monthly) : null],
      ["За неделю", billing.key_usage_weekly !== undefined ? money(billing.key_usage_weekly) : null],
      ["Сегодня", billing.key_usage_daily !== undefined ? money(billing.key_usage_daily) : null],
      ["Лимит ключа", billing.key_limit !== undefined ? money(billing.key_limit) : null],
    ]);
    const problems = billing.errors || [];
    note.textContent = billing.balance === undefined
      ? `Шлюз не сообщил баланс. ${problems.join(" ")}`.trim()
      : (billing.balance_source ? `Показан остаток по ${billing.balance_source}.` : "");
    if (billing.balance === undefined) note.classList.add("error");

    catalog = models.models || [];
    if (!models.available) {
      el("model-list").innerHTML = `<li class="card-note error">${escapeHtml(models.detail || "Каталог недоступен.")}</li>`;
    } else {
      renderModels();
    }
    renderCurrent(billing.model);
  } catch (err) {
    note.textContent = `Сбой соединения: ${err.message}`;
    note.classList.add("error");
  }
}

(async () => {
  if (inTelegram && !(await telegramLogin())) return;
  loadStatus();
  if (!inTelegram) input.focus();
})();
