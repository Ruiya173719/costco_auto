"""
main.py - 端對端 orchestrator
=================================
串接五個階段：爬蟲 -> 正規化I -> OCR -> 正規化II（finalize） -> LINE推播
"""
import csv

import costcom_V7 as crawler
import normalize_stage1 as stage1
import OCR_v8_4 as ocr
import normalize_finalize as finalize
import line_push

# 外部網頁網址
WEBPAGE_URL = "https://ruiya173719.github.io/costco_auto/"
LINE_PUSH_DRY_RUN = False

def run_pipeline():
    # ---- [1/5] 爬蟲：不匯出CSV檔資料直接保留在記憶體中 ----
    print("=" * 20 + " [1/5] 爬蟲 " + "=" * 20)
    crawled_data, article_url = crawler.run_scraper(save_csv=False)
    if not crawled_data:
        print("爬蟲沒有抓到任何資料，中止流程。")
        return None

    # ----資料清洗、識別、分流----
    conn = stage1.get_conn()    # 建立資料庫連線通道
    run_id = None                # 例外處理識別用
    try:
        # ---- [2/5] 正規化I：Cache 比對 + 950次配額防線 ----
        print("\n" + "=" * 20 + " [2/5] 正規化I " + "=" * 20)
        run_id, prev_run_id = stage1.start_new_run(conn, article_url=article_url)       #建立本次執行的版本號
        stage1_result = stage1.run_stage1_core(conn, run_id, prev_run_id, crawled_data) #核心過濾與比對邏輯

        print(f"run_id = {run_id}（上一次 run_id = {prev_run_id}）")
        print(f"共處理 {stage1_result['prepped_count']} 筆商品")
        print(f"  Cache Hit（免 OCR）: {stage1_result['cache_hit_count']} 筆")
        print(f"  配額超標強制跳過: {stage1_result['quota_skip_count']} 筆")
        print(f"  需要送 OCR 的商品: {len(stage1_result['ocr_pending_rows'])} 筆，"
              f"對應 {len(stage1_result['seen_ocr_urls'])} 張圖片（去重後）")

        # ---- [3/5] OCR：使用正規化I清洗後的圖片網址進行識別 ----
        print("\n" + "=" * 20 + " [3/5] OCR " + "=" * 20)
        image_urls = list(stage1_result['seen_ocr_urls'])
        if image_urls:     #不匯出CSV檔資料僅保留在記憶體中
            all_results, all_dropped, all_errors, calls_used, high_risk_rows = ocr.run_batch(
                urls=image_urls, write_files=False
            )
        else:
            print("這次沒有需要送 OCR 的商品，跳過 OCR 階段。")
            all_results, all_dropped, all_errors, calls_used, high_risk_rows = [], [], [], 0, []

        # ---- [4/5] 正規化II：合併結果、寫回 Supabase ----
        print("\n" + "=" * 20 + " [4/5] 正規化II " + "=" * 20)
        finalize_result = finalize.run_finalize_core(
            conn, run_id,
            cache_and_skip_rows=stage1_result['resolved_rows'],
            ocr_pending_rows=stage1_result['ocr_pending_rows'],
            result_rows=all_results,
            calls_used=calls_used,
        )
    #log紀錄
    except Exception as e:
        # 任何一階段失敗，把 run 標記成失敗並留下錯誤訊息，
        print(f"\n[錯誤] 執行過程中途發生例外，標記 run_id={run_id} 為失敗：{e}")
        if run_id is not None:
            try:
                stage1.mark_run_failed(conn, run_id, str(e))
            # 資料庫連線錯誤
            except Exception as mark_err:
                print(f"[警告] 連 mark_run_failed 都失敗了：{mark_err}")
        raise  # 重新拋出，讓 GitHub Actions 照樣標記這次執行失敗、照樣寄 email 通知

    finally:
        conn.close()  # 連線安全關閉

    # ---- 產出最終結果：唯一保留的檔案，供人工檢視或之後接 LINE 推播 ----
    finalize.save_final_output(finalize_result['cache_and_skip_rows'], finalize_result['ocr_output_rows'])

    # ---- 雙重可疑清單（特價+名稱兩個都異常）：落地成檔案，事後可以在 Actions Artifact 裡查閱 ----
    if high_risk_rows:
        with open('high_risk.csv', 'w', newline='', encoding='utf-8-sig') as f:
            fieldnames = list(high_risk_rows[0].keys())
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(high_risk_rows)
        print(f"[提醒] 有 {len(high_risk_rows)} 筆『雙重可疑』商品建議人工複核，見 high_risk.csv")
    # ---- 處理失敗清單（圖片下載失敗/API呼叫失敗） ----
    if all_errors:
        with open('errors.csv', 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=['url', 'error'])
            writer.writeheader()
            writer.writerows(all_errors)
        print(f"[提醒] 有 {len(all_errors)} 張圖片處理失敗（網路/API錯誤），見 errors.csv")

    total = len(finalize_result['all_db_rows'])
    print(f"\n{'=' * 50}")
    print(f"run_id = {run_id} 全流程執行完成，共處理 {total} 筆商品")
    for tier, count in finalize_result['tier_counts'].items():
        pct = count / total * 100 if total else 0
        print(f"  {tier}: {count} 筆 ({pct:.1f}%)")
    print(f"本次實際 Vision API 用量: {calls_used} 次（已累加回 api_usage_monthly）")
    print("已寫回 Supabase：latest_cache / product_history / api_usage_monthly / runs")
    print("已輸出：final_output.csv")

    # ---- [5/5] LINE 推播：把這次結果組成 Flex Carousel 廣播出去 ----
    print("\n" + "=" * 20 + " [5/5] LINE 推播 " + "=" * 20)
    line_result = line_push.run_line_push(
        finalize_result['all_db_rows'],
        webpage_url=WEBPAGE_URL,
        dry_run=LINE_PUSH_DRY_RUN,
    )

    return finalize_result


if __name__ == "__main__":
    run_pipeline()
