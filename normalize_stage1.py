"""
正規化I
"""
import csv
import os
import sys
from datetime import datetime, timezone
import psycopg2
import psycopg2.extras
import pytz
from dotenv import load_dotenv

load_dotenv()  # 讀取同目錄下的 .env，把 SUPABASE_DB_URL 等變數載入 os.environ

API_QUOTA_LIMIT = 950  # 月配額警示水位，達到就強制跳過 OCR


# ---------------------------------------------------------
# 共用工具
# ---------------------------------------------------------
#商品編號歸一化
def normalize_code(v):
    if v is None:
        return None
    digits = ''.join(ch for ch in str(v) if ch.isdigit())
    return digits or None

#欄位讀取器
CODE_COLUMNS = ['code', 'Code', 'CODE', '編號', '商品編號']
NAME_COLUMNS = ['chinese_name', 'name', 'Name', '中文名稱', '品名']
IMAGE_URL_COLUMNS = ['image', 'Image', 'image_url', '圖片網址', '圖片']  # 這裡要的是圖片本身的網址，不能混入 detail_url（文章頁面網址）
#時區
TAIPEI_TZ = pytz.timezone("Asia/Taipei")
#資料欄位清洗
def get_field(row, candidates):
    for key in candidates:
        v = row.get(key)
        if v not in (None, ''):
            return v
    return None
#讀取csv
def load_csv(path):
    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))
#時區設定
def current_year_month():
    return datetime.now(TAIPEI_TZ).strftime('%Y-%m')


# ---------------------------------------------------------
# 資料庫操作
# ---------------------------------------------------------
#建立連線
def get_conn():
    db_url = os.environ.get('SUPABASE_DB_URL')
    if not db_url:
        raise RuntimeError('資料庫連線失敗請先設定環境變數 SUPABASE_DB_URL')
    return psycopg2.connect(db_url)

#版本發號與快取追蹤
def start_new_run(conn, article_url=None):
    with conn.cursor() as cur:
        #找出「上一次執行」的 ID
        cur.execute("SELECT MAX(id) FROM runs")
        prev_run_id = cur.fetchone()[0]  #前次有的
        #建立「本次執行」的容器
        cur.execute(
            "INSERT INTO runs (article_url, status) VALUES (%s, 'running') RETURNING id",
            (article_url,)
        )
        this_run_id = cur.fetchone()[0]  #前次無的
    conn.commit()
    return this_run_id, prev_run_id

#API監控
def get_monthly_usage(conn, year_month):
    with conn.cursor() as cur:  
        cur.execute(    #定位至API資料庫中 年月欄位
            "SELECT call_count FROM api_usage_monthly WHERE year_month = %s",
            (year_month,)
        )
        row = cur.fetchone()
        return row[0] if row else 0

#篩選基準
def fetch_cache_rows(conn, codes, prev_run_id):
    if not codes or prev_run_id is None:
        return {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT code, chinese_name, sale_price, image_url, confidence_tier
            FROM latest_cache
            WHERE code = ANY(%s)
              AND last_run_id = %s
              AND confidence_tier = '高信心_顯示特價'
              AND sale_price IS NOT NULL
            """,
            (list(codes), prev_run_id)
        )
        return {r['code']: r for r in cur.fetchall()}

#輸入本次正規化log紀錄
def update_run_stats(conn, run_id, crawled_count, cache_hit_count, ocr_sent_count, quota_skip_count):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE runs
            SET crawled_count = %s, cache_hit_count = %s,
                ocr_sent_count = %s, quota_skip_count = %s
            WHERE id = %s
            """,
            (crawled_count, cache_hit_count, ocr_sent_count, quota_skip_count, run_id)
        )
    conn.commit()


# ---------------------------------------------------------
# 核心邏輯（純資料處理 + DB 查詢，不做任何檔案 I/O，也不開/關連線）
# orchestrator 直接呼叫這個函式，拿到的都是 Python 資料結構
# ---------------------------------------------------------
def run_stage1_core(conn, run_id, prev_run_id, crawled_rows):
    #資料預處理
    prepped = []
    for c in crawled_rows:
        code = normalize_code(get_field(c, CODE_COLUMNS))
        if not code:
            continue
        prepped.append({
            'code': code,
            'chinese_name': get_field(c, NAME_COLUMNS) or '',
            'image_url': get_field(c, IMAGE_URL_COLUMNS) or '',
        })
    # 查詢快取與當月API用量
    all_codes = {p['code'] for p in prepped}
    cache_hits = fetch_cache_rows(conn, all_codes, prev_run_id) #抓取上一次快取
    year_month = current_year_month()
    used_this_month = get_monthly_usage(conn, year_month)       #查詢本月已用API額度
    reserved = 0  # 本次API消耗數容器
    # 暫存容器
    resolved_rows = []      # 快取命中不需送OCR
    ocr_pending_rows = []   # 需要送OCR
    seen_ocr_urls = set()   # 去重用的圖片網址

    cache_hit_count = 0
    quota_skip_count = 0

    ##分流迴圈 三層邏輯判斷
    for p in prepped:
        code = p['code']

        # ---- 情境一：Cache Hit (命中上一次高信心快取） ----
        if code in cache_hits:
            hit = cache_hits[code]
            resolved_rows.append({
                'code': code,
                'chinese_name': p['chinese_name'] or hit['chinese_name'],
                'sale_price': hit['sale_price'],
                'source_url': '',
                'confidence_tier': '高信心_顯示特價',
                'source': 'cache_hit',
            })
            cache_hit_count += 1
            continue
        # ---- 情境二：Cache Miss 且API額度即將超標 ----
        if used_this_month + reserved >= API_QUOTA_LIMIT:
            resolved_rows.append({
                'code': code,
                'chinese_name': p['chinese_name'],
                'sale_price': '',                       # 價格留空
                'source_url': p['image_url'],           # 提供原網址
                'confidence_tier': '低信心_退回網址',
                'source': 'quota_skip',
            })
            quota_skip_count += 1
            continue
        # ---- 情境三：Cache Miss 且配額尚有餘裕，送去 OCR ----
        reserved += 1
        ocr_pending_rows.append(p)
        if p['image_url'] and p['image_url'] not in seen_ocr_urls:
            seen_ocr_urls.add(p['image_url'])

    #統計Log寫回資料庫（這屬於「核心」行為，不是檔案I/O，orchestrator 跟 CLI 都需要）
    update_run_stats(
        conn, run_id,
        crawled_count=len(prepped),
        cache_hit_count=cache_hit_count,
        ocr_sent_count=len(ocr_pending_rows),
        quota_skip_count=quota_skip_count,
    )

    return {
        'resolved_rows': resolved_rows,
        'ocr_pending_rows': ocr_pending_rows,
        'seen_ocr_urls': seen_ocr_urls,
        'cache_hit_count': cache_hit_count,
        'quota_skip_count': quota_skip_count,
        'used_this_month': used_this_month,
        'prepped_count': len(prepped),
    }


# ---------------------------------------------------------
# CLI 主流程（維持原本地端用法：讀 CSV -> 呼叫核心邏輯 -> 落地成檔案）
# ---------------------------------------------------------
def run_normalize_stage1(crawled_path, article_url=None):
    conn = get_conn()
    try:
        run_id, prev_run_id = start_new_run(conn, article_url=article_url)
        crawled_rows = load_csv(crawled_path)

        result = run_stage1_core(conn, run_id, prev_run_id, crawled_rows)
        resolved_rows = result['resolved_rows']
        ocr_pending_rows = result['ocr_pending_rows']
        seen_ocr_urls = result['seen_ocr_urls']
        cache_hit_count = result['cache_hit_count']
        quota_skip_count = result['quota_skip_count']
        used_this_month = result['used_this_month']

        # cache_and_skip_output.csv 無需辨識的商品清單
        with open('cache_and_skip_output.csv', 'w', newline='', encoding='utf-8-sig') as f:
            fieldnames = ['code', 'chinese_name', 'sale_price', 'source_url', 'confidence_tier', 'source']
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(resolved_rows)

        # ocr_pending.csv 等待辨識的商品清單對照表
        with open('ocr_pending.csv', 'w', newline='', encoding='utf-8-sig') as f:
            fieldnames = ['code', 'chinese_name', 'image_url']
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(ocr_pending_rows)

        # to_ocr_urls.txt 去重後的圖片網址清單
        with open('to_ocr_urls.txt', 'w', encoding='utf-8') as f:
            for url in seen_ocr_urls:
                f.write(url + '\n')

        # 記下這次的 run_id，讓收尾腳本知道要把資料寫回哪一個 run
        with open('current_run_id.txt', 'w', encoding='utf-8') as f:
            f.write(str(run_id))

        print(f"本次執行 run_id = {run_id}（上一次 run_id = {prev_run_id}）")
        print(f"共處理 {result['prepped_count']} 筆商品")
        print(f"  Cache Hit（免 OCR）: {cache_hit_count} 筆")
        print(f"  配額超標強制跳過: {quota_skip_count} 筆")
        print(f"  需要送 OCR 的商品: {len(ocr_pending_rows)} 筆，對應 {len(seen_ocr_urls)} 張圖片（去重後）")
        print(f"  本月已用配額（送出前）: {used_this_month} / {API_QUOTA_LIMIT}")
        print("\n已輸出：")
        print("  cache_and_skip_output.csv  <- 直無需辨識的商品清單")
        print("  ocr_pending.csv            <- 這次送 OCR 的商品清單（收尾比對用）")
        print("  to_ocr_urls.txt            <- (OCR) 去重後的圖片網址清單")
        print("  current_run_id.txt         <- 記錄這次 run_id，收尾腳本要用")
        return run_id

    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python normalize_stage1.py crawled_data.csv [article_url]")
        sys.exit(1)

    crawled_path = sys.argv[1]
    article_url = sys.argv[2] if len(sys.argv) > 2 else None
    run_normalize_stage1(crawled_path, article_url=article_url)
