"""
Discord 管理員通報模組
"""
import os
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

DISCORD_WEBHOOK_URL_ENV = "DISCORD_WEBHOOK_URL"

# ---- 色碼控制 ----
COLOR_GREEN = 0x2ECC71   # 一切正常
COLOR_YELLOW = 0xF5B700  # 有潛在問題（警示）
COLOR_RED = 0xE31937     # 系統崩潰/失敗

QUOTA_WARNING_RATIO = 0.8  # 配額用量超過這個比例，報告轉黃色

#Discord連線
def _post_to_discord(payload):
    webhook_url = os.environ.get(DISCORD_WEBHOOK_URL_ENV)
    if not webhook_url:
        print(f"[警告] 沒有設定 {DISCORD_WEBHOOK_URL_ENV}，跳過 Discord 通報")
        return False
    resp = requests.post(webhook_url, json=payload, timeout=10)
    if resp.status_code not in (200, 204):
        print(f"[警告] Discord 通報發送失敗 (status={resp.status_code}): {resp.text}")
        return False
    return True
#成功報告組裝器
def build_success_embed(run_id, stage1_result, finalize_result, calls_used,
                         quota_used_before, quota_limit, high_risk_count, errors_count):
    tier_counts = finalize_result['tier_counts']
    total = len(finalize_result['all_db_rows'])
    quota_used_after = quota_used_before + calls_used
    quota_ratio = quota_used_after / quota_limit if quota_limit else 0
    #卡片顏色判斷
    concerns = []
    if quota_ratio >= QUOTA_WARNING_RATIO:
        concerns.append(f"本月配額已用 {quota_ratio*100:.0f}%")
    if high_risk_count > 0:
        concerns.append(f"{high_risk_count} 筆雙重可疑商品待複核")
    if errors_count > 0:
        concerns.append(f"{errors_count} 張圖片處理失敗")

    color = COLOR_YELLOW if concerns else COLOR_GREEN
    status_line = "；".join(concerns) if concerns else "一切正常"

    def tier_line(tier_name):
        count = tier_counts.get(tier_name, 0)
        pct = count / total * 100 if total else 0
        return f"{count} 筆 ({pct:.0f}%)"

    fields = [
        {"name": "狀態", "value": status_line, "inline": False},
        {"name": "爬取商品數", "value": str(stage1_result['prepped_count']), "inline": True},
        {"name": "Cache Hit", "value": str(stage1_result['cache_hit_count']), "inline": True},
        {"name": "配額跳過", "value": str(stage1_result['quota_skip_count']), "inline": True},
        {"name": "高信心_顯示特價", "value": tier_line("高信心_顯示特價"), "inline": True},
        {"name": "低信心_退回網址", "value": tier_line("低信心_退回網址"), "inline": True},
        {"name": "OCR辨識失敗", "value": tier_line("OCR辨識失敗_退回網址"), "inline": True},
        {"name": "本次 API 用量", "value": f"{calls_used} 次", "inline": True},
        {"name": "本月累積用量", "value": f"{quota_used_after} / {quota_limit}", "inline": True},
        {"name": "待人工複核", "value": f"雙重可疑 {high_risk_count} 筆 / 處理失敗 {errors_count} 張", "inline": True},
    ]
    #組裝內容與回傳
    return {
        "title": f"Costco Pipeline 執行報告 - run_id {run_id}",
        "color": color,
        "fields": fields,
        "timestamp": datetime.utcnow().isoformat(),
    }

#失敗報告組裝器
def build_failure_embed(run_id, error_message):
    return {
        "title": f"Costco Pipeline 執行失敗 - run_id {run_id if run_id is not None else '（尚未建立）'}",
        "description": f"```\n{error_message[:1500]}\n```",
        "color": COLOR_RED,
        "timestamp": datetime.utcnow().isoformat(),
    }

#封包打包發送器
def send_success_report(run_id, stage1_result, finalize_result, calls_used,
                         quota_used_before, quota_limit, high_risk_count, errors_count):
    embed = build_success_embed(run_id, stage1_result, finalize_result, calls_used,
                                 quota_used_before, quota_limit, high_risk_count, errors_count)
    return _post_to_discord({"embeds": [embed]})

def send_failure_report(run_id, error_message):
    embed = build_failure_embed(run_id, error_message)
    return _post_to_discord({"embeds": [embed]})
