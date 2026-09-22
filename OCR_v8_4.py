import os
import re
import cv2
import numpy as np
import requests
from google.cloud import vision
from dotenv import load_dotenv

load_dotenv()  # 讀取同目錄下的 .env

# ============ 認證設定 ============
if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
    raise RuntimeError(
        "請設定環境變數 GOOGLE_APPLICATION_CREDENTIALS，指向 Google Vision 服務帳號金鑰檔路徑"
        "（本機可寫在 .env，GitHub Actions 由 workflow 的 Secrets 注入）"
    )

#圖片讀取(相容中文路徑/網址)
def imread_unicode(source: str):
    if source.startswith(("http://", "https://")):
        resp = requests.get(source, timeout=10)             #下載圖片
        resp.raise_for_status()                             #抓不到直接中斷
        buf = np.frombuffer(resp.content, dtype=np.uint8)   #取得圖片的二進位資料
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)           #例外處理

        if img is None:
            raise ValueError(f"圖片解碼失敗，請確認網址內容是否為有效圖片: {source}")
        return img

# ============ Google Vision API 呼叫與格式轉換 ============
_vision_client = None
_api_call_count = 0     # 記錄本次Vision API呼叫次數

def get_api_call_count():   #取的本次呼叫次數
    return _api_call_count  
def reset_api_call_count(): #將本次呼叫次數歸0
    global _api_call_count
    _api_call_count = 0

def get_vision_client():
    """延遲建立client，避免還沒設定好認證就報錯"""
    global _vision_client
    if _vision_client is None:
        _vision_client = vision.ImageAnnotatorClient()  #建立快取驗證憑證
    return _vision_client

def encode_for_api(img_bgr): 
    """把OpenCV的BGR圖片編碼成Vision API可以用得格式"""
    ok, buf = cv2.imencode('.png', img_bgr)
    if not ok:
        raise IOError("圖片編碼失敗，無法送往Vision API")
    return buf.tobytes()

def run_ocr_vision(img_bgr, language_hints=("zh-TW", "en")):
    """
    呼叫 Google Vision API 的 document_text_detection，
    回傳格式跟原本 tesseract 版一致的文字框清單:
    [{'text','conf','x','y','w','h'}, ...]
    """
    client = get_vision_client()        #驗證函數
    content = encode_for_api(img_bgr)   #圖片轉換函數
    image = vision.Image(content=content)
    image_context = vision.ImageContext(language_hints=list(language_hints))    #通告圖片內容有中、英文
    global _api_call_count  #API計數器
    _api_call_count += 1 
    ##密集文件文字辨識模式##
    response = client.document_text_detection(image=image, image_context=image_context)
    if response.error.message:
        raise RuntimeError(f"Vision API 錯誤: {response.error.message}")

    boxes = []  #文字座標容器
    annotation = response.full_text_annotation
    for page in annotation.pages:
        for block in page.blocks:
            for paragraph in block.paragraphs:
                for word in paragraph.words:
                    text = ''.join(s.text for s in word.symbols) #文字結果拼接
                    if not text.strip():
                        continue
                    verts = word.bounding_box.vertices           #計算外框座標
                    xs = [v.x for v in verts]
                    ys = [v.y for v in verts]
                    x0, y0 = min(xs), min(ys)
                    w, h = max(xs) - x0, max(ys) - y0
                    conf = word.confidence * 100                 #信心值標準化(統一成0-100)
                    boxes.append({'text': text, 'conf': conf, 'x': x0, 'y': y0, 'w': w, 'h': h})
    return boxes

#評估兩個方框是否有重疊 (去重與防止重複計算)
def box_iou(a, b):  
    ax1, ay1, ax2, ay2 = a['x'], a['y'], a['x'] + a['w'], a['y'] + a['h']
    bx1, by1, bx2, by2 = b['x'], b['y'], b['x'] + b['w'], b['y'] + b['h']
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    union = a['w'] * a['h'] + b['w'] * b['h'] - inter
    return inter / union if union > 0 else 0

#偵測字元大小(字元重複問題)
def containment_ratio(inner, outer):
    ax1, ay1, ax2, ay2 = inner['x'], inner['y'], inner['x'] + inner['w'], inner['y'] + inner['h']
    bx1, by1, bx2, by2 = outer['x'], outer['y'], outer['x'] + outer['w'], outer['y'] + outer['h']
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    inner_area = inner['w'] * inner['h']
    return inter / inner_area if inner_area > 0 else 0

#文字框去重與碎片過濾器
def dedupe_with_containment(boxes):
    """Vision API本身不太會像tesseract那樣重複輸出碎片，但保留這層作為保險"""
    boxes_sorted = sorted(boxes, key=lambda b: -len(b['text']))
    kept = []  #保留需要地字元
    for b in boxes_sorted:
        is_fragment = any(
            k is not b and containment_ratio(b, k) > 0.7 and len(b['text']) < len(k['text'])
            for k in kept
        )
        if not is_fragment:
            kept.append(b)
    final = []  #保留信心度高的字元
    for b in sorted(kept, key=lambda b: -b['conf']):
        if not any(box_iou(b, f) > 0.3 for f in final):
            final.append(b)
    return final

#背景色彩過濾器
def is_white_background(img_bgr, box, pad=2):
    x, y, w, h = int(box['x']), int(box['y']), int(box['w']), int(box['h'])
    y0, y1 = max(y - pad, 0), min(y + h + pad, img_bgr.shape[0])
    x0, x1 = max(x - pad, 0), min(x + w + pad, img_bgr.shape[1])
    roi = img_bgr[y0:y1, x0:x1]
    if roi.size == 0:
        return True
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)  #轉換至 HSV 色彩空間
    highlight_mask = (hsv[:, :, 0] >= 20) & (hsv[:, :, 0] <= 75) & (hsv[:, :, 1] > 70)
    vivid_packaging_mask = hsv[:, :, 1] > 80
    not_white_mask = highlight_mask | vivid_packaging_mask
    return not_white_mask.mean() < 0.15


CODE_STRICT = re.compile(r'^\D{0,2}(\d{5,7})\D{0,2}$')  # Costco商品編號常見為5~7位數字
CODE_LOOSE = re.compile(r'(\d{5,7})')

#每張圖最多允許幾次recovery呼叫(硬上限)
MAX_RECOVERY_CALLS_PER_IMAGE = 3

#快速防呆過濾器
def is_plausible_code(code_str):#快速防呆過濾器
    if code_str[0] == '0':          #排除0開頭的數字
        return False
    if len(set(code_str)) <= 1:     #排除全部相同數字
        return False
    return True

##商品編號提取與救援引擎##
def extract_codes(img_bgr, deduped_boxes, enable_recovery=True):
    confirmed, suspects = [], []
    implausible_code_like = set()  
    for b in deduped_boxes:
        m = CODE_STRICT.match(b['text'])
        if m:   #商品編號條件及除外機制
            code_candidate = m.group(1)
            if b['conf'] >= 75 and is_white_background(img_bgr, b):
                if is_plausible_code(code_candidate):
                    confirmed.append({**b, 'code': code_candidate})
                else:
                    implausible_code_like.add(code_candidate)   # 高信心度通過+防呆
        else:
            if '/' in b['text']:                                # 排除日期
                continue
            m2 = CODE_LOOSE.search(b['text'])                   #切圖救援
            if m2:
                suspects.append(b)

    prefixes = [c['code'][:2] for c in confirmed]
    common_prefix = max(set(prefixes), key=prefixes.count) if prefixes else None
    #切圖救援
    if enable_recovery:
        recovery_calls_used = 0
        for b in suspects:
            if recovery_calls_used >= MAX_RECOVERY_CALLS_PER_IMAGE: #重發上限
                break
            if not is_white_background(img_bgr, b):
                continue
            x, y, w, h = int(b['x']), int(b['y']), int(b['w']), int(b['h'])
            pad = 6
            y0, y1 = max(y - pad, 0), y + h + pad
            x0, x1 = max(x - pad, 0), x + w + pad
            crop = img_bgr[y0:y1, x0:x1]                            # 切出局部圖片
            if crop.size == 0:
                continue
            recovery_calls_used += 1                                # 重試計數器
            try:
                sub_boxes = run_ocr_vision(crop, language_hints=("en",))
            except Exception:
                continue
            joined = ''.join(sb['text'] for sb in sub_boxes)
            m3 = re.fullmatch(r'\d{5,7}', joined)
            recovered = m3.group() if m3 else None
            if (recovered and is_plausible_code(recovered)
                    and (not common_prefix or recovered[:2] == common_prefix)):
                confirmed.append({**b, 'code': recovered, 'recovered': True})

    ##去重與三重防呆機制##
    #位置去重
    final = [] 
    for c in sorted(confirmed, key=lambda b: -b['conf']):
        if not any(box_iou(c, f) > 0.2 for f in final):
            final.append(c)
    # 防止編號被誤判成特價
    all_code_like_strings = {c['code'] for c in confirmed} | implausible_code_like
    # 字體高度防呆
    if len(final) >= 2:
        max_h = max(c['h'] for c in final)
        final = [c for c in final if c['h'] >= max_h * 0.45]

    return final, all_code_like_strings

#空間歸屬匹配器(處理多張價格牌)
def assign_to_nearest_code(box, code_boxes, regions=None, max_distance=None):
    #距離計分
    def score(i):
        c = code_boxes[i]
        dx = abs((box['x'] + box['w'] / 2) - (c['x'] + c['w'] / 2))
        dy = box['y'] - c['y']
        return dx * 1.0 + dy * 0.15
    #邊界範圍估計
    if regions is not None and len(regions) == len(code_boxes):
        cx = box['x'] + box['w'] / 2
        cy = box['y'] + box['h'] / 2
        in_region = []
        for i, r in enumerate(regions):
            if r is None:
                continue
            pad_x = (r['x1'] - r['x0']) * 0.1
            if r['x0'] - pad_x <= cx <= r['x1'] + pad_x and cy >= r['y0'] - 8:
                in_region.append(i)
        if in_region:
            return min(in_region, key=score)
        # 邊界限制，只用距離+max_distance防呆)。
        
    #條件過濾：文字框高於商品編號及捨去
    candidates = [i for i, c in enumerate(code_boxes) if box['y'] >= c['y'] - 8]
    if not candidates:
        return None
    #距離過遠防呆
    best_idx = min(candidates, key=score)
    best_score = score(best_idx)
    if max_distance is not None and best_score > max_distance:
        return None
    return best_idx

#橫向拼接器(特價字元拼接)
def merge_same_line_numbers_simple(boxes):
    boxes = sorted(boxes, key=lambda b: (round(b['y'] / 8), b['x']))
    merged, used = [], [False] * len(boxes)
    for i in range(len(boxes)):
        if used[i]:
            continue
        cur = dict(boxes[i])
        used[i] = True
        for j in range(i + 1, len(boxes)):
            if used[j]:
                continue
            nxt = boxes[j]
            same_line = abs(nxt['y'] - cur['y']) < max(cur['h'], nxt['h']) * 0.6     #條件一：是否在同行
            gap = nxt['x'] - (cur['x'] + cur['w'])
            close_enough = 0 <= gap < min(cur['h'], nxt['h']) * 1.2                  #條件二：水平距離是否夠近
            if same_line and close_enough:
                cur['text'] += nxt['text']                                           #拼接
                cur['w'] = max(cur['x'] + cur['w'], nxt['x'] + nxt['w']) - cur['x']  #寬度
                cur['h'] = max(cur['h'], nxt['h'])                                   #高度
                used[j] = True                                                       #重新組合成一個文字框
        merged.append(cur)
    return merged

#============OCR識別引擎==========
def classify_group_fields(group_boxes, code_str, debug=False, img_h=None, img_bgr=None,
                           other_code_like_strings=None):
    #====雜質、容器設定====
    result = {'model': None, 'sale_price': None, 'sale_price_height': 0, 'sale_price_suspect': False,
              'sold_out': False, 'chinese_name': None, 'chinese_name_suspect': False,
              'sale_price_conf': None, 'sale_price_digit_count': None,
              'sale_price_candidate_count': None, 'sale_price_margin_ratio': None,
              'has_original_and_discount': False, 'original_price': None, 'discount_price': None}
    #偵測售完狀態
    for b in group_boxes:   
        if '已售' in b['text'] or '商品已售完' in b['text']:
            result['sold_out'] = True
    #型號判定
    model_candidates = [b for b in group_boxes if re.search(r'[A-Za-z]', b['text']) and '/' in b['text']]
    if model_candidates:
        result['model'] = max(model_candidates, key=lambda b: b['conf'])['text']

    #停用字
    BOILERPLATE_KEYWORDS = ['活動', '數量有限', '售完為止', '為止', '含稅',
                             '基本安裝', '配送服務', '系列', '優惠期間', '累計總優惠',
                             '請先', '醫療專業人員', '再食用',
                             '信用卡', '銀行', '年利率', '回饋', '房貸', '消費',
                             '無糖無香料無熱量', '無添加', '不含防腐劑']
    QUANTITY_UNIT_WORDS = ['公克', '公斤', '公升', '毫升', '公分', '公尺',
                            '吋', '入', '片', '組', '份', '包', '盒', '罐', '瓶']
    #品名規格/數量單位過濾器
    def is_pure_quantity_spec(text):
        stripped = text
        for u in QUANTITY_UNIT_WORDS:                               #單位
            stripped = stripped.replace(u, '')
        stripped = re.sub(r'[\d,\.\(\)（）\s]', '', stripped)       #數字、標點符號、空白
        return len(re.findall(r'[\u4e00-\u9fff]', stripped)) == 0
    #====中文判斷====
    #(中文)雜訊過濾邏輯
    def is_boilerplate(text):
        if '每' in text:                                            #特定字排除
            return True
        if is_pure_quantity_spec(text):                             #品名規格/數量單位排除
            return True
        return any(kw in text for kw in BOILERPLATE_KEYWORDS)       #關鍵字排除
    cjk_anchor_boxes = [b for b in group_boxes if re.search(r'[\u4e00-\u9fff]', b['text'])]   #組合成中文字框

    #運用英文對比出中文位置
    english_line_boxes = [
        b for b in group_boxes
        if re.fullmatch(r'[A-Za-z\s]+', b['text']) and len(b['text']) >= 3 and b['w'] >= 50
        and b['text'].strip().upper() != 'COSTCO'  # 排除Costco自己的店名/廣告橫幅文字，
    ]
    #利用英文定位排除頂部雜訊
    min_y_after_english = None
    if english_line_boxes:
        min_y_after_english = min(b['y'] for b in english_line_boxes)
        cjk_anchor_boxes = [b for b in cjk_anchor_boxes if b['y'] >= min_y_after_english - 5]

    #過濾浮水印
    clean_group_boxes = group_boxes
    if cjk_anchor_boxes:
        heights = sorted(b['h'] for b in cjk_anchor_boxes)      #所有中文字框的高度
        p30_idx = max(int(len(heights) * 0.3) - 1, 0)           #設定基準字體高度
        baseline_h = heights[p30_idx]
        if img_h is not None:
            baseline_h = max(baseline_h, img_h * 0.02)          #圖片高度
        height_limit = baseline_h * 2.5                         #設定判斷基準
        clean_group_boxes = [b for b in group_boxes if b['h'] <= height_limit]      #超過基準排除
        cjk_anchor_boxes = [b for b in cjk_anchor_boxes if b['h'] <= height_limit]  #超過基準排除
    #將識別到的文字框已列排序
    if cjk_anchor_boxes:
        cjk_boxes_sorted = sorted(cjk_anchor_boxes, key=lambda b: b['y'])
        seen_y = set()
        for b in cjk_boxes_sorted:
            y_key = round(b['y'] / 15)
            if y_key in seen_y:
                continue
            seen_y.add(y_key)
            same_y_boxes = [
                c for c in clean_group_boxes
                if abs(c['y'] - b['y']) < 15 and ',' not in c['text']
            ]
            same_y_boxes.sort(key=lambda c: c['x'])

            #運用水平間距聚類排除離群值(雜訊)
            best_candidate_name = None
            best_candidate_name_raw = None
            best_cjk_count = 0
            tried_anchor_indices = set()
            for anchor_idx in range(len(same_y_boxes)):
                if anchor_idx in tried_anchor_indices:
                    continue
                start = end = anchor_idx
                while (start > 0 and same_y_boxes[start]['x'] -
                       (same_y_boxes[start-1]['x'] + same_y_boxes[start-1]['w']) < 40):
                    start -= 1
                while (end < len(same_y_boxes) - 1 and same_y_boxes[end+1]['x'] -
                       (same_y_boxes[end]['x'] + same_y_boxes[end]['w']) < 40):
                    end += 1
                tried_anchor_indices.update(range(start, end + 1))
                #文字清洗
                segment = same_y_boxes[start:end+1]
                seg_raw = ''.join(c['text'] for c in segment)
                seg_clean = re.sub(r'[^\u4e00-\u9fff0-9]', '', seg_raw)
                m_leading_seg = re.match(r'^(\d*)([\u4e00-\u9fff].*)$', seg_clean)
                if m_leading_seg:   #排除文正前後英文、數字
                    leading_digits_seg, rest_seg = m_leading_seg.groups()
                    seg_clean = leading_digits_seg + re.sub(r'\d', '', rest_seg)
                #清洗後評分
                seg_cjk_count = len(re.findall(r'[\u4e00-\u9fff]', seg_clean))
                if seg_cjk_count > best_cjk_count and not is_boilerplate(seg_raw):
                    best_cjk_count = seg_cjk_count
                    best_candidate_name = seg_clean
                    best_candidate_name_raw = seg_raw

            #結果寫入與品質診斷
            candidate_name = best_candidate_name
            candidate_name_raw = best_candidate_name_raw
            cjk_char_count = best_cjk_count
            #將分數高的寫入
            if candidate_name is not None and cjk_char_count >= 1:
                result['chinese_name'] = candidate_name
                if cjk_char_count <= 3:                 # 疑似有問題的標記
                    result['chinese_name_suspect'] = True
                break
    #====特價判斷====
    #篩選出所有可能是價格的數字框
    numeric_boxes = [
        b for b in group_boxes
        if re.fullmatch(r'[\d,\$\-–]+', b['text']) and len(re.sub(r'\D', '', b['text'])) >= 2
    ]#過濾商品編號
    numeric_boxes = [b for b in numeric_boxes if re.sub(r'\D', '', b['text']) != code_str]
    #雜訊清除
    if other_code_like_strings:
        numeric_boxes = [
            b for b in numeric_boxes
            if re.sub(r'\D', '', b['text']) not in other_code_like_strings
        ]
    #設定特價限定位數
    numeric_boxes = [b for b in numeric_boxes if len(re.sub(r'\D', '', b['text'])) <= 6]
    numeric_boxes = merge_same_line_numbers_simple(numeric_boxes)
    # 合併後必須再檢查
    numeric_boxes = [b for b in numeric_boxes if len(re.sub(r'\D', '', b['text'])) <= 6]
    #特價診斷日誌
    if debug:
        print(f"\n  --- 編號 {code_str} 的診斷資料 ---")
        print(f"  這組全部文字框(共{len(group_boxes)}個):")
        for b in sorted(group_boxes, key=lambda b: (b['y'], b['x'])):
            print(f"    text='{b['text']}' x={b['x']:.0f} y={b['y']:.0f} w={b['w']:.0f} h={b['h']:.0f} conf={b['conf']:.0f}")
        print(f"  同行合併並過濾超長異常後的數字候選:")
        for b in numeric_boxes:
            print(f"    text='{b['text']}' x={b['x']:.0f} y={b['y']:.0f} h={b['h']:.0f}")

    # 設定特價候選要求位數在3~6位之間
    sale_candidates = [
        b for b in numeric_boxes
        if 3 <= len(re.sub(r'\D', '', b['text'])) <= 6
    ]
    #顏色檢測及白底過濾
    if img_bgr is not None:
        sale_candidates = [b for b in sale_candidates if is_white_background(img_bgr, b)]

    #塞選最大字體
    if sale_candidates:
        sorted_candidates = sorted(sale_candidates, key=lambda b: -b['h'])
        sale_box = sorted_candidates[0]
        result['sale_price'] = re.sub(r'\D', '', sale_box['text'])
        result['sale_price_height'] = sale_box['h']
        #信心分數計算
        result['sale_price_conf'] = sale_box.get('conf')
        result['sale_price_digit_count'] = len(result['sale_price'])
        result['sale_price_candidate_count'] = len(sorted_candidates)
        #比分方式
        if len(sorted_candidates) >= 2:
            second_h = sorted_candidates[1]['h']
            result['sale_price_margin_ratio'] = (
                (sale_box['h'] - second_h) / sale_box['h'] if sale_box['h'] > 0 else None
            )
        else:
            result['sale_price_margin_ratio'] = None

        # 交叉驗證：定位原價、折價
        remaining_for_check = [b for b in numeric_boxes if b is not sale_box]
        original_like = [   #定位原價
            b for b in remaining_for_check
            if '-' not in b['text'] and b['y'] < sale_box['y']
            and 3 <= len(re.sub(r'\D', '', b['text'])) <= 6
        ]
        original_val_box = min(original_like, key=lambda b: b['y']) if original_like else None
        discount_like = [   #定位折價
            b for b in remaining_for_check
            if b is not original_val_box
            and 2 <= len(re.sub(r'\D', '', b['text'])) <= 5
            and (original_val_box is None or b['y'] > original_val_box['y'])
            and b['y'] < sale_box['y']
        ]
        # 【新增】不管後面交叉驗證的數學對不對得上，先記錄「這組有沒有

        result['has_original_and_discount'] = bool(original_like and discount_like) #是否有原價、折扣、特價三組數字
        if original_like and discount_like:     #型態轉換
            original_val = int(re.sub(r'\D', '', min(original_like, key=lambda b: b['y'])['text']))
            discount_val = int(re.sub(r'\D', '', max(discount_like, key=lambda b: b['y'])['text']))
            result['original_price'] = original_val
            result['discount_price'] = discount_val
            expected_sale = original_val - discount_val
            actual_sale = int(result['sale_price'])
            if expected_sale > 0 and expected_sale != actual_sale:  #異常標記
                result['sale_price_suspect'] = True
                if debug:
                    print(f"  -> [可疑]原價{original_val}-折扣{discount_val}="
                          f"{expected_sale}，跟選中的特價{actual_sale}不一致"
                          f"(不自動修正，因為無法可靠判斷是特價多讀還是原價/折扣本身誤讀)")
        #診斷日誌輸出
        if debug:
            print(f"  -> 選中特價框: text='{sale_box['text']}' h={sale_box['h']:.0f}")

    return result

#多價格牌偵測
def estimate_tag_regions(code_boxes, img_shape):
    img_h, img_w = img_shape[:2]
    if not code_boxes:
        return []
    idx_by_id = {id(cb): i for i, cb in enumerate(code_boxes)}
    #識別到的商品編號二維化
    sorted_by_y = sorted(code_boxes, key=lambda b: b['y'])
    rows, row_tol = [], 60
    for b in sorted_by_y:
        placed = False
        for row in rows:
            if abs(row[0]['y'] - b['y']) < row_tol:
                row.append(b)
                placed = True
                break
        if not placed:
            rows.append([b])
    for row in rows:
        row.sort(key=lambda b: b['x'])
    rows.sort(key=lambda row: row[0]['y'])
    #計算兩張價格牌距離中位數
    all_gaps_x = [row[i+1]['x'] - row[i]['x'] for row in rows for i in range(len(row) - 1)]
    default_w = int(np.median(all_gaps_x)) if all_gaps_x else int(img_w * 0.3)
    all_gaps_y = [rows[i+1][0]['y'] - rows[i][0]['y'] for i in range(len(rows) - 1)]
    default_h = int(np.median(all_gaps_y)) if all_gaps_y else int(img_h * 0.3)
    #計算價格牌邊界
    regions = []
    for ri, row in enumerate(rows):
        for ci, b in enumerate(row):
            tag_w = (row[ci+1]['x'] - b['x']) if ci + 1 < len(row) else default_w
            tag_h = (rows[ri+1][0]['y'] - b['y']) if ri + 1 < len(rows) else default_h
            tag_w = max(tag_w, default_w * 0.6)
            tag_h = max(tag_h, default_h * 0.6)
            x0 = max(int(b['x']) - 15, 0)
            y0 = max(int(b['y']) - 10, 0)
            x1 = min(int(b['x'] + tag_w) - 5, img_w)
            y1 = min(int(b['y'] + tag_h) - 5, img_h)
            regions.append({'code': b['code'], 'idx': idx_by_id[id(b)],
                             'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1})
    return regions
#將偵測到的多價格牌編列順序清單
def align_regions_to_code_boxes(regions, code_boxes):
    by_idx = {r['idx']: r for r in regions}
    return [by_idx.get(i) for i in range(len(code_boxes))]


# ============ 主流程 ============

def process_image(source, enable_code_recovery=True, debug_codes=None):
    img = imread_unicode(source)        #1.抓圖片
    all_boxes = run_ocr_vision(img)     #2.進OCR
    deduped = dedupe_with_containment(all_boxes)    
    code_boxes, all_code_like_strings = extract_codes(img, deduped, enable_recovery=enable_code_recovery) #3.識別商品編號
    # 救援日誌：找不到商品編號
    if not code_boxes:
        print("  [診斷] 這張圖沒有偵測到任何商品編號，列出所有含4位數以上數字的候選框:")
        digit_candidates = [b for b in deduped if len(re.sub(r'\D', '', b['text'])) >= 4]
        if not digit_candidates:
            print("    (完全沒有任何數字片段被偵測到，可能是Vision API整體辨識失敗，或編號被裁切在圖片外)")
        for b in sorted(digit_candidates, key=lambda b: (b['y'], b['x'])):
            print(f"    text='{b['text']}' x={b['x']:.0f} y={b['y']:.0f} w={b['w']:.0f} h={b['h']:.0f} conf={b['conf']:.0f}")

    #4.商品編號本身從識別文字框排除
    non_code_boxes = [b for b in deduped if not any(box_iou(b, c) > 0.3 for c in code_boxes)]
    #5.定位每一張價格牌
    regions = estimate_tag_regions(code_boxes, img.shape)
    regions_aligned = align_regions_to_code_boxes(regions, code_boxes)
    #6.依據每個識別到商品編號給予容器
    groups_by_instance = {i: [] for i in range(len(code_boxes))}
    img_h, img_w = img.shape[:2]
    max_assign_distance = max(img_h, img_w) * 0.5
    for b in non_code_boxes:
        idx = assign_to_nearest_code(b, code_boxes, regions=regions_aligned, max_distance=max_assign_distance)
        if idx is not None:
            groups_by_instance[idx].append(b)

    #7.紀錄識別到之訊息
    instance_results = []
    for idx, code_box in enumerate(code_boxes):
        code = code_box['code']
        want_debug = debug_codes == 'all' or (debug_codes and code in debug_codes)
        fields = classify_group_fields(
            groups_by_instance[idx], code, debug=want_debug, img_h=img_h, img_bgr=img,
            other_code_like_strings=all_code_like_strings - {code}
        )
        fields['code'] = code
        instance_results.append(fields)
    #8.過濾器及異常標記
    merged_by_code = {}
    for fields in instance_results:
        code = fields['code']
        if code not in merged_by_code:
            merged_by_code[code] = dict(fields)
        else:
            target = merged_by_code[code]
            for key in ('model', 'chinese_name'):
                if target.get(key) is None and fields.get(key) is not None:
                    target[key] = fields[key]
            if target.get('chinese_name_suspect') is False and fields.get('chinese_name_suspect'):
                target['chinese_name_suspect'] = True
            if target.get('sale_price') is None and fields.get('sale_price') is not None:
                target['sale_price'] = fields['sale_price']
                target['sale_price_height'] = fields.get('sale_price_height', 0)
                target['sale_price_suspect'] = fields.get('sale_price_suspect', False)
                for k in ('sale_price_conf', 'sale_price_digit_count',
                          'sale_price_candidate_count', 'sale_price_margin_ratio',
                          'has_original_and_discount', 'original_price', 'discount_price'):
                    target[k] = fields.get(k)
            elif (target.get('sale_price') is not None and fields.get('sale_price') is not None
                  and fields.get('sale_price_height', 0) > target.get('sale_price_height', 0)):
                target['sale_price'] = fields['sale_price']
                target['sale_price_height'] = fields['sale_price_height']
                target['sale_price_suspect'] = fields.get('sale_price_suspect', False)
                for k in ('sale_price_conf', 'sale_price_digit_count',
                          'sale_price_candidate_count', 'sale_price_margin_ratio',
                          'has_original_and_discount', 'original_price', 'discount_price'):
                    target[k] = fields.get(k)
            if fields.get('sold_out'):
                target['sold_out'] = True
    all_candidates = list(merged_by_code.values())
    #9.完整性檢查
    results = []
    dropped = []
    for fields in all_candidates:
        is_complete = (
            fields.get('code') is not None
            and fields.get('sale_price') is not None
            and fields.get('chinese_name') is not None
        )
        if is_complete:
            results.append(fields)
        else:
            dropped.append(fields)
    #回傳結果
    return results, code_boxes, regions, dropped

#繪製識別方框(批樣測試、部屬關閉)
def save_debug_visualization(source, code_boxes, regions):
    img = imread_unicode(source)
    viz = img.copy()
    for c in code_boxes:
        x, y, w, h = int(c['x']), int(c['y']), int(c['w']), int(c['h'])
        color = (0, 200, 255) if c.get('recovered') else (0, 255, 0)
        cv2.rectangle(viz, (x - 3, y - 3), (x + w + 3, y + h + 3), color, 3)
        cv2.putText(viz, c['code'], (x, max(y - 8, 0)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
    for r in regions:
        cv2.rectangle(viz, (r['x0'], r['y0']), (r['x1'], r['y1']), (255, 0, 0), 2)

#批樣處理容錯機制
def process_single_image(source, debug_codes=None):
    try:
        results, code_boxes, regions, dropped = process_image(source, debug_codes=debug_codes)
        return results, dropped, None
    except Exception as e:  #發生錯誤則回傳錯誤訊息
        return [], [], str(e)
#讀取txt的圖片網址
def read_url_list(path):
    with open(path, 'r', encoding='utf-8') as f:
        lines = [line.strip() for line in f]
    return [line for line in lines if line and not line.startswith('#')]

#匯出引擎
def run_batch(url_list_path=None, urls=None, output_dir=".", debug_codes=None, write_files=True):
    """批次處理一批網址。"""
    import csv as csv_module
    from datetime import datetime

    if urls is None:
        urls = read_url_list(url_list_path)
    reset_api_call_count()  # 每次執行都重新歸零計數
    print(f"共讀取到 {len(urls)} 個網址，開始批次處理...\n")

    all_results = []    #識別成功的容器
    all_dropped = []    #欄位殘缺捨棄資料的容器
    all_errors = []     #處理失敗的容器

    for i, url in enumerate(urls, start=1):
        print(f"[{i}/{len(urls)}] 處理中: {url}")
        results, dropped, error = process_single_image(url, debug_codes=debug_codes)#指定商品編號列出該文字框詳情

        #判斷處理三邏輯
        if error:           #處理失敗
            print(f"    -> 失敗: {error}")
            all_errors.append({'url': url, 'error': error})
            continue
        for r in results:   #成功資料
            r['source_url'] = url
            all_results.append(r)
        for d in dropped:   #殘缺資料診斷
            missing = []
            if d.get('code') is None:
                missing.append('編號')
            if d.get('sale_price') is None:
                missing.append('特價')
            if d.get('chinese_name') is None:
                missing.append('中文名稱')
            d['source_url'] = url
            d['missing_fields'] = '+'.join(missing) if missing else '未知'
            all_dropped.append(d)

        print(f"    -> 完整 {len(results)} 筆，捨棄 {len(dropped)} 筆")

    #診斷欄位定義:信心分數、數字位數、潛在價格候選字框數、文字距離與邊界比例、是否同時包含原價與折扣
    diagnostic_fields = ['sale_price_conf', 'sale_price_digit_count',
                          'sale_price_candidate_count', 'sale_price_margin_ratio',
                          'has_original_and_discount', 'original_price', 'discount_price']

    ## 高風險清單：雙重風險條件判定:特價異常+名稱異常（無論寫不寫檔都要算出來，回傳給 orchestrator 判斷是否要人工複核）
    high_risk_rows = [
        r for r in all_results
        if r.get('sale_price_suspect') and r.get('chinese_name_suspect')
    ]

    # 摘要統計（本次實際用掉的 Vision API 呼叫次數）
    total_api_calls = get_api_call_count()

    results_path = dropped_path = errors_path = high_risk_path = None
    if write_files:
        # 輸出結果CSV（維持原本 CLI 用法，供手動除錯時仍可落地成檔案）
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        results_path = f"{output_dir}/results_{timestamp}.csv"
        dropped_path = f"{output_dir}/dropped_{timestamp}.csv"
        errors_path = f"{output_dir}/errors_{timestamp}.csv"
        high_risk_path = f"{output_dir}/high_risk_{timestamp}.csv"

        #識別成功表格格式
        with open(results_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv_module.DictWriter(
                f, fieldnames=['code', 'sale_price', 'sale_price_suspect', 'sold_out', 'chinese_name',
                               'chinese_name_suspect', 'source_url'] + diagnostic_fields
            )
            writer.writeheader()
            for r in all_results:
                writer.writerow({k: r.get(k) for k in writer.fieldnames})
        #欄位殘缺捨棄資料格式
        with open(dropped_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv_module.DictWriter(
                f, fieldnames=['code', 'sale_price', 'chinese_name', 'missing_fields', 'source_url']
            )
            writer.writeheader()
            for d in all_dropped:
                writer.writerow({k: d.get(k) for k in writer.fieldnames})
        #錯誤資料格式
        with open(errors_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv_module.DictWriter(f, fieldnames=['url', 'error'])
            writer.writeheader()
            for e in all_errors:
                writer.writerow(e)
        #風險清單格式設定
        with open(high_risk_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv_module.DictWriter(
                f, fieldnames=['code', 'sale_price', 'sale_price_suspect', 'sold_out', 'chinese_name',
                               'chinese_name_suspect', 'source_url'] + diagnostic_fields
            )
            writer.writeheader()
            for r in high_risk_rows:
                writer.writerow({k: r.get(k) for k in writer.fieldnames})

        # 把本次實際用掉的 API 呼叫數落地成檔案，供正規化II腳本讀取回寫 api_usage_monthly
        with open(f"{output_dir}/api_calls_used.txt", 'w', encoding='utf-8') as f:
            f.write(str(total_api_calls))

    print(f"\n{'='*50}")
    print(f"批次處理完成")
    print(f"  處理圖片數: {len(urls)}")
    print(f"  處理失敗: {len(all_errors)} 張")
    print(f"  完整識別成功: {len(all_results)} 筆商品")
    print(f"  其中『雙重可疑』(特價+名稱兩個都 True)：{len(high_risk_rows)} 筆，建議優先人工複核"
          + (f"，見 {high_risk_path}" if write_files else ""))
    print(f"  欄位不齊全被捨棄: {len(all_dropped)} 筆")
    if urls:
        print(f"  實際Vision API呼叫次數: {total_api_calls} 次"
              f"(平均每張圖 {total_api_calls/len(urls):.2f} 次，"
              f"其中 {len(urls)} 次是主要識別，其餘 {total_api_calls - len(urls)} 次為救援呼叫)")
    if all_dropped:
        from collections import Counter
        reason_counts = Counter(d['missing_fields'] for d in all_dropped)
        print(f"  捨棄原因分布:")
        for reason, count in reason_counts.most_common():
            print(f"    缺「{reason}」: {count} 筆")
    if write_files:
        print(f"\n已輸出:")
        print(f"  {results_path}") #成功檔
        print(f"  {dropped_path}") #缺失檔
        print(f"  {errors_path}")  #錯誤檔
        print(f"  {high_risk_path}")    #風險清單

    return all_results, all_dropped, all_errors, total_api_calls, high_risk_rows

if __name__ == "__main__":
    #批樣測試
    run_batch("to_ocr_urls.txt")