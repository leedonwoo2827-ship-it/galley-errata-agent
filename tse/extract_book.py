"""교재 PDF에서 단위 범위만 잘라 쪽 단위 텍스트로 저장한다.

산출: <단위>/00_원고/  <단위>.pdf (가제본 정독 배포용), <단위>.txt, book.json
  book.json pages[] = {page: 인쇄 쪽, pdf: PDF 쪽, text: 문단을 되살린 쪽 텍스트, wraps: 줄바꿈 이음 자리}
  앞·뒤 단위와 나눠 갖는 쪽은 단위 제목(start_head / end_head) 기준으로 잘라 낸다.
"""
import json

import fitz

from . import config
from .pdftext import Book, render

_BOOK = {}


def load_book():
    """책 전체 분석은 몇 초 걸리므로 한 번만 (PDF가 바뀌면 다시)."""
    src = config.book_pdf()
    if not src:
        raise SystemExit("교재 PDF가 없습니다. 먼저 [책 넣기]에서 올려 주세요.")
    key = (str(src), src.stat().st_mtime)
    if key not in _BOOK:
        _BOOK.clear()
        _BOOK[key] = Book(src)
    return _BOOK[key]


def unit_pages(book, u):
    """단위 → [(pdf쪽, 인쇄쪽, items)] — 시작·끝 쪽은 제목에서 자른다."""
    s, e = u["pages"]
    out = []
    for p in range(s, e + 1):
        items = book.items(p)
        if p == s and u.get("start_head"):
            k = book.find_head(p, u["start_head"])
            items = items[k:] if k is not None else items
        if p == e and u.get("end_head"):
            k = book.find_head(p, u["end_head"])
            items = items[:k] if k is not None else items
        out.append((p, book.printed(p), items))
    return out


def run(uid, book=None):
    u = config.unit(uid)
    book = book or load_book()
    out = config.stage(uid, "00_원고")
    s, e = u["pages"]

    part = fitz.open()
    part.insert_pdf(book.doc, from_page=s - 1, to_page=e - 1)
    name = config.slug(uid)
    part.save(out / f"{name}.pdf")

    pages = []
    for p, printed, items in unit_pages(book, u):
        text, wraps = render(items)
        pages.append({"page": printed, "pdf": p, "text": text, "wraps": wraps})
    (out / "book.json").write_text(json.dumps({"unit": uid, "title": u["title"], "part": u.get("part", ""),
                                               "pages": pages}, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(out / f"{name}.txt", "w", encoding="utf-8") as f:
        for pg in pages:
            f.write(f"\n[p.{pg['page']}]\n{pg['text']}\n")

    print(f"[원고] {uid} {u['title']} 인쇄 p.{pages[0]['page']}–{pages[-1]['page']} ({len(pages)}쪽) 추출 → {out}")
    return pages


def load(uid):
    f = config.stage(uid, "00_원고") / "book.json"
    if not f.exists():
        run(uid)
    return json.loads(f.read_text(encoding="utf-8"))


if __name__ == "__main__":
    import sys
    run(sys.argv[1] if len(sys.argv) > 1 else "1.1")
