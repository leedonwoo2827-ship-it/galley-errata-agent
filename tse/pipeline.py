"""명령줄 실행: 원고 추출 → 규칙 검사(책 전체) → Codex 교정 판정 → 검토 HTML.

python -m tse.pipeline --toc                 workspace/00_책 의 PDF로 단위 목록을 만들고 확정
python -m tse.pipeline 1.1 [--workers 3] [--model gpt-5] [--limit N]
python -m tse.pipeline --all                 포함된 모든 단위 (이미 판정한 구간은 캐시로 건너뜀)
"""
import argparse

from . import config, extract_book, judge_codex, report, rules, toc


def make_toc():
    pdf = config.book_pdf()
    if not pdf:
        raise SystemExit(f"{config.BOOK_DIR} 에 교재 PDF를 넣으세요.")
    t = toc.extract(pdf, extract_book.load_book())
    t["confirmed"] = True
    config.save_toc(t)
    for u in t["units"]:
        config.save_unit(u)
    print(f"[단위] {len(t['units'])}개 ({t['method']}) → {config.WS}")


def run(uid, workers=3, model=None, limit=None):
    book = extract_book.load_book()
    for u in config.units():  # 규칙 검사는 책 전체를 보므로 원고가 없는 단위도 먼저 뽑는다
        if u.get("include", True) and (u["id"] == uid or not (config.stage(u["id"], "00_원고") / "book.json").exists()):
            extract_book.run(u["id"], book)
    rules.run_book()
    judge_codex.run(uid, workers=workers, model=model, limit=limit)
    report.run(uid)


def run_all(workers=3, model=None, only_new=False):
    units = [u for u in config.units() if u.get("include", True)]
    book = extract_book.load_book()
    for u in units:
        extract_book.run(u["id"], book)
    rules.run_book()
    todo = [u for u in units if not (only_new and (config.stage(u["id"], "03_판정") / "results.json").exists())]
    print(f"[전체] {len(todo)}개 단위를 차례로 판정합니다.")
    for k, u in enumerate(todo, 1):
        print(f"[전체] {k}/{len(todo)} {u['id']} {u['title']}")
        judge_codex.run(u["id"], workers=workers, model=model)
        report.run(u["id"])
    from . import export_xlsx
    export_xlsx.merge()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("unit", nargs="?", default=None)
    ap.add_argument("--toc", action="store_true", help="교재 PDF에서 단위 목록을 뽑아 워크스페이스를 만든다")
    ap.add_argument("--all", action="store_true", help="모든 단위를 판정")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--model", default=None, help="codex 모델 (기본: codex 설정값)")
    ap.add_argument("--limit", type=int, default=None, help="테스트용: 앞 N개 구간만 판정")
    a = ap.parse_args()
    if a.toc:
        return make_toc()
    if a.all:
        return run_all(a.workers, a.model)
    if not a.unit:
        ap.error("단위 번호(예: 1.1)나 --all / --toc 를 주세요.")
    run(a.unit, a.workers, a.model, a.limit)


if __name__ == "__main__":
    main()
