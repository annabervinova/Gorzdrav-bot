TELEGRAM_TOKEN = '8719806802:AAGGORy8tUc-rfHlL62U1bv0uTk-sdCsvmA'   
CHAT_ID = '935753186'                  LPU_ID = 1                                   SPECIALTY_ID = 123

import requests
import time
import telebot

# --- НАСТРОЙКИ ---
TELEGRAM_TOKEN = 'ВАШ_ТОКЕН_ОТ_BOTFATHER'
CHAT_ID = 'ВАШ_TELEGRAM_ID'
LPU_ID = 1          # ID вашей поликлиники
SPECIALTY_ID = 123  # ID специальности (найдите через Network)
CHECK_INTERVAL = 60 # Интервал проверки в секундах
# -----------------

BASE_URL = "https://gorzdrav.spb.ru/_api/api/v2"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
}

bot = telebot.TeleBot(8719806802:AAGGORy8tUc-rfHlL62U1bv0uTk-sdCsvmA)

def get_doctors(lpu_id, specialty_id):
    """Получает список врачей для специальности."""
    url = f"{BASE_URL}/schedule/lpu/{lpu_id}/speciality/{specialty_id}/doctors"
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"Ошибка при получении врачей: {e}")
        return []

def get_free_slots(lpu_id, specialty_id, doctor_id):
    """Получает свободные слоты для врача."""
    # Внимание: точный эндпоинт может отличаться, проверьте его в Network!
    url = f"{BASE_URL}/schedule/lpu/{lpu_id}/speciality/{specialty_id}/doctors/{doctor_id}/appointments"
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"Ошибка при получении слотов: {e}")
        return None

def main():
    print("Бот запущен. Начинаю мониторинг...")
    while True:
        doctors = get_doctors(LPU_ID, SPECIALTY_ID)
        if not doctors:
            time.sleep(CHECK_INTERVAL)
            continue

        for doctor in doctors:
            doctor_id = doctor.get('id')
            doctor_name = doctor.get('name', 'Неизвестный врач')
            
            slots_data = get_free_slots(LPU_ID, SPECIALTY_ID, doctor_id)
            
            if slots_data and slots_data.get('success') and slots_data.get('result'):
                free_slots = slots_data['result']
                if free_slots:
                    message = f"🎉 Найдены талоны!\n👨‍⚕️ Врач: {doctor_name}\nКоличество: {len(free_slots)}"
                    try:
                        bot.send_message(CHAT_ID, message)
                        print(message)
                    except Exception as e:
                        print(f"Ошибка отправки: {e}")
                else:
                    print(f"У {doctor_name} нет талонов.")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()