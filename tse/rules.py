"""규칙 검사 — LLM 없이 책 전체를 훑어 일관성·번호·참조 오류를 찾는다.

번호·참조는 책 전체를 봐야 알 수 있어서 한 번에 모든 단위를 검사하고, 찾은 항목을 단위별로 나눠 저장한다.
  <단위>/02_규칙검사/rules.json   {unit, rows[]}
  00_책/rules_book.json           책 전체 메모 (따옴표 통계, 소제목 번호 체계, 인용되지 않은 참고문헌 …)

항목(row)은 Codex 판정과 같은 모양: page(인쇄 쪽) · judge_type · p_revise · old_text · new_text · reason · source="rule"
고칠 문구를 정할 수 없는 항목은 new_text = old_text (검토 화면에서 직접 고쳐 쓴다), bulk=True 는 일괄 수정 지시.
"""
import json
import re
from collections import Counter, defaultdict

from . import config
from .extract_book import load_book, unit_pages
from .pdftext import norm

CAP_RE = re.compile(r"^(표|그림)\s*(\d+)\s*-\s*(\d+)\s*(?:\([가-힣]\))?\s*\.\s*(.*)$")
REF_RE = re.compile(r"(?<![가-힣])(표|그림)\s*(\d+)\s*-\s*(\d+)(?!\s*\.\s)(?!\d)")
CITE_RE = re.compile(r"\[(\d+(?:\s*[,~–-]\s*\d+)*)\]")
REFLIST_RE = re.compile(r"^\[(\d+)\]\s")
REFS_IN_RE = re.compile(r"(?:^|\s)\[(\d+)\]\s")
SEC_RE = re.compile(r"(?<![제\d가-힣])(\d{1,2})\s?(절|장)(?=[에은는이을를의과와도로만,.\s])")
SUBNUM_RE = re.compile(r"^(\d{1,2})\)\s")
DQ_RE = re.compile(r'"([^"\n]{1,80})"')
SQ_RE = re.compile(r"(?<![\w'])'(?!\d\d)([^'\n]{1,40})'(?![\w])")
PAIR_DASH_RE = re.compile(r"(^|\s)([–—-])\s(\S.{0,40}?\S)\s([–—-])(\s|$)")
REPEAT_RE = re.compile(r"(?<![가-힣A-Za-z])([가-힣A-Za-z]{2,})\s+\1(?![가-힣A-Za-z])")
POLITE_RE = re.compile(r"((?<!아)니다|세요|십시오|에요|예요)[.!?]?$")
PLAIN_RE = re.compile(r"(다|까|라|자)[.!?]$")
BRACKETS = [("(", ")"), ("「", "」"), ("『", "』")]  # 큰따옴표는 여러 문단에 걸친 대화가 많아 뺀다


def snippet(text, s, e, pad=12):
    """text[s:e] 를 앞뒤 몇 글자와 함께 (줄을 넘지 않게)."""
    a = max(text.rfind("\n", 0, s) + 1, s - pad)
    b = text.find("\n", e)
    b = min(len(text) if b < 0 else b, e + pad)
    # 낱말 중간에서 끊기지 않게 공백까지 (조금만) 넓힌다
    while a > 0 and a > s - pad - 8 and not text[a - 1].isspace():
        a -= 1
    while b < len(text) and b < e + pad + 8 and not text[b].isspace():
        b += 1
    return a, b


def row(page, jt, p, old, new, reason, rule, **kw):
    return {"page": page, "judge_type": jt, "p_revise": p, "old_text": old, "new_text": new,
            "reason": reason, "rule": rule, "source": "rule", **kw}


def sub_row(page, text, s, e, repl, jt, p, reason, rule):
    a, b = snippet(text, s, e)
    old = text[a:b]
    new = text[a:s] + repl + text[e:b]
    return row(page, jt, p, old, new, reason, rule)


class Ctx:
    """책 전체에서 먼저 모아 두는 것."""

    def __init__(self, units, data, toc):
        self.units, self.data = units, data
        self.style = (toc or {}).get("style", {})
        self.caps = []          # (type, n, m, uid, page, text)
        self.heads = []         # (uid, page, text, size)
        self.refs = set()       # 참고문헌 번호
        self.cited = set()
        self.q = Counter()
        self.cap_size = defaultdict(Counter)
        for u in units:
            for _, printed, items in data[u["id"]]:
                for it in items:
                    t = it["text"]
                    m = CAP_RE.match(t) if it["kind"] in ("para", "head") else None
                    if m:
                        self.caps.append((m.group(1), int(m.group(2)), int(m.group(3)), u["id"], printed, t))
                        self.cap_size[m.group(1)][it.get("size") or "본문"] += 1
                    if it["kind"] == "head" and not (m and CAP_RE.match(t)):
                        self.heads.append((u["id"], printed, t, it.get("size")))
                    if is_refs(u):
                        self.refs.update(int(x) for x in REFS_IN_RE.findall(t))
                    self.q['"'] += len(DQ_RE.findall(t)); self.q["“"] += t.count("“")
                    self.q["'"] += len(SQ_RE.findall(t)); self.q["‘"] += t.count("‘")
        self.cap_set = {(c[0], c[1], c[2]) for c in self.caps}
        sizes = Counter(h[3] for h in self.heads)
        self.sub_size = min(sizes, key=lambda s: (s or 0)) if sizes else None  # 가장 작은 제목 = 소제목
        self.sec_word = Counter(m.group(1)[-1] for _, _, t, _ in self.heads
                                for m in [re.match(r"^제\s*\d+\s*(절|장)", t)] if m)
        self.dash = Counter(d for _, _, t, _ in self.heads for d in re.findall(r"\s([–—-])\s", t))


def is_refs(u):
    return "참고문헌" in u["title"] or "참고 문헌" in u["title"]


def part_no(u):
    m = re.match(r"제\s*(\d+)\s*[절장]", u.get("part", ""))
    return int(m.group(1)) if m else None


def check_quotes(u, pages, ctx, rows):
    curly = ctx.style.get("quotes", "curly") == "curly"
    if not curly:
        return
    hits = []
    for _, printed, items in pages:
        for it in items:
            t = it["text"]
            mixed = ("“" in t or "‘" in t)
            for rx, (o, c) in ((DQ_RE, "“”"), (SQ_RE, "‘’")):
                for m in rx.finditer(t):
                    hits.append((printed, t, m, o, c, mixed))
    if not hits:
        return
    many = len(hits) > 5
    for printed, t, m, o, c, mixed in hits:
        if many and not mixed:
            continue
        rows.append(sub_row(printed, t, m.start(), m.end(), f"{o}{m.group(1)}{c}", "문장부호",
                            0.8 if mixed else 0.7,
                            "한 문단 안에 곧은 따옴표와 굽은 따옴표가 섞여 있음 — 굽은 따옴표로 통일" if mixed
                            else "곧은 따옴표 → 굽은 따옴표(책 표기 통일)", "따옴표"))
    if many:
        pages_ = sorted({h[0] for h in hits})
        rows.append(row(pages_[0], "표기통일", 0.7, f'곧은 따옴표 " \' {len(hits)}곳',
                        "굽은 따옴표 “ ” ‘ ’ 로 일괄 통일",
                        f"이 단위에 곧은 따옴표가 {len(hits)}곳 있음 (p.{', '.join(map(str, pages_))}). "
                        "조판에서 일괄 치환하세요. 한 문단에 두 종류가 섞인 곳은 따로 적었습니다.",
                        "따옴표", bulk=True))


def check_dashes(u, pages, ctx, rows):
    major = ctx.dash.most_common(1)[0][0] if ctx.dash else "—"
    for _, printed, items in pages:
        for it in items:
            t = it["text"]
            for m in (PAIR_DASH_RE.finditer(t) if it["kind"] in ("para", "head") else ()):
                if m.group(2) != m.group(4):
                    d = m.group(2) if m.group(2) != "-" else major
                    rows.append(sub_row(printed, t, m.start(2), m.end(4),
                                        f"{d} {m.group(3)} {d}", "문장부호", 0.9,
                                        f"앞뒤 줄표가 서로 다름 ({m.group(2)} … {m.group(4)})", "줄표"))
            if it["kind"] == "head" and major != "-" and " - " in t:
                k = t.index(" - ")
                rows.append(sub_row(printed, t, k, k + 3, f" {major} ", "표기통일", 0.7,
                                    f"다른 제목은 '{major}' 를 쓰는데 이 제목만 하이픈(-)", "줄표"))


def check_captions(u, pages, ctx, rows):
    pn = part_no(u)
    seen = defaultdict(list)
    for c in ctx.caps:
        seen[(c[0], c[1])].append(c)
    for _, printed, items in pages:
        for k, it in enumerate(items):
            t = it["text"]
            m = CAP_RE.match(t) if it["kind"] in ("para", "head") else None
            if m:
                typ, n, num = m.group(1), int(m.group(2)), int(m.group(3))
                usual = ctx.cap_size[typ].most_common(1)[0][0]
                if (it.get("size") or "본문") != usual:
                    rows.append(row(printed, "표기통일", 0.6, t[:60], t[:60],
                                    f"이 {typ} 제목만 글자 크기가 다름 ({it.get('size') or '본문'}pt, 다른 {typ} 제목은 "
                                    f"{usual}{'' if usual == '본문' else 'pt'}) — 캡션 스타일 통일", "캡션"))
                group = seen[(typ, n)]
                nums = [g[2] for g in group]
                if nums.count(num) > 1 and not re.match(r"^\S+\s*\d+\s*-\s*\d+\s*\(", t):  # 4-1 (가)/(나) 는 따로 셈
                    rows.append(row(printed, "번호·참조", 0.9, t[:60], t[:60],
                                    f"{typ} {n}-{num} 번호가 두 번 쓰임 — 뒤 번호를 고치고 본문 참조도 함께 확인", "표그림번호"))
                prev = [x for x in nums if x < num]
                if prev and max(prev) != num - 1 and nums.index(num) == len(prev):
                    rows.append(row(printed, "번호·참조", 0.6, t[:60], t[:60],
                                    f"{typ} {n}-{max(prev)} 다음이 {typ} {n}-{num} — {typ} {n}-{max(prev) + 1} 이(가) 빠짐",
                                    "표그림번호"))
                if not prev and num != 1:
                    rows.append(row(printed, "번호·참조", 0.6, t[:60], t[:60],
                                    f"{typ} {n}- 번호가 1이 아니라 {num}부터 시작", "표그림번호"))
                if pn and n != pn:
                    rows.append(row(printed, "번호·참조", 0.7, t[:60], t[:60],
                                    f"제{pn}절 안의 {typ}인데 번호 앞자리가 {n}", "표그림번호"))
                if re.search(r"[가-힣]다\.\s*$", m.group(4)) or re.search(r"[가-힣]다\.\s", m.group(4)):
                    rows.append(row(printed, "표기통일", 0.5, t[:80], t[:80],
                                    f"{typ} 제목에 설명 문장이 붙어 있음 — 제목과 설명(주석)을 나눌지 확인", "캡션"))
                nxt = next((x for x in items[k + 1:] if x["kind"] != "small" or x["text"].strip()), None)
                prv = items[k - 1] if k else None
                if typ == "표" and nxt and nxt["kind"] in ("para", "head"):
                    rows.append(row(printed, "번호·참조", 0.4, t[:60], t[:60],
                                    "표 제목 바로 아래에 표가 없음 — 표와 제목 위치 확인", "캡션"))
                if typ == "그림" and prv and prv["kind"] in ("para", "head", "table"):
                    rows.append(row(printed, "번호·참조", 0.4, t[:60], t[:60],
                                    "그림 제목 바로 위에 그림이 없음 — 그림과 제목 위치 확인", "캡션"))
                continue
            for r in REF_RE.finditer(t):
                key = (r.group(1), int(r.group(2)), int(r.group(3)))
                if key not in ctx.cap_set:
                    a, b = snippet(t, r.start(), r.end())
                    rows.append(row(printed, "번호·참조", 0.9, t[a:b], t[a:b],
                                    f"본문이 가리키는 {key[0]} {key[1]}-{key[2]} 이(가) 책에 없음", "표그림참조"))


def check_section_refs(u, pages, ctx, rows):
    if not ctx.sec_word:
        return
    for _, printed, items in pages:
        for it in items:
            if it["kind"] not in ("para", "table", "small"):
                continue
            t = it["text"]
            for m in SEC_RE.finditer(t):
                if m.group(2) not in ctx.sec_word:
                    continue
                rows.append(sub_row(printed, t, m.start(), m.end(), f"제{m.group(1)}{m.group(2)}", "표기통일", 0.8,
                                    f"제목은 '제N{m.group(2)}'로 쓰므로 본문 지칭도 맞춤", "절표기"))


def check_subheads(u, pages, ctx, rows, last):
    """소제목 1) 2) … 번호 건너뜀·중복 (나뉜 단위는 이어서 센다)."""
    base = re.sub(r"[a-z]$", "", u["id"])
    prev = last.get(base, 0)
    for _, printed, items in pages:
        for it in items:
            if it["kind"] != "head":
                continue
            m = SUBNUM_RE.match(it["text"])
            if not m:
                if re.match(r"^\d+\.\d+\s", it["text"]):
                    prev = 0
                continue
            n = int(m.group(1))
            if n != prev + 1:
                restart = n == 1
                rows.append(row(printed, "번호·참조", 0.4 if restart else 0.8, it["text"][:60], it["text"][:60],
                                f"소제목 번호가 {prev}) 다음 {n})"
                                + (" — 다시 1)부터 (상자 안 번호면 무시)" if restart
                                   else " (중복)" if n <= prev else " (건너뜀)"), "소제목번호"))
            prev = n
    last[base] = prev


def check_register(u, pages, ctx, rows):
    """한다체 단위 안의 합니다체 문장 (따옴표 안·대화·부록 제외)."""
    if u.get("part", "").startswith("부록") or u["id"].startswith("부록"):
        return
    sents = []
    for _, printed, items in pages:
        for it in items:
            if it["kind"] != "para" or CAP_RE.match(it["text"]):
                continue
            ss = re.split(r"(?<=[.!?])\s+", it["text"])
            mixed = any(PLAIN_RE.search(s) and not POLITE_RE.search(s) for s in ss)
            sents += [(printed, it["text"], s, mixed) for s in ss]
    polite = [x for x in sents if POLITE_RE.search(x[2])]
    plain = [x for x in sents if PLAIN_RE.search(x[2]) and not POLITE_RE.search(x[2])]
    if len(plain) < len(polite) * 3:
        return  # 이 단위는 원래 높임체가 많다 (대화·안내문 위주)
    for printed, t, s, mixed in polite:
        if not mixed:  # 문단 전체가 높임체면 예시 요청문·안내문일 가능성이 크다
            continue
        if re.search(r"[\"“”'‘’]|^[^\s:]{1,8}\s?:|^[-•·□◆▶]", s):
            continue
        rows.append(row(printed, "비문", 0.4, s[-60:], s[-60:],
                        "한다체 본문에 합니다체 문장 — 예시·인용이 아니면 한다체로", "문체"))


def check_cites(u, pages, ctx, rows):
    if not ctx.refs or is_refs(u):
        return
    for _, printed, items in pages:
        for it in items:
            t = it["text"]
            if REFLIST_RE.match(t):
                continue
            for m in CITE_RE.finditer(t):
                nums = set()
                for part in re.split(r"\s*,\s*", m.group(1)):
                    ab = re.split(r"\s*[~–-]\s*", part)
                    if len(ab) == 2 and ab[0].isdigit() and ab[1].isdigit() and int(ab[1]) - int(ab[0]) < 50:
                        nums.update(range(int(ab[0]), int(ab[1]) + 1))
                    elif ab[0].isdigit():
                        nums.add(int(ab[0]))
                ctx.cited |= nums
                miss = sorted(n for n in nums if n not in ctx.refs)
                if miss:
                    a, b = snippet(t, m.start(), m.end())
                    rows.append(row(printed, "번호·참조", 0.9, t[a:b], t[a:b],
                                    f"참고문헌에 없는 번호 {miss} 를 인용", "인용번호"))


def check_repeats(u, pages, ctx, rows):
    for _, printed, items in pages:
        for it in items:
            t = it["text"]
            for m in REPEAT_RE.finditer(t):
                if m.group(1) in ("하나", "둘", "다시", "가끔", "자꾸", "천천히", "조금", "점점", "하나하나"):
                    continue
                rows.append(sub_row(printed, t, m.start(), m.end(), m.group(1), "오탈자", 0.7,
                                    f"'{m.group(1)}' 이(가) 두 번 반복됨", "반복어"))
            bare = re.sub(r"(?:^|(?<=\s))(?:\d{1,2}|[①-⑳가나다라마바사아자차카타파하])\)", "", t)  # '5)항' 같은 번호 괄호는 빼고 센다
            for o, c in BRACKETS:
                if bare.count(o) != bare.count(c) and it["kind"] == "para":
                    rows.append(row(printed, "문장부호", 0.4, t[:50], t[:50],
                                    f"이 문단에서 {o} {bare.count(o)}개, {c} {bare.count(c)}개 — 짝이 맞는지 확인", "괄호짝"))


def toc_entries(units, data):
    """앞쪽 인쇄 목차 항목 [(uid, 인쇄쪽, 항목)]."""
    out, on = [], False
    for u in units[:2]:
        for _, printed, items in data[u["id"]]:
            for it in items:
                t = it["text"].strip()
                if re.fullmatch(r"(목\s*차|차\s*례|CONTENTS)", t, re.I):
                    on = True
                    continue
                if on:
                    if len(t) > 90 or re.search(r"[가-힣]다\.$", t):
                        on = False
                        continue
                    out.append((u["id"], printed, t))
    return out


def check_toc(units, data, ctx, by_unit, notes):
    entries = toc_entries(units, data)
    if not entries:
        return
    toc_pages = {(uid, p) for uid, p, _ in entries}
    heads = [norm(t) for uid, p, t, _ in ctx.heads if (uid, p) not in toc_pages]
    heads += [norm(u["title"]) for u in units] + [norm(u.get("part", "")) for u in units]
    heads = [h for h in heads if h]
    for uid, p, t in entries:
        k = norm(t)[:12]
        if k and not any(h.startswith(k) or k in h for h in heads):
            by_unit[uid].append(row(p, "번호·참조", 0.7, t, "",
                                    "목차에만 있고 본문에 없는 항목 — 목차에서 빼거나 본문에 해당 꼭지를 넣으세요", "목차"))
    ent = [norm(t) for _, _, t in entries]
    for u in units:
        if u["id"][-1:] in "bcdefg" and re.search(r"[a-z]$", u["id"]):
            continue
        k = norm(re.sub(r"\s*\(\d+/\d+\)$", "", u["title"]))[:12]
        if k and not any(e.startswith(k) for e in ent):
            notes.append(f"본문 단위 '{u['title']}' 이(가) 인쇄 목차에 없음")


def run_book():
    t = config.toc() or {}
    units = [u for u in config.units() if u.get("include", True)]
    if not units:
        raise SystemExit("단위 워크스페이스가 없습니다. 먼저 단위 목록을 확정하세요.")
    book = load_book()
    data = {u["id"]: unit_pages(book, u) for u in units}
    ctx = Ctx(units, data, t)
    by_unit = defaultdict(list)
    notes, last = [], {}
    for u in units:
        rows, pages = by_unit[u["id"]], data[u["id"]]
        for fn in (check_quotes, check_dashes, check_captions, check_section_refs, check_register,
                   check_cites, check_repeats):
            fn(u, pages, ctx, rows)
        check_subheads(u, pages, ctx, rows, last)
    check_toc(units, data, ctx, by_unit, notes)

    # 책 전체 메모
    q = ctx.q
    dq, cdq, sq, csq = q['"'], q["“"], q["'"], q["‘"]
    rule = "굽은 따옴표" if ctx.style.get("quotes", "curly") == "curly" else "그대로 둠"
    notes.insert(0, f'따옴표: 곧은 " {dq}쌍 · 굽은 “ {cdq}개 · 곧은 \' {sq}쌍 · 굽은 ‘ {csq}개 → 기준: {rule}')
    if ctx.dash:
        notes.append("제목의 줄표: " + ", ".join(f"'{d}' {n}개" for d, n in ctx.dash.most_common()))
    numbered = defaultdict(lambda: [0, 0])
    for uid, _, txt, size in ctx.heads:
        if size == ctx.sub_size:
            numbered[re.sub(r"[a-z]$", "", uid)][0 if SUBNUM_RE.match(txt) else 1] += 1
    plain = [k for k, (a, b) in numbered.items() if a == 0 and b > 0]
    mixed = [k for k, (a, b) in numbered.items() if a and b]
    if plain and any(a for a, _ in numbered.values()):
        notes.append(f"소제목 번호 체계가 단위마다 다름 — 번호 없음: {', '.join(plain)}"
                     + (f" / 섞임: {', '.join(mixed)}" if mixed else ""))
    if ctx.refs:
        unused = sorted(ctx.refs - ctx.cited)
        if unused:
            notes.append(f"본문에서 인용하지 않은 참고문헌 {len(unused)}개: {unused[:40]}")
    empties = [f"p.{p}" for l, ttl, p in book.doc.get_toc() if ttl.strip() in ("(내용없음)", "(내용 없음)")]
    if empties:
        notes.append(f"PDF 책갈피에 '(내용없음)' 항목 {len(empties)}개 ({', '.join(empties)}) — 인쇄본과 무관, PDF 배포 시 정리")

    total = 0
    for u in units:
        rows = by_unit[u["id"]]
        for k, r in enumerate(rows):
            r["rid"] = f"R{k + 1}"
        f = config.stage(u["id"], "02_규칙검사") / "rules.json"
        f.write_text(json.dumps({"unit": u["id"], "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
        total += len(rows)
    config.save_rules_book({"notes": notes, "quotes": dict(q), "captions": len(ctx.caps),
                            "refs": len(ctx.refs), "units": len(units)})
    print(f"[규칙] {len(units)}개 단위 · 항목 {total}건 · 책 전체 메모 {len(notes)}건")
    return by_unit, notes


def load(uid):
    f = config.stage(uid, "02_규칙검사") / "rules.json"
    return json.loads(f.read_text(encoding="utf-8"))["rows"] if f.exists() else []


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    by_unit, notes = run_book()
    for n in notes:
        print(" *", n)
    for uid, rows in by_unit.items():
        for r in rows:
            print(f"{uid:>6} p.{r['page']:<4} {r['rule']:<6} {r['p_revise']:.1f} | {r['old_text'][:50]} → {r['new_text'][:50]} | {r['reason'][:60]}")
