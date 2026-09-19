import os
import time
import re
import html
import random
import csv
import logging
import pytz            # 設定時區
import schedule        # 定時排程
from datetime import datetime
from logging.handlers import RotatingFileHandler
from selenium import webdriver                  #selenium開啟html用
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait             #顯性等待
from selenium.webdriver.support import expected_conditions as EC    #顯性等待物件
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException

# =====================================================================
# 參數設定
# =====================================================================

# ---抓取文章網址清單 ---
LIST_TARGET_URL   = "https://www.daybuy.tw/costco/hypermarket-news/"
XPATH_PAGE_WIDE_POST_ID = '//*[starts-with(@id, "post-")]'  #全頁面結構化 fallback 用

# ---第四階段救援：清單頁掛掉---
HOME_URL = "https://www.daybuy.tw/"
ERROR_PAGE_INDICATORS = ("這個網站發生嚴重錯誤", "進一步了解 WordPress 中的疑難排解方式")
FALLBACK_TITLE_KEYWORDS = ("賣場隱藏優惠目擊情報", "賣場優惠目擊", "現場優惠")
WEEKDAY_KEYWORDS = ("週二", "週六", "(二)", "(六)")

# ---文章內頁：抓取圖片+文字---
ARTICLE_CONTAINER_XPATH = '//*[@id="soledad_wrapper"]/div[2]/div/div/div[1]/div/div[3]'
LIST_DIV_XPATH    = './div'
IMAGE_XPATH_ALL   = './p/img | ./img'
LIST_NODES_XPATH  = './p[img] | ./p[a] | ./img'

WAIT_TIMEOUT = 15
OUTPUT_CSV   = "crawled_data.csv"

# ---頁面載入 / 重試設定 ---
PAGE_LOAD_TIMEOUT = 45          # 逾時秒數層
MAX_RETRIES       = 3           # 最大重試次數
RETRY_DELAY_RANGE = (2.0, 5.0)  # 隨機等待秒數

# ---Logging 設定 ---
LOG_FILE       = "daybuy_scraper.log"
LOG_MAX_BYTES  = 5 * 1024 * 1024   # 檔案最大 5MB
LOG_BACKUP_CNT = 3                 # 保留最近的備份檔數

# ---排程設定 ---
TAIPEI_TZ           = pytz.timezone("Asia/Taipei")
SCHEDULE_TIME       = "03:00"                  # 基準時間
SCHEDULE_DAYS       = ("tuesday","saturday",)  # 每週執行的星期
RANDOM_DELAY_RANGE  = (0, 7200)                # 觸發後隨機延遲秒數

# ---正規化---
CODE_RE = re.compile(r'#(\d+)')
CHINESE_RE = re.compile(r'[\u4e00-\u9fff]')
SIZE_TOKENS = {'XS', 'S', 'M', 'L', 'XL', 'XXL', 'XXXL', 'XXXXL', '均', 'F'}
# =====================================================================

def setup_logger() -> logging.Logger:
    """建立 logger """
    logger = logging.getLogger("daybuy_scraper")#log名稱
    logger.setLevel(logging.DEBUG)

    if logger.handlers:  # 避免重複執行
        return logger

    formatter = logging.Formatter(
        #發生時間-日誌等級-哪個函式訊息-訊息內容
        fmt="%(asctime)s [%(levelname)s] %(funcName)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    #顯示於終端機
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    #檔案輸出器
    file_handler = RotatingFileHandler(     #備份設定
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_CNT, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)    #紀錄的門檻
    file_handler.setFormatter(formatter)
    #同時寫入輸出管道
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger

logger = setup_logger()

#正規化
def normalize_title(raw_title: str, detail_url: str = "") -> list[dict]:
    """
    把爬到的原始一行文字（可能含多個 #代號、多個尺寸/顏色變體）
    正規化成多筆 {'name', 'code', 'raw_title', 'detail_url'} 記錄。
    """
    title = html.unescape(raw_title)  # 先還原 HTML 實體，避免 &#8217; 之類誤判成代號
    codes = CODE_RE.findall(title)    # 檢索字串中所有符合CODE_RE的單詞
 
    first_hash = title.find('#')                                #HTML 實體編碼轉回標準字元
    head = title[:first_hash] if first_hash != -1 else title    #將#定位並與中文切片
    m = CHINESE_RE.search(head)                                 #定位第一個中文
    name = head[m.start():].strip() if m else head.strip()      #過濾開頭英文
 
    if not codes:   #判斷name是否分成複數列
        logger.warning(f"[警告] 找不到任何代號: {raw_title!r}")
        return [{'name': name, 'code': None, 'raw_title': raw_title, 'detail_url': detail_url}]
 
    n = len(codes)  #代碼有複數時拆分
    if n == 1:
        return [{'name': name, 'code': f'#{codes[0]}', 'raw_title': raw_title, 'detail_url': detail_url}]
 
    tokens = name.split(' ')
 
    # 斜線變體拆分
    for i, tok in enumerate(tokens):
        if '/' in tok:
            parts = [p for p in tok.split('/') if p]
            if len(parts) == n: #如果/拆出數量=代碼數時則
                rows = []
                for p, c in zip(parts, codes):
                    new_tokens = tokens[:i] + [p] + tokens[i + 1:]
                    rows.append({
                        'name': ' '.join(new_tokens).strip(),
                        'code': f'#{c}',
                        'raw_title': raw_title,
                        'detail_url': detail_url,
                    })
                return rows
 
    # 結尾尺寸序列拆分
    if len(tokens) >= n:
        tail = tokens[-n:]
        if all(t.upper() in SIZE_TOKENS for t in tail):
            head_tokens = tokens[:-n]
            return [{
                'name': ' '.join(head_tokens + [t]).strip(),
                'code': f'#{c}',
                'raw_title': raw_title,
                'detail_url': detail_url,
            } for t, c in zip(tail, codes)]
 
    # 無法辨識對應關係
    logger.debug(f"[提醒] 無法辨識變體對應，同品名複製 {n} 列，請人工複核: {raw_title!r}")
    return [{'name': name, 'code': f'#{c}', 'raw_title': raw_title, 'detail_url': detail_url} for c in codes]


#重試機制
def safe_get(driver: webdriver.Chrome, url: str, max_retries: int = MAX_RETRIES) -> bool:
    """
    帶重試機制的頁面導航。
    """
    for attempt in range(1, max_retries + 1):
        try:
            driver.get(url)
            return True
        except Exception as e:          #將錯誤資訊打包到e函數
            logger.warning(
                f"頁面導航失敗（第 {attempt}/{max_retries} 次）: {url} -> {type(e).__name__}: {e}"
            )
            if attempt < max_retries:   #逾時重試條件
                time.sleep(random.uniform(*RETRY_DELAY_RANGE))
            else:
                logger.error(f"頁面導航重試 {max_retries} 次仍失敗，放棄: {url}", exc_info=True)
                return False
    return False
#清單元素三重除錯機制
def _extract_link_from_item(el) -> dict | None:
    """從單一清單項目元素中，用多種候選路徑抓連結（依優先順序嘗試）"""
    try:
        link_el = None

        # 候選 1：舊版結構 div[1]/a
        candidates = el.find_elements(By.XPATH, "./div[1]/a")
        if candidates:
            link_el = candidates[0]
        # 候選 2：新版結構 div[2]/div[1]/h2/a
        if link_el is None:
            candidates = el.find_elements(By.XPATH, ".//h2/a")
            if candidates:
                link_el = candidates[0]
        # 候選 3：元素本身就是 <a>
        if link_el is None and el.tag_name.lower() == "a":
            link_el = el
        # 候選 4：最後手段，抓任意深度子孫中第一個 <a>
        if link_el is None:
            fallback = el.find_elements(By.XPATH, ".//a")
            if fallback:
                link_el = fallback[0]
        if link_el is None:
            return None

        href = link_el.get_attribute("href")
        text = link_el.text.strip()
        if href:
            return {"title": text, "url": href}
        return None
    except Exception as e:
        logger.warning(f"擷取單一項目連結失敗: {e}")
        return None

def build_driver(headless: bool = True) -> webdriver.Chrome:
    options = Options()
    if headless:
        options.add_argument("--headless=new")               # 無頭模式
        options.add_argument("--no-sandbox")                  # 停用沙盒保護      [Linux 容器]
    options.add_argument("--disable-dev-shm-usage")           # 停用共享記憶體區  [Linux 容器]
    options.add_argument("--disable-gpu")                     # 停用 GPU 硬體加速
    options.add_argument("--window-size=1920,1080")           # 瀏覽器解析度
    options.add_argument(                                     # 反爬蟲
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
    # 載入完成就立刻回傳主控權
    options.page_load_strategy = "eager"

    service = Service()
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)  # 設定逾時
    return driver


# =====================================================================
# 共用：模擬真人滾輪
# =====================================================================
def human_like_scroll(
    driver: webdriver.Chrome,
    max_scrolls: int = 15,
    step_range: tuple = (100, 250),
    pause_range: tuple = (0.1, 1.0),
) -> None:
    
    start_url = driver.current_url
    actions   = ActionChains(driver)

    for i in range(max_scrolls):
        step  = random.randint(*step_range)
        pause = random.uniform(*pause_range)
        actions.scroll_by_amount(0, step).perform()
        time.sleep(pause)

        # 換頁偵測
        if driver.current_url != start_url:
            logger.warning(
                f"第 {i+1} 次滾動後偵測到網址改變：{start_url} -> {driver.current_url}，已停止滾動"
            )
            return

        # 到底偵測
        scroll_pos  = driver.execute_script("return window.scrollY + window.innerHeight")
        full_height = driver.execute_script("return document.body.scrollHeight")
        if scroll_pos >= full_height - 5:
            logger.debug(f"第 {i+1} 次滾動後已到達頁面底部")
            time.sleep(1.0)
            break

# =====================================================================
# 關鍵字判斷 / 首頁救援：清單頁掛掉或內容不符時改由首頁定位最新文章
# =====================================================================
def is_wordpress_error_page(driver: webdriver.Chrome) -> bool:
    """第零階段判斷:偵測目前頁面是否為 WordPress『嚴重錯誤』頁面"""
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        return any(kw in body_text for kw in ERROR_PAGE_INDICATORS)
    except Exception as e:
        logger.warning(f"錯誤頁偵測失敗: {e}")
        return False

#關鍵字過濾器
def is_deal_article_title(title: str) -> bool:

    if not title:
        return False
    return any(kw in title for kw in FALLBACK_TITLE_KEYWORDS) and any(wk in title for wk in WEEKDAY_KEYWORDS)

#第一、二階段判斷邏輯(V7.2)
def collect_deal_links_by_keyword(driver: webdriver.Chrome) -> list[dict]:
    anchors = driver.find_elements(By.XPATH, "//a[@href]")
    candidates = []
    for a in anchors:
        try:#初篩
            text = a.text.strip()
            href = a.get_attribute("href") or ""
        except StaleElementReferenceException:
            continue
        #連結必須有文字
        if not text or "costco" not in href:
            continue
        #核心條件：標題必須符合優惠文章的關鍵字規則
        if is_deal_article_title(text):
            candidates.append({"title": text, "url": href})
    #網址去重
    seen = set()
    deduped = []
    for item in candidates:
        if item["url"] not in seen:
            seen.add(item["url"])
            deduped.append(item)
    return deduped

#第四階段救援：清單頁掛掉（WordPress 嚴重錯誤）時，改從首頁定位。
def rescue_from_homepage(driver: webdriver.Chrome) -> list[dict]:
    logger.warning("進入首頁救援：清單頁疑似掛掉或無符合內容，改由首頁定位")
    if not safe_get(driver, HOME_URL):
        logger.error("首頁救援失敗：首頁導航失敗")
        return []

    wait = WebDriverWait(driver, WAIT_TIMEOUT)
    try:
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    except TimeoutException:
        logger.error("首頁救援失敗：首頁 body 逾時未載入")
        return []
    human_like_scroll(driver)
    #關鍵字判斷邏輯
    deduped = collect_deal_links_by_keyword(driver)
    if not deduped:
        logger.error("首頁救援失敗：首頁找不到符合關鍵字的最新文章")
        return []

    logger.info(f"首頁救援成功，共找到 {len(deduped)} 筆候選文章")
    return deduped


# =====================================================================
# 清單頁，回傳網址清單
# =====================================================================
def scrape_list(driver: webdriver.Chrome) -> list[dict]:
    """
    從清單頁抓取文章標題與網址。
    第一階段以關鍵字直接掃描為主要方法
    第二階段為結構化 全頁面 post-ID 搜尋
    第三階段為首頁救援。
    """
    # ---------- 第零階段：先判斷清單頁是否直接掛掉（WordPress 嚴重錯誤）----------
    if is_wordpress_error_page(driver):
        logger.warning("清單頁為 WordPress 嚴重錯誤頁，直接進首頁救援")
        return rescue_from_homepage(driver)

    # ---------- 第一階段：關鍵字直接掃描（主要方法） ----------
    results = collect_deal_links_by_keyword(driver)
    if results:
        logger.info(f"第一階段（關鍵字掃描）命中，共找到 {len(results)} 篇優惠目擊情報文章")
        return results

    logger.warning("第一階段關鍵字掃描未命中，進入第二階段全頁面 post-ID 結構化搜尋")

    # ---------- 第二階段：全頁面 post-ID 結構化----------
    wait = WebDriverWait(driver, WAIT_TIMEOUT)
    try:
        wait.until(lambda d: len(d.find_elements(By.XPATH, XPATH_PAGE_WIDE_POST_ID)) > 0)
        item_elements = driver.find_elements(By.XPATH, XPATH_PAGE_WIDE_POST_ID)
        logger.info(f"第二階段命中，全頁面共找到 {len(item_elements)} 個候選項目")
    except (TimeoutException, StaleElementReferenceException):
        logger.warning("第二階段亦找不到任何 post-ID 元素，觸發第三階段首頁救援")
        return rescue_from_homepage(driver)

    extracted = []
    for el in item_elements:
        item = _extract_link_from_item(el)
        if item:
            extracted.append(item)

    filtered = [item for item in extracted if is_deal_article_title(item.get("title", ""))]
    excluded_count = len(extracted) - len(filtered)
    if excluded_count > 0:
        logger.info(f"內容篩選：排除 {excluded_count} 篇非優惠目擊情報類型文章")

    if not filtered:
        logger.warning("第二階段項目中找不到任何優惠目擊情報類型文章，觸發第三階段首頁救援")
        return rescue_from_homepage(driver)

    return filtered


# =====================================================================
# 文章內頁：抓取圖片+文字資料
# =====================================================================
def scroll_until_images_loaded(
    driver: webdriver.Chrome,
    list_div,
    max_scrolls: int = 500,
    stable_rounds_needed: int = 3,
    step_range: tuple = (300, 600),
    pause_range: tuple = (0.1, 0.5),
) -> None:
    """
    滾動文章頁直到圖片數量穩定（lazy-load 圖片全部載入）。
    """
    actions      = ActionChains(driver)
    last_count   = -1
    stable_rounds = 0

    for i in range(max_scrolls):
        step  = random.randint(*step_range)
        pause = random.uniform(*pause_range)
        actions.scroll_by_amount(0, step).perform()
        time.sleep(pause)

        try:
            current_count = len(list_div.find_elements(By.XPATH, IMAGE_XPATH_ALL))
        except StaleElementReferenceException:
            logger.warning("滾動過程中清單 div 失效，停止滾動偵測")
            return

        if current_count == last_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
            last_count    = current_count

        if (i + 1) % 10 == 0:
            logger.debug(f"滾動第 {i+1} 次，目前已載入圖片數: {current_count}")

        scroll_pos  = driver.execute_script("return window.scrollY + window.innerHeight")
        full_height = driver.execute_script("return document.body.scrollHeight")
        reached_bottom = scroll_pos >= full_height - 4000   #最低距離

        if stable_rounds >= stable_rounds_needed and reached_bottom:
            logger.debug(f"圖片數量穩定於 {current_count} 筆且已到底，停止滾動")
            return
        elif stable_rounds >= stable_rounds_needed and not reached_bottom:
            time.sleep(1.0)

    logger.warning(f"已達最大滾動次數 {max_scrolls}，目前圖片數: {last_count}（可能尚未載入完全）")

#除錯機制
def scrape_article(driver: webdriver.Chrome, url: str) -> list[dict]:
    """
    導向文章內頁 url，抓取圖片並進行文字標題正規化解析。
    """
    logger.info(f"進入文章頁: {url}")
    if not safe_get(driver, url):
        return []

    wait = WebDriverWait(driver, WAIT_TIMEOUT)
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    # 等待文章容器
    try:
        container = wait.until(
            EC.presence_of_element_located((By.XPATH, ARTICLE_CONTAINER_XPATH))
        )
    except TimeoutException:
        logger.error(f"找不到文章容器: {ARTICLE_CONTAINER_XPATH}（網址: {url}）")
        return []
    # 等待清單 div
    try:
        wait.until(lambda d: len(container.find_elements(By.XPATH, LIST_DIV_XPATH)) > 0)
        list_div = container.find_element(By.XPATH, LIST_DIV_XPATH)
    except TimeoutException:
        logger.error(f"找不到清單 div: {LIST_DIV_XPATH}（網址: {url}）")
        return []

    scroll_until_images_loaded(driver, list_div)

    try:
        nodes = list_div.find_elements(By.XPATH, LIST_NODES_XPATH)
    except StaleElementReferenceException:
        logger.warning("清單 div 失效，重新定位")
        container = wait.until(EC.presence_of_element_located((By.XPATH, ARTICLE_CONTAINER_XPATH)))
        list_div  = container.find_element(By.XPATH, LIST_DIV_XPATH)
        nodes     = list_div.find_elements(By.XPATH, LIST_NODES_XPATH)

    logger.debug(f"文章頁共掃描到 {len(nodes)} 個相關節點")
    #實際爬取
    results       = []
    current_image = None
    image_count   = 0
    text_count    = 0

    for node in nodes:
        try:
            tag = node.tag_name.lower() 

            if tag == 'img':    # img 標籤：暫存圖片網址
                current_image = node.get_attribute("data-src") or node.get_attribute("src")
                image_count  += 1
                continue

            if tag == 'p':      # p 內含圖片：暫存圖片網址
                imgs = node.find_elements(By.XPATH, './img')
                if imgs:
                    current_image = imgs[0].get_attribute("data-src") or imgs[0].get_attribute("src")
                    image_count  += 1
                    continue
                # p 內含文字連結：輸出一筆資料
                as_ = node.find_elements(By.XPATH, './a')
                if as_:
                    link_text = as_[0].text.strip()
                    link_href = as_[0].get_attribute("href") or ""
                    if link_text:
                        text_count += 1
                        for record in normalize_title(link_text, link_href):
                            results.append({
                                "name": record["name"],
                                "code": record["code"],
                                "image": current_image,
                                "raw_title": record["raw_title"],
                                "detail_url": record["detail_url"],
                            })     
        except Exception:
            logger.warning("處理單一節點時失敗，已略過該節點", exc_info=True)
            continue

    logger.info(f"文章頁擷取完成：{image_count} 張圖片、{text_count} 段文字，共正規化輸出 {len(results)} 筆資料")
    return results


# =====================================================================
# 儲存
# =====================================================================
def save_to_csv(data: list[dict], filename: str) -> None:
    if not data:
        logger.info("沒有資料可寫入 CSV")
        return
    try:
        with open(filename, "w", newline="", encoding="utf-8-sig") as f:
            fieldnames = ["name", "code", "image", "raw_title", "detail_url"]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(data)
        logger.info(f"已寫入 {len(data)} 筆資料至 {filename}")
    except Exception:
        logger.error(f"寫入 CSV 失敗: {filename}", exc_info=True)


# =====================================================================
# 主流程
# =====================================================================
def run_scraper():
    """
    執行一次完整的爬取流程：清單頁 -> 最新文章 -> 輸出 CSV。
    """
    start_time = time.time()
    logger.info("=" * 20 + " 程式開始執行 " + "=" * 20)

    driver = build_driver(headless=True)
    try:
        # ---清單頁 ---
        logger.info("[第一階段] 開啟清單頁，抓取文章網址清單")
        if not safe_get(driver, LIST_TARGET_URL):
            logger.error("清單頁導航失敗，程式中止")
            return
        WebDriverWait(driver, WAIT_TIMEOUT).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        human_like_scroll(driver)       # 模擬真人滾輪，避免觸發換頁
        links = scrape_list(driver)     # 抓取網址清單（依發布時間排序，[0] 為最新）

        if not links:
            logger.error("清單頁未抓到任何網址，程式中止")
            return

        latest = links[0]
        logger.info(f"清單頁共 {len(links)} 筆，鎖定最新一篇：《{latest['title']}》")

        # ---進入最新一篇文章內頁---
        logger.info("[第二階段] 進入最新一篇文章，抓取圖片 + 正規化文字資料")
        crawled_data = scrape_article(driver, latest["url"])

        # 爬取概況：只記錄統計數字，不記錄完整內容
        logger.info(f"最新文章《{latest['title']}》共擷取 {len(crawled_data)} 筆正規化後資料")

        save_to_csv(crawled_data, OUTPUT_CSV)

    except Exception:
        # 捕捉未預期例外，完整記錄 traceback 方便除錯
        logger.error("程式執行過程發生未預期例外", exc_info=True)

    finally:
        driver.quit()
        elapsed = time.time() - start_time
        logger.info(f"程式執行結束，總耗時 {elapsed:.1f} 秒")
        logger.info("=" * 50)


# =====================================================================
# 排程任務
# =====================================================================
def scheduled_job():
    """
    排程觸發後的實際任務：
      先隨機延遲一段時間（避免固定時間爬取被網站偵測為機器人），
      再執行一次完整爬取流程。
    """
    delay_seconds = random.randint(*RANDOM_DELAY_RANGE)
    now_str = datetime.now(TAIPEI_TZ).strftime("%H:%M:%S")
    logger.info(f"[{now_str}] 已進入排定時段，隨機等待 {delay_seconds} 秒後執行...")
    time.sleep(delay_seconds)

    exec_time = datetime.now(TAIPEI_TZ)
    logger.info(f"開始執行排程任務，實際觸發時間: {exec_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    run_scraper()


def setup_schedule() -> None:
    for day in SCHEDULE_DAYS:
        getattr(schedule.every(), day).at(SCHEDULE_TIME).do(scheduled_job)
    logger.info(
        f"排程已設定：每週 {'、'.join(SCHEDULE_DAYS)} {SCHEDULE_TIME}（台北時間）觸發，"
        f"觸發後隨機延遲 {RANDOM_DELAY_RANGE[0]}~{RANDOM_DELAY_RANGE[1]} 秒執行"
    )


if __name__ == "__main__":
    # 「單次」爬取流程
    if os.environ.get("GITHUB_ACTIONS") == "true":
        logger.info("偵測到 GitHub Actions 執行環境，執行單次爬取流程")
        run_scraper()
    else:
        # 本機／VS Code 執行時，維持原本的內部排程模式
        setup_schedule()
        logger.info("排程已啟動，等待執行...")
        while True:
            schedule.run_pending()
            time.sleep(1)