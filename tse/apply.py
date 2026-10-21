"""[반영] 처리: 체크된 항목으로 단위 정오표를 만들고, 원고에 반영해 본 기록을 남긴다.

산출 (<단위>/05_정오표/):
  반영내역_<단위>_<ts>.md / .csv     — 반영·미반영 전체 기록
  <단위>_반영본_<ts>.md / .html      — 반영된 원고 (html은 변경부 하이라이트)
  정오표_게시판_<단위>_<ts>.html      — 게시판·가제본 배포용 정오표 (반영 건만)
  정오표_<단위>_<ts>.xlsx            — 엑셀 정오표 (정오표 · 전체 후보 시트)
  latest.json                        — 마지막 반영 기록 (전체 합본 xlsx가 읽는다)
그리고 workspace/정오표_<책>_전체.xlsx 를 새로 만든다.

CLI(서버 없이 HTML에서 내려받은 decisions JSON 처리):
  python -m tse.apply <decisions.json>
"""
import csv
import html
import json
from datetime import datetime

from . import config, export_xlsx
from .judge_codex import find


def status(rec, pages, edits):
    if not rec["apply"]:
        return "미반영"
    if rec.get("bulk"):
        return "일괄 수정 지시"
    if rec["old_text"].strip() == rec["new_text"].strip():
        return "변경없음"
    p, m = find(pages, rec)
    if p is None:
        return "원문위치 미확인 - 수동반영 필요"
    if any(not (m.end() <= s or m.start() >= e) for s, e, _, _ in edits[p["page"]]):
        return "다른 항목과 겹침 - 수동반영 필요"
    edits[p["page"]].append((m.start(), m.end(), rec["new_text"], rec["id"]))
    rec["page"] = p["page"]
    return "반영"


def run(decisions, uid=None):
    uid = uid or decisions.get("unit")
    u = config.unit(uid)
    res = json.loads((config.stage(uid, "03_판정") / "results.json").read_text(encoding="utf-8"))
    book = json.loads((config.stage(uid, "00_원고") / "book.json").read_text(encoding="utf-8"))
    pages = [dict(p) for p in book["pages"]]
    edits = {p["page"]: [] for p in pages}
    dec = {int(d["id"]): d for d in decisions["items"]}
    reviewer = decisions.get("reviewer") or "-"
    now = datetime.now()
    ts = now.strftime("%Y%m%d-%H%M")
    name = config.slug(uid)

    records = []
    for r in res["rows"]:
        d = dec.get(r["id"], {})
        rec = {**r, "new_text": d.get("new_text", r["new_text"]), "apply": bool(d.get("apply")),
               "note": d.get("note", ""), "unit": uid}
        rec["status"] = status(rec, pages, edits)
        records.append(rec)
    manual = [r for r in records if r["status"].endswith("필요")]

    out = config.stage(uid, "05_정오표")

    # 반영된 원고 (md: 깨끗한 본문 / html: 변경부 표시)
    md, hb = [f"# {config.label(uid, res['title'])} — 반영본 {ts}\n"], []
    for p in pages:
        t, pos, clean, marked = p["text"], 0, [], []
        for s, e, new, rid in sorted(edits[p["page"]]):
            clean += [t[pos:s], new]
            marked += [html.escape(t[pos:s]),
                       f'<del>{html.escape(t[s:e])}</del><ins title="#{rid}">{html.escape(new)}</ins>']
            pos = e
        clean.append(t[pos:]); marked.append(html.escape(t[pos:]))
        md.append(f"\n## p.{p['page']}\n\n" + "".join(clean))
        hb.append(f'<section><h2>p.{p["page"]}{" ✎" if edits[p["page"]] else ""}</h2><pre>{"".join(marked)}</pre></section>')
    if manual:
        md.append("\n## 수동 반영 항목\n")
        hb.append("<section><h2>수동 반영 항목</h2>")
        for r in manual:
            md.append(f"- p.{r['page']} [{r['status']}] {r['old_text']} → {r['new_text']}")
            hb.append(f"<p>p.{r['page']} [{html.escape(r['status'])}] <del>{html.escape(r['old_text'])}</del> → "
                      f"<ins>{html.escape(r['new_text'])}</ins></p>")
        hb.append("</section>")
    (out / f"{name}_반영본_{ts}.md").write_text("\n".join(md), encoding="utf-8")
    (out / f"{name}_반영본_{ts}.html").write_text(page_html(f"{uid} 반영본 {ts}", "".join(hb)), encoding="utf-8")

    # 반영내역
    applied = [r for r in records if r["apply"]]
    errata = [r for r in sorted(applied, key=lambda r: r["id"]) if r["status"] != "변경없음"]
    src = lambda r: "규칙" if r["source"] == "rule" else "AI"
    lines = [f"# 반영내역 — {config.label(uid, res['title'])}", "",
             f"- 교재: {(config.toc() or {}).get('book', '')} · 인쇄 p.{u['printed'][0]}–{u['printed'][1]}",
             f"- 반영일시: {now:%Y-%m-%d %H:%M}", f"- 반영자: {reviewer}",
             f"- 전체 후보 {len(records)}건 / 체크 {len(applied)}건 / 정오표 {len(errata)}건 / 수동 확인 {len(manual)}건", "",
             "| # | 쪽 | 유형 | 출처 | 수정확률 | 상태 | 기존문구 | 수정문구 | 메모 |", "|---|---|---|---|---|---|---|---|---|"]
    cell = lambda s: str(s).replace("|", "\\|").replace("\n", " ")
    for r in sorted(records, key=lambda r: (not r["apply"], r["id"])):
        lines.append(f"| {r['id']} | {r['page']} | {r['judge_type']} | {src(r)} | {round(r['p_revise'] * 100)}% | "
                     f"{r['status']} | {cell(r['old_text'])} | {cell(r['new_text'])} | {cell(r['note'])} |")
    (out / f"반영내역_{name}_{ts}.md").write_text("\n".join(lines), encoding="utf-8")
    cols = ["id", "page", "judge_type", "source", "p_revise", "apply", "status", "old_text", "new_text", "reason", "note"]
    with open(out / f"반영내역_{name}_{ts}.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader(); w.writerows(records)

    # 게시판용 정오표
    trs = "".join(
        f"<tr><td class=n>{r['page']}</td><td>{html.escape(r['judge_type'])}</td>"
        f"<td><del>{html.escape(r['old_text'])}</del></td>"
        f"<td>{'<i>(삭제)</i>' if not r['new_text'].strip() else '<ins>' + html.escape(r['new_text']) + '</ins>'}</td>"
        f"<td>{html.escape(r['reason'])}{' · ' + html.escape(r['note']) if r['note'] else ''}</td></tr>"
        for r in sorted(errata, key=lambda r: r["id"]))
    board = (f"<h1>정오표 — {html.escape(config.label(uid, res['title']))}</h1>"
             f"<p class=meta>{html.escape((config.toc() or {}).get('book', ''))} · 인쇄 p.{u['printed'][0]}–{u['printed'][1]}"
             f"<br>작성일 {now:%Y-%m-%d} · 반영자 {html.escape(reviewer)} · {len(errata)}건</p>"
             f"<table><thead><tr><th>쪽</th><th>유형</th><th>기존</th><th>수정</th><th>사유</th></tr></thead>"
             f"<tbody>{trs or '<tr><td colspan=5>고칠 곳이 없습니다.</td></tr>'}</tbody></table>")
    (out / f"정오표_게시판_{name}_{ts}.html").write_text(page_html(f"정오표 {uid}", board), encoding="utf-8")

    # 엑셀 + 마지막 기록 + 전체 합본
    meta = {"unit": uid, "title": res["title"], "reviewer": reviewer, "date": f"{now:%Y-%m-%d %H:%M}",
            "printed": u["printed"], "assignee": u.get("assignee", ""), "due": u.get("due", "")}
    export_xlsx.unit_book(out / f"정오표_{name}_{ts}.xlsx", records, meta)
    (out / "latest.json").write_text(json.dumps({**meta, "records": records}, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
    try:
        merged = export_xlsx.merge()
    except PermissionError:
        merged = None
        print("[정오표] 전체 합본 xlsx가 엑셀에서 열려 있어 갱신하지 못했습니다. 닫고 [전체 xlsx 합본]을 누르세요.")

    files = sorted(str(p.relative_to(config.WS)) for p in out.glob(f"*{ts}*"))
    if merged:
        files.append(str(merged.relative_to(config.WS)))
    print(f"[정오표] {uid} 체크 {len(applied)}건 → 정오표 {len(errata)}건 → {out}")
    return {"files": files, "counts": {"total": len(records), "checked": len(applied),
                                       "applied": sum(r["status"] == "반영" for r in records),
                                       "errata": len(errata)},
            "manual": [{"id": r["id"], "page": r["page"], "status": r["status"]} for r in manual]}


def page_html(title, body):
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>
body{{font-family:"Malgun Gothic","Apple SD Gothic Neo",sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#222;background:#fff}}
h1{{font-size:22px}} h2{{font-size:16px;margin-top:28px;border-bottom:1px solid #ddd}}
.meta{{color:#555;font-size:13px;line-height:1.6}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{border:1px solid #ccc;padding:6px 8px;vertical-align:top}}
th{{background:#f3f5f8}} td.n{{text-align:right;white-space:nowrap}} pre{{white-space:pre-wrap;font-family:inherit;line-height:1.7}}
del{{color:#b42318;background:#fde8e7}} ins{{color:#05603a;background:#e3f6ea;text-decoration:none;font-weight:600}}
@media print{{body{{margin:0;max-width:none}} th{{background:#eee}}}}
</style></head><body>{body}</body></html>"""


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        raise SystemExit("사용법: python -m tse.apply <decisions.json>")
    with open(sys.argv[1], encoding="utf-8") as f:
        print(json.dumps(run(json.load(f)), ensure_ascii=False, indent=1))
