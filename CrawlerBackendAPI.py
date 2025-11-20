## 啟動指令：uvicorn CrawlerBackendAPI:app --reload

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import cloudscraper
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import time
import random
import re

app = FastAPI()

# --- 設定 CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 工具：取得台灣時間 (UTC+8) ---
# Render 伺服器是 UTC+0，必須手動加 8 小時，否則日期比對會全錯
def get_taiwan_now():
    return datetime.utcnow() + timedelta(hours=8)

# --- 隨機 User-Agent 池 ---
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

# --- 建立 Cloudscraper Session ---
def create_scraper():
    # 建立一個能繞過 Cloudflare 的 scraper
    scraper = cloudscraper.create_scraper(
        browser={
            'browser': 'chrome',
            'platform': 'windows',
            'desktop': True
        }
    )
    # 加入重試機制
    retries = Retry(total=3, backoff_factor=2, status_forcelist=[500, 502, 503, 504, 520, 522])
    adapter = HTTPAdapter(max_retries=retries)
    scraper.mount('http://', adapter)
    scraper.mount('https://', adapter)
    return scraper

# --- 解析 PTT 日期 (強制使用台灣年份) ---
def parse_ptt_date(date_str):
    try:
        date_str = date_str.strip()
        now_tw = get_taiwan_now()
        current_year = now_tw.year
        
        msg_month, msg_day = map(int, date_str.split('/'))
        post_date = datetime(current_year, msg_month, msg_day)
        
        # 跨年處理：如果現在是 1月，文章是 12月，那是去年的
        if now_tw.month < 6 and msg_month > 6:
            post_date = post_date.replace(year=current_year - 1)
            
        return post_date
    except Exception as e:
        # 解析失敗回傳當下時間
        return get_taiwan_now()

# --- 單一看板爬取邏輯 ---
def crawl_single_ptt_board(board: str, keyword: str, logic: str, target_start: datetime, target_end: datetime):
    print(f"  > [PTT-{board}] 開始抓取...")
    base_url = f"https://www.ptt.cc/bbs/{board}/index.html"
    
    scraper = create_scraper()
    
    keywords = []
    if keyword:
        keywords = [k.strip().lower() for k in keyword.replace(',', ' ').split() if k.strip()]
    
    posts = []
    current_url = base_url
    page_count = 0
    # 降低翻頁數以免太快被鎖，若穩定可再調高
    max_pages_safety = 10 
    stop_crawling = False

    while not stop_crawling and page_count < max_pages_safety:
        try:
            # 隨機延遲 1~3 秒，裝作是真人在看
            time.sleep(random.uniform(1.0, 3.0))
            
            # 隨機切換 UA
            headers = {
                "User-Agent": random.choice(USER_AGENTS),
                "Referer": current_url
            }
            
            # 使用 scraper 發送請求 (會自動帶 cookies)
            response = scraper.get(current_url, headers=headers, cookies={"over18": "1"}, timeout=15)
            
            if response.status_code != 200:
                print(f"    - [PTT-{board}] 請求失敗: {response.status_code} (URL: {current_url})")
                # 如果是 403，可能是 cloudscraper 也擋不住，或 IP 黑名單
                if response.status_code == 403:
                    print("    - [警告] 遭遇 403 Forbidden，可能需要更換 IP 或等待冷卻")
                break
                
            soup = BeautifulSoup(response.text, "html.parser")
            divs = soup.find_all("div", class_="r-ent")
            
            if not divs:
                # 沒抓到文章 div，可能是被引導到驗證頁面
                print(f"    - [PTT-{board}] 異常：抓不到文章列表，可能被驗證頁擋住")
                break

            page_posts = []
            
            for div in divs:
                if "deleted" in div.get("class", []): continue
                
                date_div = div.find("div", class_="date")
                if not date_div: continue
                
                post_date = parse_ptt_date(date_div.text.strip())
                
                # Debug: 印出第一篇的日期，確認時區是否正確
                # if len(posts) == 0 and len(page_posts) == 0:
                #     print(f"    - [Debug] 第一篇日期: {post_date}, 目標區間: {target_start} ~ {target_end}")

                if post_date > target_end: continue
                if post_date < target_start:
                    # 這一頁有太舊的文章，準備停止
                    pass 
                
                # 雖然準備停止，但如果這篇太舊就不用處理了
                if post_date < target_start:
                    continue

                title_div = div.find("div", class_="title")
                if not title_div or not title_div.a: continue
                
                raw_title = title_div.a.text.strip()
                link = "https://www.ptt.cc" + title_div.a["href"]
                
                if keywords:
                    title_lower = raw_title.lower()
                    if logic == 'AND':
                        if not all(k in title_lower for k in keywords): continue
                    else:
                        if not any(k in title_lower for k in keywords): continue

                nrec_div = div.find("div", class_="nrec")
                count_text = nrec_div.text.strip()
                count = 0
                if count_text == "爆": count = 100
                elif count_text.startswith("X"): count = -1
                elif count_text: count = int(count_text)

                page_posts.append({
                    "id": link,
                    "title": raw_title,
                    "url": link,
                    "date": post_date.isoformat(),
                    "count": count,
                    "author": div.find("div", class_="author").text.strip(),
                    "platform": "ptt",
                    "board_name": board
                })

            posts.extend(page_posts)

            # 翻頁檢查
            first_post_date = None
            if divs:
                first_post_date = parse_ptt_date(divs[0].find("div", class_="date").text.strip())
            
            if first_post_date and first_post_date < target_start:
                stop_crawling = True
            else:
                btn_group = soup.find("div", class_="btn-group-paging")
                prev_link = None
                if btn_group:
                    for btn in btn_group.find_all("a"):
                        if "上頁" in btn.text:
                            prev_link = btn["href"]
                            break
                if prev_link:
                    current_url = "https://www.ptt.cc" + prev_link
                    page_count += 1
                else:
                    stop_crawling = True
                    
        except Exception as e:
            print(f"    - [PTT-{board} Error] {e}")
            break
    
    print(f"  > [PTT-{board}] 完成，找到 {len(posts)} 篇")
    return posts

# --- 多板爬蟲入口 ---
def crawl_ptt_multi_boards(boards_str: str, keyword: str = None, logic: str = 'OR', start_date_str: str = None, end_date_str: str = None):
    # 時區處理：這裡收到的字串是 YYYY-MM-DD
    # 因為 PTT 只有日期沒有時間，我們要把 target_start 設為當天的 00:00，target_end 設為 23:59
    
    if start_date_str:
        target_start = datetime.strptime(start_date_str, "%Y-%m-%d")
    else:
        target_start = get_taiwan_now() - timedelta(days=1)
        target_start = target_start.replace(hour=0, minute=0, second=0)

    if end_date_str:
        target_end = datetime.strptime(end_date_str, "%Y-%m-%d")
        target_end = target_end.replace(hour=23, minute=59, second=59)
    else:
        target_end = get_taiwan_now()

    print(f"[系統] 台灣時間: {get_taiwan_now().strftime('%Y-%m-%d %H:%M')}")
    print(f"[搜尋] 區間: {target_start} ~ {target_end}")
    
    board_list = [b.strip() for b in boards_str.split(',') if b.strip()]
    print(f"[PTT] 啟動多板爬取: {board_list}, 關鍵字: {keyword}")
    
    all_results = []
    
    for board in board_list:
        try:
            board_results = crawl_single_ptt_board(board, keyword, logic, target_start, target_end)
            all_results.extend(board_results)
            # 板與板之間休息久一點
            time.sleep(random.uniform(2.0, 4.0))
        except Exception as e:
            print(f"[Error] 看板 {board} 爬取失敗: {e}")

    all_results.sort(key=lambda x: x['date'], reverse=True)
    return all_results

# --- Dcard 暫時回傳空 ---
def crawl_dcard(board: str, keyword: str = None, logic: str = 'OR', start_date_str: str = None, end_date_str: str = None):
    return [] 

@app.get("/search")
def search_posts(platform: str, board: str, keyword: str = None, logic: str = 'OR', startDate: str = None, endDate: str = None):
    if not startDate:
        startDate = (get_taiwan_now() - timedelta(days=3)).strftime("%Y-%m-%d")
    if not endDate:
        endDate = get_taiwan_now().strftime("%Y-%m-%d")

    if platform.lower() == "ptt":
        return crawl_ptt_multi_boards(board, keyword, logic, startDate, endDate)
    elif platform.lower() == "dcard":
        return crawl_dcard(board, keyword, logic, startDate, endDate)
    else:
        return []

@app.get("/")
def read_root():
    return {"status": "Backend is running", "time": get_taiwan_now().strftime("%Y-%m-%d %H:%M:%S")}
