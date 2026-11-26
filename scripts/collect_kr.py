"""캔들노트 국내 데이터 수집기

매일 GitHub Actions에서 실행돼요.
- 시세: 금융위원회_주식시세정보 (공공데이터포털)  → 코스피·코스닥 시가총액 상위 종목
- 재무·배당·기업정보·공시: DART 오픈API
- 뉴스: NAVER API HUB 뉴스 검색 (무료 한도 안에서만)

결과는 data/kr/ 폴더의 JSON 파일로 저장되고, index.html이 이 파일을 읽어요.
한 가지 데이터가 실패해도 이미 저장된 데이터는 지우지 않아요.
"""
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
import zipfile
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "kr"
NOTES_FILE = ROOT / "data" / "kr_notes.json"
PINS_FILE = ROOT / "data" / "kr_pins.json"
KST = dt.timezone(dt.timedelta(hours=9))

TOP_N = int(os.environ.get("TOP_N", "100"))   # 시가총액 상위 몇 종목까지 모을지
KEEP_BARS = 400            # 종목별로 보관할 일봉 개수 (1년 차트 + 120일 이동평균)
BACKFILL_DAYS = 600        # 처음 수집할 때 거슬러 올라갈 달력 일수
KEEP_DROPPED_DAYS = 30     # 상위권에서 빠진 종목도 30일간은 계속 갱신
FIN_REFRESH_DAYS = 7       # 재무·배당은 7일마다 다시 확인
INFO_REFRESH_DAYS = 30     # 기업 기본정보는 30일마다
NEWS_LOOKBACK_DAYS = 4     # 뉴스는 최근 며칠 안의 이벤트만 찾음 (검색 API가 날짜 필터를 지원하지 않음)
NEWS_MAX_CALLS = 400       # 최근 이벤트용 하루 뉴스 API 호출 상한
BACKFILL_MAX_CALLS = 5000  # 과거 이벤트 기사 채우기용 하루 상한 (둘을 합쳐도 무료 한도 하루 25,000회의 약 20%)
BACKFILL_PAGES = 10        # 검색어 하나당 최대 100건 × 10쪽

KEY_PRICE = os.environ.get("DATA_GO_KR_KEY", "").strip()
KEY_DART = os.environ.get("DART_API_KEY", "").strip()
NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "").strip()
NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "").strip()

PRICE_URL = "https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo"
DART = "https://opendart.fss.or.kr/api"
NEWS_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"

TODAY = dt.datetime.now(KST).date()
WARNINGS = []


def log(msg):
    print(msg, flush=True)


def warn(msg):
    WARNINGS.append(msg)
    print("⚠️  " + msg, flush=True)


# ---------------------------------------------------------------- HTTP
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "candlenote-collector/1.0"


def http_get(url, params=None, headers=None, timeout=30):
    """재시도 3번. 테스트에서는 이 함수를 바꿔 끼워요."""
    last = None
    for attempt in range(3):
        try:
            r = SESSION.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                last = RuntimeError(f"HTTP {r.status_code}")
                time.sleep(2 + attempt * 3)
                continue
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(2 + attempt * 3)
    raise RuntimeError(f"{urlparse(url).netloc} 연결 실패: {last}")


# ---------------------------------------------------------------- helpers
def ymd(d):
    return d.strftime("%Y%m%d")


def iso(s):
    s = str(s)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def to_int(v):
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def next_weekday(d):
    d += dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def days_since(iso_date):
    if not iso_date:
        return 10 ** 6
    return (TODAY - dt.date.fromisoformat(iso_date)).days


# ---------------------------------------------------------------- 시세 (공공데이터포털)
def price_call(**params):
    p = {"serviceKey": KEY_PRICE, "resultType": "json", "numOfRows": 1000, "pageNo": 1}
    p.update(params)
    r = http_get(PRICE_URL, params=p)
    text = r.text.strip()
    if not text.startswith("{"):
        # 키 오류 등은 XML로 와요
        m = re.search(r"<returnAuthMsg>(.*?)</returnAuthMsg>|<resultMsg>(.*?)</resultMsg>", text)
        reason = (m.group(1) or m.group(2)) if m else text[:200]
        raise RuntimeError(f"공공데이터포털 응답 오류: {reason} "
                           "(인증키가 아직 활성화 전이거나 DATA_GO_KR_KEY에 Decoding 키가 아닌 값이 들어갔을 수 있어요)")
    j = r.json()
    resp = j.get("response", j)          # 보통은 {"response": {...}}, 형식이 다르면 그대로
    header = resp.get("header", {}) if isinstance(resp, dict) else {}
    code = header.get("resultCode") or j.get("resultCode")
    if code not in (None, "00", "0"):
        msg = header.get("resultMsg") or j.get("resultMsg")
        raise RuntimeError(f"공공데이터포털 오류 {code}: {msg}")
    body = resp.get("body") if isinstance(resp, dict) else None
    if not isinstance(body, dict):
        raise RuntimeError(f"공공데이터포털 응답 형식이 예상과 달라요 (HTTP {r.status_code}): {text[:300]}")
    items = body.get("items") or {}
    items = items.get("item", []) if isinstance(items, dict) else []
    if isinstance(items, dict):
        items = [items]
    return items, int(body.get("totalCount") or 0)


def price_all(**params):
    rows, page = [], 1
    while True:
        items, total = price_call(pageNo=page, **params)
        rows += items
        if not items or len(rows) >= total:
            return rows
        page += 1
        if page > 60:
            return rows


def latest_trading_day():
    """데이터가 올라와 있는 가장 최근 거래일 (보통 전 영업일)."""
    for back in range(0, 12):
        d = TODAY - dt.timedelta(days=back)
        if d.weekday() >= 5:
            continue
        _, total = price_call(basDt=ymd(d), numOfRows=1)
        if total > 0:
            return d
    raise RuntimeError("최근 12일 동안 시세 데이터를 찾지 못했어요.")


def code_of(row):
    return str(row.get("srtnCd", "")).lstrip("A").strip()


def row_to_bar(row):
    c = to_int(row.get("clpr")) or 0
    o = to_int(row.get("mkp")) or 0
    h = to_int(row.get("hipr")) or 0
    lo = to_int(row.get("lopr")) or 0
    if o <= 0 or h <= 0 or lo <= 0:      # 거래정지일 등
        o = h = lo = c
    return [iso(row["basDt"]), o, max(h, o, c), min(lo, o, c), c, to_int(row.get("trqu")) or 0]


def is_common_stock(row):
    code, name = code_of(row), row.get("itmsNm", "")
    if row.get("mrktCtg") not in ("KOSPI", "KOSDAQ"):
        return False
    if not code.endswith("0"):           # 우선주(5, 7, 9, K 등으로 끝남) 제외
        return False
    if "스팩" in name or re.search(r"\d+호$", name):
        return False
    return True


def fetch_history(code, since, until):
    rows = price_all(likeSrtnCd=code, beginBasDt=ymd(since), endBasDt=ymd(until + dt.timedelta(days=1)))
    return [row_to_bar(r) for r in rows if code_of(r) == code]


# ---------------------------------------------------------------- 이벤트 판정 (화면과 같은 기준)
def detect_events(bars):
    out = set()
    for i in range(1, len(bars)):
        prev = bars[i - 1][4]
        if not prev:
            continue
        o, c, v = bars[i][1], bars[i][4], bars[i][5]
        win = [b[5] for b in bars[max(0, i - 20):i]]
        avg = sum(win) / len(win) if win else v
        gap, chg = o / prev - 1, c / prev - 1
        vr = v / avg if avg else 0
        if abs(gap) >= 0.03 or abs(chg) >= 0.05 or vr >= 3:
            out.add(bars[i][0])
    return out


# ---------------------------------------------------------------- DART
def dart(path, **params):
    params["crtfc_key"] = KEY_DART
    r = http_get(f"{DART}/{path}", params=params)
    j = r.json()
    status = j.get("status")
    if status == "013":                 # 조회된 데이터 없음
        return None
    if status != "000":
        raise RuntimeError(f"DART 오류 {status}: {j.get('message')}")
    return j


def dart_corp_codes():
    r = http_get(f"{DART}/corpCode.xml", params={"crtfc_key": KEY_DART}, timeout=120)
    if r.content[:2] != b"PK":
        try:
            j = r.json()
            raise RuntimeError(f"DART 오류 {j.get('status')}: {j.get('message')}")
        except ValueError:
            raise RuntimeError("DART 고유번호 파일을 받지 못했어요.")
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        xml = z.read(z.namelist()[0])
    m = {}
    for el in ET.fromstring(xml).iter("list"):
        sc = (el.findtext("stock_code") or "").strip()
        if sc:
            m[sc] = el.findtext("corp_code").strip()
    return m


ACCOUNTS = {
    "rev": ("매출액", "수익(매출액)", "영업수익", "매출"),
    "op": ("영업이익", "영업이익(손실)"),
    "ni": ("당기순이익", "당기순이익(손실)"),
    "eq": ("자본총계",),
}


def pick_accounts(items):
    """한 회사·한 보고서의 주요계정 → {key: (당기, 당기누적, 전기, 전전기)}. 연결(CFS) 우선."""
    out = {}
    for fs in ("CFS", "OFS"):
        for it in items:
            if it.get("fs_div") != fs:
                continue
            name = (it.get("account_nm") or "").replace(" ", "")
            for key, names in ACCOUNTS.items():
                if key in out:
                    continue
                if name in names:
                    out[key] = (to_int(it.get("thstrm_amount")), to_int(it.get("thstrm_add_amount")),
                                to_int(it.get("frmtrm_amount")), to_int(it.get("bfefrmtrm_amount")))
        if out:
            out["_fs"] = fs
            break
    return out


REPRT = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}


def collect_financials(corp_codes):
    """corp_code 목록 → {corp: {'y': [...], 'q': [...], 'eq': 최근자본총계, 'ni': 최근연간순이익, 'fs': 'CFS'|'OFS'}}"""
    reports = {}   # (corp, year, q) -> accounts
    y0 = TODAY.year
    plan = [(y, "11011") for y in (y0 - 1, y0 - 2, y0 - 3)]
    plan += [(y, rc) for y in (y0, y0 - 1) for rc in ("11013", "11012", "11014")]
    for i in range(0, len(corp_codes), 100):
        batch = corp_codes[i:i + 100]
        for year, rc in plan:
            try:
                j = dart("fnlttMultiAcnt.json", corp_code=",".join(batch), bsns_year=str(year), reprt_code=rc)
            except Exception as e:
                warn(f"재무 {year} {rc}: {e}")
                continue
            if not j:
                continue
            by_corp = {}
            for it in j.get("list", []):
                by_corp.setdefault(it.get("corp_code"), []).append(it)
            for corp, items in by_corp.items():
                acc = pick_accounts(items)
                if acc:
                    reports[(corp, year, REPRT[rc])] = acc
    result = {}
    for corp in corp_codes:
        annual, fs = {}, None
        for year in (y0 - 3, y0 - 2, y0 - 1):          # 오래된 것부터 → 최신 보고서 값으로 덮어씀
            acc = reports.get((corp, year, 4))
            if not acc:
                continue
            fs = fs or acc.get("_fs")
            for k in ("rev", "op", "ni", "eq"):
                if k not in acc:
                    continue
                cur, _, prev, prev2 = acc[k]
                for yy, v in ((year, cur), (year - 1, prev), (year - 2, prev2)):
                    if v is not None and (yy == year or k not in annual.get(yy, {})):
                        annual.setdefault(yy, {})[k] = v
        quarters = {}
        for year in (y0 - 1, y0):
            for q in (1, 2, 3):
                acc = reports.get((corp, year, q))
                if acc:
                    quarters[(year, q)] = {k: acc[k][0] for k in ("rev", "op", "ni") if k in acc}
            # 4분기 = 연간 − (1~3분기)
            if all((year, q) in quarters for q in (1, 2, 3)) and year in annual:
                q4 = {}
                for k in ("rev", "op", "ni"):
                    vals = [quarters[(year, q)].get(k) for q in (1, 2, 3)]
                    if annual[year].get(k) is not None and None not in vals:
                        q4[k] = annual[year][k] - sum(vals)
                if q4:
                    quarters[(year, 4)] = q4
        ys = sorted(annual)[-4:]
        qs = sorted(quarters)[-4:]
        if not ys and not qs:
            continue
        latest_eq = None
        latest_q = max((k for k in reports if k[0] == corp), key=lambda k: (k[1], k[2]), default=None)
        if latest_q and "eq" in reports[latest_q]:
            latest_eq = reports[latest_q]["eq"][0]
        elif ys:
            latest_eq = annual[ys[-1]].get("eq")
        result[corp] = {
            "fs": fs or "CFS",
            "y": [{"l": str(y), "r": annual[y].get("rev"), "o": annual[y].get("op"), "n": annual[y].get("ni")} for y in ys],
            "q": [{"l": f"{str(y)[2:]}.{q}Q", "r": quarters[(y, q)].get("rev"), "o": quarters[(y, q)].get("op"),
                   "n": quarters[(y, q)].get("ni")} for (y, q) in qs],
            "ni": annual[ys[-1]].get("ni") if ys else None,
            "ni_year": ys[-1] if ys else None,
            "eq": latest_eq,
        }
    return result


def collect_dividend(corp):
    for year in (TODAY.year - 1, TODAY.year - 2):
        j = dart("alotMatter.json", corp_code=corp, bsns_year=str(year), reprt_code="11011")
        if not j:
            continue
        for it in j.get("list", []):
            se = (it.get("se") or "").replace(" ", "")
            if se.startswith("주당현금배당금") and "보통" in (it.get("stock_knd") or "보통"):
                return {"dps": to_int(it.get("thstrm")) or 0, "year": year}
        return {"dps": 0, "year": year}
    return None


def collect_company(corp):
    j = dart("company.json", corp_code=corp)
    if not j:
        return None
    hm = (j.get("hm_url") or "").strip()
    if hm and not hm.startswith("http"):
        hm = "http://" + hm
    est = j.get("est_dt") or ""
    return {"ceo": j.get("ceo_nm"), "est": iso(est) if len(est) == 8 else None, "hm": hm or None,
            "adres": j.get("adres"), "eng": j.get("corp_name_eng")}


DISC_SKIP = ("임원ㆍ주요주주특정증권등소유상황보고서", "주식등의대량보유상황보고서", "특정증권등소유상황",
             "증권발행실적보고서", "투자설명서", "일괄신고", "공정거래자율준수", "대규모기업집단현황공시")
DISC_PRIORITY = ("잠정", "실적", "공급계약", "유상증자", "무상증자", "자기주식", "합병", "분할", "최대주주",
                 "공개매수", "기업가치제고", "배당", "전환사채", "신주인수권", "소송", "영업정지", "조회공시",
                 "투자판단", "주요사항보고서", "타법인주식")


def collect_disclosures(corp, since, until):
    out, page = [], 1
    while True:
        j = dart("list.json", corp_code=corp, bgn_de=ymd(since), end_de=ymd(until), page_no=page, page_count=100)
        if not j:
            break
        out += j.get("list", [])
        if page >= int(j.get("total_page") or 1) or page >= 15:
            break
        page += 1
    return out


def best_disclosure(discs, date_iso, prev_iso):
    cands = []
    for d in discs:
        rd = iso(d.get("rcept_dt", "00000000"))
        if rd not in (date_iso, prev_iso):
            continue
        name = re.sub(r"\s+", "", d.get("report_nm", ""))
        if any(s in name for s in DISC_SKIP):
            continue
        rank = next((k for k, w in enumerate(DISC_PRIORITY) if w in name), len(DISC_PRIORITY))
        cands.append((rank, 0 if rd == date_iso else 1, d))
    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], x[1]))
    d = cands[0][2]
    return {"t": re.sub(r"\s+", " ", d.get("report_nm", "")).strip(),
            "u": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={d.get('rcept_no')}",
            "dt": iso(d.get("rcept_dt"))}


# ---------------------------------------------------------------- 뉴스 (NAVER API HUB)
PRESS = {
    "yna.co.kr": "연합뉴스", "yonhapnewstv.co.kr": "연합뉴스TV", "hankyung.com": "한국경제", "mk.co.kr": "매일경제",
    "sedaily.com": "서울경제", "edaily.co.kr": "이데일리", "mt.co.kr": "머니투데이", "chosun.com": "조선일보",
    "biz.chosun.com": "조선비즈", "joongang.co.kr": "중앙일보", "donga.com": "동아일보", "hani.co.kr": "한겨레",
    "khan.co.kr": "경향신문", "hankookilbo.com": "한국일보", "kbs.co.kr": "KBS", "imbc.com": "MBC", "sbs.co.kr": "SBS",
    "ytn.co.kr": "YTN", "mbn.co.kr": "MBN", "newsis.com": "뉴시스", "news1.kr": "뉴스1", "fnnews.com": "파이낸셜뉴스",
    "asiae.co.kr": "아시아경제", "heraldcorp.com": "헤럴드경제", "etnews.com": "전자신문", "zdnet.co.kr": "지디넷코리아",
    "bizwatch.co.kr": "비즈니스워치", "thebell.co.kr": "더벨", "infostockdaily.co.kr": "인포스탁데일리",
}
NEWS_WORDS = ("급등", "급락", "상승", "하락", "강세", "약세", "신고가", "신저가", "상한가", "하한가", "폭등", "폭락",
              "공시", "실적", "계약", "주가", "특징주", "매수", "매도", "거래량")
NEWS_CALLS = 0
BACKFILL_CALLS = 0


def press_of(url):
    host = (urlparse(url).hostname or "").lower()
    for dom, name in PRESS.items():
        if host == dom or host.endswith("." + dom):
            return name, True
    return host.replace("www.", ""), False


def clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def news_page(query, start, sort="date"):
    global NEWS_CALLS, BACKFILL_CALLS
    if sort == "date":
        if NEWS_CALLS >= NEWS_MAX_CALLS:
            return None
        NEWS_CALLS += 1
    else:
        if BACKFILL_CALLS >= BACKFILL_MAX_CALLS:
            return None
        BACKFILL_CALLS += 1
    r = http_get(NEWS_URL, params={"query": query, "display": 100, "start": start, "sort": sort},
                 headers={"X-NCP-APIGW-API-KEY-ID": NAVER_ID, "X-NCP-APIGW-API-KEY": NAVER_SECRET})
    if r.status_code != 200:
        raise RuntimeError(f"뉴스 API HTTP {r.status_code}: {r.text[:200]}")
    return r.json().get("items", [])


def score_item(it, name):
    """(발행시각, 점수, 기사정보) 또는 None. 제목에 종목명이 있어야 함."""
    try:
        t = parsedate_to_datetime(it.get("pubDate")).astimezone(KST)
    except (TypeError, ValueError):
        return None
    title = clean(it.get("title"))
    if name not in title:
        return None
    url = it.get("originallink") or it.get("link")
    press, major = press_of(url)
    score = (2 if major else 0) + (1 if any(w in title for w in NEWS_WORDS) else 0)
    return t, score, {"t": title, "u": url, "p": press, "tm": t.strftime("%Y-%m-%d %H:%M")}


def pick_for_event(cands, date_iso, prev_iso):
    start_t = dt.datetime.fromisoformat(prev_iso + "T15:30:00+09:00")
    end_t = dt.datetime.fromisoformat(date_iso + "T23:59:59+09:00")
    inside = [c for c in cands if start_t < c[0] <= end_t]
    if not inside:
        return None
    inside.sort(key=lambda c: (-c[1], c[0]))      # 점수 높은 것, 같으면 이른 기사
    return inside[0][2]


def backfill_news(stock, pending):
    """과거 이벤트: 날짜 필터가 없어서 관련도순 검색 결과를 모아 날짜로 걸러요.
    종목당 검색어 4개 × 최대 10쪽을 한 번 모아서 그 종목의 모든 과거 이벤트에 같이 써요."""
    name, cands, seen = stock["name"], [], set()
    for q in (f"{name} 특징주", f"{name} 급등", f"{name} 급락", f"{name} 주가"):
        for page in range(BACKFILL_PAGES):
            items = news_page(q, 1 + page * 100, sort="sim")
            if items is None:
                return False                             # 오늘 상한 도달 → 내일 이어서
            for it in items:
                c = score_item(it, name)
                if c and c[2]["u"] not in seen:
                    seen.add(c[2]["u"])
                    cands.append(c)
            if len(items) < 100:
                break
    dates = [b[0] for b in stock["bars"]]
    for d in pending:
        i = dates.index(d)
        e = stock["events"].setdefault(d, {})
        found = pick_for_event(cands, d, dates[i - 1] if i else d)
        if found:
            e["n"] = found
        e["nb"] = 1                                     # 과거 기사 찾기 시도 완료
    return True


def find_news(name, date_iso, prev_iso):
    """이벤트 날 기사 1건: (전 거래일 15:30, 당일 23:59] 사이, 제목에 종목명, 주요 언론사 우선."""
    start_t = dt.datetime.fromisoformat(prev_iso + "T15:30:00+09:00")
    end_t = dt.datetime.fromisoformat(date_iso + "T23:59:59+09:00")
    best = None
    for start in range(1, 1000, 100):
        items = news_page(name, start)
        if not items:
            break
        oldest = None
        for it in items:
            try:
                t = parsedate_to_datetime(it.get("pubDate")).astimezone(KST)
            except (TypeError, ValueError):
                continue
            oldest = t if oldest is None or t < oldest else oldest
            if not (start_t < t <= end_t):
                continue
            title = clean(it.get("title"))
            if name not in title:
                continue
            url = it.get("originallink") or it.get("link")
            press, major = press_of(url)
            score = (2 if major else 0) + (1 if any(w in title for w in NEWS_WORDS) else 0)
            if best is None or score >= best[0]:   # 같은 점수면 더 이른 기사(움직임의 원인에 가까움)
                best = (score, {"t": title, "u": url, "p": press, "tm": t.strftime("%Y-%m-%d %H:%M")})
        if oldest is not None and oldest <= start_t:
            break
        if len(items) < 100:
            break
    return best[1] if best else None


# ---------------------------------------------------------------- 직접 고정한 기사: 제목 채우기
def fill_pin_titles(limit=50):
    pins = load_json(PINS_FILE, {})
    done = 0
    for code, days in pins.items():
        for d, p in days.items():
            if done >= limit:
                break
            if not isinstance(p, dict) or not p.get("u"):
                continue
            if not p.get("p"):
                p["p"] = press_of(p["u"])[0]
                done += 1
            if p.get("t") or p.get("tf", 0) >= 3:
                continue
            done += 1
            try:
                r = SESSION.get(p["u"], timeout=15, headers={"User-Agent": "Mozilla/5.0 (compatible; candlenote/1.0)"})
                if not r.encoding or r.encoding.lower() == "iso-8859-1":
                    r.encoding = r.apparent_encoding
                text = r.text[:300000]
                m = (re.search(r'<meta[^>]+property=["\']og:title["\'][^>]*content=["\']([^"\']+)', text, re.I)
                     or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:title', text, re.I)
                     or re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S))
                title = clean(m.group(1))[:200] if m else ""
                if title:
                    p["t"] = title
                else:
                    p["tf"] = p.get("tf", 0) + 1
            except Exception:
                p["tf"] = p.get("tf", 0) + 1
    if done:
        save_json(PINS_FILE, pins)
        log(f"📌 고정한 기사 {done}건 정리")


# ---------------------------------------------------------------- main
def main():
    missing = [k for k, v in (("DATA_GO_KR_KEY", KEY_PRICE), ("DART_API_KEY", KEY_DART),
                              ("NAVER_CLIENT_ID", NAVER_ID), ("NAVER_CLIENT_SECRET", NAVER_SECRET)) if not v]
    if "DATA_GO_KR_KEY" in missing:
        log("❌ DATA_GO_KR_KEY가 없어요. GitHub 저장소 Settings → Secrets and variables → Actions에 넣어 주세요.")
        sys.exit(1)
    if missing:
        warn("다음 키가 없어서 해당 부분은 건너뛰어요: " + ", ".join(missing))

    try:
        fill_pin_titles()
    except Exception as e:
        warn(f"고정 기사 제목: {e}")

    meta = load_json(DATA / "_meta.json", {})
    notes = load_json(NOTES_FILE, {})

    # 1) 최근 거래일 전체 시세 → 상위 종목
    latest = latest_trading_day()
    log(f"📅 최근 거래일: {latest}")
    meta.setdefault("news_since", latest.isoformat())   # 처음 실행한 날의 데이터부터 기사 연결
    all_rows = price_all(basDt=ymd(latest))
    log(f"   전체 종목 {len(all_rows)}개")
    rows = {code_of(r): r for r in all_rows if is_common_stock(r)}
    ranked = sorted(rows.values(), key=lambda r: to_int(r.get("mrktTotAmt")) or 0, reverse=True)
    top = [code_of(r) for r in ranked[:TOP_N]]
    tracked = list(top)
    for f in DATA.glob("[0-9A-Z]*.json"):
        code = f.stem
        if code in tracked or code not in rows:
            continue
        old = load_json(f, {})
        if days_since(old.get("last_top")) <= KEEP_DROPPED_DAYS:
            tracked.append(code)
    log(f"   상위 {len(top)}개 + 유지 {len(tracked) - len(top)}개 갱신")

    # 2) 종목별 일봉 갱신
    stocks = {}
    for n, code in enumerate(tracked, 1):
        row = rows[code]
        path = DATA / f"{code}.json"
        s = load_json(path, {})
        bars = {b[0]: b for b in s.get("bars", [])}
        last_date = max(bars) if bars else None
        try:
            if len(bars) < 200:
                log(f"   [{n}/{len(tracked)}] {row['itmsNm']} 과거 시세 수집")
                for b in fetch_history(code, latest - dt.timedelta(days=BACKFILL_DAYS), latest):
                    bars[b[0]] = b
            elif last_date and next_weekday(dt.date.fromisoformat(last_date)) < latest:   # 빠진 날 채우기
                for b in fetch_history(code, dt.date.fromisoformat(last_date) + dt.timedelta(days=1), latest):
                    bars[b[0]] = b
        except Exception as e:
            warn(f"{row['itmsNm']} 과거 시세: {e}")
        b = row_to_bar(row)
        bars[b[0]] = b
        s.update({
            "code": code, "name": row.get("itmsNm"), "market": row.get("mrktCtg"),
            "shares": to_int(row.get("lstgStCnt")), "isin": row.get("isinCd"),
            "bars": [bars[k] for k in sorted(bars)][-KEEP_BARS:],
        })
        if code in top:
            s["last_top"] = TODAY.isoformat()
        s.setdefault("events", {})
        stocks[code] = s

    # 3) DART: 재무·배당·기업정보·공시
    if KEY_DART:
        try:
            corp_map = dart_corp_codes()
        except Exception as e:
            warn(f"DART 고유번호: {e}")
            corp_map = {}
        for code, s in stocks.items():
            if code in corp_map:
                s["corp"] = corp_map[code]
        need_fin = [s["corp"] for s in stocks.values() if s.get("corp") and days_since(s.get("fin_at")) >= FIN_REFRESH_DAYS]
        if need_fin:
            log(f"📊 재무 갱신 {len(need_fin)}개")
            fin = collect_financials(need_fin)
            for s in stocks.values():
                c = s.get("corp")
                if c in need_fin:
                    if c in fin:
                        s["fin"] = fin[c]
                    s["fin_at"] = TODAY.isoformat()
                    try:
                        div = collect_dividend(c)
                        if div is not None:
                            s["div"] = div
                    except Exception as e:
                        warn(f"{s['name']} 배당: {e}")
        for s in stocks.values():
            c = s.get("corp")
            if c and days_since(s.get("info_at")) >= INFO_REFRESH_DAYS:
                try:
                    info = collect_company(c)
                    if info:
                        s["info"] = info
                    s["info_at"] = TODAY.isoformat()
                except Exception as e:
                    warn(f"{s['name']} 기업정보: {e}")
        # 공시: 아직 확인 안 한 이벤트 날짜만
        log("📑 이벤트 날 공시 확인")
        for s in stocks.values():
            c = s.get("corp")
            if not c:
                continue
            dates = [b[0] for b in s["bars"]]
            ev = sorted(d for d in detect_events(s["bars"]) if not s["events"].get(d, {}).get("dc"))
            if not ev:
                continue
            first_idx = max(0, dates.index(ev[0]) - 1)
            try:
                discs = collect_disclosures(c, dt.date.fromisoformat(dates[first_idx]), latest)
            except Exception as e:
                warn(f"{s['name']} 공시: {e}")
                continue
            for d in ev:
                i = dates.index(d)
                e = s["events"].setdefault(d, {})
                e["d"] = best_disclosure(discs, d, dates[i - 1] if i else d)
                e["dc"] = 1

    # 4) 뉴스: 연결한 날 이후, 최근 며칠 이벤트만
    if NAVER_ID and NAVER_SECRET:
        log("📰 이벤트 날 뉴스 찾기")
        for s in stocks.values():
            dates = [b[0] for b in s["bars"]]
            for d in sorted(detect_events(s["bars"])):
                e = s["events"].setdefault(d, {})
                if e.get("nc") or d < meta["news_since"] or days_since(d) > NEWS_LOOKBACK_DAYS:
                    continue
                if NEWS_CALLS >= NEWS_MAX_CALLS:
                    warn("오늘 뉴스 호출 상한에 도달해서 나머지는 내일 이어서 찾아요.")
                    break
                i = dates.index(d)
                try:
                    e["n"] = find_news(s["name"], d, dates[i - 1] if i else d)
                    e["nc"] = 1
                except Exception as err:
                    warn(f"{s['name']} 뉴스: {err}")
        log(f"   뉴스 API 호출 {NEWS_CALLS}회")

        # 4-2) 과거 이벤트 기사 채우기: 하루 상한만큼씩 매일 이어서
        todo = []
        for s in stocks.values():
            pend = [d for d in sorted(detect_events(s["bars"]))
                    if not s["events"].get(d, {}).get("n") and not s["events"].get(d, {}).get("nb")
                    and (d < meta["news_since"] or s["events"].get(d, {}).get("nc") or days_since(d) > NEWS_LOOKBACK_DAYS)]
            if pend:
                todo.append((s, pend))
        if todo:
            log(f"🗂️  과거 이벤트 기사 채우기: {len(todo)}종목 남음")
            done = 0
            for s, pend in todo:
                try:
                    if not backfill_news(s, pend):
                        break
                    done += 1
                except Exception as err:
                    warn(f"{s['name']} 과거 기사: {err}")
            log(f"   {done}종목 완료, 호출 {BACKFILL_CALLS}회" + (" (나머지는 내일 이어서)" if done < len(todo) else ""))
        meta["backfill_left"] = len(todo) - (done if todo else 0)

    # 5) 저장
    index = []
    for code, s in stocks.items():
        if code in notes:
            s["notes"] = notes[code]
        else:
            s.pop("notes", None)
        valid = {b[0] for b in s["bars"]}
        s["events"] = {d: e for d, e in s["events"].items() if d in valid}
        save_json(DATA / f"{code}.json", s)
        last, prev = s["bars"][-1], (s["bars"][-2] if len(s["bars"]) > 1 else s["bars"][-1])
        if code in top:
            index.append({"code": code, "name": s["name"], "market": s["market"], "close": last[4],
                          "chg": round(last[4] / prev[4] - 1, 5) if prev[4] else 0,
                          "cap": last[4] * (s.get("shares") or 0)})
    index.sort(key=lambda x: -x["cap"])
    meta.update({"updated": latest.isoformat(), "generated_at": dt.datetime.now(KST).isoformat(timespec="minutes"),
                 "count": len(index), "warnings": WARNINGS[-20:]})
    save_json(DATA / "_meta.json", meta)
    save_json(DATA / "index.json", {"updated": latest.isoformat(), "news_since": meta["news_since"], "stocks": index})
    log(f"✅ 완료: {len(index)}종목, 경고 {len(WARNINGS)}건")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        log("❌ " + str(e))
        sys.exit(1)
