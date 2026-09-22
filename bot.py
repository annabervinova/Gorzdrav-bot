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
# Логирование — сразу пишем в stderr (не буферизуется)
# =========================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stderr,
    force=True,
)
log = logging.getLogger(__name__)
log.info("🚀 VERSION 4: bot.py запущен")

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
# Обработчик исключений (подавляем 409 Conflict)
# =========================================================
class MyExceptionHandler(ExceptionHandler):
    def handle(self, exception):
        if isinstance(exception, telebot.apihelper.ApiTelegramException):
            if exception.error_code == 409:
                log.warning("⚠️ Конфликт 409: другой экземпляр бота. Пропускаю...")
                return True
        log.error("Ошибка polling: %s", exception)
        return True  # не даём потоку упасть

bot = telebot.TeleBot(TELEGRAM_TOKEN, exception_handler=MyExceptionHandler())
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

    # API может вернуть список напрямую
    if isinstance(data, list):
        return data

    # Или словарь с success/result/data/items
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
# Пошаговый выбор: район → ЛПУ → специальность
# =========================================================
@bot.callback_query_handler(func=lambda c: c.data == "new")
def on_new(call):
    log.info("▶ on_new для %s", call.message.chat.id)
    districts = get_districts()
    log.info("📋 Получено районов: %d", len(districts))
    if not districts:
        bot.answer_callback_query(call.id, "Не удалось загрузить районы")
        bot.send_message(call.message.chat.id, "⚠️ API не вернул районы. Попробуйте позже.")
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
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("d:"))
def on_district(call):
    district_id = call.data.split(":", 1)[1]
    lpus = get_lpus(district_id)
    log.info("▶ on_district %s → %d ЛПУ", district_id, len(lpus))
    if not lpus:
        bot.answer_callback_query(call.id, "Нет поликлиник")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for l in lpus:
        name = l.get('shortName') or l.get('name') or f"ЛПУ {l.get('id')}"
        markup.add(types.InlineKeyboardButton(name, callback_data=f"l:{l.get('id')}"))
    try:
        bot.edit_message_text("Выберите поликлинику:", call.message.chat.id,
                              call.message.message_id, reply_markup=markup)
    except Exception:
        bot.send_message(call.message.chat.id, "Выберите поликлинику:", reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("l:"))
def on_lpu(call):
    lpu_id = call.data.split(":", 1)[1]
    specs = get_specialties(lpu_id)
    log.info("▶ on_lpu %s → %d специальностей", lpu_id, len(specs))
    if not specs:
        bot.answer_callback_query(call.id, "Нет специальностей")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for s in specs:
        name = s.get('name') or f"Спец. {s.get('id')}"
        markup.add(types.InlineKeyboardButton(name, callback_data=f"s:{s.get('id')}|{lpu_id}"))
    try:
        bot.edit_message_text("Выберите специальность:", call.message.chat.id,
                              call.message.message_id, reply_markup=markup)
    except Exception:
        bot.send_message(call.message.chat.id, "Выберите специальность:", reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("s:"))
def on_spec(call):
    try:
        spec_id, lpu_id = call.data[2:].split("|", 1)
    except ValueError:
        bot.answer_callback_query(call.id, "Ошибка данных")
        return
    chat_id = call.message.chat.id
    log.info("▶ on_spec lpu=%s spec=%s", lpu_id, spec_id)

    # Достаём человекочитаемые имена
    lpu_name = f"ЛПУ {lpu_id}"
    for l in get_lpus_by_any_district(lpu_id):
        if str(l.get('id')) == str(lpu_id):
            lpu_name = l.get('shortName') or l.get('name') or lpu_name
            break

    spec_name = f"Спец. {spec_id}"
    for s in get_specialties(lpu_id):
        if str(s.get('id')) == str(spec_id):
            spec_name = s.get('name') or spec_name
            break

    subs = load_subs()
    user_subs = subs.get(str(chat_id), [])
    if not isinstance(user_subs, list):
        user_subs = []
    if any(x.get('lpu_id') == lpu_id and x.get('spec_id') == spec_id for x in user_subs):
        bot.answer_callback_query(call.id, "Такая подписка уже есть")
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
    bot.answer_callback_query(call.id, "Готово!")

def get_lpus_by_any_district(lpu_id):
    """Хелпер: получить список ЛПУ по всем районам (для поиска имени)."""
    # Пробуем несколько известных районов — если API отдаёт не список районов, а что-то другое
    districts = get_districts()
    for d in districts:
        for l in get_lpus(d.get('id')):
            if str(l.get('id')) == str(lpu_id):
                return [l]
    return []

# =========================================================
# Удаление подписок
# =========================================================
@bot.callback_query_handler(func=lambda c: c.data.startswith("del:"))
def on_del(call):
    try:
        idx = int(call.data.split(":", 1)[1])
    except ValueError:
        return
    subs = load_subs()
    chat_id = str(call.message.chat.id)
    if chat_id in subs and isinstance(subs[chat_id], list) and 0 <= idx < len(subs[chat_id]):
        subs[chat_id].pop(idx)
        save_subs(subs)
    bot.answer_callback_query(call.id, "Удалено")
    show_subs(call.message.chat.id)

@bot.callback_query_handler(func=lambda c: c.data == "clear_all")
def on_clear(call):
    subs = load_subs()
    subs.pop(str(call.message.chat.id), None)
    save_subs(subs)
    bot.answer_callback_query(call.id, "Все подписки удалены")
    bot.send_message(call.message.chat.id, "Готово, все подписки удалены.")

# =========================================================
# Фоновый мониторинг
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
# Health-check HTTP-сервер (нужен для бесплатного Render)
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
