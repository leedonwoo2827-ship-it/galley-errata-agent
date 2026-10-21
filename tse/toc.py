"""교재 PDF 전체 → 정독·판정 단위(소절) 목록.

1) PDF 책갈피에서 소절 번호('1.1', '2.3' …)가 붙은 단계를 단위로 쓴다.
   (소절 번호가 없으면 '제N절/제N장/CHAPTER N' 단계를 단위로)
   - 자동 생성 책갈피(하이픈 슬러그, '책갈피' 묶음)와 '(내용없음)'은 버린다.
   - 절 머리(절 제목 + 도입 사례)는 그 절의 첫 소절에 붙인다.
   - 하위 소절이 없는 절(머리말·참고문헌 등)은 그 자체로 한 단위.
   - 앞 단위의 끝 쪽과 다음 단위의 시작 쪽이 같으면, 그 쪽은 다음 단위 제목에서 잘라 나눠 갖는다.
2) MAX_PAGES(20쪽)를 넘는 단위는 한 단계 아래 소제목 경계에서 고르게 나눈다 (3.3 → 3.3a, 3.3b).
3) 책갈피가 없으면 pdf-20-splitter 처럼 20쪽씩 자른다. ([Codex로 다시 뽑기]도 있다)
사람이 콘솔에서 고친 뒤 [단위 확정] 하면 단위 워크스페이스가 만들어진다.
"""
import math
import re
from collections import Counter
from pathlib import Path

from .config import MAX_PAGES
from .pdftext import Book

SUB_RE = re.compile(r"^(\d+)\.(\d+)\s")
PART_RE = re.compile(r"^(제\s*\d+\s*[장절편부]|CHAPTER\s*\d+|PART\s*\d+)", re.I)
APPX_RE = re.compile(r"^부록\s*(\d+)")
EMPTY = {"", "(내용없음)", "(내용 없음)"}


def _is_slug(t):
    return " " not in t and t.count("-") >= 3


def clean_outline(raw):
    """[(level, title, page)] — 자동 책갈피 묶음과 그 하위를 걸러 낸다."""
    out, skip = [], None
    for lvl, title, page in raw:
        t = " ".join(title.split())
        if skip is not None:
            if lvl > skip:
                continue
            skip = None
        if t in ("책갈피", "Bookmarks", "북마크") or _is_slug(t):
            skip = lvl
            continue
        if page >= 1:
            out.append((lvl, t, page))
    return out


def unit_level(ents):
    for rx in (SUB_RE, PART_RE):
        cnt = Counter(l for l, t, _ in ents if rx.match(t))
        if cnt:
            return cnt.most_common(1)[0][0]
    return None


def unit_id(title, taken):
    m = SUB_RE.match(title) or APPX_RE.match(title) or PART_RE.match(title)
    if m and SUB_RE.match(title):
        uid = f"{m.group(1)}.{m.group(2)}"
    elif m and APPX_RE.match(title):
        uid = f"부록{m.group(1)}"
    elif m:
        uid = re.sub(r"\s", "", m.group(1))
    else:
        uid = re.sub(r"[^\w가-힣]", "", title)[:6] or "단위"
    base, k = uid, 2
    while uid in taken:
        uid, k = f"{base}-{k}", k + 1
    taken.add(uid)
    return uid


def _top_of_page(book, page, title):
    """제목이 그 쪽 맨 위(앞에 다른 글이 없음)에 있으면 True. 못 찾으면 None."""
    k = book.find_head(page, title)
    if k is None:
        return None
    return not any(it["text"].strip() for it in book.items(page)[:k])


def _close(units, book):
    """끝 쪽 정하기: 다음 단위 시작 쪽까지. 다음 제목이 그 쪽 맨 위면 앞 쪽까지만."""
    n = book.page_count
    for k, u in enumerate(units):
        if k + 1 < len(units):
            nxt = units[k + 1]
            e, head = nxt["pages"][0], nxt["start_head"]
            top = _top_of_page(book, e, head) if head else True
            if top is None or top:  # 못 찾으면 겹치지 않게 앞 쪽까지
                e, head = e - 1, None
            u["pages"][1], u["end_head"] = max(e, u["pages"][0]), head
        else:
            e = n
            while e > u["pages"][0] and not any(it["text"].strip() for it in book.items(e)):
                e -= 1
            u["pages"][1], u["end_head"] = e, None
        u["printed"] = [book.printed(u["pages"][0]), book.printed(u["pages"][1])]
        u["n_pages"] = u["pages"][1] - u["pages"][0] + 1


def _split(u, children, book, taken):
    """MAX_PAGES 넘는 단위를 소제목 경계에서 k 조각으로."""
    s, e = u["pages"]
    n = e - s + 1
    k = math.ceil(n / MAX_PAGES)
    cands = [(p, t) for p, t in children if s < p <= e and t not in EMPTY]
    cuts = []
    for j in range(1, k):
        target = s + round(n * j / k)
        pool = [c for c in cands if (not cuts or c[0] > cuts[-1][0])]
        cuts.append(min(pool, key=lambda c: abs(c[0] - target)) if pool else (target, None))
    starts = [(s, u["start_head"])] + cuts
    parts = [{**u, "id": f"{u['id']}{chr(97 + j)}", "title": f"{u['title']} ({j + 1}/{len(starts)})",
              "pages": [p, e], "start_head": head} for j, (p, head) in enumerate(starts)]
    taken.discard(u["id"])
    taken.update(x["id"] for x in parts)
    return parts


def from_outline(book, ents):
    L = unit_level(ents)
    if L is None:
        return []
    heads = [e for e in ents if e[0] in (L - 1, L)]
    units, pending, part, taken = [], None, "", set()
    for i, (lvl, t, p) in enumerate(heads):
        nxt = heads[i + 1] if i + 1 < len(heads) else None
        if lvl == L - 1:
            part = t
            if nxt and nxt[0] == L:  # 절 머리는 첫 소절에 붙인다
                pending = (p, t)
                continue
        elif t in EMPTY:
            pending = pending or (p, None)
            continue
        sp, st = pending or (p, t)
        pending = None
        units.append({"id": unit_id(t, taken), "title": t, "part": part if lvl == L else "",
                      "pages": [sp, sp], "start_head": st, "end_head": None})
    if not units:
        return []
    units[0]["pages"][0], units[0]["start_head"] = 1, None  # 표지·앞쪽은 첫 단위에
    _close(units, book)
    # 긴 단위 나누기
    out = []
    for k, u in enumerate(units):
        if u["n_pages"] <= MAX_PAGES:
            out.append(u)
            continue
        s, e = u["pages"]
        children = [(p, t) for l, t, p in ents if l == L + 1 and s < p <= e]
        out.extend(_split(u, children, book, taken))
    # 나눈 조각의 끝 쪽 다시 계산 (다음 단위 시작 기준)
    _close(out, book)
    return out


def fixed_chunks(book, size=MAX_PAGES):
    """책갈피가 없을 때: 20쪽씩 (pdf-20-splitter 방식)."""
    out = []
    for s in range(1, book.page_count + 1, size):
        e = min(s + size - 1, book.page_count)
        ps, pe = book.printed(s), book.printed(e)
        out.append({"id": f"{s:03d}-{e:03d}", "title": f"p.{ps}–{pe}", "part": "", "pages": [s, e],
                    "start_head": None, "end_head": None})
    _close(out, book)
    return out


def finish(units, pdf_path, book, method):
    for k, u in enumerate(units):
        u.update(order=k + 1, include=True, assignee=u.get("assignee", ""), due=u.get("due", ""))
    return {"book": Path(pdf_path).name, "page_count": book.page_count, "printed_offset": book.offset,
            "method": method, "max_pages": MAX_PAGES, "units": units, "confirmed": False}


def extract(pdf_path, book=None):
    book = book or Book(pdf_path)
    ents = clean_outline(book.doc.get_toc())
    units = from_outline(book, ents) if ents else []
    if units:
        return finish(units, pdf_path, book, "outline")
    return finish(fixed_chunks(book), pdf_path, book, "fixed")


CODEX_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["units"],
    "properties": {"units": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["title", "start"],
        "properties": {"title": {"type": "string"}, "start": {"type": "integer"}}}}},
}


def codex_extract(pdf_path):
    """책갈피로 못 찾을 때: 각 쪽 첫머리를 Codex에 보여 주고 소절 시작 쪽을 받는다."""
    from .judge_codex import ask_codex
    book = Book(pdf_path)
    lines = []
    for p in range(1, book.page_count + 1):
        head = " | ".join(it["text"] for it in book.items(p) if it["text"].strip())[:110]
        lines.append(f"p{p}: {head}")
    prompt = ("아래는 교재 PDF의 각 쪽 첫머리다. 정독을 나눠 맡길 단위(소절, 없으면 절·장)의 제목과 시작 쪽을 뽑아라. "
              f"한 단위는 {MAX_PAGES}쪽 이하가 되게 하고, 머리말·부록·참고문헌도 단위로 넣는다. "
              "start 는 PDF 쪽 번호(p숫자)다. 파일을 읽거나 명령을 실행하지 마라.\n\n" + "\n".join(lines))
    res = ask_codex(prompt, schema=CODEX_SCHEMA)
    taken, units = set(), []
    for x in sorted(res["units"], key=lambda x: x["start"]):
        units.append({"id": unit_id(x["title"], taken), "title": x["title"], "part": "",
                      "pages": [max(1, x["start"]), x["start"]], "start_head": x["title"], "end_head": None})
    if units:
        units[0]["pages"][0], units[0]["start_head"] = 1, None
        _close(units, book)
    return finish(units, pdf_path, book, "codex")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    r = extract(Path(sys.argv[1]))
    print(f"method={r['method']} pages={r['page_count']} units={len(r['units'])}")
    for u in r["units"]:
        print(f"{u['id']:>7}  PDF {u['pages'][0]:>3}-{u['pages'][1]:<3} 인쇄 {u['printed'][0]:>3}-{u['printed'][1]:<3} "
              f"{u['n_pages']:>2}쪽  {u['title'][:40]}  | 시작:{(u['start_head'] or '-')[:16]} 끝:{(u['end_head'] or '-')[:16]}")
