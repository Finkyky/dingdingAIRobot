"""
Sector scraping + LLM comparison analysis
Adapted from block/blockSpyder.py and block/basic.py
"""

import os
import json
import time
import requests
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.longcat.chat/openai/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL_ID = os.environ.get("LLM_MODEL_ID", "LongCat-Flash-Chat")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

COMPARE_PROMPT = """你是一位 A 股股票市场的行业分析专家。

现在有三份板块列表：
- 【我方板块】
- 【东方财富板块】
- 【同花顺板块】

请你完成以下三项任务：

任务一：找出【我方板块】中，在东方财富和同花顺中都找不到语义近似板块的条目，标记为「建议移除」。
任务二：找出【我方板块】中，能在东方财富或同花顺中找到语义近似板块的条目，标记为「建议保留」，并说明对应的是哪个板块。
任务三：找出东方财富和同花顺都有、但【我方板块】中缺失的板块，标记为「建议新增」。

判断标准：
- 语义近似 = 在 A 股投资语境下描述同一类投资主题
- 语义不同 = 描述的投资主题有本质差异

输出格式要求（严格按照 JSON 输出，不要有多余文字）：
{{
  "to_remove": [
    {{"name": "xxx", "reason": "原因"}}
  ],
  "to_keep": [
    {{"our_name": "xxx", "matched_to": "yyy(东方财富) / zzz(同花顺)", "reason": "原因"}}
  ],
  "to_add": [
    {{"name": "xxx", "present_in": "东方财富、同花顺", "reason": "原因"}}
  ]
}}

【我方板块】：
{our_sectors}

【东方财富板块】：
{em_sectors}

【同花顺板块】：
{ths_sectors}
"""


# ── Eastmoney scraper ─────────────────────────────────────────────

def fetch_eastmoney_sectors() -> list[str]:
    """Fetch sector names from Eastmoney (概念+行业)"""
    results = []
    targets = {
        "概念板块": "m:90+t:3",
        "行业板块": "m:90+t:2",
    }
    for label, fs in targets.items():
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get"
            "?pn=1&pz=2000&po=1&np=1"
            "&ut=bd1d9ddb04089700cf9c27f6f7426281"
            "&fltt=2&invt=2&fid=f3"
            f"&fs={fs}&fields=f14"
        )
        try:
            resp = requests.get(url, headers={**HEADERS, "Referer": "https://www.eastmoney.com/"}, timeout=15)
            resp.raise_for_status()
            items = resp.json().get("data", {}).get("diff", [])
            names = [item.get("f14", "").strip() for item in items if item.get("f14", "").strip()]
            results.extend(names)
            print(f"[EM] {label}: {len(names)} sectors")
        except Exception as e:
            print(f"[EM] {label} failed: {e}")
        time.sleep(1)
    return results


# ── THS scraper ───────────────────────────────────────────────────

def fetch_ths_sectors() -> list[str]:
    """Fetch sector names from THS (概念+行业, page-based API)"""
    from bs4 import BeautifulSoup

    results = []
    apis = {
        "行业板块": "https://q.10jqka.com.cn/thshy/index/field/199112/order/desc/page/{page}/ajax/1/",
        "概念板块": "https://q.10jqka.com.cn/gn/index/field/199112/order/desc/page/{page}/ajax/1/",
    }
    ths_headers = {
        **HEADERS,
        "Referer": "https://q.10jqka.com.cn/",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }

    for label, url_tpl in apis.items():
        total_pages = None
        for page in range(1, 100):
            url = url_tpl.format(page=page)
            try:
                resp = requests.get(url, headers=ths_headers, timeout=15)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")

                if total_pages is None:
                    pi = soup.select_one("span.page_info")
                    if pi:
                        parts = pi.get_text(strip=True).split("/")
                        total_pages = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 1
                    else:
                        total_pages = 1

                names = []
                for row in soup.select("table.m-table tbody tr"):
                    cols = row.select("td")
                    if len(cols) >= 2:
                        name = cols[1].get_text(strip=True)
                        if name:
                            names.append(name)

                if not names:
                    break

                results.extend(names)
                print(f"[THS] {label} p{page}/{total_pages}: {len(names)} sectors")

                if page >= total_pages:
                    break
                time.sleep(1.5)
            except Exception as e:
                print(f"[THS] {label} p{page} failed: {e}")
                break
        time.sleep(1)

    return results


# ── LLM analysis ─────────────────────────────────────────────────

def call_llm(prompt: str) -> str:
    resp = requests.post(
        f"{LLM_BASE_URL}/chat/completions",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
        json={
            "model": LLM_MODEL_ID,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def run_comparison(our_sectors: list[str], em_sectors: list[str], ths_sectors: list[str]) -> dict:
    """Run LLM comparison in batches if our_sectors is large"""
    all_results = {"to_remove": [], "to_keep": [], "to_add": []}
    batch_size = 50

    for i in range(0, len(our_sectors), batch_size):
        batch = our_sectors[i : i + batch_size]
        is_last = i + batch_size >= len(our_sectors)

        prompt = COMPARE_PROMPT.format(
            our_sectors="\n".join(batch),
            em_sectors="\n".join(em_sectors),
            ths_sectors="\n".join(ths_sectors),
        )

        print(f"[LLM] Analyzing batch {i // batch_size + 1} ({len(batch)} sectors)...")
        raw = call_llm(prompt)

        # Parse JSON, handle ```json``` wrapper
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]

        result = json.loads(clean.strip())
        all_results["to_remove"].extend(result.get("to_remove", []))
        all_results["to_keep"].extend(result.get("to_keep", []))
        if is_last:
            all_results["to_add"] = result.get("to_add", [])

    return all_results


# ── Format report ─────────────────────────────────────────────────

def format_report(result: dict) -> str:
    """Format analysis result as DingTalk markdown"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"## 板块差异分析报告", f"*{now}*", ""]

    removes = result.get("to_remove", [])
    if removes:
        lines.append(f"### 建议移除 ({len(removes)})")
        for item in removes[:20]:  # Limit to avoid message too long
            lines.append(f"- **{item['name']}**: {item.get('reason', '')}")
        if len(removes) > 20:
            lines.append(f"- ...等共 {len(removes)} 个")
        lines.append("")

    keeps = result.get("to_keep", [])
    if keeps:
        lines.append(f"### 建议保留 ({len(keeps)})")
        for item in keeps[:20]:
            lines.append(f"- **{item['our_name']}** ↔ {item.get('matched_to', '')}")
        if len(keeps) > 20:
            lines.append(f"- ...等共 {len(keeps)} 个")
        lines.append("")

    adds = result.get("to_add", [])
    if adds:
        lines.append(f"### 建议新增 ({len(adds)})")
        for item in adds[:20]:
            lines.append(f"- **{item['name']}** ({item.get('present_in', '')})")
        if len(adds) > 20:
            lines.append(f"- ...等共 {len(adds)} 个")
        lines.append("")

    if not removes and not adds:
        lines.append("所有板块都匹配良好，暂无需要调整的哦～")

    return "\n".join(lines)


# ── Main entry ────────────────────────────────────────────────────

def run_full_sector_check(our_sectors: list[str]) -> str:
    """Run full sector check and return markdown report"""
    print("[START] Running full sector check...")

    print("[STEP 1] Fetching Eastmoney sectors...")
    em_sectors = fetch_eastmoney_sectors()
    print(f"[STEP 1] Done: {len(em_sectors)} EM sectors")

    print("[STEP 2] Fetching THS sectors...")
    ths_sectors = fetch_ths_sectors()
    print(f"[STEP 2] Done: {len(ths_sectors)} THS sectors")

    if not em_sectors and not ths_sectors:
        return "## 板块分析失败\n\n呜呜～伊蕾娜酱抓取不到数据呢，可能是网络问题，主人稍后再试试吧～"

    print("[STEP 3] Running LLM comparison...")
    result = run_comparison(our_sectors, em_sectors, ths_sectors)
    print("[STEP 3] Done")

    report = format_report(result)
    print("[DONE] Report generated")
    return report
