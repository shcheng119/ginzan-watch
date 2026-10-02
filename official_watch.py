#!/usr/bin/env python3
"""
銀山溫泉「旅館官網」空房監測器（多間旅館版）
- 用無頭瀏覽器 (Playwright/Chromium) 直接開各旅館官網的訂房系統
- 依訂房系統分成不同解析器：
    489pro  ：能登屋、本館古勢起屋、古勢起屋別館、銀山荘、瀧見舘（同一套系統，共用解析器）
    hpdsp   ：永澤平八（網址可直接帶日期與人數）
    tabichat：藤屋（網址可直接帶日期與人數）
    form    ：古山閣、旅籠いとうや、昭和館（搜尋條件不在網址裡，需用瀏覽器填表；先跑 --debug 收集表單結構）
    url     ：通用模式，貼上搜尋結果網址即可（目前未使用）
- 旅館松本的訂房系統禁止自動存取，不在此監測範圍
- 狀態有變化就用 ntfy 推播到手機；核心旅館用最高優先度，備案旅館用次高

用法：
  python official_watch.py --debug     # 試跑：每間存一張截圖並印出原始資料
  python official_watch.py             # 檢查一次（給 GitHub Actions 用）
  python official_watch.py --loop 15   # 在自己電腦上每 ~15 分鐘檢查一次
  python official_watch.py --test      # 送測試推播
"""
import argparse
import hashlib
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# ============================ 設定區 ============================
DATES = ["2026-12-17", "2026-12-18"]   # 入住日（各 1 泊，擇一即可）
PREFERRED_DATE = "2026-12-17"          # 首選日：通知標題加 ★、優先度較高
# 可接受的住法：(每間房大人數, 房間數)。任一種有空就通知
PARTY_OPTIONS = [(2, 2), (4, 1)]       # 2人x2間、4人x1間

SITES = [
    # ---------- 核心 ----------
    {"name": "能登屋旅館",   "tier": "core", "engine": "489pro", "slug": "notoyaryokan"},
    {"name": "本館古勢起屋", "tier": "core", "engine": "489pro", "slug": "kosekikan"},
    {"name": "古勢起屋別館", "tier": "core", "engine": "489pro", "slug": "kosekiya"},
    {"name": "銀山荘",       "tier": "core", "engine": "489pro", "slug": "ginzanso"},
    {"name": "古山閣",       "tier": "core", "engine": "yadosagashi", "verified": False,
     # 宿さがし：搜尋條件用表單 POST 送出 → 用瀏覽器填表
     "home": "https://www.yado-sagashi.net/yoyaku/plan/index2.jsp?beg&all&yid=0046353751453"},
    {"name": "藤屋",         "tier": "core", "engine": "tabichat", "slug": "fujiyaginzan",
     "parties": [(2, 2)]},  # 藤屋單間最多 3 人，只能 2人x2間
    # ---------- 備案 ----------
    {"name": "瀧見舘",       "tier": "backup", "engine": "489pro", "slug": "takimikan"},
    {"name": "永澤平八",     "tier": "backup", "engine": "hpdsp", "yad_no": "319755", "path": "heihachi",
     "verified": False},
    {"name": "旅籠いとうや", "tier": "backup", "engine": "yadosagashi", "verified": False,
     # 與古山閣同一套系統（宿さがし）
     "home": "https://www.yado-sagashi.net/yoyaku/plan/index2.jsp?beg&all&yid=4116550506152"},
    # 旅館松本：官網訂房系統 (rwiths.net) 的 robots.txt 禁止自動存取，因此不放進官網爬蟲
    {"name": "昭和館",       "tier": "backup", "engine": "liberty", "verified": False,
     # Liberty 新系統：單頁式網站，頁面載入時會呼叫搜尋 API → 攔截並改成我們的日期與人數
     "home": "https://site.reservation.liberty-service.com/cc5f559e549f4f2fac443b9673014a37/facility-db690034-b45f-46b5-84d1-3f8ba4b8f522/search"},
]

# 通用模式（url 引擎）的判斷關鍵字；跑完 --debug 後可依實際頁面調整
URL_SOLD_OUT = ["ご利用できるプランがない", "該当するプランはございません", "満室", "空室がありません", "空室なし", "該当するプランがありません",
                "見つかりませんでした", "ご希望の条件に合う", "受付を終了", "販売終了",
                "予約可能なプランがありません", "条件に合うプランがありません",
                "Sold out", "No rooms available", "No plans available"]
URL_AVAILABLE = ["予約する", "詳細・予約へ", "予約へ進む", "空室あり", "残り"]
# ================================================================

def _clean_topic(raw):
    t = (raw or "").strip()
    t = re.sub(r"^https?://ntfy\.sh/", "", t).strip("/ ")
    return t


NTFY_TOPIC = _clean_topic(os.environ.get("NTFY_TOPIC", ""))
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
STATE_FILE = os.environ.get("STATE_FILE", "official_state.json")
JST = timezone(timedelta(hours=9))

AVAILABLE, PHONE, FULL, CLOSED, UNKNOWN, SKIPPED = "空室あり", "要電話", "満室", "受付不可", "無法判讀", "未設定"
TIER_LABEL = {"core": "【核心】", "backup": "【備案】"}
TIER_PRIORITY = {"core": 5, "backup": 4}


def notify_priority(tier, date_str):
    """核心+首選日=5（最高）；核心+次選日 或 備案+首選日=4；備案+次選日=3"""
    p = TIER_PRIORITY[tier]
    return p if date_str.startswith(PREFERRED_DATE) else p - 1


def date_tag(date_str):
    return "★首選日 " if date_str.startswith(PREFERRED_DATE) else ""


def now():
    return datetime.now(JST).strftime("%m-%d %H:%M:%S")


# ---------------------------- 通知 ----------------------------
def notify(title, message, click=None, priority=5):
    print(f"[{now()}] 🔔 {title}｜{message}")
    if not NTFY_TOPIC:
        return
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message,
               "priority": priority, "tags": ["hotsprings"]}
    if click:
        payload["click"] = click
    req = urllib.request.Request(NTFY_SERVER, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:300]
        print(f"[{now()}] ntfy 推播失敗：HTTP {e.code} {body}")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", NTFY_TOPIC):
            print(f"[{now()}] ⚠ NTFY_TOPIC 格式不對（長度 {len(NTFY_TOPIC)}）："
                  "只能用英文字母、數字、- 和 _，不能有空格、中文或網址")
    except Exception as e:
        print(f"[{now()}] ntfy 推播失敗：{e}")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def party_label(adults, rooms):
    return f"{adults}人x{rooms}間"


def safe_name(s):
    return re.sub(r"\W+", "_", s)


# ------------------------ 狀態判讀規則 ------------------------
# 489pro 日曆圖例：空室あり / 満室 / お電話にてお問い合わせください / 受付できません
# 下列關鍵字會同時比對格子的文字、CSS class、圖片 alt 與檔名。
# 跑完 --debug 後若發現判讀不準，只需要改這裡。
# 已用 debug 結果確認：fa-xmark=満室、fa-minus=受付不可、fa-circle=空室あり（▲残りわずか 也算有空房）
RULES = [
    (PHONE,     ["fa-phone", "電話", "tel:"]),
    (CLOSED,    ["fa-minus", "受付できません"]),
    (FULL,      ["fa-xmark", "満室", "×"]),
    (AVAILABLE, ["fa-circle", "fa-triangle", "fa-caret-up", "fa-play", "空室あり", "○", "▲", "残"]),
]


def classify(blob: str, has_link: bool) -> str:
    low = blob.lower()
    for status, keys in RULES:
        if any(k.lower() in low for k in keys):
            return status
    return AVAILABLE if has_link else UNKNOWN


# ------------------------ 489pro 專用 ------------------------
FIND_CELLS_JS = r"""
([y, m]) => {
  const re = new RegExp(`${y}\\s*年\\s*0?${m}\\s*月|${y}\\s*/\\s*0?${m}(?!\\d)`);
  const skip = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'OPTION', 'SELECT']);
  let heads = [...document.querySelectorAll('body *')]
    .filter(e => !skip.has(e.tagName) && re.test(e.textContent || ''));
  heads = heads.filter(e => ![...e.children].some(c => re.test(c.textContent || '')));
  if (!heads.length) return {ok: false, reason: 'month heading not found'};
  const out = [];
  for (const head of heads) {
    let scope = head, table = null;
    for (let i = 0; i < 8 && scope; i++) {
      table = scope.querySelector('table');
      if (table) break;
      scope = scope.parentElement;
    }
    const cells = table ? [...table.querySelectorAll('td')]
                        : [...(scope || document.body).querySelectorAll('li, div')]
                            .filter(e => (e.textContent || '').trim().length < 40);
    for (const td of cells) {
      const t = (td.innerText || '').trim();
      const mm = t.match(/^(\d{1,2})(?!\d)/);
      if (!mm) continue;
      const day = parseInt(mm[1]);
      if (out.some(o => o.day === day)) continue;
      const classes = [td.className, ...[...td.querySelectorAll('*')].map(e => e.className)]
        .filter(c => typeof c === 'string' && c).join(' ');
      const imgs = [...td.querySelectorAll('img')]
        .map(i => `${i.alt || ''} ${(i.getAttribute('src') || '').split('/').pop()}`).join(' ');
      const titles = [...td.querySelectorAll('[title],[aria-label]')]
        .map(e => `${e.getAttribute('title') || ''} ${e.getAttribute('aria-label') || ''}`).join(' ');
      const a = td.querySelector('a');
      out.push({day, text: t, classes, imgs, titles,
                link: a ? a.href : null, clickable: !!(a || td.onclick || td.getAttribute('data-date')),
                html: td.innerHTML.slice(0, 500)});
    }
    if (out.length >= 20) break;
  }
  return {ok: out.length > 0, cells: out, reason: out.length ? '' : 'no day cells'};
}
"""

MONTH_RE = r"(\d{4})\s*年\s*(\d{1,2})\s*月"


def goto_month(page, y, m, max_clicks=14):
    target = re.compile(rf"{y}\s*年\s*0?{m}\s*月")
    for _ in range(max_clicks):
        body = page.inner_text("body")
        if target.search(body):
            return True
        nxt = page.get_by_text(re.compile(r"^\s*(次月|翌月|次の月|Next)\s*[>›»]?\s*$")).first
        if nxt.count() == 0:
            return False
        nxt.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(800)
    return False


def check_489pro(page, site, debug=False):
    url = f"https://reserve.489ban.net/client/{site['slug']}/{site.get('lang', 0)}/plan/availability/daily"
    page.goto(url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(1500)
    results = {}
    for d in DATES:
        y, m, day = map(int, d.split("-"))
        if not goto_month(page, y, m):
            results[f"{d} 不分人數"] = {"status": UNKNOWN, "fp": "month-missing", "url": url,
                          "detail": f"翻不到 {y}年{m}月（可能尚未開放預約）"}
            continue
        data = page.evaluate(FIND_CELLS_JS, [y, m])
        if debug:
            fn = f"debug_{site['slug']}_{y}{m:02d}.png"
            page.screenshot(path=fn, full_page=True)
            print(f"\n=== {site['name']} {y}/{m} 原始格子資料（截圖：{fn}）===")
            for c in data.get("cells", []):
                print(json.dumps({k: c[k] for k in ("day", "text", "classes", "imgs", "titles", "link")},
                                 ensure_ascii=False))
            if not data.get("ok"):
                print("找不到格子：", data.get("reason"))
                with open(f"debug_{site['slug']}.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
        cell = next((c for c in data.get("cells", []) if c["day"] == day), None)
        if not cell:
            results[f"{d} 不分人數"] = {"status": UNKNOWN, "fp": "cell-missing", "url": url,
                          "detail": data.get("reason", "")}
            continue
        blob = " ".join([cell["text"], cell["classes"], cell["imgs"], cell["titles"]])
        status = classify(blob, bool(cell["link"]))
        fp = hashlib.md5(cell["html"].encode()).hexdigest()[:10]
        results[f"{d} 不分人數"] = {"status": status, "fp": fp, "url": cell["link"] or url,
                                   "detail": cell["text"].replace("\n", " ")[:40]}
    return results



# ------------------------ hpdsp（永澤平八） ------------------------
def check_hpdsp(page, site, debug=False):
    results = {}
    for d in DATES:
        y, m, day = map(int, d.split("-"))
        for adults, rooms in site.get("parties", PARTY_OPTIONS):
            label = party_label(adults, rooms)
            # roomCrack：每間房的人數編碼（例：4人1間=400000；2人2間=200000,200000）
            crack = ",".join([f"{adults}00000"] * rooms)
            url = (f"https://www.hpdsp.net/{site['path']}/hw/hwp3100/hww3101.do?yadNo={site['yad_no']}"
                   f"&stayYear={y}&stayMonth={m}&stayDay={day}&stayCount=1&dateUndecided=0"
                   f"&roomCount={rooms}&adultNum={adults}&roomCrack={crack}")
            page.goto(url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(1500)
            text = page.inner_text("body")
            if debug:
                fn = f"debug_{safe_name(site['name'])}_{d}_{adults}x{rooms}.png"
                page.screenshot(path=fn, full_page=True)
                print(f"\n=== {site['name']} {d} {label}（截圖：{fn}）===\n{url}\n{text[:1200]}")
            m_cnt = re.search(r"全\s*(\d+)\s*件のプラン", text)
            if m_cnt:
                status = AVAILABLE if int(m_cnt.group(1)) > 0 else FULL
            elif any(k in text for k in URL_SOLD_OUT):
                status = FULL
            else:
                status = UNKNOWN
            detail = m_cnt.group(0) if m_cnt else ""
            results[f"{d} {label}"] = {"status": status, "fp": plan_fingerprint(text),
                                       "url": url, "detail": detail}
            time.sleep(random.uniform(2, 4))
    if debug:
        # 對照組：用一個較可能有空房的日期，確認網址裡的日期參數真的有被採用
        for cd in ("2026-10-14", "2027-01-13"):
            y, m, day = map(int, cd.split("-"))
            url = (f"https://www.hpdsp.net/{site['path']}/hw/hwp3100/hww3101.do?yadNo={site['yad_no']}"
                   f"&stayYear={y}&stayMonth={m}&stayDay={day}&stayCount=1&dateUndecided=0"
                   f"&roomCount=1&adultNum=2&roomCrack=200000")
            page.goto(url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(1500)
            t = page.inner_text("body")
            hit = re.search(r"全\s*\d+\s*件のプラン|ご利用できるプランがない", t)
            print(f"--- {site['name']} 對照組 {cd} 2人x1間：{hit.group(0) if hit else '（無關鍵字）'}")
            time.sleep(2)
        fields = page.evaluate(DUMP_FORM_JS)
        print(f"--- {site['name']} 表單結構 ---")
        for f in fields:
            print(json.dumps({k: v for k, v in f.items() if v}, ensure_ascii=False))
    return results


# ------------------------ 通用：貼上搜尋結果網址 ------------------------
def plan_fingerprint(text):
    """只取跟方案/價格/空房有關的行來算指紋，避免頁面上無關的動態內容造成誤報。"""
    keys = ("円", "予約", "満室", "空室", "残", "件")
    lines = sorted({ln.strip() for ln in text.splitlines() if any(k in ln for k in keys)})
    return hashlib.md5("\n".join(lines).encode()).hexdigest()[:10]


def check_url(page, site, debug=False):
    results = {}
    urls = site.get("search_urls", {})
    if debug and not any(urls.values()):
        page.goto(site["home"], wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        fn = f"debug_{safe_name(site['name'])}_home.png"
        page.screenshot(path=fn, full_page=True)
        print(f"\n=== {site['name']} 訂房首頁（截圖：{fn}）===\n{page.inner_text('body')[:800]}")
    for d in DATES:
        for adults, rooms in site.get("parties", PARTY_OPTIONS):
            label = party_label(adults, rooms)
            key = f"{d} {label}"
            url = urls.get(f"{d}|{adults}x{rooms}", "")
            if not url:
                results[key] = {"status": SKIPPED, "fp": "", "url": site["home"], "detail": "尚未貼上搜尋網址"}
                continue
            page.goto(url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(2500)
            text = page.inner_text("body")
            if debug:
                fn = f"debug_{safe_name(site['name'])}_{d}_{adults}x{rooms}.png"
                page.screenshot(path=fn, full_page=True)
                print(f"\n=== {site['name']} {key}（截圖：{fn}）===\n{text[:1500]}")
            sold = any(k in text for k in URL_SOLD_OUT)
            avail = any(k in text for k in URL_AVAILABLE)
            status = AVAILABLE if (avail and not sold) else FULL if sold else UNKNOWN
            results[key] = {"status": status, "fp": plan_fingerprint(text), "url": url, "detail": ""}
            time.sleep(random.uniform(2, 4))
    return results


# ------------------------ tabichat（藤屋） ------------------------
def check_tabichat(page, site, debug=False):
    results = {}
    for d in DATES:
        co = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        for adults, rooms in site.get("parties", PARTY_OPTIONS):
            label = party_label(adults, rooms)
            guests = "&".join(f"guests%5B{i}%5D%5Badults%5D={adults}" for i in range(rooms))
            url = (f"https://tabichat.jp/engine/hotels/{site['slug']}?{guests}"
                   f"&checkin_date={d}&checkout_date={co}&list_type=plan_list")
            page.goto(url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3000)
            text = page.inner_text("body")
            if debug:
                fn = f"debug_{safe_name(site['name'])}_{d}_{adults}x{rooms}.png"
                page.screenshot(path=fn, full_page=True)
                print(f"\n=== {site['name']} {d} {label}（截圖：{fn}）===\n{url}\n{text[:2000]}")
            # 已用 debug 結果確認：客滿時頁面顯示「お部屋 (0)」「プラン (0)」
            m_plan = re.search(r"プラン\s*\((\d+)\)", text)
            if m_plan:
                status = AVAILABLE if int(m_plan.group(1)) > 0 else FULL
            else:
                status = UNKNOWN
            results[f"{d} {label}"] = {"status": status, "fp": plan_fingerprint(text), "url": url,
                                       "detail": m_plan.group(0) if m_plan else ""}
            time.sleep(random.uniform(2, 4))
    return results


# ------------------------ form（需填表的系統） ------------------------
DUMP_FORM_JS = r"""
() => {
  const els = [...document.querySelectorAll('input, select, button, textarea, [role=button], a.btn, a[class*=search]')];
  return els.slice(0, 120).map(e => ({
    tag: e.tagName, type: e.type || '', name: e.name || '', id: e.id || '',
    cls: (typeof e.className === 'string' ? e.className : '').slice(0, 60),
    value: (e.value || '').slice(0, 40), text: (e.innerText || '').trim().slice(0, 30),
    placeholder: e.placeholder || '',
    options: e.tagName === 'SELECT' ? [...e.options].slice(0, 40).map(o => `${o.value}=${o.text}`) : undefined,
    form: e.form ? `${e.form.method || 'get'} ${e.form.action || ''}`.slice(0, 120) : ''
  }));
}
"""


def check_form(page, site, debug=False):
    """填表規則確認前：debug 時收集表單結構與背景請求，平常執行時略過。"""
    if not debug:
        return {f"{d} {party_label(a, r)}": {"status": SKIPPED, "fp": "", "url": site["home"],
                                              "detail": "填表規則待設定"}
                for d in DATES for a, r in site.get("parties", PARTY_OPTIONS)}
    reqs = []

    def on_request(r):
        if r.resource_type in ("xhr", "fetch", "document"):
            reqs.append(f"{r.method} {r.url[:160]} {(r.post_data or '')[:200]}")

    page.on("request", on_request)
    page.goto(site["home"], wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(3000)
    fn = f"debug_{safe_name(site['name'])}_form.png"
    page.screenshot(path=fn, full_page=True)
    fields = page.evaluate(DUMP_FORM_JS)
    print(f"\n=== {site['name']} 表單結構（截圖：{fn}）===")
    for f in fields:
        print(json.dumps({k: v for k, v in f.items() if v}, ensure_ascii=False))
    print(f"--- {site['name']} 背景請求 ---")
    for r in reqs[:40]:
        print(r)
    page.remove_listener("request", on_request)
    return {}


# ------------------------ 宿さがし（古山閣、旅籠いとうや） ------------------------
FILL_YS_JS = r"""
([ds, n]) => {
  const ci = document.querySelector('#checkinday');
  if (ci) { ci.removeAttribute('readonly'); ci.value = ds;
            ci.dispatchEvent(new Event('change', {bubbles: true})); }
  const fx = document.querySelector('input[name=fixed]');
  if (fx) fx.checked = false;
  const sel = document.querySelector('select[name=people]');
  let chosen = '';
  if (sel) {
    let best = null, bestN = 0;
    for (const o of sel.options) {
      const m = o.text.match(/^(\d+)名/);
      if (!m) continue;
      const k = parseInt(m[1]);
      if (k === n && !o.text.includes('以上')) { best = o; break; }
      if (o.text.includes('以上') && k <= n && k > bestN) { best = o; bestN = k; }
    }
    if (best) { sel.value = best.value; chosen = best.text; }
  }
  return {date: ci ? ci.value : null, people: chosen};
}
"""


def check_yadosagashi(page, site, debug=False):
    """一室人數：2人x2間 → 查 2名（只能確認至少有一間 2 人房）；4人x1間 → 查 4名（或「3名以上」）。"""
    results = {}
    for d in DATES:
        ds = d.replace("-", "/")
        for adults, rooms in site.get("parties", PARTY_OPTIONS):
            label = party_label(adults, rooms)
            page.goto(site["home"], wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(1500)
            info = page.evaluate(FILL_YS_JS, [ds, adults])
            try:
                with page.expect_navigation(timeout=30000):
                    page.click("input[name=cmdKensaku]")
            except Exception:
                pass
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)
            text = page.inner_text("body")
            kept = page.evaluate("() => (document.querySelector('#checkinday') || {}).value || ''")
            n_book = text.count("詳細・予約")
            if debug:
                fn = f"debug_{safe_name(site['name'])}_{d}_{adults}x{rooms}.png"
                page.screenshot(path=fn, full_page=True)
                print(f"\n=== {site['name']} {d} {label}（截圖：{fn}）===")
                print(f"填入：{info}｜送出後日期欄：{kept!r}｜「詳細・予約」出現 {n_book} 次")
                print(text[:1500])
            if kept.replace("-", "/") != ds:
                status, detail = UNKNOWN, f"日期沒有套用（{kept!r}）"
            elif any(k in text for k in URL_SOLD_OUT):
                status, detail = FULL, ""
            else:
                status = AVAILABLE if n_book > 0 else FULL
                detail = f"{n_book} 個方案"
            results[f"{d} {label}"] = {"status": status, "fp": plan_fingerprint(text),
                                       "url": site["home"], "detail": detail}
            time.sleep(random.uniform(2, 4))
    return results


# ------------------------ Liberty 新系統（昭和館） ------------------------
LIBERTY_API = "**/api/booking/search**"


def check_liberty(page, site, debug=False):
    results = {}
    for d in DATES:
        ci = int(d.replace("-", ""))
        win_end = int((datetime.strptime(d, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y%m%d"))
        for adults, rooms in site.get("parties", PARTY_OPTIONS):
            label = party_label(adults, rooms)
            cap = {}

            def handle(route, request):
                try:
                    cap["orig"] = request.post_data
                    body = json.loads(request.post_data or "{}")
                    body["checkInDate"], body["checkOutDate"] = ci, win_end
                    body["roomNumber"], body["restNumber"] = rooms, 1
                    g = body.get("guestsPerRoom") or []
                    if g:
                        body["guestsPerRoom"] = [dict(g[0], appDateId=ci, roomGroupIndex=i, persons=adults)
                                                 for i in range(rooms)]
                    cap["sent"] = json.dumps(body, ensure_ascii=False)
                    route.continue_(post_data=cap["sent"])
                except Exception as ex:
                    cap["err"] = str(ex)
                    route.continue_()

            page.route(LIBERTY_API, handle)
            try:
                with page.expect_response(lambda r: "/api/booking/search" in r.url, timeout=60000) as ri:
                    page.goto(site["home"], wait_until="domcontentloaded", timeout=60000)
                resp = ri.value
                raw = resp.text()
                code = resp.status
            except Exception as ex:
                raw, code = "", f"error {ex}"
            page.wait_for_timeout(4000)
            page.unroute(LIBERTY_API, handle)
            text = page.inner_text("body")
            if debug:
                fn = f"debug_{safe_name(site['name'])}_{d}_{adults}x{rooms}.png"
                page.screenshot(path=fn, full_page=True)
                print(f"\n=== {site['name']} {d} {label}（截圖：{fn}）===")
                print(f"原始請求：{(cap.get('orig') or '')[:1500]}")
                print(f"改寫後：{(cap.get('sent') or '')[:1500]}  錯誤：{cap.get('err')}")
                print(f"API 回應 HTTP {code}，長度 {len(raw)}")
                print(raw[:3000])
                for mm in list(re.finditer(str(ci), raw))[:5]:
                    print("…" + raw[max(0, mm.start() - 300): mm.end() + 300] + "…")
                print("--- 畫面文字 ---\n" + text[:1500])
            results[f"{d} {label}"] = {"status": UNKNOWN, "fp": hashlib.md5(raw.encode()).hexdigest()[:10],
                                       "url": site["home"], "detail": "判讀規則待校正"}
            time.sleep(random.uniform(2, 4))
    return results


ENGINES = {"489pro": check_489pro, "hpdsp": check_hpdsp, "url": check_url,
           "tabichat": check_tabichat, "form": check_form,
           "yadosagashi": check_yadosagashi, "liberty": check_liberty}


# ---------------------------- 主流程 ----------------------------
def check_once(debug=False):
    from playwright.sync_api import sync_playwright

    state = load_state()
    summary = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="ja-JP", timezone_id="Asia/Tokyo",
                                  viewport={"width": 1280, "height": 1600})
        page = ctx.new_page()
        for i, site in enumerate(SITES):
            if i:
                time.sleep(random.uniform(3, 6))  # 網站之間停一下，對伺服器客氣
            tier = site.get("tier", "core")
            if not debug and site.get("verified") is False:
                summary.append(f"{TIER_LABEL[tier]}{site['name']}：尚未啟用（等待校正）")
                continue
            try:
                results = ENGINES[site["engine"]](page, site, debug=debug)
            except Exception as e:
                print(f"[{now()}] {site['name']} 檢查失敗：{str(e)[:200]}")
                summary.append(f"{site['name']}：錯誤")
                continue
            for d, r in results.items():
                key = f"{site['name']}|{d}"
                prev = state.get(key, {})
                summary.append(f"{TIER_LABEL[tier]}{site['name']} {d[5:]}：{r['status']}")
                if r["status"] == SKIPPED:
                    continue
                changed = bool(prev) and prev.get("fp") != r["fp"]
                if r["status"] in (AVAILABLE, PHONE) and prev.get("status") != r["status"]:
                    extra = "（官網標示需打電話）" if r["status"] == PHONE else ""
                    notify(f"{date_tag(d)}{TIER_LABEL[tier]}{site['name']} {d[5:]} {r['status']}！{extra}",
                           "快點開訂房頁確認", click=r["url"], priority=notify_priority(tier, d))
                elif r["status"] == UNKNOWN and (changed or not prev):
                    notify(f"{TIER_LABEL[tier]}{site['name']} {d[5:]} 狀態有變化",
                           f"程式無法判讀，請手動看一下 {r['detail']}", click=r["url"], priority=3)
                state[key] = {"status": r["status"], "fp": r["fp"], "at": now()}
        browser.close()
    save_state(state)
    print("\n===== 本輪摘要 =====\n" + "\n".join(summary))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=float, metavar="MIN")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()
    if a.test:
        notify("官網監測：測試推播", f"設定成功 {now()}", priority=3)
    elif a.loop:
        print(f"開始監測，每約 {a.loop} 分鐘一次（Ctrl+C 停止）")
        while True:
            try:
                check_once()
            except Exception as e:
                print(f"[{now()}] 錯誤：{e}")
            time.sleep(a.loop * 60 + random.uniform(-45, 45))
    else:
        check_once(debug=a.debug)


if __name__ == "__main__":
    main()
