import requests
import json
import ccxt
import pandas as pd
import os
import time
import re
import csv
import xml.etree.ElementTree as ET
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator, MACD
from datetime import datetime, timedelta # ДОБАВЛЕНО ДЛЯ КУЛДАУНА

# ==========================================
# БЛОК 1: КОНФИГУРАЦИЯ И НАСТРОЙКИ
# ==========================================
STATE_FILE = "trading_state.json"
LOG_FILE = "trade_history.csv"

STOP_LOSS_PCT = 0.02
TAKE_PROFIT_PCT = 0.02 
TRAILING_ACTIVATION_PCT = 0.025 
TRAILING_CALLBACK_PCT = 0.99    
SPREAD_PCT = 0.0005 # НЮАНС 1: Реалистичный спред биржи (0.05%)
COOLDOWN_MINUTES = 60  # НЮАНС 2: Время отдыха после Стоп-лосса

MODE = "PAPER" 

BINANCE_API_KEY = "ТВОЙ_API_КЛЮЧ"
BINANCE_API_SECRET = "ТВОЙ_СИКРЕТ_КЛЮЧ"

TG_TOKEN = "ТВОЙ_TELEGRAM_ТОКЕН"
TG_CHAT_ID = "ТВОЙ_TELEGRAM_ТОКЕН"

SYMBOL = "SOL/USDT"
SYMBOL_BASE = "SOL" 
TIMEFRAME_SLOW = "15m"
TIMEFRAME_FAST = "5m"
SLEEP_OUT = 300      
SLEEP_IN = 60        

if not os.path.exists(STATE_FILE):
    initial_state = {
        "balance_usdt": 10000.0,
        "asset_balance": 0.0, 
        "last_buy_price": 0.0,
        "position_open": False,
        "entry_time": None,
        "peak_price": 0.0,
        "cooldown_until": None # ДОБАВЛЕНО ДЛЯ КУЛДАУНА
    }
    with open(STATE_FILE, "w") as f:
        json.dump(initial_state, f, indent=4)

data_provider = ccxt.binance({'enableRateLimit': True})

if MODE == "TESTNET":
    executor = ccxt.binance({
        'apiKey': BINANCE_API_KEY,
        'secret': BINANCE_API_SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'},
        'sandbox': True
    })
else:
    executor = None

# ==========================================
# БЛОК 2: ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================
def send_tg(message):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=5)
    except Exception: pass

def load_state():
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)

def log_trade_to_csv(entry_time, exit_price, reason):
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["entry_time", "exit_time", "exit_price", "reason"])
        writer.writerow([entry_time, datetime.now().isoformat(), exit_price, reason])

def fetch_crypto_news(asset_name):
    try:
        url = "https://cryptopanic.com/news/rss/"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        root = ET.fromstring(response.content)
        asset_news = []
        for item in root.findall('.//item')[:15]:
            title_elem = item.find('title')
            if title_elem is not None and title_elem.text:
                if asset_name.lower() in title_elem.text.lower():
                    asset_news.append(f"- {title_elem.text.strip()}")
        if asset_news:
            return f"Специфичные новости по {asset_name}:\n" + "\n".join(asset_news[:3])
        else:
            return f"Важных новостей по {asset_name} нет."
    except Exception:
        return "Ошибка загрузки новостей."

def get_market_data(symbol, timeframe):
    bars = data_provider.fetch_ohlcv(symbol, timeframe=timeframe, limit=50)
    df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    
    df['RSI'] = RSIIndicator(close=df['close'], window=14).rsi()
    df['SMA_20'] = SMAIndicator(close=df['close'], window=20).sma_indicator()
    
    macd_obj = MACD(close=df['close'], window_slow=26, window_fast=12, window_sign=9)
    df['MACD'] = macd_obj.macd()
    df['MACD_HIST'] = macd_obj.macd_diff()
    
    df['VOL_SMA'] = df['volume'].rolling(window=20).mean()
    df.dropna(inplace=True)
    last_row = df.iloc[-1]
    vol_ratio = last_row['volume'] / last_row['VOL_SMA'] if last_row['VOL_SMA'] > 0 else 1.0
    
    macd_trend = "BULLISH" if last_row['MACD_HIST'] > 0 else "BEARISH"
    
    return {
        "price": float(last_row['close']),
        "rsi": round(float(last_row['RSI']), 2),
        "sma": round(float(last_row['SMA_20']), 2),
        "trend": "UPTREND" if float(last_row['close']) > float(last_row['SMA_20']) else "DOWNTREND",
        "vol_ratio": round(float(vol_ratio), 2),
        "macd_hist": round(float(last_row['MACD_HIST']), 4),
        "macd_trend": macd_trend
    }

# ==========================================
# БЛОК 3: ГЛАВНЫЙ ЦИКЛ ТРЕЙДИНГА
# ==========================================
print(f"=== AI Trader [{SYMBOL}] | Режим: {MODE} ===")
send_tg(f"🟡 <b>AI Trader v2.1 Запущен</b>\nРежим: {MODE}\nСпред: {SPREAD_PCT*100}%\nКулдаун: {COOLDOWN_MINUTES} мин")

while True:
    try:
        state = load_state()

        # ==========================================
        # БЛОК 3.1: УПРАВЛЕНИЕ ОТКРЫТОЙ ПОЗИЦИЕЙ
        # ==========================================
        if state['position_open']:
            ticker = data_provider.fetch_ticker(SYMBOL)
            current_price = ticker['last']
            buy_price = state['last_buy_price']
            price_change = (current_price - buy_price) / buy_price
            
            current_peak = state.get("peak_price", buy_price)
            if current_price > current_peak:
                state["peak_price"] = current_price
                save_state(state)
                current_peak = current_price
            
            if price_change <= -STOP_LOSS_PCT:
                print(f"[!] СТОП-ЛОСС (-{STOP_LOSS_PCT*100}%). Продаем!")
                if MODE == "TESTNET" and executor:
                    executor.create_market_sell_order(SYMBOL, state['asset_balance'])
                state['balance_usdt'] = state['asset_balance'] * current_price
                state['asset_balance'] = 0.0
                state['position_open'] = False
                state['peak_price'] = 0.0
                # НЮАНС 2: Включаем кулдаун на 1 час после убытка
                state['cooldown_until'] = (datetime.now() + timedelta(minutes=COOLDOWN_MINUTES)).isoformat()
                log_trade_to_csv(state.get("entry_time"), current_price, "STOP_LOSS")
                send_tg(f"🛑 <b>СТОП-ЛОСС</b> {SYMBOL}\nУбыток: -{STOP_LOSS_PCT*100}%\n⏳ Кулдаун {COOLDOWN_MINUTES} мин.")
                save_state(state)
                continue
                
            elif price_change >= TAKE_PROFIT_PCT:
                print(f"[+] ТЕЙК-ПРОФИТ (+{TAKE_PROFIT_PCT*100}%). Фиксируем!")
                if MODE == "TESTNET" and executor:
                    executor.create_market_sell_order(SYMBOL, state['asset_balance'])
                state['balance_usdt'] = state['asset_balance'] * current_price
                state['asset_balance'] = 0.0
                state['position_open'] = False
                state['peak_price'] = 0.0
                state['cooldown_until'] = None # При плюсе кулдаун не нужен
                log_trade_to_csv(state.get("entry_time"), current_price, "TAKE_PROFIT")
                send_tg(f"💰 <b>ТЕЙК-ПРОФИТ</b> {SYMBOL}\nПрибыль: +{TAKE_PROFIT_PCT*100}%")
                save_state(state)
                continue

            peak_change = (current_peak - buy_price) / buy_price
            if peak_change >= 0.04:
                dynamic_callback = 0.995 
            elif peak_change >= TRAILING_ACTIVATION_PCT:
                dynamic_callback = TRAILING_CALLBACK_PCT
            else:
                dynamic_callback = 0

            if dynamic_callback > 0 and current_price <= current_peak * dynamic_callback:
                trailing_profit_pct = (current_price - buy_price) / buy_price * 100
                print(f"[T] TRAILING STOP. Прибыль +{trailing_profit_pct:.2f}%")
                if MODE == "TESTNET" and executor:
                    executor.create_market_sell_order(SYMBOL, state['asset_balance'])
                state['balance_usdt'] = state['asset_balance'] * current_price
                state['asset_balance'] = 0.0
                state['position_open'] = False
                state['peak_price'] = 0.0
                state['cooldown_until'] = None # При плюсе кулдаун не нужен
                log_trade_to_csv(state.get("entry_time"), current_price, f"TRAILING_STOP (+{trailing_profit_pct:.2f}%)")
                send_tg(f"📊 <b>TRAILING STOP</b> {SYMBOL}\nЗабрали: +{trailing_profit_pct:.2f}%")
                save_state(state)
                continue

            print(f"[IN POSITION] {SYMBOL}: {current_price} | Изменение: {price_change*100:.2f}% | Пик: {current_peak}")
            time.sleep(SLEEP_IN)
            continue

        # ==========================================
        # БЛОК 3.2: СБОР ДАННЫХ И СВОБОДНЫЙ АНАЛИЗ ИИ
        # ==========================================
        
        # НЮАНС 2: Проверка кулдауна
        cooldown_str = state.get("cooldown_until")
        if cooldown_str:
            cooldown_time = datetime.fromisoformat(cooldown_str)
            if datetime.now() < cooldown_time:
                remaining = (cooldown_time - datetime.now()).seconds // 60
                print(f"[КУЛДАУН] После убытка осталось {remaining} мин. Спим.")
                time.sleep(SLEEP_OUT)
                continue

        print(f"\n--- Поиск входа {SYMBOL} ---")
        
        print("Проверка тренда BTC/USDT...")
        btc_data = get_market_data('BTC/USDT', TIMEFRAME_SLOW)
        
        if btc_data['trend'] == "DOWNTREND" or btc_data['rsi'] > 75:
            print(f"[ФИЛЬТР BTC] Рынок опасен. Спим.")
            time.sleep(SLEEP_OUT)
            continue

        data_slow = get_market_data(SYMBOL, TIMEFRAME_SLOW)
        data_fast = get_market_data(SYMBOL, TIMEFRAME_FAST)
        current_price = data_fast['price']
        
        if data_fast['vol_ratio'] < 1.0:
            print(f"[ФИЛЬТР ОБЪЕМА] Объем низкий ({data_fast['vol_ratio']}x). Спим.")
            time.sleep(SLEEP_OUT)
            continue

        news = fetch_crypto_news(SYMBOL_BASE)

        market_context = f"""
        Актив: {SYMBOL}
        15 минут: Тренд {data_slow['trend']} | RSI {data_slow['rsi']} | MACD {data_slow['macd_trend']} (Гистограмма: {data_slow['macd_hist']})
        5 минут: Тренд {data_fast['trend']} | RSI {data_fast['rsi']} | MACD {data_fast['macd_trend']} (Гистограмма: {data_fast['macd_hist']})
        Объем 5m: {data_fast['vol_ratio']}x от среднего.
        Новости: {news}
        """

        print("Анализ нейросети (MACD+RSI Confluence)...")
        url = "http://localhost:11434/api/generate"
        
        prompt_text = f"""Ты профессиональный трейдер. Ищи точку входа на основе конвергенции индикаторов.
        
ПРАВИЛА ДЛЯ BUY:
1. Оба тренда (15м и 5м) должны быть UPTREND.
2. MACD-гистограмма на 5 минутах должна быть BULLISH (выше 0) или резко расти.
3. RSI на 5 минутах не должен быть выше 75.
4. Объем выше 1.0x.

ПРАВИЛО ДЛЯ HOLD:
Если хоть одно условие не сходится -> HOLD.

Формат строго JSON: {{"decision": "HOLD", "reason": "краткий анализ конвергенции"}}
Данные:
{market_context}"""

        payload = {
            "model": "gemma2:9b",
            "prompt": prompt_text,
            "stream": False,
            "format": "json"
        }

        try:
            response = requests.post(url, json=payload, timeout=180)
            result = response.json()
            if 'error' in result:
                print(f"[ОШИБКА МОДЕЛИ] {result['error']}")
                time.sleep(60)
                continue
            raw_ai_text = result.get('response', "")
        except requests.exceptions.Timeout:
            print("[ОШИБКА] Таймаут Ollama.")
            time.sleep(60)
            continue
        except Exception as e:
            print(f"[ОШИБКА СВЯЗИ] {e}")
            time.sleep(60)
            continue
        
        json_match = re.search(r'\{\s*"decision"\s*:\s*"(BUY|SELL|HOLD)"\s*,\s*"reason"\s*:\s*".*?"\s*\}', raw_ai_text, re.DOTALL)
        if json_match:
            ai_response = json.loads(json_match.group(0))
        else:
            ai_response = {"decision": "HOLD", "reason": "Parse error"}
        
        decision = ai_response.get("decision")
        reason = ai_response.get("reason")
        print(f"[ИИ]: {decision} | {reason}\n")
        
        # ==========================================
        # БЛОК 3.3: ИСПОЛНЕНИЕ ОРДЕРА
        # ==========================================
        if decision == "BUY":
            # НЮАНС 1: Рассчитываем входную цену со спредом (хуже чем на графике)
            real_entry_price = current_price * (1 + SPREAD_PCT)
            
            amount_to_buy = state['balance_usdt'] / real_entry_price
            if MODE == "TESTNET" and executor:
                market_data = executor.market(SYMBOL)
                amount_to_buy = float(executor.amount_to_precision(SYMBOL, amount_to_buy))
                executor.create_market_buy_order(SYMBOL, amount_to_buy)
                
            state['asset_balance'] = amount_to_buy
            state['last_buy_price'] = real_entry_price # Сохраняем реальную цену со спредом
            state['peak_price'] = real_entry_price
            state['balance_usdt'] = 0.0
            state['position_open'] = True
            state['entry_time'] = datetime.now().isoformat()
            print(f"[>>] BUY {SYMBOL} по {real_entry_price} (График: {current_price}, Спред: +{SPREAD_PCT*100}%)")
            send_tg(f"🟢 <b>МАКРО-ВХОД</b> {SYMBOL}\nГрафик: {current_price}\nРеал. вход: {real_entry_price}\nПричина: {reason}")
        else:
            print("[--] HOLD")
            
        save_state(state)

    except ccxt.NetworkError as e:
        print(f"[СЕТЬ] {e}")
    except ccxt.ExchangeError as e:
        print(f"[БИРЖА] {e}")
    except Exception as e:
        print(f"[КРИТИЧЕСКАЯ ОШИБКА] {e}")
        
    time.sleep(SLEEP_OUT)