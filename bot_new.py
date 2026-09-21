import os
import json
import time
import threading
import requests
import telebot
from telebot import types
from http.server import HTTPServer, BaseHTTPRequestHandler

# --- Настройки ---
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHECK_INTERVAL = 60              # интервал проверки талонов (сек)
DATA_FILE = 'subscriptions.json' # где хранятся подписки

BASE_URL = "https://gorzdrav.spb.ru/_api/api/v2"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

bot = telebot.TeleBot(TELEGRAM_TOKEN)
user_state = {}  # временное состояние выбора (chat_id -> dict)


# --- Хранилище подписок ---
def load_subs():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_subs(subs):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(subs, f, ensure_ascii=False, indent=2)


# --- Запросы к API gorzdrav ---
def api_get(path):
    try:
        r = requests.get(f"{BASE_URL}{path}", headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get('success'):
            return data.get('result', data.get('data'))
        return None
    except Exception as e:
        print(f"API error {path}: {e}")
        return None

def get_districts():
    return api_get("/shared/districts") or []

def get_lpus(district_id):
    return api_get(f"/shared/district/{district_id}/lpus") or []

def get_specialties(lpu_id):
    return api_get(f"/schedule/lpu/{lpu_id}/specialties") or []

def get_doctors(lpu_id, spec_id):
    return api_get(f"/schedule/lpu/{lpu_id}/speciality/{spec_id}/doctors") or []

def get_appointments(lpu_id, spec_id, doctor_id):
    return api_get(f"/schedule/lpu/{lpu_id}/speciality/{spec_id}/doctors/{doctor_id}/appointments")


# --- Команды бота ---
@bot.message_handler(commands=['start'])
def cmd_start(message):
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
        reply_markup=markup
    )

@bot.message_handler(commands=['help'])
def cmd_help(message):
    bot.send_message(message.chat.id,
        "Как пользоваться:\n"
        "1. /start → «Добавить подписку»\n"
        "2. Выберите район → поликлинику → специальность\n"
        "3. Я начну проверять талоны каждую минуту и пришлю уведомление, если что-то появится.\n\n"
        "/start — меню\n"
        "/list — мои подписки\n"
        "/stop — удалить все подписки"
    )

@bot.message_handler(commands=['list'])
def cmd_list(message):
    show_subs(message.chat.id)

@bot.message_handler(commands=['stop'])
def cmd_stop(message):
    subs = load_subs()
    subs.pop(str(message.chat.id), None)
    save_subs(subs)
    bot.send_message(message.chat.id, "Все ваши подписки удалены.")


# --- Показ подписок ---
def show_subs(chat_id):
    subs = load_subs().get(str(chat_id), [])
    if not subs:
        bot.send_message(chat_id, "У вас пока нет подписок.")
        return
    text = "Ваши подписки:\n"
    markup = types.InlineKeyboardMarkup(row_width=1)
    for i, s in enumerate(subs):
        text += f"\n{i+1}. {s['lpu_name']} — {s['spec_name']}"
        markup.add(types.InlineKeyboardButton(
            f"❌ Удалить: {s['spec_name']}", callback_data=f"del:{i}"
        ))
    bot.send_message(chat_id, text, reply_markup=markup)


# --- Инлайн-меню: выбор района ---
@bot.callback_query_handler(func=lambda c: c.data == "new")
def on_new(call):
    districts = get_districts()
    if not districts:
        bot.answer_callback_query(call.id, "Не удалось загрузить районы")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for d in districts:
        markup.add(types.InlineKeyboardButton(d['name'], callback_data=f"d:{d['id']}"))
    user_state[call.message.chat.id] = {}
    bot.edit_message_text("Выберите район:", call.message.chat.id,
                          call.message.message_id, reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("d:"))
def on_district(call):
    district_id = call.data.split(":", 1)[1]
    lpus = get_lpus(district_id)
    if not lpus:
        bot.answer_callback_query(call.id, "Нет поликлиник")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for l in lpus:
        name = l.get('shortName') or l.get('name') or f"ЛПУ {l['id']}"
        markup.add(types.InlineKeyboardButton(name, callback_data=f"l:{l['id']}"))
    user_state.setdefault(call.message.chat.id, {})['district'] = district_id
    bot.edit_message_text("Выберите поликлинику:", call.message.chat.id,
                          call.message.message_id, reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("l:"))
def on_lpu(call):
    lpu_id = call.data.split(":", 1)[1]
    specs = get_specialties(lpu_id)
    if not specs:
        bot.answer_callback_query(call.id, "Нет специальностей")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for s in specs:
        markup.add(types.InlineKeyboardButton(s['name'], callback_data=f"s:{s['id']}|{lpu_id}"))
    user_state.setdefault(call.message.chat.id, {})['lpu_id'] = lpu_id
    bot.edit_message_text("Выберите специальность:", call.message.chat.id,
                          call.message.message_id, reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("s:"))
def on_spec(call):
    spec_id, lpu_id = call.data[2:].split("|", 1)
    chat_id = call.message.chat.id

    # Получаем имена для читаемого вида
    lpu_name = "Поликлиника"
    for l in get_lpus(user_state.get(chat_id, {}).get('district', '')):
        if str(l['id']) == str(lpu_id):
            lpu_name = l.get('shortName') or l.get('name') or lpu_name
            break
    spec_name = "Специальность"
    for s in get_specialties(lpu_id):
        if str(s['id']) == str(spec_id):
            spec_name = s['name']
            break

    subs = load_subs()
    user_subs = subs.get(str(chat_id), [])
    # Защита от дублей
    if any(x['lpu_id'] == lpu_id and x['spec_id'] == spec_id for x in user_subs):
        bot.answer_callback_query(call.id, "Такая подписка уже есть")
        return

    user_subs.append({
        'lpu_id': lpu_id, 'spec_id': spec_id,
        'lpu_name': lpu_name, 'spec_name': spec_name,
        'seen': []  # уже показанные талоны
    })
    subs[str(chat_id)] = user_subs
    save_subs(subs)

    bot.edit_message_text(
        f"✅ Подписка добавлена:\n{lpu_name}\n{spec_name}\n\n"
        f"Буду проверять каждые {CHECK_INTERVAL} сек и пришлю уведомление при появлении талонов.",
        chat_id, call.message.message_id
    )
    bot.answer_callback_query(call.id, "Готово!")


# --- Удаление подписки ---
@bot.callback_query_handler(func=lambda c: c.data.startswith("del:"))
def on_del(call):
    idx = int(call.data.split(":", 1)[1])
    subs = load_subs()
    chat_id = str(call.message.chat.id)
    if chat_id in subs and 0 <= idx < len(subs[chat_id]):
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


# --- Фоновый мониторинг ---
def monitor_loop():
    print("Мониторинг запущен")
    while True:
        subs = load_subs()
        for chat_id, user_subs in list(subs.items()):
            for sub in user_subs:
                try:
                    doctors = get_doctors(sub['lpu_id'], sub['spec_id'])
                    for doc in doctors:
                        appts = get_appointments(sub['lpu_id'], sub['spec_id'], doc['id'])
                        if not appts:
                            continue
                        for a in appts:
                            key = f"{doc['id']}_{a.get('visitDate','')}_{a.get('visitTime','')}"
                            if key in sub.get('seen', []):
                                continue
                            text = (f"🎉 Найден талон!\n"
                                    f"🏥 {sub['lpu_name']}\n"
                                    f"👨‍⚕️ {doc.get('name','Врач')}\n"
                                    f"📅 {a.get('visitDate','?')} в {a.get('visitTime','?')}")
                            try:
                                bot.send_message(int(chat_id), text)
                            except Exception as e:
                                print(f"Send error: {e}")
                            sub.setdefault('seen', []).append(key)
                    # Ограничиваем историю seen
                    sub['seen'] = sub.get('seen', [])[-500:]
                except Exception as e:
                    print(f"Monitor error for {chat_id}: {e}")
        save_subs(subs)
        time.sleep(CHECK_INTERVAL)


# --- HTTP health check (нужен для Render free) ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')
    def log_message(self, *args):
        pass

def run_health_server():
    port = int(os.environ.get('PORT', 10000))
    HTTPServer(('0.0.0.0', port), HealthHandler).serve_forever()


# --- Запуск ---
if __name__ == "__main__":
    if not TELEGRAM_TOKEN:
        raise SystemExit("Не задан TELEGRAM_TOKEN в переменных окружения!")

    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    print("Бот запущен")
    bot.infinity_polling(timeout=30, long_polling_timeout=25)