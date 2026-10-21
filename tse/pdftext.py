"""PDF 한 쪽 → 읽는 순서대로 정리한 항목(제목·문단·표 행·그림) 목록.

HWP에서 내보낸 PDF는 줄마다 블록이 따로이고, 단어 중간에서도 줄이 바뀐다 ('실' / '제').
그래서 줄끝 공백을 그대로 두고 줄을 이어 붙여 문단을 되살린다 (줄바꿈이 띄어쓰기 오류로 잡히지 않게).
줄끝에 공백 없이 이어 붙인 자리(wraps)는 기록해 둔다 — 인쇄본에서 줄끝 띄어쓰기는 보이지 않으므로
그 자리에 '띄어 써야 한다'는 판정은 정오표에서 뺀다 (judge_codex.at_wrap).

항목 kind:
  head    본문보다 큰 글자 (절·소절·소제목)
  para    본문 문단 (캡션 '표 n-m.' '그림 n-m.' 도 여기)
  table   표의 한 행 — 칸을 ' | ' 로 이은 한 줄
  small   표 밖의 작은 글자 (표 아래 출처·교육용 주석 등)
  image   그림 자리 (텍스트 없음)
"""
import re
from collections import Counter

import fitz


def _lines(page):
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for l in b["lines"]:
            spans = [s for s in l["spans"] if s["text"]]
            t = "".join(s["text"] for s in spans)
            if not t.strip():
                continue
            ink = [s for s in spans if s["text"].strip()]
            out.append({"text": t, "x0": l["bbox"][0], "x1": l["bbox"][2], "y0": l["bbox"][1], "y1": l["bbox"][3],
                        "size": round(max(s["size"] for s in ink), 1),
                        "bold": all("bold" in s["font"].lower() or s["flags"] & 16 for s in ink)})
    return out


def body_metrics(doc, sample=40):
    """본문 글자 크기(글자 수로 가장 많이 쓰인 크기)와 본문 오른쪽 끝(양쪽맞춤 줄의 끝)."""
    cnt, ends = Counter(), Counter()
    n = doc.page_count
    for i in range(0, n, max(1, n // sample)):
        for l in _lines(doc[i]):
            cnt[l["size"]] += len(l["text"])
            ends[round(l["x1"])] += 1
    bsize = cnt.most_common(1)[0][0] if cnt else 11.0
    right = ends.most_common(1)[0][0] if ends else doc[0].rect.width
    return bsize, right


def _footer(lines, height):
    """쪽 아래 끝의 숫자 = 인쇄 쪽번호."""
    num, rest = None, []
    for l in lines:
        if l["y0"] > height * 0.92 and re.fullmatch(r"\s*-?\s*\d{1,4}\s*-?\s*", l["text"]):
            num = int(re.sub(r"\D", "", l["text"]))
        else:
            rest.append(l)
    return num, rest


def _join(parts):
    """줄 잇기 → (텍스트, 공백 없이 이은 자리 목록).
    줄끝 공백이 있으면 띄어 쓴 것, 없으면 단어 안에서(또는 보이지 않는 공백에서) 줄이 바뀐 것."""
    text, wraps = "", []
    for part in parts:
        part = re.sub(r"[ \t ]+", " ", part)
        if not text:
            text = part.lstrip()
            continue
        if not text.endswith(" ") and not part.startswith(" "):
            wraps.append(len(text))
        text += part
    stripped = text.rstrip()
    return stripped, [w for w in wraps if w < len(stripped)]


def _same_baseline(lines):
    """양쪽맞춤으로 낱말 사이가 크게 벌어진 줄(참고문헌 등)은 낱말마다 줄로 잡힌다 — 같은 높이면 한 줄로."""
    out = []
    for l in sorted(lines, key=lambda l: (round(l["y0"]), l["x0"])):
        p = out[-1] if out else None
        if p and abs(p["y0"] - l["y0"]) < 1 and l["x0"] >= p["x1"] - 1:
            sep = "" if p["text"].endswith(" ") or l["text"].startswith(" ") else " "
            p.update(text=p["text"] + sep + l["text"], x1=l["x1"], y1=max(p["y1"], l["y1"]))
        else:
            out.append(dict(l))
    return out


def _table_rows(small, pitch):
    """작은 글자 줄 → 칸(같은 x에서 줄 간격으로 이어지는 줄) → 행(세로로 겹치는 칸)."""
    cells = []
    for l in sorted(small, key=lambda l: (round(l["x0"]), l["y0"])):
        c = cells[-1] if cells else None
        if c and abs(c["x0"] - l["x0"]) < 3 and 0 < l["y0"] - c["last"] <= pitch:
            c["parts"].append(l["text"]); c["last"] = l["y0"]; c["y1"] = l["y1"]
        else:
            cells.append({"x0": l["x0"], "y0": l["y0"], "y1": l["y1"], "last": l["y0"], "parts": [l["text"]]})
    rows = []
    for c in sorted(cells, key=lambda c: c["y0"]):
        r = rows[-1] if rows else None
        if r and c["y0"] < r["y1"] - 2:
            r["cells"].append(c); r["y1"] = max(r["y1"], c["y1"])
        else:
            rows.append({"y0": c["y0"], "y1": c["y1"], "cells": [c]})
    out = []
    for r in rows:
        text, wraps = "", []
        for k, c in enumerate(sorted(r["cells"], key=lambda c: c["x0"])):
            if k:
                text += " | "
            t, w = _join(c["parts"])
            wraps += [len(text) + x for x in w]
            text += t
        out.append({"kind": "table" if len(r["cells"]) > 1 else "small", "text": text, "wraps": wraps,
                    "y0": r["y0"], "y1": r["y1"]})
    return out


def page_items(page, bsize, right):
    """한 쪽 → (인쇄 쪽번호, 항목 목록)."""
    num, lines = _footer(_lines(page), page.rect.height)
    body = _same_baseline([l for l in lines if abs(l["size"] - bsize) <= 0.3])
    pitch = bsize * 2.9  # 본문 줄 간격(약 2.6배)보다 조금 넉넉하게, 문단 사이(약 3.1배)보다 작게
    items, para = [], None
    for l in sorted(body, key=lambda l: l["y0"]):
        # 앞 줄이 오른쪽 끝까지 차지 않았으면 거기서 문단이 끝난 것 (차례·짧은 줄)
        joined = (para and abs(l["x0"] - para["x0"]) < 2 and 0 < l["y0"] - para["last"] <= pitch
                  and para["end"] >= right - bsize * 1.5)
        if joined:
            para["parts"].append(l["text"]); para["last"] = l["y0"]; para["y1"] = l["y1"]; para["end"] = l["x1"]
            continue
        para = {"kind": "para", "x0": l["x0"], "y0": l["y0"], "y1": l["y1"], "last": l["y0"], "end": l["x1"],
                "parts": [l["text"]]}
        items.append(para)
    for it in items:
        it["text"], it["wraps"] = _join(it.pop("parts"))
        for k in ("last", "x0", "end"):
            it.pop(k)
    items += [{"kind": "head", "text": _join([l["text"]])[0], "wraps": [], "y0": l["y0"], "y1": l["y1"],
               "size": l["size"]} for l in lines if l["size"] > bsize + 0.3]
    items += _table_rows([l for l in lines if l["size"] < bsize - 0.3], bsize * 2.0)
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") == 1 and (b["bbox"][3] - b["bbox"][1]) > 20:
            items.append({"kind": "image", "text": "", "wraps": [], "y0": b["bbox"][1], "y1": b["bbox"][3]})
    items.sort(key=lambda it: it["y0"])
    # 이어지는 같은 크기 제목 줄(두 줄로 넘어간 제목)은 하나로
    out = []
    for it in items:
        p = out[-1] if out else None
        if p and it["kind"] == p["kind"] == "head" and p.get("size") == it.get("size") \
                and 0 < it["y0"] - p["y1"] < bsize:
            p["text"] = f"{p['text']} {it['text']}"; p["y1"] = it["y1"]
            continue
        out.append(it)
    return num, out


PREFIX = {"table": "[표] ", "image": "[그림]"}


def render(items):
    """항목 → (판정·반영에 쓰는 쪽 텍스트, 쪽 텍스트 안의 줄바꿈 이음 자리). 한 항목 한 줄."""
    out, wraps, pos = [], [], 0
    for it in items:
        pre = PREFIX.get(it["kind"], "")
        line = pre + it["text"]
        wraps += [pos + len(pre) + w for w in it.get("wraps", [])]
        out.append(line)
        pos += len(line) + 1
    return "\n".join(out), wraps


def norm(s):
    return re.sub(r"[\s ]+", "", s or "")


class Book:
    """책 전체를 한 번 읽어 쪽마다 항목·인쇄 쪽번호를 들고 있는다."""

    def __init__(self, pdf):
        self.doc = fitz.open(pdf)
        self.bsize, self.right = body_metrics(self.doc)
        self.pages = [page_items(p, self.bsize, self.right) for p in self.doc]  # [(인쇄쪽 or None, items)] 0-based
        diffs = Counter(i + 1 - n for i, (n, _) in enumerate(self.pages) if n is not None)
        self.offset = diffs.most_common(1)[0][0] if diffs else 0  # PDF쪽 - 인쇄쪽

    @property
    def page_count(self):
        return len(self.pages)

    def printed(self, pdf_page):
        n = self.pages[pdf_page - 1][0]
        return n if n is not None else pdf_page - self.offset

    def items(self, pdf_page):
        return self.pages[pdf_page - 1][1]

    def find_head(self, pdf_page, title):
        """그 쪽에서 제목(책갈피 이름)으로 시작하는 항목의 순번. 없으면 None."""
        key = norm(title)[:14]
        if not key:
            return None
        for k, it in enumerate(self.items(pdf_page)):
            if it["kind"] in ("head", "para") and norm(it["text"]).startswith(key):
                return k
        return None
