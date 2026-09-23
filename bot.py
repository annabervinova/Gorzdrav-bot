# -*- coding: utf-8 -*-
import os
import sys
import json
import time
import logging
import threading
import requests
import telebot
from telebot import types, ExceptionHandler
from http.server import HTTPServer, BaseHTTPRequestHandler

# =========================================================
# Логирование — сразу в stderr (не буферизуется)
# =========================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stderr,
    force=True,
)
log = logging.getLogger(__name__)
log.info("🚀 VERSION 5: bot.py запущен")

# =========================================================
# Настройки
# =========================================================
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHECK_INTERVAL = 60                # интервал проверки талонов (сек)
DATA_FILE = 'subscriptions.json'   # файл с подписками

BASE_URL = "https://gorzdrav.spb.ru/_api/api/v2"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': 'application/json',
}

if not TELEGRAM_TOKEN:
    raise SystemExit("❌ Не задан TELEGRAM_TOKEN в переменных окружения!")
if ':' not in TELEGRAM_TOKEN:
    raise SystemExit("❌ TELEGRAM_TOKEN выглядит некорректно (нет ':').")

log.info("✅ TELEGRAM_TOKEN получен, длина: %d символов", len(TELEGRAM_TOKEN))

# =========================================================
# Обработчик исключений: подавляем 409, чтобы бот не падал
# =========================================================
class MyExceptionHandler(ExceptionHandler):
    def handle(self, exception):
        if isinstance(exception, telebot.apihelper.ApiTelegramException):
            if exception.error_code == 409:
                log.warning("⚠️ Конфликт 409: другой экземпляр бота. Пропускаю...")
                return True
            # Ошибку "query is too old" тоже подавляем — это не критично
            if exception.error_code == 400 and 'too old' in str(exception):
                log.warning("⚠️ Query too old — пропускаю")
                return True
        log.error("Ошибка polling: %s", exception)
        return True

bot = telebot.TeleBot(TELEGRAM_TOKEN, exception_handler=MyExceptionHandler())

# Временное состояние выбора: chat_id -> {'lpu_id':..., 'lpu_name':..., ...}
user_state = {}

# =========================================================
# Хранилище подписок
# =========================================================
def load_subs():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            log.warning("Не удалось прочитать %s: %s", DATA_FILE, e)
            return {}
    return {}

def save_subs(subs):
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(subs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("Не удалось сохранить %s: %s", DATA_FILE, e)

# =========================================================
# API-запросы (всегда возвращают список или dict, но не None)
# =========================================================
def api_get(path):
    url = f"{BASE_URL}{path}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        log.info("GET %s → %s", url, r.status_code)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.error("API error %s: %s", url, e)
        return []

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        if 'success' in data and not data.get('success'):
            log.warning("API %s success=false: %s", url, str(data)[:200])
            return []
        for key in ('result', 'data', 'items'):
            val = data.get(key)
            if val is not None:
                return val
        return []

    return []

def get_districts():
    return api_get("/shared/districts") or []

def get_lpus(district_id):
    return api_get(f"/shared/district/{district_id}/lpus") or []

def get_specialties(lpu_id):
    return api_get(f"/schedule/lpu/{lpu_id}/specialties") or []

def get_doctors(lpu_id, spec_id):
    return api_get(f"/schedule/lpu/{lpu_id}/speciality/{spec_id}/doctors") or []

def get_appointments(lpu_id, spec_id, doctor_id):
    return api_get(f"/schedule/lpu/{lpu_id}/speciality/{spec_id}/doctors/{doctor_id}/appointments") or []

# =========================================================
# Команды
# =========================================================
@bot.message_handler(commands=['start'])
def cmd_start(message):
    log.info("📩 /start от %s", message.chat.id)
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🏥 Добавить подписку", callback_data="new"),
        types.InlineKeyboardButton("📋 Мои подписки", callback_data="list"),
        types.InlineKeyboardButton("🗑 Очистить всё", callback_data="clear_all"),
    )
    bot.send_message(
        message.chat.id,
        "Привет! Я слежу за появлением талонов на gorzdrav.spb.ru.\n"
        "Нажмите «Добавить подписку», чтобы выбрать поликлинику и специальность.",
        reply_markup=markup,
    )

@bot.message_handler(commands=['help'])
def cmd_help(message):
    bot.send_message(message.chat.id,
        "Как пользоваться:\n"
        "1. /start → «Добавить подписку»\n"
        "2. Выберите район → поликлинику → специальность\n"
        "3. Я проверяю талоны каждую минуту и пришлю уведомление.\n\n"
        "/list — мои подписки\n"
        "/stop — удалить все подписки")

@bot.message_handler(commands=['list'])
def cmd_list(message):
    show_subs(message.chat.id)

@bot.message_handler(commands=['stop'])
def cmd_stop(message):
    subs = load_subs()
    subs.pop(str(message.chat.id), None)
    save_subs(subs)
    bot.send_message(message.chat.id, "Все ваши подписки удалены.")

# =========================================================
# Показ подписок
# =========================================================
def show_subs(chat_id):
    subs = load_subs().get(str(chat_id), [])
    if not subs:
        bot.send_message(chat_id, "У вас пока нет подписок.")
        return
    text = "Ваши подписки:\n"
    markup = types.InlineKeyboardMarkup(row_width=1)
    for i, s in enumerate(subs):
        text += f"\n{i+1}. {s.get('lpu_name','?')} — {s.get('spec_name','?')}"
        markup.add(types.InlineKeyboardButton(
            f"❌ Удалить: {s.get('spec_name','?')}", callback_data=f"del:{i}"
        ))
    bot.send_message(chat_id, text, reply_markup=markup)

# =========================================================
# Шаг 1: Добавить подписку — выбор района
# =========================================================
@bot.callback_query_handler(func=lambda c: c.data == "new")
def on_new(call):
    # Сразу подтверждаем callback, чтобы Telegram не ругался "too old"
    try:
        bot.answer_callback_query(call.id)
    except Exception as e:
        log.warning("answer_callback_query: %s", e)

    log.info("▶ on_new для %s", call.message.chat.id)
    districts = get_districts()
    log.info("📋 Получено районов: %d", len(districts))

    if not districts:
        bot.send_message(
            call.message.chat.id,
            "⚠️ API не вернул районы. Возможно, сайт временно недоступен. Попробуйте позже."
        )
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for d in districts:
        name = d.get('name') or d.get('shortName') or f"Район {d.get('id')}"
        markup.add(types.InlineKeyboardButton(name, callback_data=f"d:{d.get('id')}"))

    try:
        bot.edit_message_text("Выберите район:", call.message.chat.id,
                              call.message.message_id, reply_markup=markup)
    except Exception as e:
        log.warning("edit_message_text: %s", e)
        bot.send_message(call.message.chat.id, "Выберите район:", reply_markup=markup)

# =========================================================
# Шаг 2: Выбор поликлиники
# =========================================================
@bot.callback_query_handler(func=lambda c: c.data.startswith("d:"))
def on_district(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    district_id = call.data.split(":", 1)[1]
    lpus = get_lpus(district_id)
    log.info("▶ on_district %s → %d ЛПУ", district_id, len(lpus))

    if not lpus:
        bot.send_message(call.message.chat.id, "⚠️ Список поликлиник пуст.")
        return

    # Запоминаем district и маппинг lpu_id -> название
    st = user_state.setdefault(call.message.chat.id, {})
    st['district_id'] = district_id
    st['lpu_names'] = {
        str(l.get('id')): (l.get('shortName') or l.get('name') or f"ЛПУ {l.get('id')}")
        for l in lpus
    }

    markup = types.InlineKeyboardMarkup(row_width=1)
    for l in lpus:
        name = l.get('shortName') or l.get('name') or f"ЛПУ {l.get('id')}"
        markup.add(types.InlineKeyboardButton(name, callback_data=f"l:{l.get('id')}"))

    try:
        bot.edit_message_text("Выберите поликлинику:", call.message.chat.id,
                              call.message.message_id, reply_markup=markup)
    except Exception:
        bot.send_message(call.message.chat.id, "Выберите поликлинику:", reply_markup=markup)

# =========================================================
# Шаг 3: Выбор специальности
# =========================================================
@bot.callback_query_handler(func=lambda c: c.data.startswith("l:"))
def on_lpu(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    lpu_id = call.data.split(":", 1)[1]
    specs = get_specialties(lpu_id)
    log.info("▶ on_lpu %s → %d специальностей", lpu_id, len(specs))

    if not specs:
        bot.send_message(call.message.chat.id, "⚠️ Список специальностей пуст.")
        return

    st = user_state.setdefault(call.message.chat.id, {})
    st['lpu_id'] = lpu_id
    st['lpu_name'] = st.get('lpu_names', {}).get(lpu_id, f"ЛПУ {lpu_id}")
    st['spec_names'] = {
        str(s.get('id')): (s.get('name') or f"Спец. {s.get('id')}")
        for s in specs
    }

    markup = types.InlineKeyboardMarkup(row_width=1)
    for s in specs:
        markup.add(types.InlineKeyboardButton(
            s.get('name') or f"Спец. {s.get('id')}",
            callback_data=f"s:{s.get('id')}"
        ))

    try:
        bot.edit_message_text("Выберите специальность:", call.message.chat.id,
                              call.message.message_id, reply_markup=markup)
    except Exception:
        bot.send_message(call.message.chat.id, "Выберите специальность:", reply_markup=markup)

# =========================================================
# Шаг 4: Подтверждение и сохранение подписки
# =========================================================
@bot.callback_query_handler(func=lambda c: c.data.startswith("s:"))
def on_spec(call):
    # СРАЗУ подтверждаем callback
    try:
        bot.answer_callback_query(call.id, "Добавляю...")
    except Exception:
        pass

    spec_id = call.data.split(":", 1)[1]
    chat_id = call.message.chat.id
    st = user_state.get(chat_id, {})
    lpu_id = st.get('lpu_id')
    lpu_name = st.get('lpu_name', f"ЛПУ {lpu_id}")
    spec_name = st.get('spec_names', {}).get(spec_id, f"Спец. {spec_id}")

    log.info("▶ on_spec lpu=%s spec=%s", lpu_id, spec_id)

    if not lpu_id:
        bot.send_message(chat_id, "⚠️ Не удалось определить поликлинику. Начните заново.")
        return

    subs = load_subs()
    user_subs = subs.get(str(chat_id), [])
    if not isinstance(user_subs, list):
        user_subs = []
    if any(x.get('lpu_id') == lpu_id and x.get('spec_id') == spec_id for x in user_subs):
        bot.send_message(chat_id, "Такая подписка уже есть.")
        return

    user_subs.append({
        'lpu_id': lpu_id,
        'spec_id': spec_id,
        'lpu_name': lpu_name,
        'spec_name': spec_name,
        'seen': [],
    })
    subs[str(chat_id)] = user_subs
    save_subs(subs)

    try:
        bot.edit_message_text(
            f"✅ Подписка добавлена:\n{lpu_name}\n{spec_name}\n\n"
            f"Буду проверять каждые {CHECK_INTERVAL} сек.",
            chat_id, call.message.message_id)
    except Exception:
        bot.send_message(chat_id,
            f"✅ Подписка добавлена:\n{lpu_name}\n{spec_name}")

# =========================================================
# Показ подписок через кнопку
# =========================================================
@bot.callback_query_handler(func=lambda c: c.data == "list")
def on_list_btn(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    show_subs(call.message.chat.id)

# =========================================================
# Удаление подписок
# =========================================================
@bot.callback_query_handler(func=lambda c: c.data.startswith("del:"))
def on_del(call):
    try:
        bot.answer_callback_query(call.id, "Удалено")
    except Exception:
        pass

    try:
        idx = int(call.data.split(":", 1)[1])
    except ValueError:
        return

    subs = load_subs()
    chat_id = str(call.message.chat.id)
    if chat_id in subs and isinstance(subs[chat_id], list) and 0 <= idx < len(subs[chat_id]):
        subs[chat_id].pop(idx)
        save_subs(subs)
    show_subs(call.message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "clear_all")
def on_clear(call):
    try:
        bot.answer_callback_query(call.id, "Удаляю...")
    except Exception:
        pass
    subs = load_subs()
    subs.pop(str(call.message.chat.id), None)
    save_subs(subs)
    bot.send_message(call.message.chat.id, "Готово, все подписки удалены.")

# =========================================================
# Фоновый мониторинг талонов
# =========================================================
def monitor_loop():
    log.info("👀 Мониторинг запущен")
    while True:
        try:
            subs = load_subs()
            for chat_id, user_subs in list(subs.items()):
                if not isinstance(user_subs, list):
                    continue
                for sub in user_subs:
                    lpu_id = sub.get('lpu_id')
                    spec_id = sub.get('spec_id')
                    if not lpu_id or not spec_id:
                        continue
                    doctors = get_doctors(lpu_id, spec_id)
                    for doc in doctors:
                        appts = get_appointments(lpu_id, spec_id, doc.get('id'))
                        for a in appts:
                            key = f"{doc.get('id')}_{a.get('visitDate','')}_{a.get('visitTime','')}"
                            if key in sub.get('seen', []):
                                continue
                            text = (f"🎉 Найден талон!\n"
                                    f"🏥 {sub.get('lpu_name','?')}\n"
                                    f"👨‍⚕️ {doc.get('name','Врач')}\n"
                                    f"📅 {a.get('visitDate','?')} в {a.get('visitTime','?')}")
                            try:
                                bot.send_message(int(chat_id), text)
                            except Exception as e:
                                log.error("send_message %s: %s", chat_id, e)
                            sub.setdefault('seen', []).append(key)
                    sub['seen'] = sub.get('seen', [])[-500:]
            save_subs(subs)
        except Exception as e:
            log.exception("Ошибка в monitor_loop: %s", e)
        time.sleep(CHECK_INTERVAL)

# =========================================================
# Health-check HTTP-сервер (для бесплатного Render)
# =========================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')
    def log_message(self, *args):
        pass

def run_health_server():
    port = int(os.environ.get('PORT', 10000))
    log.info("🌐 Health-сервер на 0.0.0.0:%d", port)
    HTTPServer(('0.0.0.0', port), HealthHandler).serve_forever()

# =========================================================
# Запуск
# =========================================================
if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    log.info("✅ Бот запущен, начинаю polling")

    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=25,
                allowed_updates=["message", "callback_query"],
            )
        except Exception as e:
            log.exception("Polling упал, перезапуск через 5 сек: %s", e)
            time.sleep(5)