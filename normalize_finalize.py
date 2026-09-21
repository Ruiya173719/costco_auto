"""
正規化II
"""
import csv
import sys
from datetime import datetime, timezone
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()  # 讀取同目錄下的 .env

from normalize_stage1 import get_conn, current_year_month, normalize_code  # 共用工具

CODE_COLUMNS = ['code', 'Code', 'CODE', '編號', '商品編號']

#讀取csv
def load_csv(path):
    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))
#資料欄位清洗
def get_field(row, candidates):
    for key in candidates:
        v = row.get(key)
        if v not in (None, ''):
            return v
    return None

#讀取正規化I的current_run_id.txt定位資料庫寫入位置
def read_run_id():
    with open('current_run_id.txt', 'r', encoding='utf-8') as f:
        return int(f.read().strip())
#取得本次實際消耗的API次數
def read_api_calls_used(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        print(f"[警告] 讀不到 {path}，本次 API 用量以 0 計（請確認 OCR 腳本有輸出這個檔案）")
        return 0
#整數轉換
def to_int_or_none(v):
    if v in (None, ''):
        return None
    try:
        return int(v)
    except ValueError:
        return None
#布林轉換
def to_bool_or_none(v):
    if v in (None, ''):
        return None
    return str(v).strip().lower() == 'true'
#浮點數轉換
def to_float_or_none(v):
    if v in (None, ''):
        return None
    try:
        return float(v)
    except ValueError:
        return None


# ---------------------------------------------------------
# 合併邏輯：OCR 結果分流、回傳supabase之Log
# ---------------------------------------------------------
#OCR結果分流邏輯判斷
def classify_ocr_pending(ocr_pending_rows, result_rows):
    #OCR 辨識結果Key值化
    result_by_code = {}
    for r in result_rows:
        code = normalize_code(get_field(r, CODE_COLUMNS))
        if code:
            result_by_code.setdefault(code, r)

    output_rows = []       # 給 即時推播 用（使用者看得到的欄位）
    db_rows = []            # 給 supabase 用（完整診斷欄位）
    #即時推播檔邏輯判斷
    for p in ocr_pending_rows:
        code = p['code']
        name = p.get('chinese_name', '')
        url = p.get('image_url', '')
        r = result_by_code.get(code)
    #判斷三等級分流
        det_price = (r or {}).get('sale_price', '')
        has_od = str((r or {}).get('has_original_and_discount', '')).strip().lower() == 'true'
        #高信心呈現價格
        if r is not None and det_price and has_od:
            tier = '高信心_顯示特價'
            output_rows.append({'code': code, 'chinese_name': name, 'sale_price': det_price,
                                 'source_url': '', 'confidence_tier': tier})
            source = 'ocr_success'
        #低信心呈現網址
        elif r is not None and det_price:
            tier = '低信心_退回網址'
            output_rows.append({'code': code, 'chinese_name': name, 'sale_price': '',
                                 'source_url': url, 'confidence_tier': tier})
            source = 'ocr_low'
        #識別失敗呈現網址
        else:
            tier = 'OCR辨識失敗_退回網址'
            output_rows.append({'code': code, 'chinese_name': name, 'sale_price': '',
                                 'source_url': url, 'confidence_tier': tier})
            source = 'ocr_failed'
        #寫入supabase作為log的格式
        db_rows.append({
            'code': code,
            'chinese_name': name,
            'sale_price_db': to_int_or_none(det_price),  # 即使低信心，DB 內部仍存 OCR 猜到的值
            'image_url': url,
            'confidence_tier': tier,
            'source': source,
            # 存入完整的 OCR 診斷數據（original_price/discount_price 已由 classify_group_fields 回傳）
            'original_price': to_int_or_none((r or {}).get('original_price')),
            'discount_price': to_int_or_none((r or {}).get('discount_price')),
            'sale_price_suspect': to_bool_or_none((r or {}).get('sale_price_suspect')),
            'sale_price_conf': to_float_or_none((r or {}).get('sale_price_conf')),
            'sale_price_digit_count': to_int_or_none((r or {}).get('sale_price_digit_count')),
            'sale_price_candidate_count': to_int_or_none((r or {}).get('sale_price_candidate_count')),
            'sale_price_margin_ratio': to_float_or_none((r or {}).get('sale_price_margin_ratio')),
            'has_original_and_discount': to_bool_or_none((r or {}).get('has_original_and_discount')),
        })
    return output_rows, db_rows

#Log輸入格式對齊
def cache_and_skip_to_db_rows(cache_and_skip_rows):
    db_rows = []
    for row in cache_and_skip_rows:
        db_rows.append({
            'code': row['code'],
            'chinese_name': row.get('chinese_name'),
            'sale_price_db': to_int_or_none(row.get('sale_price')),
            'image_url': row.get('source_url') or '',
            'confidence_tier': row.get('confidence_tier'),
            'source': row.get('source'),  # cache_hit / quota_skip
            #v10.1折價對比用；若就沒有這項資料，讀不到是None
            'original_price': to_int_or_none(row.get('original_price')),
            'discount_price': to_int_or_none(row.get('discount_price')),
            'sale_price_suspect': None,
            'sale_price_conf': None,
            'sale_price_digit_count': None,
            'sale_price_candidate_count': None,
            'sale_price_margin_ratio': None,
            'has_original_and_discount': None,
        })
    return db_rows


# ---------------------------------------------------------
# 資料庫寫入、操作
# ---------------------------------------------------------
#若商品編號已存在就更新，不存在就新增
def upsert_latest_cache(conn, run_id, db_rows):
    with conn.cursor() as cur:
        for row in db_rows:
            cur.execute(
                """
                INSERT INTO latest_cache (code, chinese_name, sale_price, original_price, discount_price,
                                           image_url, confidence_tier, last_run_id, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (code) DO UPDATE SET
                    chinese_name = EXCLUDED.chinese_name,
                    sale_price = EXCLUDED.sale_price,
                    original_price = EXCLUDED.original_price,
                    discount_price = EXCLUDED.discount_price,
                    image_url = EXCLUDED.image_url,
                    confidence_tier = EXCLUDED.confidence_tier,
                    last_run_id = EXCLUDED.last_run_id,
                    updated_at = now()
                """,
                (row['code'], row['chinese_name'], row['sale_price_db'], row['original_price'], row['discount_price'],
                 row['image_url'], row['confidence_tier'], run_id)
            )
    conn.commit()

#批次寫入資料庫
def insert_product_history(conn, run_id, db_rows):
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            """
            INSERT INTO product_history (
                run_id, code, chinese_name, sale_price, original_price, discount_price,
                sale_price_suspect, sale_price_conf, sale_price_digit_count,
                sale_price_candidate_count, sale_price_margin_ratio,
                has_original_and_discount, confidence_tier, image_url, source
            ) VALUES (
                %(run_id)s, %(code)s, %(chinese_name)s, %(sale_price_db)s, %(original_price)s, %(discount_price)s,
                %(sale_price_suspect)s, %(sale_price_conf)s, %(sale_price_digit_count)s,
                %(sale_price_candidate_count)s, %(sale_price_margin_ratio)s,
                %(has_original_and_discount)s, %(confidence_tier)s, %(image_url)s, %(source)s
            )
            """,
            [dict(row, run_id=run_id) for row in db_rows]
        )
    conn.commit()

#累加當月 API 用量
def update_api_usage(conn, calls_used):
    year_month = current_year_month()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO api_usage_monthly (year_month, call_count, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (year_month) DO UPDATE SET
                call_count = api_usage_monthly.call_count + EXCLUDED.call_count,
                updated_at = now()
            """,
            (year_month, calls_used)
        )
    conn.commit()
#標記任務完成
def finish_run(conn, run_id):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE runs SET finished_at = now(), status = 'success' WHERE id = %s",
            (run_id,)
        )
    conn.commit()

#合併最終結果並輸出 CSV 檔
def save_final_output(cache_and_skip_rows, ocr_output_rows, out_path='final_output.csv'):
    fieldnames = ['code', 'chinese_name', 'sale_price', 'source_url', 'confidence_tier']
    with open(out_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        #寫入快取命中
        for row in cache_and_skip_rows:
            writer.writerow({k: row.get(k) for k in fieldnames})
        #寫入OCR辨識結果
        for row in ocr_output_rows:
            writer.writerow(row)


# ---------------------------------------------------------
# 核心邏輯（純資料處理 + DB 寫入，不做任何檔案 I/O，也不開/關連線）
# orchestrator 直接呼叫這個函式，拿到的都是 Python 資料結構
# ---------------------------------------------------------
def run_finalize_core(conn, run_id, cache_and_skip_rows, ocr_pending_rows, result_rows, calls_used):
    #彙整資料
    ocr_output_rows, ocr_db_rows = classify_ocr_pending(ocr_pending_rows, result_rows)  #OCR結果進行分類
    cache_skip_db_rows = cache_and_skip_to_db_rows(cache_and_skip_rows)                 #將階段一的 Cache Hit / Skip 資料轉
    all_db_rows = cache_skip_db_rows + ocr_db_rows                                      #合併所有資料

    ##寫回 Supabase 資料庫
    upsert_latest_cache(conn, run_id, all_db_rows)      # 更新快取表
    insert_product_history(conn, run_id, all_db_rows)   # 寫入歷史紀錄
    update_api_usage(conn, calls_used)                  # 累加 API 用量
    finish_run(conn, run_id)                            # 將任務狀態標記為成功

    tier_counts = {}
    for row in all_db_rows:
        tier_counts[row['confidence_tier']] = tier_counts.get(row['confidence_tier'], 0) + 1

    return {
        'cache_and_skip_rows': cache_and_skip_rows,   # 直接給 save_final_output / LINE 推播用
        'ocr_output_rows': ocr_output_rows,
        'all_db_rows': all_db_rows,
        'tier_counts': tier_counts,
    }


# ---------------------------------------------------------
# CLI 主流程（維持原本地端用法：讀 CSV -> 呼叫核心邏輯 -> 落地成檔案）
# ---------------------------------------------------------
def run_finalize(cache_and_skip_path, ocr_pending_path, results_path, dropped_path, api_calls_path):
    run_id = read_run_id()                              #讀取本次 run_id
    calls_used = read_api_calls_used(api_calls_path)    #讀取本次 OCR 耗用的API次數
    #載入需計入於log之檔案
    cache_and_skip_rows = load_csv(cache_and_skip_path)
    ocr_pending_rows = load_csv(ocr_pending_path)
    result_rows = load_csv(results_path)
    # dropped.csv 目前資訊已包含在「result_by_code 查不到」的邏輯裡，
    # 這裡讀入只是為了之後如果要用 missing_fields 做更細的分類，先留著介面。
    _dropped_rows = load_csv(dropped_path) if dropped_path else []

    conn = get_conn()
    try:
        result = run_finalize_core(conn, run_id, cache_and_skip_rows, ocr_pending_rows, result_rows, calls_used)
    finally:
        conn.close()                                        # 確保連線安全關閉

    save_final_output(result['cache_and_skip_rows'], result['ocr_output_rows'])   #產出推撥所需之資料

    # 統計輸出
    total = len(result['all_db_rows'])
    tier_counts = result['tier_counts']
    #列出結論
    print(f"run_id = {run_id} 收尾完成")
    print(f"共處理 {total} 筆商品")
    for tier, count in tier_counts.items():
        pct = count / total * 100 if total else 0
        print(f"  {tier}: {count} 筆 ({pct:.1f}%)")
    print(f"本次實際 Vision API 用量: {calls_used} 次（已累加回 api_usage_monthly）")
    print("\n已輸出：final_output.csv")
    print("已寫回Supabase各表單：latest_cache / product_history / api_usage_monthly / runs")


if __name__ == "__main__":
    if len(sys.argv) < 6:
        print("用法: python normalize_finalize.py cache_and_skip_output.csv ocr_pending.csv "
              "results.csv dropped.csv api_calls_used.txt")
        sys.exit(1)

    run_finalize(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
