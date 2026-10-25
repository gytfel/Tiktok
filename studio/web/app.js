"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const DRAFT_KEY = "tiktok-studio-draft-v1";

const state = {
  defaults: null,
  project: null,
  status: null,
  fonts: {},
  voices: [],
  uploads: [],
  videos: [],
  jobId: null,
  pollTimer: null,
  previewWord: 0,
  aiBusy: {}, // номер сцены -> текст состояния, пока идёт генерация
  tunnelTimer: null,
};

const SAMPLE_TEXT = `Знаете, почему осьминогов считают одними из самых умных существ на планете?

У осьминога три сердца и голубая кровь. А две трети его нейронов находятся не в голове, а в щупальцах.

Каждое щупальце может само принимать решения: исследовать, хватать и даже пробовать на вкус.

Подпишись, если хочешь больше таких фактов!`;

const SUB_PRESETS = [
  { name: "Классика", values: { color: "#FFFFFF", highlight: "#FFE600", outline_color: "#000000", outline: 6, shadow: 3, style: "outline", uppercase: true, highlight_mode: "color" } },
  { name: "Неон", values: { color: "#FFFFFF", highlight: "#39FF14", outline_color: "#000000", outline: 7, shadow: 0, style: "outline", uppercase: true, highlight_mode: "scale" } },
  { name: "Розовый", values: { color: "#FFFFFF", highlight: "#FE2C55", outline_color: "#000000", outline: 6, shadow: 2, style: "outline", uppercase: true, highlight_mode: "scale" } },
  { name: "Плашка", values: { color: "#FFFFFF", highlight: "#25F4EE", box_color: "#000000", box_opacity: 0.65, outline: 6, style: "box", uppercase: false, highlight_mode: "color" } },
  { name: "Минимал", values: { color: "#FFFFFF", highlight: "#FFFFFF", outline_color: "#000000", outline: 0, shadow: 4, style: "outline", uppercase: false, highlight_mode: "none" } },
];

// ------------------------------------------------------------------ утилиты

const clone = (o) => JSON.parse(JSON.stringify(o));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function getPath(obj, path) {
  return path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
}
function setPath(obj, path, value) {
  const keys = path.split(".");
  const last = keys.pop();
  const target = keys.reduce((o, k) => (o[k] ??= {}), obj);
  target[last] = value;
}
function mergeDefaults(base, data) {
  const out = clone(base);
  if (!data || typeof data !== "object") return out;
  for (const [k, v] of Object.entries(data)) {
    if (!(k in out)) continue;
    if (out[k] && typeof out[k] === "object" && !Array.isArray(out[k]) && v && typeof v === "object" && !Array.isArray(v)) {
      out[k] = mergeDefaults(out[k], v);
    } else {
      out[k] = v;
    }
  }
  return out;
}
function hex(v, fallback = "#ffffff") {
  return typeof v === "string" && /^#?[0-9a-f]{6}$/i.test(v) ? (v.startsWith("#") ? v : "#" + v).toLowerCase() : fallback;
}
function rgba(h, a) {
  const v = hex(h).slice(1);
  return `rgba(${parseInt(v.slice(0, 2), 16)},${parseInt(v.slice(2, 4), 16)},${parseInt(v.slice(4, 6), 16)},${a})`;
}
function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}
function fmtDuration(sec) {
  sec = Math.round(sec || 0);
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}

function toast(msg, type = "info", ms = 5000) {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), ms);
}

async function api(url, opts = {}) {
  const init = { ...opts };
  if (init.body && !(init.body instanceof FormData) && typeof init.body !== "string") {
    init.body = JSON.stringify(init.body);
    init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
  }
  const res = await fetch(url, init);
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch (_) { /* не JSON */ }
    throw new Error(msg);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res;
}

// ------------------------------------------------------------------ черновик

function saveDraft() {
  try { localStorage.setItem(DRAFT_KEY, JSON.stringify(state.project)); } catch (_) { /* приватный режим */ }
}
function loadDraft() {
  try { return JSON.parse(localStorage.getItem(DRAFT_KEY) || "null"); } catch (_) { return null; }
}

// ------------------------------------------------------------------ привязка полей

function readEl(el) {
  if (el.type === "checkbox") {
    if (el.dataset.true !== undefined) return el.checked ? el.dataset.true : el.dataset.false;
    return el.checked;
  }
  if (el.type === "range" || el.type === "number") return Number(el.value);
  if (el.dataset.bind === "music.file") return el.value || null;
  return el.value;
}
function writeEl(el, v) {
  if (el.type === "checkbox") el.checked = el.dataset.true !== undefined ? v === el.dataset.true : !!v;
  else if (el.type === "color") el.value = hex(v);
  else el.value = v ?? "";
}
function bindFields() {
  $$("[data-bind]").forEach((el) => {
    const evt = el.tagName === "SELECT" || el.type === "checkbox" ? "change" : "input";
    el.addEventListener(evt, () => {
      setPath(state.project, el.dataset.bind, readEl(el));
      onChange(el.dataset.bind);
    });
  });
}
function syncForm() {
  $$("[data-bind]").forEach((el) => {
    if (el.id === "voiceSelect") return;
    writeEl(el, getPath(state.project, el.dataset.bind));
  });
  $$("[data-grad]").forEach((el) => { el.value = hex(state.project.background.gradient[Number(el.dataset.grad)], "#202040"); });
  $$("#bgType button").forEach((b) => b.classList.toggle("active", b.dataset.value === state.project.background.type));
  showBgSection();
  markGradientSwatch();
  fillVoiceSelect(true);
  updateVoiceWarning();
  renderMedia();
  renderScenes();
  renderMusicSelect();
  updateOutputs();
  updateStats();
  renderPreview();
}
function updateOutputs() {
  $$("[data-out]").forEach((o) => {
    const v = getPath(state.project, o.dataset.out);
    let text;
    if (o.dataset.zero !== undefined && Number(v) === 0) text = o.dataset.zero;
    else if (o.dataset.percent !== undefined) text = `${Math.round(Number(v) * 100)}%`;
    else text = `${o.dataset.sign !== undefined && v > 0 ? "+" : ""}${v}${o.dataset.suffix || ""}`;
    o.textContent = text;
  });
}
function updateVoiceWarning() {
  // Та же проверка, что на сервере: английский и т.п. голос не прочитает русский текст.
  const voice = state.project.voice.voice || "";
  const letters = (state.project.text || "").match(/\p{L}/gu) || [];
  const cyr = letters.filter((c) => /[\u0400-\u04FF]/.test(c)).length;
  const lang = voice.split("-")[0].toLowerCase();
  const ok = !voice || voice.startsWith("system:") || /multilingual/i.test(voice) ||
    ["ru", "uk", "be", "bg", "sr", "mk", "kk", "ky", "mn", "tg", "tt", "ba"].includes(lang);
  const el = $("#voiceWarn");
  el.hidden = ok || !letters.length || cyr / letters.length <= 0.3;
  el.textContent = "Этот голос не умеет читать русский текст — Microsoft не пришлёт звук. Выберите русский голос или голос с пометкой Multilingual.";
}
function onChange(path) {
  updateOutputs();
  if (path === "text" || path === "voice.voice") updateVoiceWarning();
  if (path === "text" || path === "voice.rate") updateStats();
  if (path === "text") renderScenes();
  if (path.startsWith("music")) updateMusicPlayer();
  renderPreview();
  saveDraft();
}

// ------------------------------------------------------------------ текст

function tokenize(text) {
  const tokens = [];
  for (const raw of text.split(/\s+/)) {
    if (!raw) continue;
    if (!/[\p{L}\p{N}]/u.test(raw)) {
      if (tokens.length) tokens[tokens.length - 1] += " " + raw;
      continue;
    }
    tokens.push(raw);
  }
  return tokens;
}
function firstChunk(words, perChunk) {
  const chunk = [];
  for (const w of words) {
    chunk.push(w);
    if (chunk.length >= perChunk || /[.!?…,;:—–-]["»')\]]*$/.test(w)) break;
  }
  return chunk;
}
function updateStats() {
  const text = state.project.text || "";
  const words = tokenize(text.replace(/\p{Extended_Pictographic}/gu, "")).length;
  const chars = text.replace(/\s+/g, " ").trim().length;
  const speed = 14 * (1 + (state.project.voice.rate || 0) / 100);
  const sec = Math.round(chars / Math.max(speed, 1));
  const warn = sec > 180 ? " · длиннее 3 минут" : "";
  $("#textStats").textContent = `${words} ${plural(words, "слово", "слова", "слов")} · ~${sec} сек${warn}`;
}

// ------------------------------------------------------------------ голоса

function voiceGroup(v) {
  if (v.id.startsWith("system:")) return "system";
  if (/Multilingual/i.test(v.id)) return "multi";
  return (v.locale || "").split("-")[0].toLowerCase();
}
function genderLabel(g) {
  return g === "Male" ? "муж." : g === "Female" ? "жен." : "";
}
function fillVoiceSelect(keepLang) {
  const sel = $("#voiceSelect");
  const langSel = $("#voiceLang");
  const cur = state.project.voice.voice;
  if (keepLang) {
    const v = state.voices.find((x) => x.id === cur);
    if (v) langSel.value = [...langSel.options].some((o) => o.value === voiceGroup(v)) ? voiceGroup(v) : "all";
  }
  const lang = langSel.value;
  const list = state.voices.filter((v) => lang === "all" || voiceGroup(v) === lang);
  sel.innerHTML = list
    .map((v) => {
      const extra = [genderLabel(v.gender), lang === "all" || lang === "multi" ? v.locale : ""].filter(Boolean).join(", ");
      return `<option value="${esc(v.id)}">${esc(v.name)}${extra ? " — " + esc(extra) : ""}</option>`;
    })
    .join("");
  if (!list.length) {
    sel.innerHTML = `<option value="">${lang === "system" ? "Системные голоса недоступны (нужен pyttsx3)" : "Нет голосов"}</option>`;
    return;
  }
  if (list.some((v) => v.id === cur)) {
    sel.value = cur;
  } else {
    sel.value = list[0].id;
    state.project.voice.voice = list[0].id;
    saveDraft();
  }
}
async function loadVoices() {
  try {
    const data = await api("/api/voices");
    state.voices = data.voices;
    $("#voiceHint").textContent = data.source === "fallback"
      ? "Нет связи с каталогом голосов — показаны основные. Для озвучки нужен интернет."
      : `Доступно голосов: ${data.voices.length}`;
  } catch (e) {
    $("#voiceHint").textContent = "Не удалось загрузить голоса: " + e.message;
  }
  fillVoiceSelect(true);
}
async function previewVoice() {
  const btn = $("#previewVoiceBtn");
  const words = tokenize(state.project.text || "");
  const sample = words.length ? words.slice(0, 18).join(" ") : "";
  btn.disabled = true;
  btn.lastChild.textContent = " Синтез…";
  try {
    const res = await api("/api/voice-preview", { method: "POST", body: { ...state.project.voice, text: sample } });
    const blob = await res.blob();
    const audio = $("#voiceAudio");
    if (audio.src) URL.revokeObjectURL(audio.src);
    audio.src = URL.createObjectURL(blob);
    try {
      await audio.play();
    } catch (_) {
      // iPhone не разрешает включить звук без отдельного нажатия — показываем плеер.
      audio.controls = true;
      audio.hidden = false;
      toast("Нажмите ▶ в плеере под кнопкой, чтобы прослушать");
    }
  } catch (e) {
    toast(e.message, "error", 9000);
  } finally {
    btn.disabled = false;
    btn.lastChild.textContent = " Прослушать голос";
  }
}

// ------------------------------------------------------------------ шрифты и пресеты

function setupFonts() {
  const css = state.status.fonts.map((f) => `@font-face{font-family:"${f.key}";src:url("/fonts/${f.file}") format("truetype");font-display:swap;}`).join("\n");
  const style = document.createElement("style");
  style.textContent = css;
  document.head.append(style);
  $("#fontSelect").innerHTML = state.status.fonts.map((f) => `<option value="${f.key}" style="font-family:'${f.key}'">${esc(f.label)}</option>`).join("");
  for (const f of state.status.fonts) state.fonts[f.key] = f;
  document.fonts?.ready?.then(renderPreview);
}
function setupPresets() {
  $("#subPresets").innerHTML = SUB_PRESETS.map((p, i) => {
    const v = p.values;
    const look = v.style === "box"
      ? `background:${rgba(v.box_color, v.box_opacity)};color:${v.color}`
      : `color:${v.color};text-shadow:0 0 2px #000,0 0 3px #000`;
    return `<button class="preset" data-i="${i}" style="${look}">${esc(p.name)} <span style="color:${v.highlight}">●</span></button>`;
  }).join("");
  $("#subPresets").addEventListener("click", (e) => {
    const b = e.target.closest(".preset");
    if (!b) return;
    Object.assign(state.project.subtitles, SUB_PRESETS[Number(b.dataset.i)].values);
    syncForm();
    saveDraft();
  });
}

// ------------------------------------------------------------------ фон

function setupBackground() {
  const sw = $("#gradientSwatches");
  sw.innerHTML = Object.entries(state.status.gradients)
    .map(([name, c]) => `<button class="swatch" title="${name}" data-name="${name}" style="background:linear-gradient(180deg,${c.join(",")})"></button>`)
    .join("");
  sw.addEventListener("click", (e) => {
    const b = e.target.closest(".swatch");
    if (!b) return;
    state.project.background.gradient = [...state.status.gradients[b.dataset.name]];
    syncForm();
    saveDraft();
  });
  $$("[data-grad]").forEach((el) =>
    el.addEventListener("input", () => {
      const g = state.project.background.gradient;
      while (g.length < 3) g.push(g[g.length - 1] || "#202040");
      g[Number(el.dataset.grad)] = el.value;
      markGradientSwatch();
      renderPreview();
      saveDraft();
    })
  );
  $("#bgType").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    state.project.background.type = b.dataset.value;
    $$("#bgType button").forEach((x) => x.classList.toggle("active", x === b));
    showBgSection();
    renderPreview();
    saveDraft();
  });
}
function showBgSection() {
  const type = state.project.background.type;
  $$(".bg-section").forEach((s) => s.classList.toggle("active", s.dataset.bg === type));
  $("#bgCommon").hidden = type === "gradient";
  $$(".not-ai").forEach((el) => { el.hidden = type === "ai"; });
  renderAiStatus();
  const st = state.status?.settings;
  $("#pexelsKeyState").textContent = st?.pexels_api_key_set ? `Ключ сохранён (${st.pexels_api_key_hint})` : "Ключ ещё не указан.";
}
function markGradientSwatch() {
  const cur = state.project.background.gradient.map((c) => hex(c)).join();
  $$(".swatch").forEach((s) => {
    const colors = state.status.gradients[s.dataset.name].map((c) => hex(c)).join();
    s.classList.toggle("active", colors === cur);
  });
}

// ------------------------------------------------------------------ медиа

function mediaTile(u, opts) {
  const pic = u.thumb_url ? `<img src="${esc(u.thumb_url)}" alt="" loading="lazy">` : `<div class="noimg">${esc(u.name)}</div>`;
  const kind = u.kind === "video" ? `видео ${u.duration ? Math.round(u.duration) + "с" : ""}` : "фото";
  return `<div class="media-tile ${opts.cls || ""}" data-id="${esc(u.id)}" title="${esc(u.name)}" tabindex="0">
    ${pic}${opts.badge ? `<span class="badge">${opts.badge}</span>` : ""}<span class="kind">${kind}</span>
    <div class="tools">${opts.tools}</div></div>`;
}
function renderMedia() {
  const byId = Object.fromEntries(state.uploads.map((u) => [u.id, u]));
  const bg = state.project.background;
  bg.media = bg.media.filter((id) => byId[id]);
  $("#sequence").innerHTML = bg.media
    .map((id, i) => mediaTile(byId[id], {
      badge: i + 1,
      tools: `<button data-act="left" title="Раньше">←</button><button data-act="remove" title="Убрать из ролика">✕</button><button data-act="right" title="Позже">→</button>`,
    }))
    .join("");
  const visual = state.uploads.filter((u) => u.kind !== "audio");
  $("#mediaLibrary").innerHTML = visual
    .map((u) => mediaTile(u, {
      cls: bg.media.includes(u.id) ? "in-seq" : "",
      tools: `<button data-act="add" title="Добавить в ролик">+</button><button data-act="delete" title="Удалить файл">🗑</button>`,
    }))
    .join("");
}
function setupMedia() {
  $("#sequence").addEventListener("click", (e) => {
    const tile = e.target.closest(".media-tile");
    const act = e.target.closest("button")?.dataset.act;
    if (!tile || !act) return;
    const list = state.project.background.media;
    const i = [...$("#sequence").children].indexOf(tile);
    if (act === "remove") list.splice(i, 1);
    if (act === "left" && i > 0) [list[i - 1], list[i]] = [list[i], list[i - 1]];
    if (act === "right" && i < list.length - 1) [list[i + 1], list[i]] = [list[i], list[i + 1]];
    renderMedia();
    renderPreview();
    saveDraft();
  });
  $("#mediaLibrary").addEventListener("click", async (e) => {
    const tile = e.target.closest(".media-tile");
    if (!tile) return;
    const id = tile.dataset.id;
    const act = e.target.closest("button")?.dataset.act || "add";
    if (act === "delete") {
      if (!confirm("Удалить файл из библиотеки?")) return;
      await api(`/api/uploads/${id}`, { method: "DELETE" }).catch((err) => toast(err.message, "error"));
      await loadUploads();
      return;
    }
    state.project.background.media.push(id);
    renderMedia();
    renderPreview();
    saveDraft();
  });

  const dz = $("#dropzone");
  $("#pickFiles").addEventListener("click", () => $("#fileInput").click());
  $("#fileInput").addEventListener("change", (e) => { handleFiles([...e.target.files]); e.target.value = ""; });
  ["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => handleFiles([...e.dataTransfer.files]));
}
const TUNNEL_UPLOAD_LIMIT = 95 * 1024 * 1024; // Cloudflare не пропускает загрузки больше 100 МБ
function uploadOne(file, onProgress) {
  // Файл уходит телом запроса как есть — без multipart-формы и временных файлов на сервере.
  if (location.hostname.endsWith(".trycloudflare.com") && file.size > TUNNEL_UPLOAD_LIMIT) {
    return Promise.reject(new Error(`«${file.name}»: через интернет можно загрузить файл до 100 МБ, а этот — ${Math.round(file.size / 1048576)} МБ. Обрежьте видео или загрузите его на компьютере.`));
  }
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded); };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (_) { /* пусто */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error(`«${file.name}»: ${data?.detail || `ошибка загрузки (${xhr.status})`}`));
    };
    xhr.onerror = () => reject(new Error(`«${file.name}»: связь с сервером студии прервалась`));
    xhr.open("POST", `/api/uploads/raw?name=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.send(file);
  });
}
async function uploadFiles(files) {
  const bar = $("#uploadProgress");
  const total = files.reduce((n, f) => n + f.size, 0) || 1;
  let done = 0;
  const saved = [];
  const errors = [];
  bar.hidden = false;
  try {
    for (const f of files) {
      try {
        saved.push(await uploadOne(f, (loaded) => { bar.firstElementChild.style.width = `${((done + loaded) / total) * 100}%`; }));
      } catch (e) {
        errors.push(e.message);
      }
      done += f.size;
    }
  } finally {
    bar.hidden = true;
    bar.firstElementChild.style.width = "0";
  }
  if (errors.length && !saved.length) throw new Error(errors.join("\n"));
  if (errors.length) toast("Не все файлы загрузились:\n" + errors.join("\n"), "error", 12000);
  return saved;
}
async function handleFiles(files) {
  if (!files.length) return;
  try {
    const saved = await uploadFiles(files);
    await loadUploads();
    const visual = saved.filter((u) => u.kind !== "audio");
    const audio = saved.filter((u) => u.kind === "audio");
    if (visual.length) {
      state.project.background.media.push(...visual.map((u) => u.id));
      state.project.background.type = "media";
    }
    if (audio.length) state.project.music.file = audio[0].id;
    syncForm();
    saveDraft();
    toast(`Загружено файлов: ${saved.length}`);
  } catch (e) {
    toast(e.message, "error");
  }
}
async function loadUploads() {
  try { state.uploads = await api("/api/uploads"); } catch (e) { toast(e.message, "error"); }
  renderMedia();
  renderScenes();
  renderMusicSelect();
  renderPreview();
}

// ------------------------------------------------------------------ ИИ-картинки

function sceneMediaId(sc) {
  if (!sc) return null;
  return sc.use_video && sc.video ? sc.video : sc.image;
}
function renderAiStatus() {
  const el = $("#aiStatus");
  const st = state.status?.settings;
  if (!el || !st) return;
  if (st.ai_ready) {
    el.className = "ai-status ok";
    el.innerHTML = `<span>Нейросеть: <b>${esc(st.ai_label)}</b>${st.ai_video ? " · можно делать и видео" : " · только картинки"}</span><button class="link-btn" data-open-settings>изменить</button>`;
  } else {
    el.className = "ai-status bad";
    el.innerHTML = `<span>Нейросеть не подключена. Бесплатно — Cloudflare Workers AI, с видео — Pollinations.</span><button class="btn secondary" data-open-settings>Подключить</button>`;
  }
}
function scenesStale() {
  const scenes = state.project.background.ai_scenes;
  if (!scenes.length) return false;
  const norm = (t) => t.replace(/\p{Extended_Pictographic}/gu, "").replace(/\s+/g, " ").trim();
  return norm(scenes.map((s) => s.text).join(" ")) !== norm(state.project.text || "");
}
function renderScenes() {
  const box = $("#aiScenes");
  if (!box || !state.project) return;
  const scenes = state.project.background.ai_scenes;
  const byId = Object.fromEntries(state.uploads.map((u) => [u.id, u]));
  const canVideo = !!state.status?.settings?.ai_video;
  let html = "";
  if (scenesStale()) html += `<div class="stale">Текст изменился — нажмите «Придумать кадры», чтобы обновить сцены. Уже нарисованные картинки для неизменённых фраз сохранятся.</div>`;
  if (!scenes.length) html += `<p class="hint">Сцен пока нет. Нажмите «Придумать кадры» — текст разделится на сцены по 3–6 секунд, и для каждой нейросеть опишет кадр.</p>`;
  html += scenes.map((sc, i) => {
    const media = byId[sceneMediaId(sc)];
    const busy = state.aiBusy[i];
    const pic = media?.thumb_url ? `style="background-image:url('${esc(media.thumb_url)}')"` : "";
    const tag = sc.use_video && sc.video ? `<span class="tag">видео</span>` : "";
    const video = canVideo
      ? `<button class="btn secondary" data-ai-act="video" ${busy ? "disabled" : ""}>🎬 ${sc.video ? "Новое видео" : "Сделать видео"}</button>` +
        (sc.video ? `<label class="switch"><input type="checkbox" data-ai-act="use-video" ${sc.use_video ? "checked" : ""}><span>видео вместо картинки</span></label>` : "")
      : "";
    return `<div class="scene" data-i="${i}">
      <div class="pic ${busy ? "busy" : ""}" ${pic}>${media ? "" : "нет<br>картинки"}${tag}</div>
      <div class="body">
        <div class="head"><span>Сцена ${i + 1}</span><span>~${sc.seconds ?? "?"} сек</span></div>
        <div class="say">«${esc(sc.text)}»</div>
        <textarea data-ai-prompt placeholder="Описание кадра для нейросети (лучше на английском)">${esc(sc.prompt)}</textarea>
        <div class="acts">
          <button class="btn secondary" data-ai-act="image" ${busy ? "disabled" : ""}>🎨 ${sc.image ? "Перерисовать" : "Нарисовать"}</button>
          ${video}
          <span class="state ${busy?.startsWith("Ошибка") ? "err" : ""}">${esc(busy || "")}</span>
        </div>
      </div></div>`;
  }).join("");
  box.innerHTML = html;
}
async function aiMakeScenes(rewrite = false) {
  if (!state.project.text.trim()) {
    toast("Сначала введите текст ролика", "error");
    return false;
  }
  const btn = $("#aiScenesBtn");
  btn.disabled = true;
  btn.textContent = "Придумываю…";
  try {
    const bg = state.project.background;
    const data = await api("/api/ai/scenes", {
      method: "POST",
      body: { text: state.project.text, context: bg.ai_context, scenes: bg.ai_scenes, rewrite },
    });
    bg.ai_scenes = data.scenes;
    state.aiBusy = {};
    if (data.warning) toast(data.warning, "error", 9000);
    saveDraft();
    renderScenes();
    renderPreview();
    return true;
  } catch (e) {
    toast(e.message, "error", 9000);
    return false;
  } finally {
    btn.disabled = false;
    btn.textContent = "Придумать кадры";
  }
}
async function aiGenerate(i, kind) {
  const sc = state.project.background.ai_scenes[i];
  if (!sc || state.aiBusy[i] && !state.aiBusy[i].startsWith("Ошибка")) return;
  if (!sc.prompt.trim()) {
    toast("Сначала опишите кадр (или нажмите «Придумать кадры»)", "error");
    return;
  }
  state.aiBusy[i] = kind === "video" ? "Делаю видео — это может занять несколько минут…" : "Рисую…";
  renderScenes();
  try {
    const style = state.project.background.ai_style;
    const meta = kind === "video"
      ? await waitAiTask(await api("/api/ai/video", { method: "POST", body: { prompt: sc.prompt, style, seconds: sc.seconds || 4 } }), i)
      : await api("/api/ai/image", { method: "POST", body: { prompt: sc.prompt, style } });
    if (kind === "video") {
      sc.video = meta.id;
      sc.use_video = true;
    } else {
      sc.image = meta.id;
      sc.use_video = false;
    }
    delete state.aiBusy[i];
    saveDraft();
    await loadUploads();
  } catch (e) {
    state.aiBusy[i] = "Ошибка: " + e.message;
    renderScenes();
    throw e;
  }
}
async function waitAiTask(task, i) {
  // Видео делается минутами — спрашиваем сервер, пока не будет готово.
  while (task.status === "running") {
    await new Promise((r) => setTimeout(r, 3000));
    task = await api(`/api/ai/tasks/${task.id}`);
    if (task.stage) {
      state.aiBusy[i] = `Делаю видео (${task.stage})…`;
      renderScenes();
    }
  }
  if (task.status === "error") throw new Error(task.error || "Не удалось сделать видео");
  return task.result;
}
async function aiDrawAll() {
  const btn = $("#aiDrawBtn");
  btn.disabled = true;
  try {
    const bg = state.project.background;
    if ((!bg.ai_scenes.length || scenesStale() || bg.ai_scenes.some((s) => !s.prompt.trim())) && !(await aiMakeScenes())) return;
    const todo = bg.ai_scenes.map((s, i) => (sceneMediaId(s) ? -1 : i)).filter((i) => i >= 0);
    if (!todo.length) {
      toast("Все кадры уже нарисованы. Чтобы заменить кадр — нажмите «Перерисовать» у сцены.");
      return;
    }
    for (const [n, i] of todo.entries()) {
      btn.textContent = `Рисую ${n + 1} из ${todo.length}…`;
      try {
        await aiGenerate(i, "image");
      } catch (e) {
        toast(e.message, "error", 10000);
        return;
      }
    }
    toast("Картинки готовы — можно создавать видео");
  } finally {
    btn.disabled = false;
    btn.textContent = "Нарисовать все";
  }
}
function setupAi() {
  $("#aiStyle").innerHTML = Object.entries(state.status.ai_styles || {})
    .map(([k, label]) => `<option value="${esc(k)}">${esc(label)}</option>`)
    .join("");
  $("#aiScenesBtn").addEventListener("click", () => {
    const scenes = state.project.background.ai_scenes;
    const rewrite = scenes.length > 0 && !scenesStale();
    if (rewrite && !confirm("Придумать описания всех кадров заново? Ваши правки в описаниях заменятся, картинки останутся.")) return;
    aiMakeScenes(rewrite);
  });
  $("#aiDrawBtn").addEventListener("click", aiDrawAll);
  const box = $("#aiScenes");
  box.addEventListener("input", (e) => {
    const ta = e.target.closest("[data-ai-prompt]");
    if (!ta) return;
    const i = Number(ta.closest(".scene").dataset.i);
    state.project.background.ai_scenes[i].prompt = ta.value;
    saveDraft();
  });
  box.addEventListener("click", (e) => {
    const el = e.target.closest("[data-ai-act]");
    if (!el || el.type === "checkbox") return;
    const i = Number(el.closest(".scene").dataset.i);
    if (el.dataset.aiAct === "video" && !confirm("Видео делается платно и дольше картинки (обычно 1–5 минут). Продолжить?")) return;
    aiGenerate(i, el.dataset.aiAct).catch((err) => toast(err.message, "error", 10000));
  });
  box.addEventListener("change", (e) => {
    const el = e.target.closest('[data-ai-act="use-video"]');
    if (!el) return;
    const i = Number(el.closest(".scene").dataset.i);
    state.project.background.ai_scenes[i].use_video = el.checked;
    saveDraft();
    renderScenes();
    renderPreview();
  });
  // Кнопки «Подключить» внутри статуса появляются динамически.
  document.addEventListener("click", (e) => {
    if (e.target.closest("#aiStatus [data-open-settings]")) openSettings();
  });
}

// ------------------------------------------------------------------ музыка

function renderMusicSelect() {
  const sel = $("#musicSelect");
  const tracks = state.uploads.filter((u) => u.kind === "audio");
  sel.innerHTML = `<option value="">Без музыки</option>` + tracks.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("");
  if (state.project.music.file && !tracks.some((t) => t.id === state.project.music.file)) state.project.music.file = null;
  sel.value = state.project.music.file || "";
  updateMusicPlayer();
}
function updateMusicPlayer() {
  const audio = $("#musicAudio");
  const track = state.uploads.find((u) => u.id === state.project.music.file);
  audio.hidden = !track;
  if (track && !audio.src.endsWith(track.url)) audio.src = track.url;
  audio.volume = Math.min(1, state.project.music.volume * 2);
}

// ------------------------------------------------------------------ превью

function renderPreview() {
  if (!state.project || !state.status) return;
  const p = state.project;
  const screen = $("#screen");
  const k = (screen.clientWidth || 280) / 1080;

  // Фон
  const bg = $("#screenBg");
  bg.classList.remove("animated");
  bg.style.backgroundImage = "";
  if (p.background.type === "gradient") {
    const c = p.background.gradient;
    bg.style.backgroundImage = `linear-gradient(180deg, ${c.join(", ")}, ${c[0]})`;
    if (p.background.gradient_speed > 0) {
      bg.classList.add("animated");
      bg.style.animationDuration = `${Math.max(3, 3600 / p.background.gradient_speed / 10)}s`;
    }
  } else if (p.background.type === "media" || p.background.type === "ai") {
    const firstId = p.background.type === "ai" ? sceneMediaId(p.background.ai_scenes[0]) : p.background.media[0];
    const first = state.uploads.find((u) => u.id === firstId);
    bg.style.backgroundImage = first?.thumb_url ? `url("${first.thumb_url}")` : "linear-gradient(160deg,#1d1d28,#0d0d12)";
  } else {
    bg.style.backgroundImage = "linear-gradient(160deg,#243b55,#141e30)";
  }
  $("#screenDim").style.opacity = p.background.dim;

  // Заголовок
  const title = $("#screenTitle");
  const s = p.subtitles;
  const ratio = state.fonts[s.font]?.ratio || 1.22;
  if (p.title.text.trim()) {
    title.hidden = false;
    title.style.top = `${p.title.position}%`;
    title.style.fontFamily = `"${s.font}", sans-serif`;
    title.style.fontSize = `${(p.title.size / ratio) * k}px`;
    title.innerHTML = `<span style="background:${hex(p.title.box_color)};color:${hex(p.title.color)}">${esc(p.title.text)}</span>`;
  } else {
    title.hidden = true;
  }

  // Субтитры
  const cap = $("#screenCaption");
  cap.hidden = !s.enabled;
  if (!s.enabled) return;
  let words = firstChunk(tokenize(p.text || ""), s.words_per_chunk);
  if (!words.length) words = firstChunk("Так будут выглядеть субтитры".split(" "), s.words_per_chunk);
  if (s.uppercase) words = words.map((w) => w.toUpperCase());
  const active = state.previewWord % words.length;
  const o = s.outline * k;
  let shadow = "";
  if (s.style === "outline" && o > 0) {
    const ring = [];
    for (let i = 0; i < 16; i++) {
      const a = (i / 16) * Math.PI * 2;
      ring.push(`${(Math.cos(a) * o).toFixed(2)}px ${(Math.sin(a) * o).toFixed(2)}px 0 ${hex(s.outline_color)}`);
    }
    shadow = ring.join(",");
  }
  if (s.style === "outline" && s.shadow > 0) {
    const d = s.shadow * k;
    shadow += `${shadow ? "," : ""}${d}px ${d}px ${d}px rgba(0,0,0,.55)`;
  }
  cap.style.top = `${s.position}%`;
  cap.style.fontFamily = `"${s.font}", sans-serif`;
  cap.style.fontSize = `${(s.size / ratio) * k}px`;
  cap.style.color = hex(s.color);
  cap.style.textShadow = shadow || "none";
  const inner = words
    .map((w, i) => {
      const on = i === active && s.highlight_mode !== "none";
      const st = on ? `color:${hex(s.highlight)};${s.highlight_mode === "scale" ? "transform:scale(1.12);" : ""}` : "";
      return `<span class="w" style="${st}">${esc(w)}</span>`;
    })
    .join(" ");
  if (s.style === "box") {
    const pad = Math.max(8, s.outline * 2) * k;
    cap.innerHTML = `<span style="background:${rgba(s.box_color, s.box_opacity)};padding:${pad * 0.6}px ${pad}px;line-height:1.6;box-decoration-break:clone;-webkit-box-decoration-break:clone">${inner}</span>`;
  } else {
    cap.innerHTML = inner;
  }
}
function setupPreview() {
  new ResizeObserver(() => renderPreview()).observe($("#screen"));
  setInterval(() => {
    if (!$("#resultVideo").hidden) return;
    state.previewWord++;
    renderPreview();
  }, 650);
  $("#safeZone").addEventListener("change", (e) => { $("#safeOverlay").hidden = !e.target.checked; });
  $("#backToPreview").addEventListener("click", showMockup);
}
function showVideo(url) {
  const v = $("#resultVideo");
  v.src = url;
  v.hidden = false;
  $("#previewSwitch").hidden = false;
  v.play().catch(() => {});
}
function showMockup() {
  const v = $("#resultVideo");
  v.pause();
  v.hidden = true;
  $("#previewSwitch").hidden = true;
}

// ------------------------------------------------------------------ рендер

async function startRender() {
  if (!state.project.text.trim()) {
    toast("Сначала введите текст ролика", "error");
    $("#text").focus();
    return;
  }
  const btn = $("#renderBtn");
  btn.disabled = true;
  $("#resultBox").hidden = true;
  const box = $("#jobBox");
  box.hidden = false;
  box.classList.remove("error");
  $("#jobError").hidden = true;
  $("#jobLog").textContent = "";
  $("#jobLog").hidden = true;
  $("#cancelBtn").hidden = false;
  setJobProgress("Отправка…", 0);
  try {
    const job = await api("/api/render", { method: "POST", body: state.project });
    state.jobId = job.id;
    pollJob();
  } catch (e) {
    btn.disabled = false;
    setJobProgress("Ошибка", 0);
    showJobError(e.message);
  }
}
function showJobError(message, stage) {
  const box = $("#jobBox");
  box.classList.add("error");
  $("#cancelBtn").hidden = true;
  const where = stage && stage !== "Ошибка" ? ` на этапе «${esc(stage)}»` : "";
  let action = "";
  if (/бесплатный ключ|ключ API|ключ Pexels|Настройках|токен/i.test(message)) action = `<button class="btn secondary" data-fix="settings">Открыть настройки</button>`;
  else if (/фон/i.test(message)) action = `<button class="btn secondary" data-fix="bg">Перейти на вкладку «Фон»</button>`;
  else if (/музык/i.test(message)) action = `<button class="btn secondary" data-fix="music">Перейти на вкладку «Музыка»</button>`;
  const el = $("#jobError");
  el.innerHTML = `<b>Видео не создано${where}.</b>\n${esc(message)}${action ? "\n" + action : ""}`;
  el.hidden = false;
  toast(message, "error", 9000);
}
function setJobProgress(stage, percent) {
  $("#jobStage").textContent = stage;
  $("#jobPercent").textContent = `${Math.round(percent)}%`;
  $("#jobBar").style.width = `${percent}%`;
}
async function pollJob() {
  clearTimeout(state.pollTimer);
  if (!state.jobId) return;
  let job;
  try {
    job = await api(`/api/jobs/${state.jobId}`);
  } catch (e) {
    state.pollTimer = setTimeout(pollJob, 2000);
    return;
  }
  setJobProgress(job.stage, job.progress);
  $("#jobLog").textContent = (job.log || []).join("\n");
  if (job.status === "queued" || job.status === "running") {
    state.pollTimer = setTimeout(pollJob, 700);
    return;
  }
  $("#renderBtn").disabled = false;
  $("#cancelBtn").hidden = true;
  state.jobId = null;
  if (job.status === "done") {
    const r = job.result;
    $("#downloadBtn").href = r.download_url;
    $("#srtBtn").href = r.srt_url || "#";
    $("#srtBtn").hidden = !r.srt_url;
    $("#resultBox").hidden = false;
    showVideo(r.url);
    toast(`Видео готово: ${fmtDuration(r.duration)}, собрано за ${r.render_seconds} с`);
    loadVideos();
    if (r.ai_scenes) {
      state.project.background.ai_scenes = r.ai_scenes;
      saveDraft();
      loadUploads().then(renderScenes);
    }
  } else if (job.status === "error") {
    showJobError(job.error || "Неизвестная ошибка — откройте «Подробности»", job.failed_stage);
  } else {
    toast("Рендер отменён");
  }
}
function setupRender() {
  $("#renderBtn").addEventListener("click", startRender);
  $("#cancelBtn").addEventListener("click", async () => {
    if (state.jobId) await api(`/api/jobs/${state.jobId}/cancel`, { method: "POST" }).catch(() => {});
  });
  $("#toggleLog").addEventListener("click", () => { $("#jobLog").hidden = !$("#jobLog").hidden; });
  $("#jobError").addEventListener("click", (e) => {
    const fix = e.target.closest("[data-fix]")?.dataset.fix;
    if (fix === "settings") openSettings();
    else if (fix) {
      openTab(fix);
      $(".tabs").scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && !$("#renderBtn").disabled) startRender();
  });
}

// ------------------------------------------------------------------ галерея

async function loadVideos() {
  try { state.videos = await api("/api/videos"); } catch (e) { return; }
  $("#galleryCount").textContent = state.videos.length ? `${state.videos.length} ${plural(state.videos.length, "ролик", "ролика", "роликов")}` : "";
  $("#gallery").innerHTML = state.videos
    .map((v) => {
      const date = new Date(v.created * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
      const thumb = v.thumb_url ? `background-image:url('${esc(v.thumb_url)}')` : "";
      return `<div class="video-card" data-id="${esc(v.id)}">
        <div class="thumb" data-act="play" style="${thumb}"><span class="dur">${fmtDuration(v.duration)}</span></div>
        <div class="meta"><div class="t">${esc(v.title || v.id)}</div><div class="d">${date}</div></div>
        <div class="actions">
          <a href="${esc(v.download_url)}" title="Скачать">⬇</a>
          <button data-act="load" title="Загрузить текст и настройки">↺</button>
          <button data-act="delete" title="Удалить">🗑</button>
        </div></div>`;
    })
    .join("");
}
function setupGallery() {
  $("#gallery").addEventListener("click", async (e) => {
    const card = e.target.closest(".video-card");
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (!card || !act) return;
    const v = state.videos.find((x) => x.id === card.dataset.id);
    if (!v) return;
    if (act === "play") {
      showVideo(v.url);
      $("#downloadBtn").href = v.download_url;
      $("#srtBtn").href = v.srt_url || "#";
      $("#srtBtn").hidden = !v.srt_url;
      $("#resultBox").hidden = false;
      $(".preview-card").scrollIntoView({ behavior: "smooth", block: "start" });
    } else if (act === "load") {
      try {
        const full = await api(`/api/videos/${v.id}`);
        state.project = mergeDefaults(state.defaults, full.project);
        syncForm();
        saveDraft();
        toast("Текст и настройки загружены");
        window.scrollTo({ top: 0, behavior: "smooth" });
      } catch (err) { toast(err.message, "error"); }
    } else if (act === "delete") {
      if (!confirm("Удалить это видео?")) return;
      await api(`/api/videos/${v.id}`, { method: "DELETE" }).catch((err) => toast(err.message, "error"));
      loadVideos();
    }
  });
}

// ------------------------------------------------------------------ настройки

async function openSettings() {
  const dlg = $("#settingsDialog");
  const form = $("#settingsForm");
  try {
    const s = await api("/api/settings");
    form.proxy.value = s.proxy || "";
    form.pexels_api_key.value = "";
    $("#pexelsHint").textContent = s.pexels_api_key_set ? `(сохранён ${s.pexels_api_key_hint})` : "(не указан)";
    form.ai_provider.value = s.ai_provider || "";
    form.cf_account_id.value = s.cf_account_id || "";
    form.cf_api_token.value = "";
    form.cf_image_model.value = s.cf_image_model || "flux";
    form.pollinations_key.value = "";
    form.pollinations_image_model.value = s.pollinations_image_model || "";
    form.pollinations_video_model.value = s.pollinations_video_model || "";
    $("#cfTokenHint").textContent = s.cf_api_token_set ? `(сохранён ${s.cf_api_token_hint})` : "(не указан)";
    $("#polKeyHint").textContent = s.pollinations_key_set ? `(сохранён ${s.pollinations_key_hint})` : "(не указан)";
    showAiSettings();
  } catch (e) { toast(e.message, "error"); }
  if (!dlg.open) dlg.showModal();
}
function showAiSettings() {
  const provider = $("#settingsForm").ai_provider.value;
  $$(".ai-settings").forEach((el) => { el.hidden = el.dataset.ai !== provider; });
}
async function saveSettings(extra = {}) {
  const form = $("#settingsForm");
  const body = {
    proxy: form.proxy.value.trim(),
    ai_provider: form.ai_provider.value,
    cf_account_id: form.cf_account_id.value.trim(),
    cf_image_model: form.cf_image_model.value,
    pollinations_image_model: form.pollinations_image_model.value.trim(),
    pollinations_video_model: form.pollinations_video_model.value.trim(),
    ...extra,
  };
  for (const secret of ["pexels_api_key", "cf_api_token", "pollinations_key"]) {
    if (form[secret].value.trim()) body[secret] = form[secret].value.trim();
  }
  try {
    const s = await api("/api/settings", { method: "POST", body });
    state.status.settings = s;
    showBgSection();
    renderScenes();
    $("#settingsDialog").close();
    toast("Настройки сохранены");
  } catch (e) { toast(e.message, "error"); }
}
function setupSettings() {
  $("#settingsBtn").addEventListener("click", openSettings);
  $$("[data-open-settings]").forEach((b) => b.addEventListener("click", openSettings));
  $("#saveSettings").addEventListener("click", () => saveSettings());
  $("#clearPexels").addEventListener("click", () => saveSettings({ pexels_api_key: "" }));
  $("#settingsForm").ai_provider.addEventListener("change", showAiSettings);
}

// ------------------------------------------------------------------ телефон

async function openPhone() {
  $("#phoneDialog").showModal();
  $("#phoneBody").innerHTML = `
    <h3 class="sub-head">Через интернет — с мобильной сети или любого Wi-Fi</h3>
    <div id="tunnelBox"><p class="hint">Загрузка…</p></div>
    <h3 class="sub-head">Через домашний Wi-Fi</h3>
    <div id="lanBox"><p class="hint">Загрузка…</p></div>`;
  renderLan();
  refreshTunnel();
}
async function renderLan() {
  const box = $("#lanBox");
  let data;
  try { data = await api("/api/phone"); } catch (e) { box.textContent = e.message; return; }
  if (!data.enabled) {
    box.innerHTML = `<p class="hint">Если телефон в той же сети Wi-Fi, можно обойтись без интернета: закройте окно студии и запустите <b>start-phone.bat</b> (на Mac/Linux: <code>./start.sh --phone</code>).</p>`;
    return;
  }
  if (!data.urls.length) {
    box.innerHTML = `<p class="hint">Не удалось найти адрес компьютера в сети — проверьте подключение к Wi-Fi.</p>`;
    return;
  }
  box.innerHTML = `<div class="phone-qr">${data.qr ? `<div class="qr">${data.qr}</div>` : ""}
      <div><div class="hint">Код доступа</div><div class="code">${esc(data.key)}</div></div></div>
    ${data.urls.map((u) => `<div class="phone-url">${esc(u)}</div>`).join("")}
    <p class="hint">Телефон должен быть в той же сети Wi-Fi. Не открывается? Разрешите доступ в брандмауэре Windows для «Частных сетей» и сделайте сеть Wi-Fi «Частной».</p>`;
}
async function refreshTunnel() {
  clearTimeout(state.tunnelTimer);
  const box = $("#tunnelBox");
  if (!box || !$("#phoneDialog").open) return;
  let t;
  try { t = await api("/api/tunnel"); } catch (e) { box.textContent = e.message; return; }
  if (t.state === "off") {
    box.innerHTML = `<p class="hint">Студия получит временную защищённую ссылку через бесплатный сервис Cloudflare — её можно открыть с телефона где угодно. Компьютер со студией при этом должен быть включён. В первый раз скачается программа cloudflared (~40 МБ).</p>
      <button class="btn primary" data-tunnel="start">Включить доступ из интернета</button>`;
  } else if (t.state === "downloading") {
    box.innerHTML = `<p>Скачиваю cloudflared… ${t.progress}%</p><div class="progress"><div style="width:${t.progress}%"></div></div>
      <button class="link-btn danger" data-tunnel="stop">Отменить</button>`;
  } else if (t.state === "starting" || (t.state === "running" && !t.url)) {
    const how = t.attempt > 1 ? ` Первый способ не сработал — пробую другой (${t.attempt} из ${t.attempts}: ${esc(t.method)}).` : "";
    box.innerHTML = `<p>Подключаюсь к Cloudflare… обычно это 5–15 секунд.${how}</p>
      <button class="link-btn danger" data-tunnel="stop">Отменить</button>`;
  } else if (t.state === "running") {
    box.innerHTML = `<div class="phone-qr">${t.qr ? `<div class="qr">${t.qr}</div>` : ""}
        <div><div class="hint">Код доступа</div><div class="code">${esc(t.key || "")}</div></div></div>
      <div class="phone-url">${esc(t.phone_url || t.url)}</div>
      <ol class="phone-steps">
        <li>Наведите камеру телефона на QR-код и откройте ссылку — подойдёт любой интернет, хоть мобильный.</li>
        <li>Если страница не открылась сразу — подождите 10–20 секунд и обновите её.</li>
        <li>Ссылка действует, пока студия запущена и доступ включён. После перезапуска будет новая ссылка — отсканируйте QR заново.</li>
      </ol>
      <button class="btn secondary" data-tunnel="stop">Выключить доступ из интернета</button>`;
  } else {
    box.innerHTML = `<p class="job-error">${esc(t.error || "Не удалось включить доступ")}</p>
      <p class="hint">Если мобильный интернет или провайдер блокирует Cloudflare, попробуйте позже, через VPN или с помощью Tailscale (см. README).</p>
      <button class="btn primary" data-tunnel="start">Попробовать снова</button>`;
  }
  if (["downloading", "starting"].includes(t.state) || (t.state === "running" && !t.url)) {
    state.tunnelTimer = setTimeout(refreshTunnel, 1000);
  }
}
async function setupPhone() {
  $("#phoneBtn").addEventListener("click", openPhone);
  $("#phoneBody").addEventListener("click", async (e) => {
    const act = e.target.closest("[data-tunnel]")?.dataset.tunnel;
    if (!act) return;
    e.target.disabled = true;
    try { await api(`/api/tunnel/${act}`, { method: "POST" }); } catch (err) { toast(err.message, "error"); }
    refreshTunnel();
  });
  $("#phoneClose").addEventListener("click", () => $("#phoneDialog").close());
  try {
    const data = await api("/api/phone");
    if (data.remote) $("#phoneBtn").hidden = true; // уже открыто с телефона
    else if (location.hash === "#phone") openPhone();
  } catch (_) { /* не критично */ }
}

// ------------------------------------------------------------------ запуск

function openTab(name) {
  $$(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === name));
  $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.dataset.panel === name));
}

function showFfmpegStatus() {
  const f = state.status.ffmpeg;
  const pill = $("#ffmpegStatus");
  if (!f.found) {
    pill.textContent = "ffmpeg не найден";
    pill.className = "pill bad";
    toast("ffmpeg не найден. Установите его (см. README) и перезапустите студию.", "error", 15000);
  } else if (!f.ok) {
    const missing = Object.entries(f.filters).filter(([, ok]) => !ok).map(([n]) => n);
    if (!f.libx264) missing.push("libx264");
    pill.textContent = "ffmpeg неполный";
    pill.className = "pill bad";
    toast(`В вашей сборке ffmpeg нет: ${missing.join(", ")}. Установите полную версию (см. README).`, "error", 15000);
  } else {
    pill.textContent = `ffmpeg ${String(f.version).split("-")[0]} ✓`;
    pill.className = "pill ok";
  }
}

async function init() {
  try {
    state.status = await api("/api/status");
  } catch (e) {
    toast("Сервер студии не отвечает: " + e.message, "error", 20000);
    return;
  }
  state.defaults = state.status.defaults;
  state.project = mergeDefaults(state.defaults, loadDraft());
  showFfmpegStatus();
  setupFonts();
  setupPresets();
  setupBackground();
  setupMedia();
  setupAi();
  setupPreview();
  setupRender();
  setupGallery();
  setupSettings();
  setupPhone();
  bindFields();

  $$(".tab").forEach((t) => t.addEventListener("click", () => openTab(t.dataset.tab)));
  $("#voiceLang").addEventListener("change", () => { fillVoiceSelect(false); updateVoiceWarning(); saveDraft(); });
  $("#previewVoiceBtn").addEventListener("click", previewVoice);
  $("#sampleBtn").addEventListener("click", () => {
    if (state.project.text.trim() && !confirm("Заменить текущий текст примером?")) return;
    state.project.text = SAMPLE_TEXT;
    if (!state.project.title.text) state.project.title.text = "Почему осьминоги такие умные?";
    syncForm();
    saveDraft();
  });
  $("#uploadMusicBtn").addEventListener("click", () => $("#musicInput").click());
  $("#musicInput").addEventListener("change", (e) => { handleFiles([...e.target.files]); e.target.value = ""; });
  $("#resetBtn").addEventListener("click", () => {
    if (!confirm("Сбросить все настройки? Текст останется.")) return;
    const { text, title } = state.project;
    state.project = clone(state.defaults);
    state.project.text = text;
    state.project.title.text = title.text;
    syncForm();
    saveDraft();
  });

  syncForm();
  await Promise.all([loadVoices(), loadUploads(), loadVideos()]);
}

init();
