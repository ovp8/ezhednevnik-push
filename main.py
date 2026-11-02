# main.py — Push-сервер «Ежедневника» для Render (FastAPI + pywebpush + firebase-admin).
#
# Что делает:
#   * Принимает push-подписки браузера/PWA:      POST /subscribe
#   * Удаляет подписки:                          POST /unsubscribe
#   * Принимает расписание напоминаний с PWA
#     (из локального снапшота db_snapshot_<uid>): POST /schedule
#   * Отправляет тестовый push:                  POST /test
#   * Health-check для мониторов (UptimeRobot):  GET  /health
#
# Планировщик: каждые TICK_SECONDS (60 c) проверяет расписание в Firestore
# и отправляет Web Push (VAPID) на все подписки пользователя.
#
# Устойчивость к сну Render Free (инстанс засыпает после ~15 минут без
# запросов, а деплой пересоздаёт контейнер с пустым диском):
#   * Подписки и расписание хранятся в Firestore — переживают рестарт.
#   * При старте сразу выполняется первый «догоняющий» тик: все напоминания,
#     наступившие за время сна (в пределах MISSED_GRACE_HOURS), отправляются
#     с пометкой «Пропущенное напоминание».
#   * Повторная отправка исключена: факт отправки фиксируется документом в
#     коллекции push_log с детерминированным id — create() атомарен, второй
#     раз одно и то же напоминание не уйдёт ни после сна, ни после рестарта.
#
# Все переменные окружения — см. .env.example.
import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import firebase_admin
from firebase_admin import credentials, firestore as fs_admin
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pywebpush import webpush, WebPushException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("push")

# ------------------------- Конфигурация -------------------------

VAPID_PRIVATE_PEM_B64 = (os.getenv("VAPID_PRIVATE_PEM_B64") or "").strip()
VAPID_SUBJECT = (os.getenv("VAPID_SUBJECT") or "mailto:daily-planner@example.com").strip()
TICK_SECONDS = int(os.getenv("TICK_SECONDS") or "60")
MISSED_GRACE_HOURS = float(os.getenv("MISSED_GRACE_HOURS") or "12")
TZ_OFFSET_HOURS = float(os.getenv("TZ_OFFSET_HOURS") or "3")   # Europe/Moscow по умолчанию
NOTIFY_ALL_TASKS = (os.getenv("NOTIFY_ALL_TASKS") or "1").strip() not in ("0", "false", "no")
CORS_ORIGINS = [o.strip() for o in (os.getenv("CORS_ORIGINS") or "https://ovp8.github.io").split(",") if o.strip()]
PUSH_TTL = int(os.getenv("PUSH_TTL") or str(7 * 24 * 3600))
APP_URL = (os.getenv("APP_URL") or "https://ovp8.github.io/ezhednevnik-offline/").strip()

# Внутренний сдвиг: напоминание считается «опоздавшим» сильнее этого порога —
# в заголовке пишем «Пропущенное…» вместо «Через N мин»/«Пора начинать».
LATE_THRESHOLD_SEC = 180

# Подавление дублей: напоминание только что показано ВНУТРИ приложения
# (PWA сообщила через /suppress) — push на это напоминание не отправляем.
# Ключ "uid:task_id" + окно: действует только если fire_dt рядом с «сейчас»
# (±3 мин) — догон давно просроченных подавление не отменяет.
_SUPPRESS: Dict[str, float] = {}   # "uid:task_id" -> expires (time.monotonic())
_SUPPRESS_TTL = 300.0
_SUPPRESS_WINDOW_SEC = 180


def _fail(msg: str) -> None:
    raise RuntimeError(msg)


# VAPID-ключ обязателен: без него Web Push не работает.
if not VAPID_PRIVATE_PEM_B64:
    _fail("Не задана переменная окружения VAPID_PRIVATE_PEM_B64 "
          "(base64 PEM приватного VAPID-ключа — см. README, шаг 2)")

try:
    _pem = base64.b64decode(VAPID_PRIVATE_PEM_B64).decode("ascii")
    if "BEGIN" not in _pem:
        _fail("VAPID_PRIVATE_PEM_B64 декодируется, но это не PEM")
    VAPID_PRIVATE_PEM = _pem
    # pywebpush принимает vapid_private_key как ПУТЬ к PEM-файлу (строку-PEM он
    # не разбирает: py_vapid.from_string ждёт RAW/DER). Поэтому пишем PEM во
    # временный файл при старте — контейнер одноразовый, /tmp пересоздаётся.
    import tempfile as _tempfile
    with _tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as _kf:
        _kf.write(_pem)
        VAPID_KEY_PATH = _kf.name
    from cryptography.hazmat.primitives import serialization as _ser
    _priv = _ser.load_pem_private_key(_pem.encode("ascii"), password=None)
    VAPID_PUBLIC_B64URL = base64.urlsafe_b64encode(
        _priv.public_key().public_bytes(
            encoding=_ser.Encoding.X962,
            format=_ser.PublicFormat.UncompressedPoint,
        )
    ).rstrip(b"=").decode("ascii")
    log.info("VAPID OK: public key %s…", VAPID_PUBLIC_B64URL[:16])
except Exception as ex:  # noqa: BLE001
    _fail(f"VAPID_PRIVATE_PEM_B64 некорректен: {ex}")

# Firebase Admin — обязательна (Firestore хранит подписки и расписание).
_SA_B64 = (os.getenv("FIREBASE_SERVICE_ACCOUNT_B64") or "").strip()
_SA_PATH = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
if _SA_B64:
    try:
        _sa_json = json.loads(base64.b64decode(_SA_B64).decode("utf-8"))
        firebase_admin.initialize_app(credentials.Certificate(_sa_json))
        log.info("Firebase Admin OK (service account из FIREBASE_SERVICE_ACCOUNT_B64)")
    except Exception as ex:  # noqa: BLE001
        _fail(f"FIREBASE_SERVICE_ACCOUNT_B64 некорректен: {ex}")
elif _SA_PATH and os.path.exists(_SA_PATH):
    firebase_admin.initialize_app(credentials.Certificate(_SA_PATH))
    log.info("Firebase Admin OK (файл %s)", _SA_PATH)
else:
    _fail("Не заданы ни FIREBASE_SERVICE_ACCOUNT_B64, ни GOOGLE_APPLICATION_CREDENTIALS")

_db = fs_admin.client()


# ------------------------- Вспомогательные -------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_fire_at(s: str) -> Optional[datetime]:
    """Парсит 'YYYY-MM-DDTHH:MM:SS(.ffffff)?Z' из reminders (UTC)."""
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.strptime(s.rstrip("Z"), "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        return datetime.strptime(s.rstrip("Z"), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_task_dt(s: str, tz) -> Optional[datetime]:
    """Парсит 'YYYY-MM-DD HH:MM' (локальное время задачи) → aware-UTC."""
    if not isinstance(s, str) or not s:
        return None
    try:
        naive = datetime.strptime(s.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    try:
        return naive.replace(tzinfo=tz).astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return naive.replace(tzinfo=timezone(timedelta(hours=TZ_OFFSET_HOURS))).astimezone(timezone.utc)


def uid_tz(tz_name: str):
    """IANA-имя часового пояса (приходит с PWA: Intl.DateTimeFormat)."""
    if not tz_name or not isinstance(tz_name, str):
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        return None


def default_tz():
    return timezone(timedelta(hours=TZ_OFFSET_HOURS))


def endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256((endpoint or "").encode("utf-8")).hexdigest()[:40]


def log_key(uid: str, task_id: int, epoch_sec: int) -> str:
    """Детерминированный ключ дедупликации отправок."""
    return f"{uid}:{int(task_id)}:{int(epoch_sec)}"


async def fire_and_forget(fn, *args):
    try:
        await asyncio.to_thread(fn, *args)
    except Exception as ex:  # noqa: BLE001
        log.warning("background task error: %s", ex)


# ------------------------- Отправка push -------------------------

def build_payload(reminder: dict, late: bool) -> dict:
    kind = reminder.get("kind") or "before"
    task_text = (reminder.get("task_text") or "").strip() or "Напоминание"
    minutes = int(reminder.get("minutes_before") or 0)

    if late:
        title = "Пропущенное напоминание"
    elif kind == "before" and minutes > 0:
        title = f"Через {minutes} мин"
    elif kind == "at_time":
        title = "Пора начинать"
    elif kind == "repeat":
        title = "Задача не выполнена"
    else:
        title = "Напоминание"

    return {
        "title": title,
        "body": task_text,
        "tag": f"task-{int(reminder.get('task_id') or 0)}",
        "task_id": int(reminder.get("task_id") or 0),
        "url": "./",
    }


def send_to_uid(uid: str, payload: dict) -> Dict[str, int]:
    """Отправляет payload на все подписки uid. 404/410 → подписка удаляется."""
    result = {"sent": 0, "failed": 0, "dropped": 0}
    try:
        docs = _db.collection("push_subs").where("uid", "==", uid).stream()
        targets = [d.to_dict() for d in docs]
    except Exception as ex:  # noqa: BLE001
        log.warning("[push] list subs error: %s", ex)
        return result

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    claims = {"sub": VAPID_SUBJECT}

    for sub in targets:
        info = {"endpoint": sub.get("endpoint"), "keys": sub.get("keys") or {}}
        if not info["endpoint"] or not info["keys"].get("p256dh") or not info["keys"].get("auth"):
            result["dropped"] += 1
            continue
        try:
            resp = webpush(
                subscription_info=info,
                data=data,
                vapid_private_key=VAPID_KEY_PATH,
                vapid_claims=claims,
                ttl=PUSH_TTL,
            )
            code = getattr(resp, "status_code", 200) if resp is not None else 200
            if code >= 400:
                log.warning("[push] endpoint status %s", code)
                if code in (404, 410):
                    _db.collection("push_subs").document(endpoint_hash(info["endpoint"])).delete()
                    result["dropped"] += 1
                else:
                    result["failed"] += 1
            else:
                result["sent"] += 1
        except WebPushException as ex:
            code = getattr(getattr(ex, "response", None), "status_code", None)
            if code in (404, 410):
                try:
                    _db.collection("push_subs").document(endpoint_hash(info["endpoint"])).delete()
                except Exception:  # noqa: BLE001
                    pass
                result["dropped"] += 1
            else:
                log.warning("[push] send error: %s", ex)
                result["failed"] += 1
        except Exception as ex:  # noqa: BLE001
            log.warning("[push] unexpected send error: %s", ex)
            result["failed"] += 1

    return result


def try_claim_log(uid: str, task_id: int, epoch: int, payload: dict, kind: str) -> bool:
    """Атомарно резервирует отправку: True — мы первые, False — уже отправлено."""
    ref = _db.collection("push_log").document(log_key(uid, task_id, epoch))
    try:
        ref.create({
            "sent_at": now_utc().isoformat(),
            "task_id": int(task_id),
            "kind": kind,
            "title": payload.get("title") or "",
            "uid": uid,
        })
        return True
    except Exception:  # noqa: BLE001  (AlreadyExists / конфликт)
        return False


# ------------------------- Планировщик -------------------------

def _merge_task_status(schedule_doc: dict, uid: str) -> Dict[int, dict]:
    """Задачи пользователя: из снапшота (свежее) + из Firestore (для офлайн-созданных
    на других устройствах). Формат snapshot-карты: {id: [task, datetime, status]}."""
    result: Dict[int, dict] = {}

    # 1) Firestore — источник для задач, которых нет в снапшоте
    try:
        docs = _db.collection("tasks").where("userId", "==", uid).stream()
        for d in docs:
            t = d.to_dict() or {}
            try:
                tid = int(t.get("localId") or d.id or 0)
            except (TypeError, ValueError):
                tid = 0
            if not tid or t.get("deleted"):
                continue
            try:
                status = int(t.get("status") or 0)
            except (TypeError, ValueError):
                status = 0
            result[tid] = {
                "task": (t.get("task") or "")[:200],
                "datetime": t.get("datetime") or "",
                "status": status,
            }
    except Exception as ex:  # noqa: BLE001
        log.warning("[tick] tasks query error (uid=%s…): %s", str(uid)[:8], ex)

    # 2) Снапшот PWA — приоритет (свежее: офлайн-правки ещё не синкнулись)
    snap_tasks = schedule_doc.get("tasks") or {}
    if isinstance(snap_tasks, dict):
        for k, v in snap_tasks.items():
            try:
                tid = int(k)
            except (TypeError, ValueError):
                continue
            if isinstance(v, (list, tuple)) and len(v) >= 3:
                try:
                    status = int(v[2] or 0)
                except (TypeError, ValueError):
                    status = 0
                result[tid] = {
                    "task": (str(v[0] or ""))[:200],
                    "datetime": str(v[1] or ""),
                    "status": status,
                }

    return result


def _schedule_tick(schedule_doc: dict, uid: str, tz, tasks_map: Dict[int, dict]) -> dict:
    """Один проход по расписанию одного пользователя.
    Возвращает статистику. Побочные эффекты: push_log, отметки sent, повторы."""
    stats = {"due": 0, "sent": 0, "skipped_done": 0, "late": 0, "repeats": 0}
    now = now_utc()
    grace_start = now - timedelta(hours=MISSED_GRACE_HOURS)

    reminders: List[dict] = schedule_doc.get("reminders") or []
    if not isinstance(reminders, list):
        return stats

    changed = False
    for r in reminders:
        if not isinstance(r, dict) or r.get("sent"):
            continue
        fire_dt = parse_fire_at(r.get("fire_at") or "")
        if fire_dt is None:
            continue
        if fire_dt > now or fire_dt < grace_start:
            continue  # ещё рано / слишком старо (за пределами grace)

        stats["due"] += 1
        if fire_dt < now - timedelta(seconds=LATE_THRESHOLD_SEC):
            stats["late"] += 1

        task_id = int(r.get("task_id") or 0)

        # Показано в приложении? Тогда push-дубль не нужен.
        sup_key = f"{uid}:{task_id}"
        if (_SUPPRESS.get(sup_key, 0.0) > time.monotonic()
                and abs((fire_dt - now).total_seconds()) <= _SUPPRESS_WINDOW_SEC):
            r["sent"] = True
            r["sent_at"] = now.isoformat()
            r["skip"] = "in_app"
            changed = True
            stats["skipped_done"] += 1
            log.info("[tick] suppressed (in-app shown) uid=%s… task=%s", str(uid)[:8], task_id)
            continue

        tinfo = tasks_map.get(task_id) or {}
        if int(tinfo.get("status") or 0) == 1:
            # задача уже выполнена — напоминание не нужно
            r["sent"] = True
            r["sent_at"] = now.isoformat()
            r["skip"] = "done"
            changed = True
            stats["skipped_done"] += 1
            continue

        payload = build_payload(r, late=(fire_dt < now - timedelta(seconds=LATE_THRESHOLD_SEC)))
        if not try_claim_log(uid, task_id, fire_dt.timestamp(), payload, r.get("kind") or "before"):
            # уже уходило (например, до сна сервера) — только помечаем
            r["sent"] = True
            r["sent_at"] = now.isoformat()
            r["skip"] = "already_sent"
            changed = True
            continue

        res = send_to_uid(uid, payload)
        log.info("[tick] uid=%s… task=%s kind=%s fire=%s → %s",
                 str(uid)[:8], task_id, r.get("kind"), r.get("fire_at"), res)
        if res["sent"] == 0 and res["dropped"] == 0:
            # ни одна подписка не приняла push — отпустим замок лога,
            # чтобы следующая попытка (после починки подписок) смогла отправить
            try:
                _db.collection("push_log").document(
                    log_key(uid, task_id, fire_dt.timestamp())).delete()
            except Exception:  # noqa: BLE001
                pass
            continue

        stats["sent"] += 1
        r["sent"] = True
        r["sent_at"] = now.isoformat()
        changed = True

        # --- автоповтор невыполненной задачи (как в приложении) ---
        # Цепочка: parent (before/at_time) → repeat №1 → repeat №2 …
        interval = int(r.get("repeat_interval_min") or 0)
        rmax = int(r.get("repeat_max") or 0)
        rcount = int(r.get("repeat_count") or 0)
        if interval > 0 and rmax > 0 and rcount < rmax:
            child_count = rcount + 1
            exists = any(
                isinstance(x, dict) and x.get("kind") == "repeat"
                and int(x.get("task_id") or 0) == task_id
                and int(x.get("repeat_count") or 0) == child_count
                and not x.get("sent")
                for x in reminders
            )
            if not exists:
                reminders.append({
                    "task_id": task_id,
                    "task_text": r.get("task_text") or tinfo.get("task") or "",
                    "fire_at": (now + timedelta(minutes=interval)).strftime(
                        "%Y-%m-%dT%H:%M:%S.000Z"),
                    "minutes_before": 0,
                    "kind": "repeat",
                    "repeat_interval_min": interval,
                    "repeat_max": rmax,
                    "repeat_count": child_count,
                    "origin": "server",
                    "sent": False,
                })
                stats["repeats"] += 1
                changed = True

    if changed:
        # чистим отправленные старше 1 дня, чтобы документ не пух
        cutoff = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        keep = [r for r in reminders
                if not (isinstance(r, dict) and r.get("sent")
                        and (r.get("sent_at") or "9") < cutoff)]
        schedule_doc["reminders"] = keep
        try:
            _db.collection("push_schedule").document(uid).set(
                {"reminders": keep, "touched_at": now.isoformat()}, merge=True)
        except Exception as ex:  # noqa: BLE001
            log.warning("[tick] schedule save error (uid=%s…): %s", str(uid)[:8], ex)

    return stats


def _task_scan_tick(schedule_doc: dict, uid: str, tz, tasks_map: Dict[int, dict]) -> dict:
    """Fallback: напоминание «в момент задачи» для задач БЕЗ явного напоминания.
    Можно отключить env NOTIFY_ALL_TASKS=0. Дедуп с /schedule — общий push_log."""
    stats = {"due": 0, "sent": 0}
    if not NOTIFY_ALL_TASKS:
        return stats
    now = now_utc()
    grace_start = now - timedelta(hours=MISSED_GRACE_HOURS)

    # минуты, уже покрытые ЯВНЫМИ напоминаниями этой задачи (в любом
    # состоянии — отправленные тоже закрывают своё время). Минутная
    # дискретизация закрывает расхождение секунд между fire_at напоминания
    # и временем задачи.
    covered: Dict[int, set] = {}
    for r in (schedule_doc.get("reminders") or []):
        if not isinstance(r, dict):
            continue
        f = parse_fire_at(r.get("fire_at") or "")
        if f is not None:
            covered.setdefault(int(r.get("task_id") or 0), set()).add(
                int(f.timestamp()) // 60)

    for tid, t in tasks_map.items():
        if int(t.get("status") or 0) == 1:
            continue
        dt = parse_task_dt(t.get("datetime") or "", tz)
        if dt is None or dt > now or dt < grace_start:
            continue
        stats["due"] += 1
        epoch = int(dt.timestamp())
        if (epoch // 60) in covered.get(tid, set()):
            continue  # явное at_time-напоминание уже обработано
        payload = {
            "title": "Пропущенное напоминание" if dt < now - timedelta(seconds=LATE_THRESHOLD_SEC)
                     else "Пора начинать",
            "body": (t.get("task") or "").strip() or "Задача",
            "tag": f"task-{tid}",
            "task_id": tid,
            "url": "./",
        }
        if not try_claim_log(uid, tid, epoch, payload, "task_scan"):
            continue
        res = send_to_uid(uid, payload)
        log.info("[tick] task-scan uid=%s… task=%s dt=%s → %s",
                 str(uid)[:8], tid, t.get("datetime"), res)
        if res["sent"] > 0:
            stats["sent"] += 1

    return stats


def _cleanup_tick() -> None:
    """Раз в N тиков чистим push_log старше 7 дней и несвежие подписки."""
    cutoff = now_utc() - timedelta(days=7)
    cutoff_iso = cutoff.isoformat()
    try:
        docs = _db.collection("push_log").where("sent_at", "<", cutoff_iso).limit(300).stream()
        n = 0
        for d in docs:
            d.reference.delete()
            n += 1
        if n:
            log.info("[cleanup] push_log удалено: %d", n)
    except Exception as ex:  # noqa: BLE001
        log.debug("[cleanup] push_log: %s", ex)


_tick_counter = 0


def run_tick() -> dict:
    """Полный проход планировщика по всем расписаниям."""
    global _tick_counter
    _tick_counter += 1
    stats_total = {"uids": 0, "due": 0, "sent": 0, "repeats": 0, "scan_sent": 0}
    started = time.monotonic()

    try:
        schedules = _db.collection("push_schedule").stream()
        docs = [(d.id, d.to_dict() or {}) for d in schedules]
    except Exception as ex:  # noqa: BLE001
        log.warning("[tick] schedule read error: %s", ex)
        return stats_total

    for uid, sdoc in docs:
        if not uid or not isinstance(sdoc, dict):
            continue
        stats_total["uids"] += 1
        tz = uid_tz(sdoc.get("timezone") or "") or default_tz()
        tasks_map = _merge_task_status(sdoc, uid)
        s1 = _schedule_tick(sdoc, uid, tz, tasks_map)
        s2 = _task_scan_tick(sdoc, uid, tz, tasks_map)
        stats_total["due"] += s1["due"] + s2["due"]
        stats_total["sent"] += s1["sent"] + s2["sent"]
        stats_total["repeats"] += s1["repeats"]
        stats_total["scan_sent"] += s2["sent"]

    if _tick_counter % 15 == 0:  # каждые ~15 минут
        fire_and_forget_sync(_cleanup_tick)

    log.info("[tick] done in %.2fs: %s", time.monotonic() - started, stats_total)
    return stats_total


def fire_and_forget_sync(fn):
    """Синхронная очистка вне event loop (вызывается из run_tick в потоке)."""
    try:
        fn()
    except Exception as ex:  # noqa: BLE001
        log.warning("[cleanup] error: %s", ex)


async def scheduler_loop():
    # Первый тик — сразу при старте: догоняем напоминания, наступившие,
    # пока Render спал / пересобирался. Это и есть устойчивость к пробуждению.
    await asyncio.sleep(3)
    while True:
        try:
            await asyncio.to_thread(run_tick)
        except Exception as ex:  # noqa: BLE001
            log.warning("[tick] loop error: %s", ex)
        await asyncio.sleep(max(10, TICK_SECONDS))


# ------------------------- HTTP -------------------------

app = FastAPI(title="Ежедневник — Push-сервер", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "*"],
)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(scheduler_loop())
    log.info("Push-сервер запущен. Тик каждые %s c, grace %s ч, tz по умолчанию UTC+%s",
             TICK_SECONDS, MISSED_GRACE_HOURS, TZ_OFFSET_HOURS)


@app.get("/")
async def root():
    return JSONResponse({
        "service": "ezhednevnik-push",
        "status": "ok",
        "vapid_public_key": VAPID_PUBLIC_B64URL,
        "app": APP_URL,
    })


@app.get("/health")
async def health():
    subs = 0
    schedules = 0
    try:
        subs = len(list(_db.collection("push_subs").limit(100).stream()))
        schedules = len(list(_db.collection("push_schedule").limit(100).stream()))
    except Exception as ex:  # noqa: BLE001
        log.warning("[health] firestore error: %s", ex)
        return JSONResponse({"ok": False, "error": "firestore"}, status_code=500)
    return {
        "ok": True,
        "service": "ezhednevnik-push",
        "time": now_utc().isoformat(),
        "subscriptions": subs,
        "schedules": schedules,
        "tick_seconds": TICK_SECONDS,
        "notify_all_tasks": NOTIFY_ALL_TASKS,
    }


@app.post("/subscribe")
async def subscribe(req: Request):
    try:
        data = await req.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)

    uid = str(data.get("uid") or "").strip()
    sub = data.get("subscription") or {}
    endpoint = str(sub.get("endpoint") or "").strip()
    keys = sub.get("keys") or {}
    tz_name = str(data.get("timezone") or "").strip()

    if not uid or not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return JSONResponse({"ok": False, "error": "uid/endpoint/keys required"}, status_code=400)

    doc = {
        "uid": uid,
        "endpoint": endpoint,
        "keys": {"p256dh": str(keys["p256dh"]), "auth": str(keys["auth"])},
        "timezone": tz_name,
        "user_agent": req.headers.get("user-agent", "")[:200],
        "updated_at": now_utc().isoformat(),
    }
    ref = _db.collection("push_subs").document(endpoint_hash(endpoint))
    existing = ref.get()
    if existing.exists:
        doc["created_at"] = (existing.to_dict() or {}).get("created_at") or doc["updated_at"]
    else:
        doc["created_at"] = doc["updated_at"]
    ref.set(doc)

    # Включение уведомлений — триггер: если расписание ещё не загружено,
    # пользователь получит напоминания после первого /schedule с PWA.
    log.info("[subscribe] uid=%s… tz=%s total_ok", str(uid)[:8], tz_name or "?")
    return {"ok": True}


@app.post("/unsubscribe")
async def unsubscribe(req: Request):
    try:
        data = await req.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    endpoint = str(data.get("endpoint") or "").strip()
    if not endpoint and isinstance(data.get("subscription"), dict):
        endpoint = str(data["subscription"].get("endpoint") or "").strip()
    if not endpoint:
        return JSONResponse({"ok": False, "error": "endpoint required"}, status_code=400)
    _db.collection("push_subs").document(endpoint_hash(endpoint)).delete()
    log.info("[unsubscribe] ok")
    return {"ok": True}


@app.post("/schedule")
async def schedule_upload(req: Request):
    """PWA загружает расписание напоминаний из локального снапшота.
    Напоминания origin=server (созданные сервером автоповторы) сохраняются."""
    try:
        data = await req.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)

    uid = str(data.get("uid") or "").strip()
    tz_name = str(data.get("timezone") or "").strip()
    reminders_in = data.get("reminders") or []
    tasks_in = data.get("tasks") or {}
    if not uid or not isinstance(reminders_in, list):
        return JSONResponse({"ok": False, "error": "uid/reminders required"}, status_code=400)

    # ---- нормализация пришедших напоминаний ----
    clean: List[dict] = []
    for r in reminders_in[:500]:
        if not isinstance(r, dict):
            continue
        fire = parse_fire_at(r.get("fire_at") or "")
        if fire is None:
            continue
        # берём только несработавшие; окно в прошлое = MISSED_GRACE_HOURS
        # (догон после сна сервера), в будущее — 8 дней
        if r.get("fired"):
            continue
        if (fire < now_utc() - timedelta(hours=MISSED_GRACE_HOURS)
                or fire > now_utc() + timedelta(days=8)):
            continue
        try:
            task_id = int(r.get("task_id") or 0)
        except (TypeError, ValueError):
            continue
        if not task_id:
            continue
        clean.append({
            "task_id": task_id,
            "task_text": str(r.get("task_text") or "")[:300],
            "fire_at": fire.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "minutes_before": int(r.get("minutes_before") or 0),
            "kind": str(r.get("kind") or "before")[:16],
            "repeat_interval_min": int(r.get("repeat_interval_min") or 0),
            "repeat_max": int(r.get("repeat_max") or 0),
            "repeat_count": int(r.get("repeat_count") or 0),
            "origin": "device",
            "sent": False,
        })

    # ---- карта задач (для проверки «выполнена ли» и task-scan) ----
    tasks_map: Dict[str, list] = {}
    if isinstance(tasks_in, dict):
        for k, v in list(tasks_in.items())[:1000]:
            try:
                int(k)
            except (TypeError, ValueError):
                continue
            if isinstance(v, (list, tuple)) and len(v) >= 3:
                tasks_map[str(k)] = [str(v[0] or "")[:200], str(v[1] or "")[:20],
                                     int(v[2] or 0)]

    ref = _db.collection("push_schedule").document(uid)
    existing = ref.get()
    server_reminders: List[dict] = []
    if existing.exists:
        old = existing.to_dict() or {}
        server_reminders = [r for r in (old.get("reminders") or [])
                            if isinstance(r, dict) and r.get("origin") == "server"
                            and not r.get("sent")]

    merged = clean + server_reminders
    ref.set({
        "uid": uid,
        "timezone": tz_name,
        "reminders": merged,
        "tasks": tasks_map,
        "updated_at": now_utc().isoformat(),
    })

    log.info("[schedule] uid=%s… reminders=%d (server_kept=%d) tasks=%d",
             str(uid)[:8], len(merged), len(server_reminders), len(tasks_map))
    return {"ok": True, "accepted": len(clean), "server_kept": len(server_reminders)}


@app.post("/suppress")
async def suppress(req: Request):
    """PWA сообщает: напоминание уже показано внутри приложения.
    Планировщик не будет слать push-дубль на это же время."""
    try:
        data = await req.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    uid = str((data or {}).get("uid") or "").strip()
    try:
        task_id = int((data or {}).get("task_id") or 0)
    except (TypeError, ValueError):
        task_id = 0
    if not uid or not task_id:
        return JSONResponse({"ok": False, "error": "uid/task_id required"}, status_code=400)

    # подчистим просроченные записи
    now_mono = time.monotonic()
    if len(_SUPPRESS) > 500:
        for k in [k for k, v in _SUPPRESS.items() if v <= now_mono]:
            _SUPPRESS.pop(k, None)
    _SUPPRESS[f"{uid}:{task_id}"] = now_mono + _SUPPRESS_TTL
    return {"ok": True}


@app.post("/test")
async def test_push(req: Request):
    """Тестовая отправка: {'uid': '...'} — на все подписки пользователя."""
    try:
        data = await req.json()
    except Exception:  # noqa: BLE001
        data = {}
    uid = str((data or {}).get("uid") or "").strip()
    if not uid:
        return JSONResponse({"ok": False, "error": "uid required"}, status_code=400)
    payload = {
        "title": "Проверка уведомлений",
        "body": "Если видите это сообщение — push работает ✓",
        "tag": f"test-{int(time.time())}",
        "task_id": 0,
        "url": "./",
    }
    res = await asyncio.to_thread(send_to_uid, uid, payload)
    return {"ok": True, **res}


# ------------------------- Локальный запуск -------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT") or "8080"))
