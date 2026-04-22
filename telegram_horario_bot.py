import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Set

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

# =========================
# CONFIGURACIÓN
# =========================
# Variables de entorno esperadas:
# TELEGRAM_BOT_TOKEN
# GOOGLE_SHEET_ID
# GOOGLE_SHEET_RANGE=Horario!A:E
# TZ=America/Santiago
# AUTHORIZED_CHAT_ID=opcional
# NOTIFY_BEFORE_MINUTES=10
# GOOGLE_SERVICE_ACCOUNT_JSON=(contenido completo del json)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_SHEET_RANGE = os.environ.get("GOOGLE_SHEET_RANGE", "Horario!A:E")
TIMEZONE_NAME = os.environ.get("TZ", "America/Santiago")
AUTHORIZED_CHAT_ID = os.environ.get("AUTHORIZED_CHAT_ID")
NOTIFY_BEFORE_MINUTES = int(os.environ.get("NOTIFY_BEFORE_MINUTES", "10"))
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
LAST_CHAT_ID = None

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

DAY_MAP = {
    "monday": "lunes",
    "tuesday": "martes",
    "wednesday": "miercoles",
    "thursday": "jueves",
    "friday": "viernes",
    "saturday": "sabado",
    "sunday": "domingo",
}

DAY_ALIASES = {
    "miércoles": "miercoles",
    "miercoles": "miercoles",
    "sábado": "sabado",
    "sabado": "sabado",
}

ORDERED_DAYS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]


@dataclass
class ClassSlot:
    day: str
    start_minutes: int
    end_minutes: int
    subject: str
    room: str

    @property
    def start_str(self) -> str:
        return minutes_to_hhmm(self.start_minutes)

    @property
    def end_str(self) -> str:
        return minutes_to_hhmm(self.end_minutes)


def normalize_day(day_value: str) -> str:
    value = (day_value or "").strip().lower()
    return DAY_ALIASES.get(value, value)


def hhmm_to_minutes(value: str) -> int:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Hora inválida: {value}")
    return int(parts[0]) * 60 + int(parts[1])


def minutes_to_hhmm(total_minutes: int) -> str:
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def get_now_local() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(TIMEZONE_NAME))
    except Exception:
        return datetime.now()


def build_sheets_service():
    service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=SCOPES,
    )
    return build("sheets", "v4", credentials=credentials)


def fetch_schedule_rows() -> List[List[str]]:
    service = build_sheets_service()
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=GOOGLE_SHEET_ID, range=GOOGLE_SHEET_RANGE)
        .execute()
    )
    return result.get("values", [])


def parse_schedule(rows: List[List[str]]) -> List[ClassSlot]:
    parsed: List[ClassSlot] = []
    if not rows:
        return parsed

    start_index = 1 if rows and rows[0] and rows[0][0].strip().lower() in {"dia", "día"} else 0

    for row in rows[start_index:]:
        if len(row) < 5:
            continue
        day, start_time, end_time, subject, room = row[:5]
        try:
            parsed.append(
                ClassSlot(
                    day=normalize_day(day),
                    start_minutes=hhmm_to_minutes(start_time),
                    end_minutes=hhmm_to_minutes(end_time),
                    subject=subject.strip(),
                    room=room.strip(),
                )
            )
        except Exception:
            continue

    return sorted(
        parsed,
        key=lambda c: (ORDERED_DAYS.index(c.day) if c.day in ORDERED_DAYS else 99, c.start_minutes),
    )


def find_current_class(schedule: List[ClassSlot], now: Optional[datetime] = None) -> Optional[ClassSlot]:
    now = now or get_now_local()
    current_day = DAY_MAP.get(now.strftime("%A").lower(), "")
    current_minutes = now.hour * 60 + now.minute

    for class_slot in schedule:
        if class_slot.day == current_day and class_slot.start_minutes <= current_minutes < class_slot.end_minutes:
            return class_slot
    return None


def find_next_class(schedule: List[ClassSlot], now: Optional[datetime] = None) -> Optional[ClassSlot]:
    now = now or get_now_local()
    current_day = DAY_MAP.get(now.strftime("%A").lower(), "")
    current_minutes = now.hour * 60 + now.minute

    current_class = find_current_class(schedule, now)
    if current_class is not None:
        return current_class

    today_classes = [c for c in schedule if c.day == current_day and c.start_minutes >= current_minutes]
    if today_classes:
        return min(today_classes, key=lambda c: c.start_minutes)

    current_index = ORDERED_DAYS.index(current_day) if current_day in ORDERED_DAYS else 0
    for offset in range(1, 8):
        day_to_check = ORDERED_DAYS[(current_index + offset) % 7]
        classes_that_day = [c for c in schedule if c.day == day_to_check]
        if classes_that_day:
            return min(classes_that_day, key=lambda c: c.start_minutes)

    return None


def get_today_classes(schedule: List[ClassSlot], now: Optional[datetime] = None) -> List[ClassSlot]:
    now = now or get_now_local()
    current_day = DAY_MAP.get(now.strftime("%A").lower(), "")
    return sorted([c for c in schedule if c.day == current_day], key=lambda c: c.start_minutes)


def get_tomorrow_classes(schedule: List[ClassSlot], now: Optional[datetime] = None):
    now = now or get_now_local()
    current_day = DAY_MAP.get(now.strftime("%A").lower(), "lunes")
    current_index = ORDERED_DAYS.index(current_day) if current_day in ORDERED_DAYS else 0
    tomorrow_day = ORDERED_DAYS[(current_index + 1) % 7]
    tomorrow_classes = sorted([c for c in schedule if c.day == tomorrow_day], key=lambda c: c.start_minutes)
    return tomorrow_classes, tomorrow_day


def format_current_class_message(current_class: Optional[ClassSlot]) -> str:
    if current_class is None:
        return "Ahora mismo no estás en clases."

    return (
        "Estás en clase ahora:\n"
        f"Ramo: {current_class.subject}\n"
        f"Sala: {current_class.room}\n"
        f"Termina: {current_class.end_str}"
    )


def format_class_message(next_class: Optional[ClassSlot]) -> str:
    if next_class is None:
        return "No encontré clases en tu horario."

    return (
        "Tu próxima clase es:\n"
        f"Ramo: {next_class.subject}\n"
        f"Sala: {next_class.room}\n"
        f"Hora: {next_class.start_str}"
    )


def format_today_classes_message(today_classes: List[ClassSlot], now: Optional[datetime] = None) -> str:
    now = now or get_now_local()
    current_day = DAY_MAP.get(now.strftime("%A").lower(), "hoy")

    if not today_classes:
        return f"No tienes clases para {current_day}."

    lines = [f"Tus clases de {current_day}:"]
    for class_slot in today_classes:
        lines.append(f"{class_slot.start_str} - {class_slot.end_str} | {class_slot.subject} | {class_slot.room}")
    return "\n".join(lines)


def format_tomorrow_classes_message(tomorrow_classes: List[ClassSlot], tomorrow_day: str) -> str:
    if not tomorrow_classes:
        return f"No tienes clases para {tomorrow_day}."

    lines = [f"Tus clases de {tomorrow_day}:"]
    for class_slot in tomorrow_classes:
        lines.append(f"{class_slot.start_str} - {class_slot.end_str} | {class_slot.subject} | {class_slot.room}")
    return "\n".join(lines)


def send_message(chat_id: int, text: str) -> None:
    response = requests.post(
        f"{TELEGRAM_API_BASE}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=20,
    )
    response.raise_for_status()


def get_updates(offset: Optional[int] = None, timeout_seconds: int = 30) -> List[dict]:
    payload = {"timeout": timeout_seconds}
    if offset is not None:
        payload["offset"] = offset

    response = requests.get(
        f"{TELEGRAM_API_BASE}/getUpdates",
        params=payload,
        timeout=timeout_seconds + 10,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram devolvió error: {json.dumps(data, ensure_ascii=False)}")
    return data.get("result", [])


def is_authorized(chat_id: int) -> bool:
    if not AUTHORIZED_CHAT_ID:
        return True
    return str(chat_id) == str(AUTHORIZED_CHAT_ID)


def notification_key(class_slot: ClassSlot) -> str:
    return f"{class_slot.day}|{class_slot.start_minutes}|{class_slot.subject}|{class_slot.room}"


def maybe_send_notifications(schedule: List[ClassSlot], sent_notifications: Set[str], now: Optional[datetime] = None) -> None:
    if LAST_CHAT_ID is None:
        return

    now = now or get_now_local()
    current_day = DAY_MAP.get(now.strftime("%A").lower(), "")
    current_minutes = now.hour * 60 + now.minute
    target_minutes = current_minutes + NOTIFY_BEFORE_MINUTES

    for class_slot in schedule:
        if class_slot.day != current_day:
            continue

        key = notification_key(class_slot)
        if key in sent_notifications:
            continue

        if class_slot.start_minutes == target_minutes:
            message = (
                f"En {NOTIFY_BEFORE_MINUTES} minutos tienes clase:\n"
                f"Ramo: {class_slot.subject}\n"
                f"Sala: {class_slot.room}\n"
                f"Hora: {class_slot.start_str}"
            )
            send_message(LAST_CHAT_ID, message)
            sent_notifications.add(key)


def cleanup_sent_notifications(sent_notifications: Set[str], schedule: List[ClassSlot], now: Optional[datetime] = None) -> Set[str]:
    now = now or get_now_local()
    current_day = DAY_MAP.get(now.strftime("%A").lower(), "")
    current_minutes = now.hour * 60 + now.minute

    active_keys = set()
    for class_slot in schedule:
        if class_slot.day == current_day and class_slot.end_minutes >= current_minutes - 120:
            active_keys.add(notification_key(class_slot))

    return {key for key in sent_notifications if key in active_keys}


def handle_message(text: str) -> str:
    normalized = (text or "").strip().lower()
    rows = fetch_schedule_rows()
    schedule = parse_schedule(rows)

    if normalized in {"/start", "hola", "hi"}:
        return (
            "Hola. Escríbeme:\n"
            "- próxima clase\n"
            "- qué tengo ahora\n"
            "- /hoy\n"
            "- /mañana"
        )

    if normalized in {"/hoy", "hoy", "clases de hoy", "que tengo hoy", "qué tengo hoy"}:
        return format_today_classes_message(get_today_classes(schedule))

    if normalized in {"/mañana", "mañana", "manana", "clases de mañana", "clases de manana", "que tengo mañana", "que tengo manana", "qué tengo mañana"}:
        tomorrow_classes, tomorrow_day = get_tomorrow_classes(schedule)
        return format_tomorrow_classes_message(tomorrow_classes, tomorrow_day)

    if normalized in {"que tengo ahora", "qué tengo ahora", "estoy en clase", "tengo clase ahora", "/ahora"}:
        return format_current_class_message(find_current_class(schedule))

    if normalized in {"proxima clase", "próxima clase", "cual es mi proxima clase", "cuál es mi próxima clase", "siguiente clase", "/proxima"}:
        return format_class_message(find_next_class(schedule))

    return "No entendí. Usa: próxima clase, /hoy, /mañana o qué tengo ahora"


def run_bot_polling() -> None:
    global LAST_CHAT_ID
    print("Bot iniciado. Esperando mensajes y notificaciones...")
    update_offset = None
    sent_notifications: Set[str] = set()
    last_notification_check = None

    while True:
        try:
            updates = get_updates(offset=update_offset, timeout_seconds=20)

            for update in updates:
                update_offset = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message")
                if not message:
                    continue

                chat = message.get("chat", {})
                chat_id = chat.get("id")
                text = message.get("text", "")

                if chat_id is None:
                    continue

                LAST_CHAT_ID = chat_id

                if not is_authorized(chat_id):
                    send_message(chat_id, "Este bot no está habilitado para este chat.")
                    continue

                try:
                    reply = handle_message(text)
                except Exception as exc:
                    reply = f"Hubo un error leyendo el horario: {exc}"

                send_message(chat_id, reply)

            now = get_now_local()
            current_check = now.strftime("%Y-%m-%d %H:%M")
            if last_notification_check != current_check:
                rows = fetch_schedule_rows()
                schedule = parse_schedule(rows)
                sent_notifications = cleanup_sent_notifications(sent_notifications, schedule, now)
                maybe_send_notifications(schedule, sent_notifications, now)
                last_notification_check = current_check

        except requests.RequestException as exc:
            print(f"Error de red: {exc}")
            time.sleep(5)
        except Exception as exc:
            print(f"Error inesperado: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    run_bot_polling()

