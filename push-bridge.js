/* push-bridge.js — мост между «Ежедневником» и браузером (звук + уведомления + push-сервер).
 *
 * Подключается из index.html (и из push-setup.html). Никак не влияет на работу
 * приложения: при недоступности Service Worker / Notification API тихо завершается.
 *
 * Что делает:
 *  1. Регистрирует ./sw.js со скоупом ./push/ (Web Push).
 *  2. Слушает BroadcastChannel 'ezh-notify': Python-воркер (Pyodide) публикует
 *     напоминание {title, body, tag, task_id, url} — мост играет короткий
 *     сигнал (Web Audio) и показывает системное уведомление. Работает, пока
 *     PWA открыта (пусть и свёрнута).
 *  3. PUSH-СЕРВЕР (напоминания при ЗАКРЫТОМ приложении):
 *     - подписка pushManager с VAPID-ключом → POST {server}/subscribe;
 *     - загрузка расписания напоминаний из локального снапшота
 *       db_snapshot_<uid> → POST {server}/schedule (сервер шлёт push в срок);
 *     - при срабатывании напоминания ВНУТРИ приложения сообщаем серверу
 *       POST {server}/suppress, чтобы не приходил дубль push.
 *  4. Баннер «Включить напоминания» — включение в один тап (iOS требует
 *     запрос разрешения из жеста пользователя).
 *  5. НАДЁЖНОСТЬ (v2.2): подписка на сервер повторяется автоматически, пока
 *     сервер её не подтвердит. Отправка идёт в ТРИ ступени: обычный JSON →
 *     «простой» запрос text/plain (БЕЗ CORS-прелифта — уходит на спящий
 *     сервер немедленно) → navigator.sendBeacon (уходит даже при закрытии
 *     страницы). Сервер FastAPI парсит JSON-тело независимо от Content-Type.
 *     Все события пишутся в диагностический журнал localStorage
 *     ['ezh_push_diag'] — виден на push-setup.html.
 *  6. Экспортирует window.fletPush для push-setup.html:
 *     fletPush.subscribe() / unsubscribe() / status() / getUid() / config /
 *     version / diagLog().
 *
 * ФИКС ЗВУКА (iOS): AudioContext создаётся заранее и разблокируется на
 * первом касании экрана.
 *
 * Все пути ОТНОСИТЕЛЬНЫЕ — работает из корня домена и из подпапки
 * (GitHub Pages: /ezhednevnik-offline/).
 */
(async function () {
  'use strict';

  if (typeof window === 'undefined') return;

  /* ================== НАСТРОЙКИ PUSH-СЕРВЕРА ==================
   * 1. PUSH_SERVER — адрес сервиса на Render, например
   *    https://ezhednevnik-push.onrender.com  (без / на конце).
   * 2. VAPID_PUBLIC_KEY — ПУБЛИЧНЫЙ VAPID-ключ (tools/generate_vapid_keys.py).
   *    Он же задан на сервере (из того же приватного ключа).
   * ============================================================ */
  const PUSH_SERVER = 'https://ezhednevnik-push.onrender.com';
  const VAPID_PUBLIC_KEY = 'BH5n1Po_d5bwGcex0i4qohzubGwT22LuPI9Km9u8PiSFXOZaBHCYia_NYKglKXJXtBA2E0Eun0sO46kRLjsCJZw';

  const NOTIFY_CHANNEL = 'ezh-notify';
  const APP_NAME = 'Ежедневник';
  const SCHEDULE_UPLOAD_INTERVAL_MS = 5 * 60 * 1000; // 5 минут, пока PWA открыта
  const SNAPSHOT_PREFIX = 'db_snapshot_';
  const DISMISS_KEY = 'ezh_push_banner_dismissed';

  const serverConfigured = /^https?:\/\//.test(PUSH_SERVER) && PUSH_SERVER.indexOf('ВАШ') === -1;

  /* ============ ВЕРСИЯ И ДИАГНОСТИЧЕСКИЙ ЖУРНАЛ (v2.2) ============
   * Журнал — в localStorage['ezh_push_diag'], последние 40 событий.
   * push-setup.html показывает его кнопкой «Диагностика» — по нему видно,
   * что именно происходило на устройстве (регистрация SW, ответы сервера). */
  const BRIDGE_VERSION = 'v2.2';
  const DIAG_KEY = 'ezh_push_diag';

  const isStandalone = () => {
    try {
      return (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) ||
        window.navigator.standalone === true;
    } catch (e) { return false; }
  };

  const diag = (m) => {
    try {
      const arr = JSON.parse(localStorage.getItem(DIAG_KEY) || '[]');
      arr.push({ t: new Date().toISOString().slice(11, 23), m: String(m).slice(0, 220) });
      while (arr.length > 40) arr.shift();
      localStorage.setItem(DIAG_KEY, JSON.stringify(arr));
    } catch (e) { /* приватный режим */ }
  };

  diag('мост ' + BRIDGE_VERSION + ' старт; standalone=' + isStandalone());

  const urlBase64ToUint8Array = (base64String) => {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(base64);
    const output = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i += 1) output[i] = raw.charCodeAt(i);
    return output;
  };

  const getUidFromUrl = () => {
    try {
      return new URLSearchParams(window.location.search).get('uid') || '';
    } catch (e) {
      return '';
    }
  };

  /* uid берём из ключа снапшота localStorage (db_snapshot_<uid>) —
   * он появляется после входа в аккаунт и офлайн-сохранения данных. */
  const getUid = () => {
    const fromUrl = getUidFromUrl();
    if (fromUrl) return fromUrl;
    try {
      for (let i = 0; i < localStorage.length; i += 1) {
        const k = localStorage.key(i);
        if (k && k.indexOf(SNAPSHOT_PREFIX) === 0) {
          const uid = k.slice(SNAPSHOT_PREFIX.length);
          if (uid && uid !== 'anon') return uid;
        }
      }
    } catch (e) { /* приватный режим и т.п. */ }
    return '';
  };

  const tzName = () => {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ''; }
    catch (e) { return ''; }
  };

  /* ====== Отправка на сервер в ТРИ ступени (v2.2) ======
   * Ступень 1 — обычный JSON-запрос. Браузер перед ним делает CORS-прелифт
   * (OPTIONS); на спящем Render прелифт висит 20–60 с холодного старта и
   * может отвалиться — тогда POST вообще не уйдёт.
   * Ступень 2 — «простой» запрос: Content-Type: text/plain. Прелифт НЕ
   * требуется, POST уходит немедленно. FastAPI читает тело через req.json()
   * и парсит JSON независимо от Content-Type (проверено на живом сервере).
   * Ступень 3 — navigator.sendBeacon с Blob text/plain: отправка уходит даже
   * при скрытии/закрытии страницы, тоже без прелифта.
   * opts.quiet — не писать неудачи в диагностический журнал (для второстепенных
   * вызовов вроде /suppress, чтобы не засорять журнал). */
  const sendToServer = async (url, payload, opts) => {
    const quiet = !!(opts && opts.quiet);
    const body = JSON.stringify(payload);
    let lastErr = null;

    // Ступень 1: обычный JSON-запрос (стандартный путь)
    try {
      const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
      });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      return await resp.json().catch(() => ({}));
    } catch (e) {
      lastErr = e;
      if (!quiet) diag('POST json → ' + (e && e.message ? e.message : e));
    }

    // Ступень 2: простой запрос без прелифта
    try {
      const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'text/plain;charset=UTF-8' },
        body,
      });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      diag('POST plain → ок');
      return await resp.json().catch(() => ({}));
    } catch (e) {
      lastErr = e;
      if (!quiet) diag('POST plain → ' + (e && e.message ? e.message : e));
    }

    // Ступень 3: sendBeacon (fire-and-forget)
    try {
      if (typeof navigator !== 'undefined' && navigator.sendBeacon) {
        const blob = new Blob([body], { type: 'text/plain;charset=UTF-8' });
        if (navigator.sendBeacon(url, blob)) {
          diag('POST beacon → принят браузером');
          return { ok: true, beacon: true };
        }
      }
    } catch (e) {
      lastErr = e;
      if (!quiet) diag('POST beacon → ' + (e && e.message ? e.message : e));
    }
    throw (lastErr || new Error('Сервер недоступен'));
  };

  /* ====== Надёжная синхронизация подписки с сервером (v2) ======
   * Локальная подписка создаётся мгновенно, но POST /subscribe уходит на
   * сервер, который может СПАТЬ (бесплатный Render: холодный старт 20–60 с,
   * сон после ~15 мин простоя). Одиночная попытка могла теряться навсегда.
   * Теперь: повторяем /subscribe с уже существующей локальной подпиской
   * (сервер сохраняет идемпотентно, по хэшу endpoint) с ретраями, пока
   * сервер не подтвердит. */
  let serverSynced = false;
  let syncTimer = null;

  const syncSubscription = async (retriesLeft) => {
    if (!swReg || !serverConfigured) return serverSynced;
    try {
      const sub = await swReg.pushManager.getSubscription();
      const uid = getUid();
      if (!sub || !uid) return serverSynced;
      await sendToServer(PUSH_SERVER + '/subscribe', {
        uid: uid,
        timezone: tzName(),
        subscription: sub.toJSON(),
      });
      serverSynced = true;
      if (syncTimer) { clearTimeout(syncTimer); syncTimer = null; }
      diag('подписка подтверждена сервером (uid=' + String(uid).slice(0, 8) + '…)');
      console.info('[push-bridge] подписка подтверждена сервером');
    } catch (e) {
      console.warn('[push-bridge] /subscribe не дошёл:', e && e.message);
      if (typeof retriesLeft === 'number' && retriesLeft > 0) {
        if (syncTimer) clearTimeout(syncTimer);
        const delay = retriesLeft > 8 ? 15000 : 60000; // сначала чаще, потом раз в минуту
        diag('/subscribe не дошёл (' + (e && e.message ? e.message : e) + '), ретрай через ' + delay / 1000 + ' с');
        syncTimer = setTimeout(() => { syncSubscription(retriesLeft - 1).catch(() => {}); }, delay);
      }
    }
    return serverSynced;
  };

  const registerSW = async () => {
    if (!('serviceWorker' in navigator)) { diag('SW: navigator.serviceWorker недоступен'); return null; }
    try {
      // Скоуп ./push/ — чтобы НЕ замещать родной SW Flet (flutter_service_worker.js)
      // на корне страницы. Push-события приходят в SW без контроля страницы.
      const reg = await navigator.serviceWorker.register('./sw.js', { scope: './push/' });
      diag('SW: зарегистрирован, scope=' + (reg && reg.scope ? reg.scope : '?'));
      return reg;
    } catch (e) {
      diag('SW: ОШИБКА регистрации: ' + (e && e.message ? e.message : e));
      console.warn('[push-bridge] SW register failed:', e);
      return null;
    }
  };

  const swReg = await registerSW();

  /* ====== Починка кнопки «Уведомления» в настройках приложения (v2.1) ======
   * Настройки открывают '/push-setup.html' (адрес ОТ КОРНЯ сайта) через
   * launch_url(..., BLANK) → window.open. Из-за подпапки GitHub Pages это
   * ведёт на https://ovp8.github.io/push-setup.html (404), а в установленном
   * PWA новая вкладка = Safari, где Web Push не работает. Перехватываем
   * window.open: адрес строим от текущей страницы; внутри установленного
   * PWA открываем страницу В ТОМ ЖЕ окне (только там есть Web Push). */
  try {
    const origWindowOpen = window.open ? window.open.bind(window) : null;
    window.open = function (url, target, features) {
      try {
        if (typeof url === 'string' && url.indexOf('/push-setup.html') === 0) {
          // '/push-setup.html' — абсолютный путь: с любым base он даст корень
          // сайта (404 на GitHub Pages). Превращаем в ОТНОСИТЕЛЬНЫЙ и строим
          // полный адрес от текущей страницы (с учётом подпапки Pages).
          const rel = url.replace(/^\/+/, '');
          const abs = new URL(rel, window.location.href).href;
          if (isStandalone()) {
            window.location.href = abs;
            return null;
          }
          if (origWindowOpen) return origWindowOpen(abs, '_blank', features);
          window.location.href = abs;
          return null;
        }
      } catch (e) { /* не наш случай — идём дальше */ }
      if (origWindowOpen) return origWindowOpen(url, target, features);
      return null;
    };
  } catch (e) { console.warn('[push-bridge] window.open hook failed:', e); }

  /* ================= Звук (Web Audio) =================
   * iOS разблокирует AudioContext только в обработчике жеста пользователя.
   * Держим ОДИН живой контекст и разблокируем его на первом касании —
   * тогда к моменту напоминания (таймер вне жеста) звук уже разрешён. */

  let audioCtx = null;
  const ensureAudio = () => {
    try {
      if (!audioCtx) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return null;
        audioCtx = new AC();
      }
      if (audioCtx.state === 'suspended') {
        audioCtx.resume().catch(() => {});
      }
      return audioCtx;
    } catch (e) {
      return null;
    }
  };
  ['pointerdown', 'touchstart', 'keydown'].forEach((ev) =>
    window.addEventListener(ev, ensureAudio, { passive: true }));

  // Двойной «динь» (880 Гц + 1318.5 Гц) — как в будильнике
  const beepTwice = () => {
    const ctx = ensureAudio();
    if (!ctx) return;
    const run = () => {
      try {
        const t0 = ctx.currentTime + 0.03;
        [[880, 0], [1318.5, 0.22]].forEach(([freq, dt]) => {
          const osc = ctx.createOscillator();
          const gain = ctx.createGain();
          osc.type = 'sine';
          osc.frequency.value = freq;
          gain.gain.setValueAtTime(0.0001, t0 + dt);
          gain.gain.exponentialRampToValueAtTime(0.28, t0 + dt + 0.02);
          gain.gain.exponentialRampToValueAtTime(0.0001, t0 + dt + 0.34);
          osc.connect(gain);
          gain.connect(ctx.destination);
          osc.start(t0 + dt);
          osc.stop(t0 + dt + 0.36);
        });
      } catch (e) { /* звук не критичен */ }
    };
    if (ctx.state === 'running') run();
    else ctx.resume().then(run).catch(() => {});
  };

  /* ================= Системное уведомление ================= */

  const showSystemNotification = async (title, body, extra) => {
    extra = extra || {};
    try {
      if (!('Notification' in window)) return;
      let permission = Notification.permission;
      if (permission === 'default') {
        try { permission = await Notification.requestPermission(); } catch (e) {}
      }
      if (permission !== 'granted') return;
      const options = {
        body: body || '',
        icon: './icons/icon-192.png',
        badge: './icons/icon-192.png',
        tag: extra.tag || ('reminder-' + Date.now()),
        renotify: true,
        requireInteraction: false,
        silent: false,
        data: { url: extra.url || './', task_id: extra.task_id || 0 },
      };
      let reg = swReg;
      if (!reg && navigator.serviceWorker) {
        reg = await navigator.serviceWorker.getRegistration('./push/');
      }
      if (reg && reg.showNotification) {
        await reg.showNotification(title || APP_NAME, options);
      } else if (typeof Notification !== 'undefined') {
        try { new Notification(title || APP_NAME, options); } catch (e) { /* ignore */ }
      }
    } catch (e) { /* ignore */ }
  };

  /* ============ PUSH-СЕРВЕР: расписание и подписка ============ */

  let lastScheduleBody = '';
  let scheduleUploading = false;

  const collectSchedule = () => {
    const uid = getUid();
    if (!uid) return null;
    let snap;
    try {
      const raw = localStorage.getItem(SNAPSHOT_PREFIX + uid);
      if (!raw) return null;
      snap = JSON.parse(raw);
    } catch (e) {
      return null;
    }
    const tables = (snap && snap.tables) || {};
    const nowMs = Date.now();
    const HORIZON = 8 * 24 * 3600 * 1000; // 8 дней вперёд
    const reminders = [];
    (Array.isArray(tables.reminders) ? tables.reminders : []).forEach((r) => {
      if (!r || r.fired) return;
      const t = Date.parse(r.fire_at || '');
      if (!isFinite(t)) return;
      if (t < nowMs - 3600 * 1000 || t > nowMs + HORIZON) return;
      reminders.push({
        task_id: r.task_id | 0,
        task_text: String(r.task_text || ''),
        fire_at: r.fire_at,
        minutes_before: r.minutes_before | 0,
        kind: r.kind || 'before',
        repeat_interval_min: r.repeat_interval_min | 0,
        repeat_max: r.repeat_max | 0,
        repeat_count: r.repeat_count | 0,
      });
    });
    const tasks = {};
    (Array.isArray(tables.tasks) ? tables.tasks : []).forEach((t) => {
      if (!t || t.is_deleted) return;
      tasks[t.id] = [String(t.task || ''), String(t.datetime || ''), t.status | 0];
    });
    return { uid, timezone: tzName(), reminders, tasks };
  };

  /* Загружает расписание на сервер. Возвращает true при успехе. */
  const uploadSchedule = async (force) => {
    if (!serverConfigured || scheduleUploading || !navigator.onLine) return false;
    const payload = collectSchedule();
    if (!payload) return false;
    const body = JSON.stringify(payload);
    if (!force && body === lastScheduleBody) return true; // без изменений — не спамим
    scheduleUploading = true;
    try {
      await sendToServer(PUSH_SERVER + '/schedule', payload);
      lastScheduleBody = body;
      return true;
    } finally {
      scheduleUploading = false;
    }
  };

  /* Сообщает серверу «это напоминание уже показано внутри приложения» —
   * чтобы push не пришёл дублем, пока PWA открыта. */
  const suppressOnServer = async (data) => {
    if (!serverConfigured) return;
    const uid = getUid();
    if (!uid) return;
    try {
      await sendToServer(PUSH_SERVER + '/suppress', {
        uid: uid,
        task_id: data.task_id || 0,
      }, { quiet: true });
    } catch (e) { /* не критично */ }
  };

  /* ================= Баннер «Включить напоминания» ================= */

  let bannerEl = null;

  const setBannerText = (text) => {
    if (bannerEl) {
      const t = bannerEl.querySelector('.ezh-text');
      if (t) t.textContent = text;
    }
  };

  const hideBanner = () => {
    if (bannerEl) {
      bannerEl.remove();
      bannerEl = null;
    }
  };

  const showBanner = (text) => {
    if (bannerEl || !('Notification' in window)) return;
    const el = document.createElement('div');
    el.id = 'ezh-push-banner';
    el.style.cssText = [
      'position:fixed',
      'top:calc(env(safe-area-inset-top, 0px) + 8px)',
      'left:12px', 'right:12px',
      'z-index:2147483000',
      'display:flex', 'align-items:center', 'gap:10px',
      'background:#171A21', 'color:#E8EAED',
      'border:1px solid #2A3040', 'border-radius:14px',
      'padding:12px 14px',
      'font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,sans-serif',
      'font-size:15px', 'font-weight:600',
      'box-shadow:0 8px 30px rgba(0,0,0,.45)',
      'cursor:pointer',
      '-webkit-tap-highlight-color:transparent',
    ].join(';');

    const bell = document.createElement('span');
    bell.textContent = '🔔';
    bell.style.cssText = 'font-size:18px;flex:none';

    const txt = document.createElement('span');
    txt.className = 'ezh-text';
    txt.textContent = text || 'Включить напоминания';
    txt.style.cssText = 'flex:1;line-height:1.3';

    const close = document.createElement('span');
    close.textContent = '✕';
    close.style.cssText = 'flex:none;color:#9AA0A6;font-size:14px;padding:4px 2px';

    el.appendChild(bell);
    el.appendChild(txt);
    el.appendChild(close);

    const enableFlow = async () => {
      try {
        setBannerText('Запрашиваем разрешение…');
        await window.fletPush.subscribe();
        setBannerText('Уведомления включены ✓');
        try { localStorage.removeItem(DISMISS_KEY); } catch (e) {}
        setTimeout(hideBanner, 1600);
      } catch (e) {
        setBannerText('Не получилось: ' + (e && e.message ? e.message : 'ошибка'));
      }
    };

    el.addEventListener('click', (ev) => {
      if (ev.target === close) {
        hideBanner();
        try { localStorage.setItem(DISMISS_KEY, '1'); } catch (e) {}
        return;
      }
      enableFlow();
    });

    document.body.appendChild(el);
    bannerEl = el;
  };

  /* ================= Мост из Python-воркера ================= */

  try {
    if ('BroadcastChannel' in window) {
      const notifyChannel = new BroadcastChannel(NOTIFY_CHANNEL);
      notifyChannel.onmessage = (event) => {
        let data = {};
        try {
          data = (typeof event.data === 'string')
            ? JSON.parse(event.data) : (event.data || {});
        } catch (e) {}
        beepTwice();
        showSystemNotification(data.title || APP_NAME, data.body || '', data);
        // напоминание показано в приложении — серверу push на эту же минуту не нужен
        suppressOnServer(data);
        // расписание изменилось (напоминание сработало, могли создаться повторы)
        setTimeout(() => { uploadSchedule(true).catch(() => {}); }, 3500);
      };
    }
  } catch (e) { /* BroadcastChannel недоступен — не критично */ }

  /* ================= Web Push API ================= */

  window.fletPush = {
    supported: !!(swReg && 'PushManager' in window && 'Notification' in window),

    config: {
      server: PUSH_SERVER,
      vapidPublicKey: VAPID_PUBLIC_KEY,
      serverConfigured: serverConfigured,
    },

    getUid: getUid,

    version: BRIDGE_VERSION,

    diagLog() {
      try { return JSON.parse(localStorage.getItem(DIAG_KEY) || '[]'); }
      catch (e) { return []; }
    },

    async status() {
      if (!('Notification' in window)) {
        return { supported: false, permission: 'unsupported', subscribed: false };
      }
      let subscribed = false;
      try {
        if (swReg) {
          const sub = await swReg.pushManager.getSubscription();
          subscribed = !!sub;
        }
      } catch (e) { /* ignore */ }
      return {
        supported: this.supported,
        permission: Notification.permission,
        subscribed,
        serverSynced: subscribed ? serverSynced : true,
      };
    },

    async requestPermission() {
      if (!('Notification' in window)) return 'unsupported';
      if (Notification.permission === 'granted') return 'granted';
      try {
        return await Notification.requestPermission();
      } catch (e) {
        return 'denied';
      }
    },

    async subscribe(vapidKey) {
      if (!swReg) throw new Error('ServiceWorker недоступен');
      if (!('PushManager' in window)) throw new Error('PushManager недоступен');
      if (!serverConfigured) throw new Error('Не задан адрес push-сервера (push-bridge.js, PUSH_SERVER)');

      const key = vapidKey || VAPID_PUBLIC_KEY;
      if (!key) throw new Error('VAPID-ключ не задан');

      const permission = await this.requestPermission();
      if (permission !== 'granted') throw new Error('Разрешение не выдано');

      let sub = await swReg.pushManager.getSubscription();
      if (!sub) {
        sub = await swReg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(key),
        });
      }

      const uid = getUid();
      diag('подписка создана локально, uid=' + (uid ? String(uid).slice(0, 8) + '…' : 'НЕТ')); 
      try {
        const res = await sendToServer(PUSH_SERVER + '/subscribe', {
          uid: uid,
          timezone: tzName(),
          subscription: sub.toJSON(),
        });
        if (res && res.beacon) {
          // sendBeacon не возвращает ответ сервера — подтверждаем ретраем
          diag('подписка отправлена через beacon; ждём подтверждения ретраем');
          syncSubscription(12).catch(() => {});
        } else {
          serverSynced = true;
          if (syncTimer) { clearTimeout(syncTimer); syncTimer = null; }
          diag('подписка подтверждена сервером');
        }
      } catch (e) {
        if (!uid) throw new Error('Сервер недоступен или вход не выполнен (нет uid)');
        // Сервер мог спать (холодный старт) — запускаем автоповтор в фоне.
        syncSubscription(12).catch(() => {});
        throw new Error('Сервер не ответил (' + (e && e.message ? e.message : 'ошибка') + ') — подписка сохранена и будет передана автоматически');
      }

      // подписка готова — сразу отправляем расписание
      uploadSchedule(true).catch(() => {});
      return sub;
    },

    async unsubscribe() {
      if (!swReg) return false;
      const sub = await swReg.pushManager.getSubscription();
      if (!sub) return false;
      try {
        await sendToServer(PUSH_SERVER + '/unsubscribe', { endpoint: sub.endpoint });
      } catch (e) { /* локальная отписка важнее */ }
      return sub.unsubscribe();
    },
  };

  /* ================= Автозапуск ================= */

  try {
    if (window.fletPush.supported) {
      // Стартовая задержка: даём приложению восстановить сессию и снапшот.
      setTimeout(async () => {
        try {
          const st = await window.fletPush.status();
          diag('автозапуск: permission=' + st.permission + ', subscribed=' + st.subscribed + ', serverSynced=' + st.serverSynced);
          let dismissed = false;
          try { dismissed = localStorage.getItem(DISMISS_KEY) === '1'; } catch (e) {}

          if (st.permission === 'granted') {
            if (!st.subscribed) {
              // разрешение есть, подписки нет (обновление браузера/SW) — молча восстановим
              await window.fletPush.subscribe().catch(() => {});
            } else {
              // Локальная подписка есть: ОБЯЗАТЕЛЬНО повторяем /subscribe —
              // прежняя попытка могла уйти в спящий сервер и потеряться (v2).
              if (!serverSynced) syncSubscription(12).catch(() => {});
              await uploadSchedule().catch(() => {});
            }
            hideBanner();
          } else if (st.permission === 'default') {
            if (!dismissed) showBanner();
          } else if (st.permission === 'denied') {
            console.info('[push-bridge] уведомления запрещены в настройках Safari');
          }

          // Периодическая загрузка расписания, пока приложение открыто.
          // Заодно дотягиваем подписку до сервера, если она ещё не подтверждена (v2).
          setInterval(async () => {
            if (document.visibilityState === 'visible') {
              if (!serverSynced) await syncSubscription(2).catch(() => {});
              await uploadSchedule().catch(() => {});
            }
          }, SCHEDULE_UPLOAD_INTERVAL_MS);

          // Уход в фон — последний шанс синхронизировать расписание и подписку
          document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'hidden') {
              if (!serverSynced) syncSubscription(2).catch(() => {});
              uploadSchedule().catch(() => {});
            }
          });
        } catch (e) {
          console.debug('[push-bridge] init skipped:', e && e.message);
        }
      }, 5000);
    }
  } catch (e) {
    console.debug('[push-bridge] autosubscribe skipped:', e && e.message);
  }
})();
