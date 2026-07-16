# -*- coding: utf-8 -*-
"""
제품·산업 디자이너 채용 공고 수집기 (멀티 소스)
- 소스: 사람인(공식 API) / 원티드(JSON) / 잡코리아(스크래핑) / 캐치(스크래핑)
- 결과: docs/jobs.json  →  GitHub Pages 대시보드(docs/index.html)가 읽음
- 필터: 서울/경기/인천, 요구 경력 8년 이하, 제외 키워드(패키지·웹·코스메틱·식품 등)

환경변수: SARAMIN_ACCESS_KEY (사람인만 필요)
의존성: requests, beautifulsoup4
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
OUTPUT_PATH = os.path.join(BASE_DIR, "docs", "jobs.json")

KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


# ---------------------------------------------------------------- filtering
REGION_WORDS = ("서울", "경기", "인천")
EXP_MIN_PATTERNS = [
    re.compile(r"경력\s*(\d+)\s*[~년]"),      # 경력 3~..., 경력 3년
    re.compile(r"(\d+)\s*년\s*이상"),          # 5년 이상
]


def guess_exp_min(text):
    """텍스트에서 최소 요구 경력(년)을 추정. 못 찾으면 None."""
    for pat in EXP_MIN_PATTERNS:
        m = pat.search(text or "")
        if m:
            return int(m.group(1))
    return None


def passes_filters(job, cfg):
    blob = " ".join([
        job.get("title", ""), job.get("company", ""),
        job.get("location", ""), job.get("experience", ""),
        job.get("extra", ""),
    ]).lower()

    # 제외 키워드
    for word in cfg["exclude_keywords"]:
        if word.lower() in blob:
            return False

    # 포함 키워드 (제품/산업 디자인 공고인지)
    if cfg.get("require_keywords"):
        if not any(w.lower() in blob for w in cfg["require_keywords"]):
            return False

    # 지역: 위치 정보가 있는데 수도권 단어가 하나도 없으면 제외 (없으면 통과)
    loc = job.get("location", "")
    if loc and not any(r in loc for r in REGION_WORDS):
        return False

    # 경력: 최소 요구 경력이 상한을 넘으면 제외 (파악 불가면 통과)
    exp_min = job.get("exp_min")
    if exp_min is None:
        exp_min = guess_exp_min(job.get("experience", "") + " " + job.get("title", ""))
    if exp_min is not None and exp_min > cfg["max_experience_years"]:
        return False

    return True


# ---------------------------------------------------------------- sources
def src_saramin(cfg):
    key = os.environ.get("SARAMIN_ACCESS_KEY")
    if not key:
        raise RuntimeError("SARAMIN_ACCESS_KEY 미설정 (Settings > Secrets 확인)")
    jobs = []
    loc = ",".join(cfg["location_codes_saramin"])
    for kw in cfg["search_keywords"]:
        r = session.get("https://oapi.saramin.co.kr/job-search", params={
            "access-key": key, "keywords": kw, "loc_cd": loc,
            "sort": "pd", "count": 110,
        }, headers={"Accept": "application/json"}, timeout=30)
        r.raise_for_status()
        for j in (r.json().get("jobs", {}).get("job", []) or []):
            pos = j.get("position", {})
            exp = pos.get("experience-level") or {}
            try:
                exp_min = int(exp.get("min", 0)) or None
            except (TypeError, ValueError):
                exp_min = None
            jobs.append({
                "id": f"saramin:{j.get('id')}",
                "source": "saramin",
                "title": pos.get("title", ""),
                "company": ((j.get("company") or {}).get("detail") or {}).get("name", ""),
                "location": (pos.get("location") or {}).get("name", "").replace("&gt;", ">"),
                "experience": exp.get("name", ""),
                "exp_min": exp_min,
                "extra": " ".join([
                    (pos.get("industry") or {}).get("name", ""),
                    (pos.get("job-code") or {}).get("name", ""),
                    j.get("keyword", ""),
                ]),
                "url": j.get("url", ""),
            })
        time.sleep(1)
    return jobs


def src_wanted(cfg):
    """원티드 프론트엔드가 사용하는 검색 JSON 엔드포인트 (비공식)"""
    jobs = []
    headers = {"Referer": "https://www.wanted.co.kr/search",
               "Accept": "application/json"}
    for kw in cfg["search_keywords"]:
        r = session.get("https://www.wanted.co.kr/api/v4/search", params={
            "query": kw, "country": "kr", "job_sort": "job.latest_order",
        }, headers=headers, timeout=30)
        if r.status_code == 404:  # 구버전 경로 폴백
            r = session.get("https://www.wanted.co.kr/api/v4/jobs", params={
                "query": kw, "country": "kr", "job_sort": "job.latest_order",
                "locations": "all", "limit": 50, "offset": 0,
            }, headers=headers, timeout=30)
        r.raise_for_status()
        body = r.json()
        data = body.get("data", [])
        if isinstance(data, dict):  # /search 응답은 {"jobs": [...]} 형태일 수 있음
            data = data.get("jobs", [])
        if not data:
            print(f"[wanted] '{kw}' 응답 비어있음. keys={list(body)[:8]}")
        for j in data:
            jid = j.get("id")
            addr = (j.get("address") or {})
            jobs.append({
                "id": f"wanted:{jid}",
                "source": "wanted",
                "title": j.get("position", ""),
                "company": (j.get("company") or {}).get("name", ""),
                "location": addr.get("full_location") or addr.get("location", ""),
                "experience": "",
                "exp_min": None,
                "extra": "",
                "url": f"https://www.wanted.co.kr/wd/{jid}",
            })
        time.sleep(1)
    return jobs


def src_jobkorea(cfg):
    """잡코리아 검색 결과 페이지 스크래핑 (사이트 개편 시 조정 필요)"""
    jobs = []
    for kw in cfg["search_keywords"]:
        r, last_err = None, None
        # PC → 모바일 순서로 시도 (해외 IP 차단 우회), 각 2회 재시도
        for base in ("https://www.jobkorea.co.kr/Search/",
                     "https://m.jobkorea.co.kr/Search/"):
            for _ in range(2):
                try:
                    r = session.get(base, params={
                        "stext": kw, "tabType": "recruit", "Page_No": 1,
                    }, timeout=12)
                    r.raise_for_status()
                    break
                except Exception as e:
                    last_err, r = e, None
                    time.sleep(2)
            if r is not None:
                break
        if r is None:
            raise last_err
        soup = BeautifulSoup(r.text, "html.parser")
        # 공고 상세 링크(/Recruit/GI_Read/)를 앵커로 삼아 주변 텍스트 수집
        for a in soup.select("a[href*='/Recruit/GI_Read/']"):
            href = a.get("href", "")
            m = re.search(r"/Recruit/GI_Read/(\d+)", href)
            title = a.get_text(" ", strip=True)
            if not m or not title or len(title) < 5:
                continue
            jid = m.group(1)
            if any(j["id"] == f"jobkorea:{jid}" for j in jobs):
                continue
            container = a.find_parent("article") or a.find_parent("li") \
                or a.find_parent("div") or a
            ctx = container.get_text(" ", strip=True)
            # 주변 텍스트에서 지역 단어만 추출 (필터 오탐 방지: ctx 전체를 쓰지 않음)
            region = next((w for w in ("서울", "경기", "인천") if w in ctx[:200]), "")
            m_exp = re.search(r"경력\s*\d+[~년][^\s]{0,6}", ctx)
            jobs.append({
                "id": f"jobkorea:{jid}",
                "source": "jobkorea",
                "title": title,
                "company": "",
                "location": region,     # 미상("")이면 필터에서 통과 처리됨
                "experience": m_exp.group(0) if m_exp else "",
                "exp_min": None,
                "extra": "",
                "url": f"https://www.jobkorea.co.kr/Recruit/GI_Read/{jid}",
            })
        time.sleep(1)
    return jobs


def src_catch(cfg):
    """캐치 검색 결과 스크래핑 (사이트 개편 시 조정 필요)"""
    jobs = []
    for kw in cfg["search_keywords"]:
        r = session.get("https://www.catch.co.kr/Search/SearchDetail",
                        params={"Keyword": kw},
                        headers={"Referer": "https://www.catch.co.kr/"},
                        timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a[href*='RecruitInfoDetails']"):
            href = a.get("href", "")
            m = re.search(r"(\d{4,})", href)
            title = a.get_text(" ", strip=True)
            if not m or not title or len(title) < 5:
                continue
            jid = m.group(1)
            if any(j["id"] == f"catch:{jid}" for j in jobs):
                continue
            container = a.find_parent("li") or a.find_parent("div") or a
            ctx = container.get_text(" ", strip=True)[:300]
            url = href if href.startswith("http") else "https://www.catch.co.kr" + href
            jobs.append({
                "id": f"catch:{jid}",
                "source": "catch",
                "title": title,
                "company": "",
                "location": ctx,
                "experience": ctx,
                "exp_min": None,
                "extra": "",
                "url": url,
            })
        time.sleep(1)
    return jobs


def enrich_jobkorea(job):
    """잡코리아 상세 페이지에서 회사명/근무지역/경력을 보강 (신규 공고만 호출)"""
    try:
        r = session.get(job["url"], timeout=12)
        r.raise_for_status()
    except Exception:
        r = session.get(job["url"].replace("://www.", "://m."), timeout=12)
        r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # 회사명: <title> 이 보통 "회사명 채용 - 공고제목 …" 형태
    t = soup.title.get_text(strip=True) if soup.title else ""
    m = re.match(r"(.+?)\s*채용", t)
    if m and not job.get("company"):
        job["company"] = m.group(1).strip()[:40]

    text = soup.get_text(" ", strip=True)
    # 근무지역: "서울 강남구" 같은 광역+시군구 패턴 (수도권 외 지역도 잡아 재필터에 사용)
    lm = re.search(
        r"(서울|경기|인천|부산|대구|대전|광주|울산|세종|강원|충북|충남|"
        r"전북|전남|경북|경남|제주)\s?[가-힣]{1,8}[시군구]", text)
    if lm:
        job["location"] = lm.group(0)
    # 경력 조건
    em = re.search(r"경력\s*\d+\s*년[^\s]{0,4}|경력\s*\d+\s*~\s*\d+|경력무관|신입", text)
    if em:
        job["experience"] = em.group(0)


SOURCES = {
    "saramin": src_saramin,
    "wanted": src_wanted,
    "jobkorea": src_jobkorea,
    "catch": src_catch,
}

KEEP_DAYS = 21  # 이 기간 동안 재관측되지 않은 공고는 목록에서 제거


def main():
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        sys.exit("config.json 이 없습니다.")

    prev = load_json(OUTPUT_PATH, {"jobs": [], "sources": {}})
    prev_jobs = {j["id"]: j for j in prev.get("jobs", [])}

    now = datetime.now(KST)
    now_iso = now.isoformat(timespec="seconds")
    status = {}
    current = {}

    for name, fn in SOURCES.items():
        if not cfg.get("enabled_sources", {}).get(name, True):
            status[name] = {"ok": True, "count": 0, "note": "비활성화됨", "at": now_iso}
            continue
        try:
            raw = fn(cfg)
            kept = [j for j in raw if passes_filters(j, cfg)]
            for j in kept:
                current.setdefault(j["id"], j)
            status[name] = {"ok": True, "count": len(kept),
                            "raw": len(raw), "at": now_iso}
            print(f"[{name}] 수집 {len(raw)} → 필터 통과 {len(kept)}")
        except Exception as e:
            status[name] = {"ok": False, "error": str(e)[:200], "at": now_iso}
            # 실패한 소스의 기존 공고는 유지
            for jid, j in prev_jobs.items():
                if j.get("source") == name:
                    current.setdefault(jid, j)
            print(f"[{name}] 실패: {e}")

    # 잡코리아 신규 공고 상세 보강 → 보강된 정보로 재필터
    enriched = 0
    for jid in list(current.keys()):
        j = current[jid]
        if j.get("source") != "jobkorea" or jid in prev_jobs or enriched >= 25:
            continue
        try:
            enrich_jobkorea(j)
            enriched += 1
            time.sleep(0.7)
            if not passes_filters(j, cfg):
                del current[jid]   # 상세에서 지역/경력/제외어 확인돼 탈락
        except Exception as e:
            print(f"[jobkorea] 상세 보강 실패 {jid}: {e}")
    if enriched:
        print(f"[jobkorea] 상세 보강 {enriched}건")

    # first_seen / last_seen 병합
    merged = []
    cutoff = now - timedelta(days=KEEP_DAYS)
    for jid, j in current.items():
        old = prev_jobs.get(jid)
        j["first_seen"] = old.get("first_seen", now_iso) if old else now_iso
        j["last_seen"] = now_iso if jid in current else old.get("last_seen", now_iso)
        merged.append(j)
    # 이번에 안 잡힌 기존 공고도 유예기간 내면 유지 (마감 전 잠깐 누락될 수 있음)
    for jid, old in prev_jobs.items():
        if jid in current:
            continue
        try:
            last = datetime.fromisoformat(old.get("last_seen", now_iso))
        except ValueError:
            last = now
        if last >= cutoff:
            merged.append(old)

    merged.sort(key=lambda j: j.get("first_seen", ""), reverse=True)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"updated": now_iso, "sources": status, "jobs": merged},
                  f, ensure_ascii=False, indent=1)
    print(f"총 {len(merged)}건 저장 → docs/jobs.json")


if __name__ == "__main__":
    main()
