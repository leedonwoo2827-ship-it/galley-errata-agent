"""워크스페이스 구조와 단위(소절) 설정.

workspace/
  00_책/                      교재 원본 PDF + toc.json (확정된 단위 목록 · 담당자 · 마감) + rules_book.json
  1.1_생성형_AI_무엇이…/        단위 워크스페이스 (단위번호_제목)
    unit.json                 단위 설정 (PDF 쪽 범위, 인쇄 쪽, 시작·끝 제목, 담당자, 마감)
    00_원고/                  단위 PDF(가제본 정독 배포용), unit.txt, book.json
    02_규칙검사/              rules.json (표기통일·번호·참조 — LLM 없이)
    03_판정/                  results.json, cache/ (Codex 교정 판정)
    04_검토/                  검토_<단위>.html
    05_정오표/                반영내역 md/csv · 반영본 · 정오표_게시판 html · 정오표 xlsx · latest.json
  정오표_<책>_전체.xlsx         모든 단위 합본
"""
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WS = Path(os.environ.get("TSE_WORKSPACE", ROOT / "workspace"))
BACKUP = WS.parent / "backup"
BOOK_DIR = WS / "00_책"
STAGES = ["00_원고", "02_규칙검사", "03_판정", "04_검토", "05_정오표"]
MAX_PAGES = 20  # 이보다 긴 소절은 소제목 경계에서 나눈다


def _read(p, default=None):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def _write(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- 책 ----------
def book_pdf():
    if not BOOK_DIR.exists():
        return None
    pdfs = sorted(BOOK_DIR.glob("*.pdf"))
    return pdfs[0] if pdfs else None


def book_name():
    pdf = book_pdf()
    return slug(pdf.stem) if pdf else "책"


def toc():
    return _read(BOOK_DIR / "toc.json")


def save_toc(data):
    _write(BOOK_DIR / "toc.json", data)


def rules_book():
    return _read(BOOK_DIR / "rules_book.json")


def save_rules_book(data):
    _write(BOOK_DIR / "rules_book.json", data)


# ---------- 단위 ----------
def slug(title):
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", title.strip())
    return s.strip("_.")[:36] or "단위"


def units():
    if not WS.exists():
        return []
    out = []
    for d in WS.iterdir():
        u = _read(d / "unit.json") if d.is_dir() else None
        if u:
            out.append(u)
    return sorted(out, key=lambda u: u.get("order", 0))


def label(uid, title):
    """'1.1' + '1.1 생성형 AI…' → '1.1 생성형 AI…' (번호가 두 번 나오지 않게)."""
    return title if title.startswith(uid) else f"{uid} {title}"


def unit_dir(uid):
    """uid = '1.1', '3.3a', '부록1' 같은 단위 번호. 폴더 이름은 '<uid>_<제목>'."""
    if WS.exists():
        for d in WS.iterdir():
            u = _read(d / "unit.json") if d.is_dir() else None
            if u and u["id"] == uid:
                return d
    raise SystemExit(f"단위 워크스페이스가 없습니다: {uid} (먼저 단위 목록을 확정하세요)")


def unit(uid):
    return _read(unit_dir(uid) / "unit.json")


def save_unit(u):
    d = WS / f"{slug(u['id'])}_{slug(u['title'])}"
    try:
        old = unit_dir(u["id"])
        if old != d:  # 제목이 바뀌면 폴더 이름도 맞춘다
            old.rename(d)
    except SystemExit:
        pass
    for s in STAGES:
        (d / s).mkdir(parents=True, exist_ok=True)
    _write(d / "unit.json", u)
    return d


def stage(uid, name):
    d = unit_dir(uid) / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def merged_xlsx():
    return WS / f"정오표_{book_name()}_전체.xlsx"


# ---------- 새 책 / 백업 ----------
def _ws_empty():
    return not WS.exists() or not any(WS.iterdir())


def backup_current():
    """현재 워크스페이스를 backup/<시각>_<책이름>/ 으로 옮긴다 (지우지 않음)."""
    import shutil
    from datetime import datetime
    if _ws_empty():
        return None
    dest = BACKUP / f"{datetime.now():%Y%m%d-%H%M%S}_{book_name() if book_pdf() else '책없음'}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(WS), str(dest))
    WS.mkdir(parents=True, exist_ok=True)
    return dest


def backups():
    if not BACKUP.exists():
        return []
    out = []
    for d in sorted(BACKUP.iterdir(), reverse=True):
        if not (d / "00_책").is_dir():  # [새 책 넣기]로 만든 책 전체 백업만
            continue
        t = _read(d / "00_책" / "toc.json") or {}
        out.append({"name": d.name, "book": t.get("book"),
                    "units": sum(1 for x in d.iterdir() if (x / "unit.json").exists())})
    return out


def restore(name):
    """백업을 워크스페이스로 되돌린다. 지금 작업은 먼저 백업한다."""
    import shutil
    src = BACKUP / name
    if not (src / "00_책").is_dir() or src.parent != BACKUP:
        raise ValueError(f"백업이 없습니다: {name}")
    saved = backup_current()
    if WS.exists():
        WS.rmdir()  # backup_current 뒤의 빈 폴더
    shutil.move(str(src), str(WS))
    return saved
