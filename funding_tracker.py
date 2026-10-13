import os
import json
import requests
from datetime import datetime

# ================= 參數與設定 =================
CACHE_FILE = "squeeze_watchlist.json"
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

# 門檻設定
THRESHOLD_8H_EXTREME = -0.50   # 8H 週期極端負費率 (%)
REMOVE_THRESHOLD = -0.10       # 回升退池門檻 (%)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}
# ============================================

def send_telegram(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("未設定 TG Token，略過推播。")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"TG 發送失敗: {e}")

def get_binance_data():
    """取得幣安所有 USDT 永續合約資費與週期"""
    url = "https://fapi.binance.com/fapi/v1/premiumIndex"
    bn_map = {}
    try:
        res = requests.get(url, headers=HEADERS, timeout=10).json()
        for item in res:
            sym = item.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            rate = float(item.get("lastFundingRate") or 0.0) * 100
            interval = int(item.get("fundingIntervalHours") or 8)
            coin = sym[:-4] # 去除 USDT
            bn_map[coin] = {"rate": round(rate, 4), "interval": interval}
    except Exception as e:
        print(f"幣安 API 異常: {e}")
    return bn_map

def get_bitget_data():
    """取得 Bitget 所有 USDT 永續合約資費與週期"""
    url = "https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES"
    bg_map = {}
    try:
        res = requests.get(url, headers=HEADERS, timeout=10).json()
        for item in res.get("data", []):
            sym = item.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            rate_str = item.get("fundingRate")
            if not rate_str:
                continue
            rate = float(rate_str) * 100
            coin = sym[:-4]
            # Bitget 預設為 8h (少數動態 4h)
            bg_map[coin] = {"rate": round(rate, 4), "interval": 8}
    except Exception as e:
        print(f"Bitget API 異常: {e}")
    return bg_map

def get_okx_single(coin: str):
    """精準查詢特定幣種在 OKX 的即時資費與週期"""
    inst_id = f"{coin}-USDT-SWAP"
    url = f"https://www.okx.com/api/v5/public/funding-rate?instId={inst_id}"
    try:
        res = requests.get(url, headers=HEADERS, timeout=5).json()
        data = res.get("data", [])
        if data:
            rate = float(data[0].get("fundingRate") or 0.0) * 100
            # 計算結算週期
            funding_time = int(data[0].get("fundingTime", 0))
            next_time = int(data[0].get("nextFundingTime", 0))
            diff_hours = max(1, round((next_time - funding_time) / (1000 * 3600))) if next_time > funding_time else 8
            if diff_hours not in [1, 2, 4, 8]:
                diff_hours = 8
            return {"rate": round(rate, 4), "interval": diff_hours}
    except Exception:
        pass
    return None

def main():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{now_str}] 開始執行跨所軋空資金費率監控...")

    bn_data = get_binance_data()
    bg_data = get_bitget_data()

    # 讀取本地池子
    pool = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                pool = json.load(f)
        except Exception:
            pool = {}

    # 1. 檢驗入池候選標的 (幣安或 Bitget 觸發)
    all_coins = set(bn_data.keys()).union(set(bg_data.keys()))
    candidate_coins = set(pool.keys()) # 原本在池子裡的繼續比對

    for coin in all_coins:
        bn_info = bn_data.get(coin, {"rate": 0, "interval": 8})
        bg_info = bg_data.get(coin, {"rate": 0, "interval": 8})
        
        # 條件 1: 週期縮短 (< 8h)
        is_short_interval = (bn_info["interval"] < 8) or (bg_info["interval"] < 8)
        # 條件 2: 8h 負資費 <= -0.5%
        is_extreme_neg = (bn_info["rate"] <= THRESHOLD_8H_EXTREME) or (bg_info["rate"] <= THRESHOLD_8H_EXTREME)

        if is_short_interval or is_extreme_neg:
            candidate_coins.add(coin)

    # 2. 針對所有候選幣對齊 OKX 並更新池子狀態
    updated_pool = {}
    for coin in candidate_coins:
        bn_info = bn_data.get(coin, {"rate": 0.0, "interval": 8})
        bg_info = bg_data.get(coin, {"rate": 0.0, "interval": 8})
        okx_info = get_okx_single(coin) or {"rate": 0.0, "interval": 8}

        min_interval = min(bn_info["interval"], bg_info["interval"], okx_info["interval"])
        min_rate = min(bn_info["rate"], bg_info["rate"], okx_info["rate"])

        # 退池判定：週期恢復 8h 且費率回升 > -0.10%
        if min_interval >= 8 and min_rate > REMOVE_THRESHOLD:
            print(f"幣種 {coin} 已回升正常，自動移出池子。")
            continue

        updated_pool[coin] = {
            "min_interval": min_interval,
            "min_rate": min_rate,
            "BN": bn_info,
            "Bitget": bg_info,
            "OKX": okx_info
        }

    # 儲存池子狀態
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(updated_pool, f, ensure_ascii=False, indent=2)

    # 3. 整理發送 TG 推播
    if not updated_pool:
        print("當前無任何異常軋空標的，不發送通知。")
        return

    # 分類：縮短週期優先置頂，其次為 8H 極端負費率
    short_pool = []
    normal_pool = []
    for coin, data in updated_pool.items():
        if data["min_interval"] < 8:
            short_pool.append((coin, data))
        else:
            normal_pool.append((coin, data))

    # 排序：最負的排前面
    short_pool.sort(key=lambda x: x[1]["min_rate"])
    normal_pool.sort(key=lambda x: x[1]["min_rate"])

    msg = f"🔥 *【跨交易所合約軋空監控池】* (`{now_str}`)\n"
    msg += f"門檻：縮小週期全入池 | 8H資費 $\\le -0.5\\%$\n"
    msg += "─" * 28 + "\n"

    # 置頂區：縮小週期
    if short_pool:
        msg += "🚨 *【強制加速結算池（週期 < 8H）】*\n"
        for coin, d in short_pool:
            msg += f"• *{coin}USDT*\n"
            msg += f"  └ BN: `{d['BN']['interval']}h` ({d['BN']['rate']}%)\n"
            msg += f"  └ Bitget: `{d['Bitget']['interval']}h` ({d['Bitget']['rate']}%)\n"
            msg += f"  └ OKX: `{d['OKX']['interval']}h` ({d['OKX']['rate']}%)\n"
        msg += "─" * 28 + "\n"

    # 次級區：8H 極端負費率
    if normal_pool:
        msg += "📉 *【8H 極端負資費池（≤ -0.5%）】*\n"
        for coin, d in normal_pool:
            msg += f"• *{coin}USDT* (最深 `{d['min_rate']}%`)\n"
            msg += f"  └ BN: `{d['BN']['rate']}%` | BG: `{d['Bitget']['rate']}%` | OKX: `{d['OKX']['rate']}%`\n"

    msg += "\n💡 _退池條件：週期恢復8H且資費回升至 > -0.1%_"
    send_telegram(msg)
    print("推播已成功發送！")

if __name__ == "__main__":
    main()
