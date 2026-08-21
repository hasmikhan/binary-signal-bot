import requests
import time
import os
from datetime import datetime, timedelta

# ---------- CONFIG ----------
# Railway/cloud এ deploy করলে এগুলো Environment Variable হিসেবে সেট করবেন।
# লোকাল কম্পিউটারে টেস্ট করার জন্য fallback ভ্যালু হিসেবে আগেরগুলো রাখা আছে।
TWELVE_API_KEY = os.environ.get("TWELVE_API_KEY", "06877f1eef144221a5a997eeda323c5e")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8744717305:AAFNvMHVBLXTHzH3wYdNIrcKJRkU_vM_FLs")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "6157699826")

SYMBOLS = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD"]
INTERVAL = "5min"
HIGHER_INTERVAL = "15min"
EXPIRY_MINUTES = 5
ADVANCE_NOTICE_MINUTES = 5
CHECK_GAP = 30

ADX_PERIOD = 14
ADX_THRESHOLD = 12   # আগে ছিল 20, খুব কম signal আসছিল তাই কমিয়ে দেওয়া হলো

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
# -----------------------------


def get_price_data(symbol, interval=INTERVAL):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": 30,
        "apikey": TWELVE_API_KEY
    }
    response = requests.get(url, params=params)
    data = response.json()
    return data.get("values", [])


def calculate_rsi(closes, period=14):
    prices = closes[::-1]
    gains = []
    losses = []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))
    if len(gains) < period:
        return None
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_adx(candles, period=ADX_PERIOD):
    """
    Wilder's ADX (Average Directional Index).
    candles আসে TwelveData থেকে newest-first অর্ডারে, তাই আগে reverse করে
    oldest -> newest বানানো হয়েছে যাতে হিসাব ঠিকমতো হয়।
    """
    data = candles[::-1]
    if len(data) < period + 1:
        return None

    highs = [float(c["high"]) for c in data]
    lows = [float(c["low"]) for c in data]
    closes = [float(c["close"]) for c in data]

    plus_dm = []
    minus_dm = []
    tr_list = []

    for i in range(1, len(data)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]

        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0)

        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        tr_list.append(tr)

    if len(tr_list) < period:
        return None

    # প্রথম smoothed value = period টা মানের যোগফল
    smoothed_tr = sum(tr_list[:period])
    smoothed_plus_dm = sum(plus_dm[:period])
    smoothed_minus_dm = sum(minus_dm[:period])

    dx_list = []

    def compute_dx(str_, spdm, smdm):
        if str_ == 0:
            return 0
        plus_di = 100 * (spdm / str_)
        minus_di = 100 * (smdm / str_)
        di_sum = plus_di + minus_di
        if di_sum == 0:
            return 0
        return 100 * (abs(plus_di - minus_di) / di_sum)

    dx_list.append(compute_dx(smoothed_tr, smoothed_plus_dm, smoothed_minus_dm))

    # Wilder's smoothing বাকি ডেটার জন্য
    for i in range(period, len(tr_list)):
        smoothed_tr = smoothed_tr - (smoothed_tr / period) + tr_list[i]
        smoothed_plus_dm = smoothed_plus_dm - (smoothed_plus_dm / period) + plus_dm[i]
        smoothed_minus_dm = smoothed_minus_dm - (smoothed_minus_dm / period) + minus_dm[i]
        dx_list.append(compute_dx(smoothed_tr, smoothed_plus_dm, smoothed_minus_dm))

    if len(dx_list) < period:
        return None

    adx = sum(dx_list[-period:]) / period
    return adx


def calculate_ema_series(values, period):
    """values ওল্ডেস্ট -> নিউয়েস্ট অর্ডারে থাকতে হবে।"""
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    ema_values = [sum(values[:period]) / period]  # প্রথম EMA = SMA
    for price in values[period:]:
        ema_values.append(price * k + ema_values[-1] * (1 - k))
    return ema_values


def calculate_macd(candles):
    """
    MACD line = EMA(fast) - EMA(slow)
    Signal line = EMA(MACD line, signal period)
    Histogram = MACD line - Signal line
    candles newest-first আসে, তাই আগে reverse করে oldest->newest বানানো হয়েছে।
    Return করে (histogram, is_bullish) - বর্তমান আর আগের histogram compare করে
    momentum বাড়ছে না কমছে সেটাও বোঝা যায়।
    """
    data = candles[::-1]
    closes = [float(c["close"]) for c in data]

    if len(closes) < MACD_SLOW + MACD_SIGNAL:
        return None, None

    ema_fast = calculate_ema_series(closes, MACD_FAST)
    ema_slow = calculate_ema_series(closes, MACD_SLOW)

    # দুটো EMA সিরিজের দৈর্ঘ্য আলাদা হবে, তাই শেষের দিক থেকে align করা হচ্ছে
    diff = min(len(ema_fast), len(ema_slow))
    macd_line = [ema_fast[-diff + i] - ema_slow[-diff + i] for i in range(diff)]

    if len(macd_line) < MACD_SIGNAL:
        return None, None

    signal_line = calculate_ema_series(macd_line, MACD_SIGNAL)
    if not signal_line:
        return None, None

    histogram = macd_line[-1] - signal_line[-1]
    is_bullish = histogram > 0
    return histogram, is_bullish


def get_trend(candles):
    closes = [float(c["close"]) for c in candles]
    if len(closes) < 8:
        return None
    short_ma = sum(closes[:3]) / 3
    long_ma = sum(closes[:8]) / 8
    if short_ma > long_ma:
        return "BUY"
    elif short_ma < long_ma:
        return "SELL"
    return None


def calculate_signal(candles_main, candles_higher):
    closes = [float(c["close"]) for c in candles_main]
    rsi = calculate_rsi(closes)
    adx = calculate_adx(candles_main)
    macd_hist, macd_bullish = calculate_macd(candles_main)

    if rsi is None:
        return "HOLD", None, adx, macd_hist

    # ADX filter: market যথেষ্ট trending না হলে signal দেওয়া হবে না
    if adx is None or adx < ADX_THRESHOLD:
        return "HOLD", rsi, adx, macd_hist

    # MACD filter: momentum data না থাকলে signal স্কিপ
    if macd_bullish is None:
        return "HOLD", rsi, adx, macd_hist

    signal_main = get_trend(candles_main)
    signal_higher = get_trend(candles_higher)

    if signal_main is None or signal_higher is None or signal_main != signal_higher:
        return "HOLD", rsi, adx, macd_hist

    # আগে MACD না মিললে সরাসরি HOLD করে দিত, এখন soft filter -
    # শুধু log-এ দেখা যাবে MACD agree করছে কিনা, কিন্তু signal আটকাবে না
    final = signal_main
    if final == "BUY" and rsi < 70:
        return "BUY", rsi, adx, macd_hist
    elif final == "SELL" and rsi > 30:
        return "SELL", rsi, adx, macd_hist
    else:
        return "HOLD", rsi, adx, macd_hist


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    r = requests.post(url, data=payload)
    return r.json()


def find_signal():
    for symbol in SYMBOLS:
        try:
            candles_main = get_price_data(symbol, INTERVAL)
            candles_higher = get_price_data(symbol, HIGHER_INTERVAL)

            if len(candles_main) < 20 or len(candles_higher) < 8:
                continue

            signal, rsi, adx, macd_hist = calculate_signal(candles_main, candles_higher)
            price = candles_main[0]["close"]
            rsi_display = f"{rsi:.1f}" if rsi is not None else "N/A"
            adx_display = f"{adx:.1f}" if adx is not None else "N/A"
            macd_display = f"{macd_hist:.5f}" if macd_hist is not None else "N/A"
            print(f"{symbol} | Signal: {signal} | Price: {price} | RSI: {rsi_display} | ADX: {adx_display} | MACD: {macd_display}")

            if signal != "HOLD":
                return symbol, signal

        except Exception as e:
            print(f"Error with {symbol}: {e}")

    return None, None


def run_bot():
    print("Bot starting...\n")

    while True:
        symbol, action = None, None
        while symbol is None:
            symbol, action = find_signal()
            if symbol is None:
                time.sleep(CHECK_GAP)

        display_action = "CALL" if action == "BUY" else "PUT"
        symbol_clean = symbol.replace("/", "")

        entry_time_dt = datetime.now() + timedelta(minutes=ADVANCE_NOTICE_MINUTES)
        entry_time_str = entry_time_dt.strftime("%I:%M %p").upper()

        notice_msg = (f"📊 {symbol_clean} SIGNAL\n\n"
                       f"DIRECTION: {display_action}\n"
                       f"ENTRY TIME: {entry_time_str}\n"
                       f"EXPIRY: {EXPIRY_MINUTES} MIN")
        send_telegram_message(notice_msg)
        print(f"Signal sent for {symbol} ({display_action}). Entry at {entry_time_str}...")

        time.sleep(ADVANCE_NOTICE_MINUTES * 60)
        candles = get_price_data(symbol)
        entry_price = float(candles[0]["close"])

        time.sleep(EXPIRY_MINUTES * 60)
        candles = get_price_data(symbol)
        current_price = float(candles[0]["close"])

        if action == "BUY":
            result = "WIN ✅" if current_price > entry_price else "LOSS ❌"
        else:
            result = "WIN ✅" if current_price < entry_price else "LOSS ❌"

        result_msg = f"📈 RESULT: {symbol_clean}\n\nOUTCOME: {result}"
        send_telegram_message(result_msg)
        print(f"Result: {result}\n")

        time.sleep(5)


if __name__ == "__main__":
    run_bot()
