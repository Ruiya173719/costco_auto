"""
LINE 推播模組
=================================
輸入：normalize_finalize.run_finalize_core() 回傳的 all_db_rows
輸出：呼叫 LINE Messaging API 的 Broadcast 端點，推播給所有好友
"""
import os
import math
import requests
from datetime import datetime
from dotenv import load_dotenv
import pytz

load_dotenv()

LINE_BROADCAST_URL = "https://api.line.me/v2/bot/message/broadcast"

# ---- LINE免費版 官方規格上限（2026 年查證版本）----
MAX_BUBBLES_PER_CAROUSEL = 12   # 1 個 Carousel 最多 12 張 bubble
MAX_MESSAGES_PER_CALL = 5       # 1 次 API 呼叫最多 5 個訊息物件
MAX_LABEL_LEN = 20              # 按鈕字元上限
MAX_ALT_TEXT_LEN = 400          # 備用替代文字字元 上限
# ---- 這個系統的分卡規則 ----
HIGH_CONFIDENCE_ROWS_PER_CARD = 7    # 每張高信心表格卡放幾列
MAX_HIGH_CONFIDENCE_HIGHLIGHTS = 21  # 高信心商品最多精選幾筆上 LINE（其餘去網頁看）
TAIPEI_TZ = pytz.timezone("Asia/Taipei")

BRAND_RED = "#E31937"

# 字數保護器
def truncate(text, max_len, suffix="…"):
    if not text:
        return text
    text = str(text)
    if len(text) <= max_len:
        return text
    return text[: max_len - len(suffix)] + suffix

# ---------------------------------------------------------
# 資料處理：挑選+排序高信心精選、整理低信心清單
# ---------------------------------------------------------
#高信心精選邏輯篩選器
def select_top_high_confidence(all_db_rows, limit=MAX_HIGH_CONFIDENCE_HIGHLIGHTS):
    high_rows = [r for r in all_db_rows if r.get('confidence_tier') == '高信心_顯示特價']
    #計算折扣率
    def discount_ratio(r):
        original = r.get('original_price')
        sale = r.get('sale_price_db') if 'sale_price_db' in r else r.get('sale_price')
        if not original or not sale:
            return -1  # 缺資料的排到最後面
        try:
            return (original - sale) / original
        except (TypeError, ZeroDivisionError):
            return -1
    #由大到小排序，取前 21 筆
    high_rows.sort(key=discount_ratio, reverse=True)
    return high_rows[:limit]

#低信心/失敗商品篩選器
def get_low_confidence_rows(all_db_rows):
    return [r for r in all_db_rows
            if r.get('confidence_tier') in ('低信心_退回網址', 'OCR辨識失敗_退回網址')]


# ---------------------------------------------------------
# Bubble 組裝
# ---------------------------------------------------------
#第1張卡片:總覽卡（含免責聲明+查看全部按鈕)
def build_overview_bubble(run_date_str, webpage_url):
    return {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {"type": "text", "text": "COSTCO好市多", "weight": "bold", "size": "sm", "color": BRAND_RED},
                {"type": "text", "text": f"{run_date_str} 最新優惠消息", "weight": "bold", "size": "lg", "wrap": True},
                {"type": "separator", "margin": "md"},
                {"type": "text", "text": "本卡片精選本次高信心特價商品，完整清單（含所有已辨識商品與待人工確認項目）請點擊下方按鈕查看。",
                 "size": "xs", "color": "#666666", "wrap": True, "margin": "md"},
                {"type": "text",
                 "text": "⚠️ 免責聲明與來源\n本訊息資料自動抓取自今買網並經 AI 系統辨識，僅供參考。實際商品價格、折扣與活動期間請以好市多（Costco）賣場現場公告為準。若價格資訊有誤或僅顯示圖片網址，請點擊查看原圖確認。",
                 "size": "xxs", "color": "#8c8c8c", "wrap": True, "margin": "md"},
                {"type": "text", "text": "來源：https://www.daybuy.tw/",
                 "size": "xxs", "color": "#8c8c8c", "wrap": True, "margin": "sm"},
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": BRAND_RED,
                    "action": {"type": "uri", "label": truncate("查看全部商品項", MAX_LABEL_LEN), "uri": webpage_url},
                }
            ],
        },
    }
#第2-4張卡片:高信心精選表格卡
def build_high_confidence_bubble(chunk, card_index, total_cards):
    rows = []
    for r in chunk:
        name = truncate(r.get('chinese_name') or '（未知商品）', 18)    #字數限字、防缺值
        price = r.get('sale_price_db') if 'sale_price_db' in r else r.get('sale_price')
        price_text = f"${price}" if price not in (None, '') else "-"
        rows.append({
            "type": "box",
            "layout": "horizontal",
            "contents": [
                {"type": "text", "text": name, "size": "sm", "wrap": True, "flex": 3},  #商品名稱
                {"type": "text", "text": price_text, "size": "sm", "weight": "bold",    #特價金額
                 "color": BRAND_RED, "flex": 1, "align": "end"},
            ],
        })
        rows.append({"type": "separator", "margin": "sm"})
    if rows:
        rows.pop()  # 去除多餘分隔線
    #標題格式
    title = "本週最划算精選" if total_cards <= 1 else f"本週最划算精選（{card_index}/{total_cards}）"
    return {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": BRAND_RED,
            "paddingAll": "md",
            "contents": [{"type": "text", "text": title, "weight": "bold", "color": "#ffffff", "size": "sm"}],
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": rows},
    }

#第5張卡片以後:低信心/失敗商品
def build_low_confidence_bubble(row):
    name = truncate(row.get('chinese_name') or '商品名稱未辨識', 30)    #字數限字、缺值回傳
    image_url = row.get('image_url') or ''
    tier_label = "辨識失敗" if row.get('confidence_tier') == 'OCR辨識失敗_退回網址' else "待人工確認"
    #圖片顯示區
    hero = None
    if image_url:
        hero = {"type": "image", "url": image_url, "size": "full",
                "aspectRatio": "1:1", "aspectMode": "cover"}
    #商品名
    body_contents = [
        {"type": "text", "text": tier_label, "size": "xxs", "color": "#999999"},
        {"type": "text", "text": name, "weight": "bold", "size": "sm", "wrap": True, "margin": "sm"},
    ]
    #動態拼接卡片區塊
    bubble = {
        "type": "bubble",
        "size": "kilo",
        "body": {"type": "box", "layout": "vertical", "contents": body_contents},
    }
    if hero:
        bubble["hero"] = hero
    if image_url:
        bubble["footer"] = {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "button", "style": "secondary",
                 "action": {"type": "uri", "label": truncate("查看原圖確認", MAX_LABEL_LEN), "uri": image_url}}
            ],
        }
    return bubble


# ---------------------------------------------------------
# 動態分卡：組出所有訊息物件
# ---------------------------------------------------------
def build_carousels(all_db_rows, run_date_str, webpage_url):
    # 導入塞選器
    top_high = select_top_high_confidence(all_db_rows)
    low_rows = get_low_confidence_rows(all_db_rows)
    # 組合成高信心表格卡
    high_chunks = [top_high[i:i + HIGH_CONFIDENCE_ROWS_PER_CARD]
                   for i in range(0, len(top_high), HIGH_CONFIDENCE_ROWS_PER_CARD)]
    high_bubbles = [build_high_confidence_bubble(chunk, idx + 1, len(high_chunks))
                    for idx, chunk in enumerate(high_chunks)]

    # 組合成低信心圖卡：先組合好，再依剩餘空位分配
    low_bubbles = [build_low_confidence_bubble(r) for r in low_rows]

    # ---- 第一個 Carousel：總覽卡 + 高信心卡 + 剩餘空位的低信心卡 ----
    first_carousel_bubbles = [build_overview_bubble(run_date_str, webpage_url)] + high_bubbles
    remaining_slots_in_first = max(0, MAX_BUBBLES_PER_CAROUSEL - len(first_carousel_bubbles))
    first_carousel_bubbles += low_bubbles[:remaining_slots_in_first]
    leftover_low_bubbles = low_bubbles[remaining_slots_in_first:]

    carousels_bubbles = [first_carousel_bubbles]

    # ---- 後續 Carousel：剩下的低信心卡，依實際數量動態算需要幾張（無條件進位），不固定輪數 ----
    if leftover_low_bubbles:
        num_more = math.ceil(len(leftover_low_bubbles) / MAX_BUBBLES_PER_CAROUSEL)
        for i in range(num_more):
            chunk = leftover_low_bubbles[i * MAX_BUBBLES_PER_CAROUSEL: (i + 1) * MAX_BUBBLES_PER_CAROUSEL]
            carousels_bubbles.append(chunk)

    # ---- 組成 LINE 訊息物件（每個 Carousel 一則 flex 訊息）----
    messages = []
    total_rounds = len(carousels_bubbles)
    for idx, bubbles in enumerate(carousels_bubbles, start=1):
        alt_text = truncate(
            f"COSTCO好市多 {run_date_str} 優惠消息（第{idx}/{total_rounds}輪，共{len(bubbles)}項）",
            MAX_ALT_TEXT_LEN,
        )
        messages.append({
            "type": "flex",
            "altText": alt_text,
            "contents": {"type": "carousel", "contents": bubbles},
        })
    #回傳結果與統計數據
    return messages, {
        'high_confidence_selected': len(top_high),
        'high_confidence_cards': len(high_chunks),
        'low_confidence_total': len(low_rows),
        'total_carousels': len(messages),
    }


# ---------------------------------------------------------
# 呼叫 LINE Broadcast API
# ---------------------------------------------------------
def broadcast_messages(messages, dry_run=False):
    """
    dry_run=True 時不會真的打 API，只回傳切好的批次，方便測試。
    """
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    if not dry_run and not token:
        raise RuntimeError("請設定環境變數 LINE_CHANNEL_ACCESS_TOKEN")
    #每5個訊息物件為一批
    batches = [messages[i:i + MAX_MESSAGES_PER_CALL] for i in range(0, len(messages), MAX_MESSAGES_PER_CALL)]
    # 測試模式下不發出 API 請求
    if dry_run:
        return batches
    #權限身分驗證
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    #批次發送
    for batch_idx, batch in enumerate(batches, start=1):
        resp = requests.post(LINE_BROADCAST_URL, headers=headers, json={"messages": batch}, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(     #錯誤檢驗與防護機制
                f"LINE Broadcast API 第 {batch_idx}/{len(batches)} 批呼叫失敗 "
                f"(status={resp.status_code}): {resp.text}"
            )
        print(f"  已送出第 {batch_idx}/{len(batches)} 批（{len(batch)} 個訊息物件）")

    return batches

# ---------------------------------------------------------
# 主流程
# ---------------------------------------------------------
def run_line_push(all_db_rows, webpage_url, run_date=None, dry_run=False):
    if run_date is None:
        run_date = datetime.now(TAIPEI_TZ)
    run_date_str = run_date.strftime("%Y/%m/%d")

    messages, stats = build_carousels(all_db_rows, run_date_str, webpage_url)

    print(f"LINE 推播內容整理完成：")
    print(f"  高信心精選: {stats['high_confidence_selected']} 筆 -> {stats['high_confidence_cards']} 張表格卡")
    print(f"  低信心/失敗: {stats['low_confidence_total']} 筆 -> 全部做成圖卡")
    print(f"  共組成 {stats['total_carousels']} 個 Carousel")

    batches = broadcast_messages(messages, dry_run=dry_run)
    print(f"  打包成 {len(batches)} 次 API 呼叫" + ("（dry_run，未真的發送）" if dry_run else "（已發送）"))

    return {'messages': messages, 'stats': stats, 'batches': batches}


if __name__ == "__main__":
    # 簡單的手動測試：假資料 + dry_run，確認排版邏輯正確
    fake_rows = [
        {'code': '111111', 'chinese_name': '歐姆龍溫熱低周波治療器', 'sale_price_db': 3499,
         'original_price': 4999, 'discount_price': 1500, 'confidence_tier': '高信心_顯示特價', 'image_url': ''},
        {'code': '222222', 'chinese_name': '好菇道有機組合8入', 'sale_price_db': 160,
         'original_price': 200, 'discount_price': 40, 'confidence_tier': '高信心_顯示特價', 'image_url': ''},
        {'code': '333333', 'chinese_name': '待確認商品', 'sale_price_db': None,
         'original_price': None, 'discount_price': None, 'confidence_tier': '低信心_退回網址',
         'image_url': 'https://example.com/333.jpg'},
    ]
    result = run_line_push(fake_rows, webpage_url="https://ruiya173719.github.io/costco_auto/", dry_run=True)
    import json
    print(json.dumps(result['messages'], ensure_ascii=False, indent=2)[:2000])
