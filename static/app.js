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

function attachmentLink(event) {
  // Готовый документ (КП, отчёт, расчёт). В Mini App скачивает загрузчик
  // Telegram: обычная ссылка во встроенном браузере файл не сохраняет.
  const link = document.createElement("a");
  link.className = "attachment";
  link.href = `/api/files/${encodeURIComponent(event.id)}`;
  link.download = event.name || "document";
  link.textContent = `📎 ${event.name || "Документ"} — скачать`;
  if (inTelegram && tg.downloadFile) {
    link.addEventListener("click", (e) => {
      e.preventDefault();
      tg.downloadFile({ url: new URL(link.href, window.location.href).href, file_name: link.download });
    });
  }
  return link;
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
    case "attachment":
      turn.mount(attachmentLink(event));
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
/* Разделы и переходы                                                  */
/* ------------------------------------------------------------------ */

const PAGES = ["today", "chat", "docs", "tasks", "settings"];
let dashboard = null;
let catalog = [];
let currency = "₽";
let currentModel = "";
let taskFilter = "all";
let documents = [];

const money = (value, digits = 2) =>
  value === undefined || value === null
    ? "—"
    : `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: digits, minimumFractionDigits: Math.min(digits, 2) }).format(value)} ${currency}`;

const price = (value) => (value === null || value === undefined ? "?" : money(value, value < 1 ? 4 : 2));
function plural(count, one, few, many) {
  const n = Math.abs(count);
  const m10 = n % 10;
  const m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return `${count} ${one}`;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return `${count} ${few}`;
  return `${count} ${many}`;
}

function facts(target, rows) {
  target.innerHTML = rows
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(String(value))}</dd>`)
    .join("");
}

function pill(label, state) {
  return `<span class="pill ${state}"><span class="dot"></span>${escapeHtml(label)}</span>`;
}

function showView(view) {
  document.querySelectorAll(".tab").forEach((tab) => {
    const active = tab.dataset.view === view;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  PAGES.forEach((name) => {
    const node = name === "chat" ? chat : el(name === "today" ? "view-today" : name === "docs" ? "view-docs" : name === "tasks" ? "view-tasks" : "settings");
    if (node) node.classList.toggle("hidden", name !== view);
  });
  el("composer").classList.toggle("hidden", view !== "chat");
  if (view === "today") loadDashboard();
  if (view === "docs") loadDocuments();
  if (view === "tasks") loadTasks();
  if (view === "settings") loadSettings();
  if (view === "chat") input.focus();
  window.scrollTo(0, 0);
}

el("tabs").addEventListener("click", (event) => {
  const tab = event.target.closest(".tab");
  if (tab) showView(tab.dataset.view);
});

// Кнопки пульта: переход к разделу или к блоку на той же странице.
document.addEventListener("click", (event) => {
  const go = event.target.closest("[data-go]");
  if (!go) return;
  const target = go.dataset.go;
  if (PAGES.includes(target)) {
    showView(target);
  } else {
    showView("today");
    el(target).scrollIntoView({ behavior: "smooth", block: "start" });
  }
});

// Кнопки «Написать в чате»: текст уходит в поле ввода, человек дописывает и отправляет сам.
// data-send — для вопросов без последствий: отправляется сразу.
function askInChat(text, sendNow = false) {
  showView("chat");
  input.value = text;
  input.dispatchEvent(new Event("input"));
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  if (sendNow) send();
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-ask]");
  if (!button || busy) return;
  askInChat(button.dataset.ask, button.hasAttribute("data-send"));
});

function ask(text) {
  // Переключение модели меняет работу бота для всех — только после согласия.
  if (tg && tg.showConfirm && inTelegram) return new Promise((resolve) => tg.showConfirm(text, resolve));
  return Promise.resolve(window.confirm(text));
}

/* ------------------------------------------------------------------ */
/* Состояние системы (шапка)                                           */
/* ------------------------------------------------------------------ */

async function loadStatus() {
  try {
    const response = await fetch("/api/status");
    if (response.status === 401 && !inTelegram) {
      window.location.href = "/login";
      return;
    }
    const status = await response.json();
    document.title = `Ассистент ${status.org}`;
    el("model-line").textContent = `${status.model} · ${status.timezone}`;
    const kb = status.knowledge_base;
    el("indicators").innerHTML = [
      kb.documents ? pill(`База: ${kb.documents}`, "ok") : pill("База пуста", "off"),
      status.google.connected ? pill("Google", "ok") : pill("Google не подключён", "off"),
    ].join("");
  } catch {
    el("model-line").textContent = "сервер недоступен";
  }
}

/* ------------------------------------------------------------------ */
/* Пульт «Сегодня»                                                     */
/* ------------------------------------------------------------------ */

async function fetchDashboard() {
  const response = await fetch("/api/dashboard");
  if (response.status === 401 && !inTelegram) {
    window.location.href = "/login";
    throw new Error("нужен вход");
  }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function loadDashboard() {
  try {
    dashboard = await fetchDashboard();
    renderDashboard(dashboard);
  } catch (err) {
    el("today-sub").textContent = `Не удалось загрузить: ${err.message}`;
  }
}

el("refresh-today").addEventListener("click", loadDashboard);

function renderDashboard(d) {
  const label = d.today.label.charAt(0).toUpperCase() + d.today.label.slice(1);
  el("today-title").textContent = label;
  el("today-sub").textContent = `Данные на ${d.today.time}. Изменения появятся в карточке подтверждения в чате.`;

  // Google — если не подключён, говорим прямо, что и как сделать.
  const banner = el("google-banner");
  banner.classList.toggle("hidden", d.google.connected);
  if (!d.google.connected) {
    el("google-banner-text").textContent = " Календарь, Диск и таблицы пока недоступны ассистенту. В Telegram-боте отправьте команду /auth и войдите своей почтой Google."; 
  }

  // Четыре цифры сверху.
  const meetings = d.meetings.items || [];
  el("st-meetings").textContent = d.meetings.available ? String(meetings.length) : "—";
  el("st-meetings-hint").textContent = !d.meetings.available
    ? d.meetings.reason || "календарь не подключён"
    : meetings.length
      ? `дальше: ${meetings.find((m) => !m.all_day)?.time || meetings[0].time}`
      : "свободный день";

  el("st-overdue").textContent = String(d.tasks.overdue);
  el("st-overdue").classList.toggle("late", d.tasks.overdue > 0);
  el("st-tasks-hint").textContent = `открыто ${d.tasks.open}, на неделе ${d.tasks.week}`;

  const outside = d.kpi.critical + d.kpi.warning;
  el("st-kpi").textContent = String(outside);
  el("st-kpi-hint").textContent = d.kpi.total ? `критичных ${d.kpi.critical} из ${d.kpi.total}` : "показателей пока нет";

  const kb = d.knowledge;
  el("st-docs").textContent = kb.enabled && kb.access ? String(kb.documents) : "—";
  el("st-docs-hint").textContent = !kb.enabled
    ? "Квант не подключён"
    : !kb.access
      ? "нет доступа по роли"
      : kb.dated ? `с датой: ${kb.dated}` : "";

  renderMeetings(d);
  renderTodayTasks(d.tasks.items);
  renderKpi(d.kpi);
  renderKnowledgeBox(kb);
  renderReminders(d.reminders);

  const failed = Object.keys(d.errors || {});
  el("today-errors").classList.toggle("hidden", failed.length === 0);
  el("today-errors").textContent = failed.length
    ? `Не загрузилось: ${failed.join(", ")}. Остальное показано. Подробности — в журнале сервера.`
    : "";
}

function renderMeetings(d) {
  const list = el("meetings-list");
  if (!d.meetings.available) {
    list.innerHTML = `<div class="empty">Календарь недоступен: ${escapeHtml(d.meetings.reason || "Google не подключён")}.</div>`;
    return;
  }
  const items = d.meetings.items || [];
  if (!items.length) {
    list.innerHTML = '<div class="empty">На сегодня встреч нет.</div>';
    return;
  }
  const day = d.today.label;
  list.innerHTML = items.map((m) => {
    const when = m.all_day ? "весь день" : `${m.time}${m.end ? "–" + m.end : ""}`;
    const meta = [m.location, m.attendees ? `участников: ${m.attendees}` : "", m.organizer && m.organizer !== "" ? `организатор: ${m.organizer}` : ""]
      .filter(Boolean).join(" · ");
    const timeText = m.all_day ? "" : m.time;
    return `
      <div class="row">
        <div class="row-time">${escapeHtml(timeText || "•")}</div>
        <div class="row-main">
          <div class="row-title">${escapeHtml(m.title)}</div>
          <div class="row-meta">${escapeHtml(when)}${meta ? " · " + escapeHtml(meta) : ""}</div>
        </div>
        <div class="row-actions">
          <button type="button" class="btn btn-ghost btn-small" data-ask="${escapeHtml(`Перенеси встречу «${m.title}» (${day}, ${when}) на `)}">Перенести</button>
          <button type="button" class="btn btn-ghost btn-small" data-ask="${escapeHtml(`Удали встречу «${m.title}» (${day}, ${when})`)}">Удалить</button>
        </div>
      </div>`;
  }).join("");
}

function dueText(task) {
  if (!task.due) return '<span class="row-meta">без срока</span>';
  if (task.overdue) return `<span class="late">просрочено на ${plural(Math.abs(task.days_left), "день", "дня", "дней")}</span>`;
  if (task.days_left === 0) return '<span class="today-mark">сегодня</span>';
  return `<span class="row-meta">до ${escapeHtml(task.due_ru)}</span>`;
}

function taskRow(task) {
  const badge = task.priority === "high" ? '<span class="tag crit">важно</span>' : "";
  const who = task.assignee ? ` · ${escapeHtml(task.assignee)}` : "";
  return `
    <div class="row">
      <div class="row-main">
        <div class="row-title">${escapeHtml(task.title)} ${badge}</div>
        <div class="row-meta">${dueText(task)}${who} · ${escapeHtml(task.status_ru)} · ${escapeHtml(task.id)}</div>
      </div>
      <div class="row-actions">
        <button type="button" class="btn btn-ghost btn-small" data-ask="${escapeHtml(`Отметь поручение «${task.title}» (${task.id}) выполненным`)}">Закрыть</button>
        <button type="button" class="btn btn-ghost btn-small" data-ask="${escapeHtml(`Перенеси срок поручения «${task.title}» (${task.id}) на `)}">Срок</button>
      </div>
    </div>`;
}

function renderTodayTasks(items) {
  const urgent = items.filter((t) => t.overdue || t.days_left === 0 || (t.days_left !== null && t.days_left <= 7));
  const shown = (urgent.length ? urgent : items).slice(0, 6);
  el("today-tasks").innerHTML = shown.length
    ? shown.map(taskRow).join("")
    : '<div class="empty">Открытых поручений нет.</div>';
}

function renderKpi(kpi) {
  if (!kpi.total) {
    el("kpi-list").innerHTML = '<div class="empty">Показатели ещё не заведены. Их можно добавить в чате: «заведи показатель …».</div>';
    return;
  }
  if (!kpi.items.length) {
    el("kpi-list").innerHTML = '<div class="empty">Все показатели в норме.</div>';
    return;
  }
  el("kpi-list").innerHTML = kpi.items.map((k) => {
    const tag = k.status === "critical" ? '<span class="tag crit">критично</span>' : '<span class="tag warn">внимание</span>';
    const dev = k.deviation_pct === null || k.deviation_pct === undefined ? "" : ` · отклонение ${k.deviation_pct > 0 ? "+" : ""}${k.deviation_pct}%`;
    const num = (v) => (v === null || v === undefined ? "—" : `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(v)}${k.unit ? " " + k.unit : ""}`);
    return `
      <div class="row kpi-row clickable" data-ask="${escapeHtml(`Разбери отклонение по показателю «${k.name}» за ${k.period}: почему и что делать`)}">
        <div class="row-main">
          <div class="kpi-name">${escapeHtml(k.name)}</div>
          <div class="kpi-nums">${escapeHtml(k.period)} · план ${escapeHtml(num(k.plan))} · факт ${escapeHtml(num(k.fact))}${escapeHtml(dev)}</div>
        </div>
        ${tag}
      </div>`;
  }).join("");
}

function renderKnowledgeBox(kb) {
  const box = el("knowledge-box");
  if (!kb.enabled) {
    box.innerHTML = '<div class="empty">Квант не подключён на сервере. Документы берутся из папки базы знаний.</div>';
    return;
  }
  if (!kb.access) {
    box.innerHTML = `<div class="empty">${escapeHtml(kb.reason || "Документы недоступны: нет роли на сайте базы знаний.")}</div>`;
    return;
  }
  const sources = Object.entries(kb.by_source || {}).map(([k, v]) => `${escapeHtml(k)}: ${v}`).join(" · ");
  const collections = (kb.collections || []).map((c) => `${escapeHtml(c.name)} — ${c.points ?? "?"} фрагм.`).join(" · ");
  box.innerHTML = `
    <div class="kb-summary">
      <span><b>${kb.documents}</b> документов</span>
      <span><b>${kb.dated}</b> с датой</span>
      ${kb.latest ? `<span>последнее: <b>${escapeHtml(kb.latest)}</b></span>` : ""}
    </div>
    ${sources ? `<div class="card-note">${sources}</div>` : ""}
    ${collections ? `<div class="card-note">${collections}</div>` : ""}
    <div class="list" style="margin-top:10px">
      ${(kb.items || []).map((d) => `
        <div class="row doc-row clickable" data-ask="${escapeHtml(`Что написано в документе «${d.title}»? Коротко, со ссылкой на источник`)}">
          <div class="row-main">
            <div class="row-title">${escapeHtml(d.title)}</div>
            <div class="doc-date">${escapeHtml(d.source)}${d.date ? " · " + escapeHtml(d.date) : ""}${d.topic_status ? " · " + escapeHtml(d.topic_status) : ""}</div>
          </div>
        </div>`).join("") || '<div class="empty">Документов пока нет.</div>'}
    </div>`;
}

function renderReminders(rem) {
  const list = el("reminders-list");
  if (!rem.active) {
    list.innerHTML = '<div class="empty">Активных напоминаний нет.</div>';
    return;
  }
  list.innerHTML = rem.items.map((r) => `
    <div class="row">
      <div class="row-time">${escapeHtml(r.at.slice(11, 16) || "•")}</div>
      <div class="row-main">
        <div class="row-title">${escapeHtml(r.text)}</div>
        <div class="row-meta">${escapeHtml(r.at.slice(0, 10))}${r.repeat ? " · повтор" : ""}</div>
      </div>
      <div class="row-actions">
        <button type="button" class="btn btn-ghost btn-small" data-ask="${escapeHtml(`Отмени напоминание ${r.id}`)}">Отменить</button>
      </div>
    </div>`).join("");
}

/* Новая встреча: короткая форма → текст запроса в чат → карточка подтверждения. */

function nextFullHour() {
  const now = new Date();
  now.setMinutes(0, 0, 0);
  now.setHours(now.getHours() + 1);
  return now;
}

el("new-meeting-toggle").addEventListener("click", () => {
  const form = el("meeting-form");
  const open = form.classList.toggle("hidden") === false;
  if (open) {
    const start = nextFullHour();
    form.elements.date.value = start.toLocaleDateString("sv-SE");
    form.elements.time.value = `${String(start.getHours()).padStart(2, "0")}:00`;
    form.elements.title.focus();
  }
});

el("meeting-cancel").addEventListener("click", () => el("meeting-form").classList.add("hidden"));

el("meeting-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const title = form.elements.title.value.trim();
  if (!title) return;
  const [y, m, d] = form.elements.date.value.split("-");
  let text = `Создай встречу «${title}» ${d}.${m}.${y} в ${form.elements.time.value}, длительность ${form.elements.duration.value} мин`;
  const place = form.elements.place.value.trim();
  const guests = form.elements.guests.value.trim();
  if (place) text += `, место: ${place}`;
  if (guests) text += `, участники: ${guests}`;
  form.reset();
  form.classList.add("hidden");
  askInChat(text);
});

/* ------------------------------------------------------------------ */
/* Поручения                                                           */
/* ------------------------------------------------------------------ */

async function loadTasks() {
  try {
    if (!dashboard) dashboard = await fetchDashboard();
    renderTasks();
  } catch (err) {
    el("tasks-sub").textContent = `Не удалось загрузить: ${err.message}`;
  }
}

function renderTasks() {
  if (!dashboard) return;
  const tasks = dashboard.tasks;
  el("tasks-sub").textContent = `Открыто ${tasks.open}, просрочено ${tasks.overdue}. Закрыть или перенести срок можно из карточки — изменение пойдёт через подтверждение.`;
  const filters = {
    all: () => true,
    overdue: (t) => t.overdue,
    today: (t) => t.days_left === 0,
    week: (t) => t.days_left !== null && t.days_left >= 0 && t.days_left <= 7,
    nodate: (t) => !t.due,
  };
  const rows = tasks.items.filter(filters[taskFilter] || filters.all);
  el("tasks-list").innerHTML = rows.length
    ? rows.map(taskRow).join("")
    : `<div class="empty">${taskFilter === "all" ? "Открытых поручений нет." : "По этому фильтру пусто."}</div>`;
  document.querySelectorAll("#task-filters .chip-btn").forEach((b) => b.classList.toggle("active", b.dataset.filter === taskFilter));
}

el("task-filters").addEventListener("click", (event) => {
  const chip = event.target.closest(".chip-btn");
  if (!chip) return;
  taskFilter = chip.dataset.filter;
  renderTasks();
});

el("new-task").addEventListener("click", () => askInChat("Поставь поручение: "));

/* ------------------------------------------------------------------ */
/* Документы «Квант»                                                   */
/* ------------------------------------------------------------------ */

async function loadDocuments() {
  try {
    const response = await fetch("/api/documents");
    if (response.status === 401 && !inTelegram) {
      window.location.href = "/login";
      return;
    }
    const data = await response.json();
    documents = data.items || [];
    if (!data.enabled) {
      el("docs-sub").textContent = "Квант не подключён на сервере.";
    } else if (!data.access) {
      el("docs-sub").textContent = data.reason || "Документы недоступны: нет роли на сайте базы знаний.";
    } else {
      el("docs-sub").textContent = `Документов: ${data.documents}, с датой: ${data.dated}${data.latest ? `, последнее: ${data.latest}` : ""}. Видны только документы вашей роли.`;
    }
    renderDocuments();
  } catch (err) {
    el("docs-sub").textContent = `Не удалось загрузить: ${err.message}`;
  }
}

function renderDocuments() {
  const query = el("docs-search").value.trim().toLowerCase();
  const rows = documents.filter((d) => !query || d.title.toLowerCase().includes(query));
  el("docs-list").innerHTML = rows.map((d) => {
    const status = d.topic_status ? `<span class="tag ${d.topic_status === "утверждена" ? "ok" : "warn"}">${escapeHtml(d.topic_status)}</span>` : "";
    const date = d.date ? `${escapeHtml(d.date)}${d.date_meaning ? " — " + escapeHtml(d.date_meaning) : ""}` : "без даты";
    const open = d.link ? `<a class="btn btn-ghost btn-small" href="${escapeHtml(d.link)}" target="_blank" rel="noopener">Открыть</a>` : "";
    return `
      <div class="row">
        <div class="row-main">
          <div class="row-title">${escapeHtml(d.title)} ${status}</div>
          <div class="row-meta">${escapeHtml(d.source)} · ${date} · фрагментов: ${d.fragments}</div>
        </div>
        <div class="row-actions">
          ${open}
          <button type="button" class="btn btn-ghost btn-small" data-ask="${escapeHtml(`Что написано в документе «${d.title}»? Коротко, со ссылкой на источник`)}">Спросить</button>
        </div>
      </div>`;
  }).join("") || `<div class="empty">${documents.length ? "Ничего не найдено." : "Документов пока нет. Загрузите их на сайте базы знаний или подключите Google Drive."}</div>`;
}

el("docs-search").addEventListener("input", renderDocuments);

/* ------------------------------------------------------------------ */
/* Настройки: Google, база, баланс, модель                             */
/* ------------------------------------------------------------------ */

const FAMILY_NOTE = {
  deepseek: "DeepSeek",
  gpt: "GPT (OpenAI)",
  claude: "Claude (Anthropic)",
};
let familyCounts = [];

function renderGoogleCard(google) {
  if (!google.connected) {
    el("google-state").innerHTML = `
      <dl class="kv"><dt>Статус</dt><dd><span class="tag warn">не подключён</span></dd></dl>
      <div class="card-note">${escapeHtml(google.reason || "Google не подключён")}. Откройте бота в Telegram и отправьте команду /auth — он покажет вход в Google.</div>`;
    return;
  }
  const how = { delegated: "от имени пользователя (организация)", shared: "открытый боту календарь" }[google.mode] || "вход через Google";
  el("google-state").innerHTML = `
    <dl class="kv">
      <dt>Статус</dt><dd><span class="tag ok">подключён</span></dd>
      <dt>Почта</dt><dd>${escapeHtml(google.account || "определена")}</dd>
      <dt>Способ</dt><dd>${escapeHtml(how)}</dd>
    </dl>
    <div class="card-note">Календарь, Диск и таблицы доступны ассистенту. Создание и изменение — только после подтверждения.</div>`;
}

function renderQuantCard(kb) {
  if (!kb.enabled) {
    el("quant-state").innerHTML = '<div class="empty">Квант не подключён на сервере.</div>';
    return;
  }
  const collections = (kb.collections || []).map((c) => `${escapeHtml(c.name)} · ${c.points ?? "?"} фрагм.`).join("<br>");
  el("quant-state").innerHTML = `
    <dl class="kv">
      <dt>Доступ</dt><dd>${kb.access ? `по роли «${escapeHtml(kb.role || "—")}»` : `<span class="tag crit">нет роли</span>`}</dd>
      <dt>Документов</dt><dd>${kb.access ? kb.documents : "—"}</dd>
      <dt>Коллекции</dt><dd>${collections || "—"}</dd>
    </dl>
    ${kb.access ? "" : `<div class="card-note">${escapeHtml(kb.reason || "Нужна роль на сайте базы знаний.")}</div>`}`;
}

async function loadSettings() {
  const note = el("balance-note");
  note.classList.remove("error");
  note.textContent = "Загружаю…";
  try {
    const [billing, models, dash] = await Promise.all([
      fetch("/api/billing").then((r) => r.json()),
      fetch("/api/models").then((r) => r.json()),
      dashboard ? Promise.resolve(dashboard) : fetchDashboard().catch(() => null),
    ]);
    if (dash) {
      dashboard = dash;
      renderGoogleCard(dash.google);
      renderQuantCard(dash.knowledge);
    }
    currentModel = (billing.current && billing.current.model) || "";
    if (!billing.available) {
      el("balance-value").textContent = "—";
      note.textContent = "Баланс и каталог доступны при работе через RouterAI (ROUTERAI_API_KEY).";
      el("model-groups").innerHTML = '<div class="card-note">Список моделей появится, когда на сервере подключён RouterAI.</div>';
      renderCurrent(billing.model);
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
    familyCounts = models.families || [];
    if (!models.available) {
      el("model-groups").innerHTML = `<div class="card-note error">${escapeHtml(models.detail || "Каталог недоступен.")}</div>`;
    } else {
      renderModels();
    }
    renderCurrent(billing.model);
  } catch (err) {
    note.textContent = `Сбой соединения: ${err.message}`;
    note.classList.add("error");
  }
}

function renderCurrent(model) {
  const info = catalog.find((m) => m.id === currentModel) || model;
  el("current-name").textContent = (info && info.name) || currentModel || "не выбрана";
  el("current-id").textContent = currentModel;
  facts(el("current-facts"), info ? [
    ["Семейство", FAMILY_NOTE[info.family] || (dashboard && dashboard.model && dashboard.model.family) || ""],
    ["Вход, 1 млн токенов", price(info.price_in)],
    ["Выход, 1 млн токенов", price(info.price_out)],
    ["Контекст", info.context_length ? `${new Intl.NumberFormat("ru-RU").format(info.context_length)} токенов` : null],
  ] : []);
}

function modelRow(m) {
  return `
    <li class="model-item${m.id === currentModel ? " selected" : ""}" data-id="${escapeHtml(m.id)}">
      <span class="m-name">${escapeHtml(m.name)}${m.tools === null ? ' <span class="m-flag">инструменты не подтверждены</span>' : ""}</span>
      <span class="m-price">${escapeHtml(price(m.price_in))}<br>${escapeHtml(price(m.price_out))}</span>
      <span class="m-id">${escapeHtml(m.id)}</span>
    </li>`;
}

function renderModels() {
  const query = el("model-search").value.trim().toLowerCase();
  const sort = el("model-sort").value;
  const list = catalog.filter((m) => !query || m.name.toLowerCase().includes(query) || m.id.toLowerCase().includes(query));
  const cost = (m) => (m.price_in ?? Infinity) + (m.price_out ?? Infinity);
  const sorted = (items) => (sort === "price" ? [...items].sort((a, b) => cost(a) - cost(b)) : items);
  const html = familyCounts.map((family) => {
    const items = sorted(list.filter((m) => m.family === family.key));
    if (!items.length) return "";
    return `<div class="fam-title">${escapeHtml(family.label)} <span>${items.length}</span></div>
      <ul class="model-list">${items.map(modelRow).join("")}</ul>`;
  }).join("");
  el("model-groups").innerHTML = html || '<div class="card-note">Ничего не найдено.</div>';
}

el("model-search").addEventListener("input", renderModels);
el("model-sort").addEventListener("change", renderModels);

el("model-groups").addEventListener("click", async (event) => {
  const item = event.target.closest(".model-item");
  if (!item || item.dataset.id === currentModel) return;
  const model = catalog.find((m) => m.id === item.dataset.id);
  const agreed = await ask(`Переключить ассистента на «${model ? model.name : item.dataset.id}»? Это действует и в чате бота.`);
  if (!agreed) return;
  const who = inTelegram && tg.initDataUnsafe && tg.initDataUnsafe.user ? String(tg.initDataUnsafe.user.id) : "web";
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

el("google-help").addEventListener("click", () => askInChat("Как подключить Google к моему аккаунту? Коротко, по шагам.", true));

/* ------------------------------------------------------------------ */
/* Запуск                                                              */
/* ------------------------------------------------------------------ */

(async () => {
  if (inTelegram && !(await telegramLogin())) return;
  loadStatus();
  showView("today");
  if (!inTelegram) input.focus();
})();
