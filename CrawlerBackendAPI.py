## 啟動指令：uvicorn CrawlerBackendAPI:app --reload

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import requests
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

# --- 強力偽裝 Headers ---
def get_headers(referer_url="https://www.google.com/"):
    return {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": referer_url,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    }

def parse_ptt_date(date_str):
    try:
        date_str = date_str.strip()
        today = datetime.now()
        current_year = today.year
        msg_month, msg_day = map(int, date_str.split('/'))
        post_date = datetime(current_year, msg_month, msg_day)
        if today.month < 6 and msg_month > 6:
            post_date = post_date.replace(year=current_year - 1)
        return post_date
    except Exception as e:
        return datetime.now()

# --- 建立具備重試功能的 Session ---
def create_session():
    session = requests.Session()
    # 設定重試策略：遇到 500, 502, 503, 504, 520 時重試 3 次
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504, 520, 522])
    adapter = HTTPAdapter(max_retries=retries)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

# --- 單一看板爬取邏輯 ---
def crawl_single_ptt_board(board: str, keyword: str, logic: str, target_start: datetime, target_end: datetime):
    print(f"  > [PTT-{board}] 開始抓取...")
    base_url = f"https://www.ptt.cc/bbs/{board}/index.html"
    
    # 使用 Session 保持連線
    session = create_session()
    session.cookies.update({"over18": "1"})
    
    keywords = []
    if keyword:
        keywords = [k.strip().lower() for k in keyword.replace(',', ' ').split() if k.strip()]
    
    posts = []
    current_url = base_url
    page_count = 0
    max_pages_safety = 20 
    stop_crawling = False

    while not stop_crawling and page_count < max_pages_safety:
        try:
            # 每次請求前隨機休息，模擬人類閱讀 (0.5 ~ 1.5秒)
            time.sleep(random.uniform(0.5, 1.5))
            
            headers = get_headers(current_url)
            response = session.get(current_url, headers=headers, timeout=10)
            
            if response.status_code != 200:
                print(f"    - [PTT-{board}] 請求失敗: {response.status_code} (已嘗試重試)")
                break
                
            soup = BeautifulSoup(response.text, "html.parser")
            divs = soup.find_all("div", class_="r-ent")
            
            page_posts = []
            has_valid_post_in_page = False # 檢查這一頁是否有我們需要的文章

            for div in divs:
                if "deleted" in div.get("class", []): continue
                
                date_div = div.find("div", class_="date")
                if not date_div: continue
                
                date_raw = date_div.text.strip()
                post_date = parse_ptt_date(date_raw)
                
                # 如果文章比目標結束日期還新 (未來)，跳過
                if post_date > target_end: 
                    continue
                
                # 如果文章比目標開始日期還舊
                if post_date < target_start:
                    # 置底文通常沒有分隔線，但一般文章如果太舊，就代表我們可以準備停止了
                    # 但為了保險，我們不單看一篇，而是看這一頁的趨勢
                    pass 
                else:
                    has_valid_post_in_page = True

                # 雖然我們主要依靠頁面趨勢停止，但單篇過舊還是不需要加入列表
                if post_date < target_start:
                    continue

                title_div = div.find("div", class_="title")
                if not title_div or not title_div.a: continue
                
                raw_title = title_div.a.text.strip()
                link = "https://www.ptt.cc" + title_div.a["href"]
                
                if keywords:
                    title_lower = raw_title.lower()
                    if logic == 'AND':
                        if not all(k in title_lower for k in keywords):
                            continue
                    else:
                        if not any(k in title_lower for k in keywords):
                            continue

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

            # 翻頁判斷機制
            # PTT 列表順序：最上面是舊的，最下面是新的 (同一頁內)
            # 但我們是按「上頁」往回翻，所以越翻越舊
            
            # 取得這一頁「最上面」(最舊) 的一篇文章日期
            first_post_date = None
            if divs:
                raw_date = divs[0].find("div", class_="date").text.strip()
                first_post_date = parse_ptt_date(raw_date)
            
            # 如果這一頁最舊的文章已經早於我們的開始時間，那就不需要再往前翻了
            if first_post_date and first_post_date < target_start:
                # print(f"    - [PTT-{board}] 達到日期邊界 {first_post_date} < {target_start}")
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
                    # print(f"    - [PTT-{board}] 翻頁: {page_count}")
                else:
                    stop_crawling = True
                    
        except Exception as e:
            print(f"    - [PTT-{board} Error] {e}")
            break
    
    print(f"  > [PTT-{board}] 完成，找到 {len(posts)} 篇")
    return posts

# --- PTT 多板爬蟲入口 ---
def crawl_ptt_multi_boards(boards_str: str, keyword: str = None, logic: str = 'OR', start_date_str: str = None, end_date_str: str = None):
    target_start = datetime.strptime(start_date_str, "%Y-%m-%d") if start_date_str else datetime.now() - timedelta(days=1)
    target_end = datetime.strptime(end_date_str, "%Y-%m-%d") if end_date_str else datetime.now()
    target_end = target_end.replace(hour=23, minute=59, second=59)
    
    board_list = [b.strip() for b in boards_str.split(',') if b.strip()]
    print(f"[PTT] 啟動多板爬取: {board_list}, 關鍵字: {keyword}, 邏輯: {logic}")
    
    all_results = []
    
    for board in board_list:
        try:
            board_results = crawl_single_ptt_board(board, keyword, logic, target_start, target_end)
            all_results.extend(board_results)
            # 板與板之間也要休息，避免因為換板太快被擋
            time.sleep(random.uniform(1.0, 2.0))
        except Exception as e:
            print(f"[Error] 看板 {board} 爬取失敗: {e}")

    all_results.sort(key=lambda x: x['date'], reverse=True)
    return all_results

# --- Dcard 爬蟲 (暫時回傳空) ---
def crawl_dcard(board: str, keyword: str = None, logic: str = 'OR', start_date_str: str = None, end_date_str: str = None):
    return [] 

@app.get("/search")
def search_posts(platform: str, board: str, keyword: str = None, logic: str = 'OR', startDate: str = None, endDate: str = None):
    if not startDate:
        startDate = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    if not endDate:
        endDate = datetime.now().strftime("%Y-%m-%d")

    if platform.lower() == "ptt":
        return crawl_ptt_multi_boards(board, keyword, logic, startDate, endDate)
    elif platform.lower() == "dcard":
        return crawl_dcard(board, keyword, logic, startDate, endDate)
    else:
        return []

@app.get("/")
def read_root():
    return {"status": "Backend is running"}