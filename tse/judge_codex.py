"""교정 판정: 단위를 4쪽 구간으로 나눠 구간마다 Codex CLI(ChatGPT OAuth 로그인)를 호출해
오탈자·맞춤법·띄어쓰기·문장부호·표기통일·비문을 찾는다. 규칙 검사(rules.py) 항목과 합쳐 검토 후보를 만든다.

산출: <단위>/03_판정/results.json   (캐시: 03_판정/cache/<구간>_<sha12>.json)
"""
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config, extract_book, rules

WINDOW = 4  # 한 번에 보는 쪽 수
JUDGE_TYPES = ["오탈자", "맞춤법", "띄어쓰기", "문장부호", "표기통일", "비문"]
ALL_TYPES = JUDGE_TYPES + ["번호·참조"]  # 번호·참조는 규칙 검사만

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "items"],
    "properties": {
        "summary": {"type": "string"},
        "items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["page", "judge_type", "p_revise", "old_text", "new_text", "reason"],
            "properties": {
                "page": {"type": "integer"},
                "judge_type": {"type": "string", "enum": JUDGE_TYPES},
                "p_revise": {"type": "number"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
                "reason": {"type": "string"},
            }}},
    },
}

PROMPT = """너는 통신연수 교재의 교정·교열 편집자다. 가제본을 사람이 정독하기 전에, [교재 구간]을 한 문장씩 읽고 정오표에 올릴 오류를 찾아라.

찾을 것 (judge_type):
- 오탈자: 빠지거나 잘못 들어간 글자, 잘못 변환된 글자, 같은 낱말·조사의 반복
- 맞춤법: 한글 맞춤법·표준어 규정 위반 (예: 되/돼, 안/않, -든지/-던지, 로서/로써, 사이시옷, 외래어 표기법)
- 띄어쓰기: 한글 맞춤법 띄어쓰기 규정 위반 (의존 명사, 보조 용언, 단위 명사, 조사 붙여 쓰기 등)
- 문장부호: 마침표 누락·중복, 괄호·따옴표 짝, 쉼표·가운뎃점 오용
- 표기통일: 이 구간 안에서 같은 용어·숫자·단위가 다르게 쓰인 곳 (예: 보이스피싱/보이스 피싱, 6천억원/6천억 원)
- 비문: 주어·서술어 호응이 어긋나거나, 조사가 틀려 뜻이 달라지거나, 문장이 끝나지 않은 곳

판정 방식 (JEV):
- J(Judge): 오류 유형을 위에서 고른다.
- E(Evidence): old_text = 교재 원문을 그대로(띄어쓰기·기호 포함) 10~80자 복사. 오류 부분과 앞뒤 몇 글자를 넣어 이 구간에서 한 번만 나오게 한다.
- V(Verdict): new_text = old_text 에서 오류만 고친 문구(나머지 글자는 그대로). p_revise = 고쳐야 할 확률(0~1).
  규정상 명백한 오류 0.9 이상 · 대부분 고치는 편 0.6~0.8 · 문맥·관행에 따라 다를 수 있음 0.3~0.5.
  reason = 근거 한 문장 (가능하면 규정 이름: 예 '의존 명사는 띄어 쓴다').

규칙:
1. 확실한 오류만 낸다. 맞는 표현을 취향대로 바꾸지 마라. 문체 다듬기·요약·어순 바꾸기·내용 보충은 하지 마라.
2. 사실·수치·법령·기관명·인용 내용이 맞는지는 판단하지 마라 (사람이 정독에서 본다).
3. 본문 문체는 {register}다. 예시 대화·요청문·안내문 속 높임말은 그대로 둔다.
4. 다음은 규칙 검사가 따로 잡으므로 내지 마라: 곧은/굽은 따옴표 종류, 표·그림 번호, '1절/제1절' 표기, 줄표(—/-) 종류, 참고문헌 번호.
5. 줄바꿈은 이미 정리되어 있다. 표는 '[표] 칸 | 칸 | 칸' 한 줄, 그림 자리는 '[그림]'으로 보인다. ' | ' 와 '[표]' '[그림]'은 원문이 아니니 고치지 마라.
6. 한 항목에 오류 하나. 항목끼리 old_text 가 겹치지 않게. 같은 오류가 여러 곳이면 곳마다 따로 낸다.
7. page 는 [p.N] 표시를 따른다. 지어내지 마라. 오류가 없으면 items 를 빈 배열로 둔다.
8. 파일을 읽거나 명령을 실행하지 말고, 이 프롬프트만으로 답하라.
summary = 이 구간의 교정 상태를 한 문장으로.

[교재 구간: {title} p.{s}–{e}]
{part}
"""


def codex_bin():
    exe = shutil.which("codex") or shutil.which("codex.cmd")
    if not exe:
        raise SystemExit("codex CLI를 찾을 수 없습니다. setup.bat 을 먼저 실행하세요.")
    return exe


class CodexAuthError(RuntimeError):
    """이 PC의 Codex(ChatGPT) 로그인이 만료·해제됨 — 다시 로그인해야 한다."""
    kind = "auth"


AUTH_RE = re.compile(r"Failed to refresh token|log out and sign in|unauthorized \(401\)|\(401\)|HTTP 401|not logged in"
                     r"|Please (?:re-?)?login|codex login", re.I)
AUTH_MSG = ("이 PC의 Codex 로그인이 만료됐습니다. 콘솔 왼쪽 아래 Codex 메뉴의 [로그인]"
            "(또는 codex-다시로그인.bat)으로 다시 로그인한 뒤 다시 누르세요.")


def _tidy(err, keep=6):
    """codex stderr: 시각·중복 줄을 걷어 내고 마지막 몇 줄만."""
    out = []
    for l in err.splitlines():
        l = re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s*", "", l).strip()
        if l and re.search(r"[A-Za-z가-힣]", l) and l not in out:
            out.append(l)
    return " / ".join(out[-keep:])[-600:]


def ask_codex(prompt, model=None, timeout=900, schema=None):
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "schema.json").write_text(json.dumps(schema or SCHEMA, ensure_ascii=False), encoding="utf-8")
        cmd = [codex_bin(), "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
               "-C", str(td), "--output-schema", str(td / "schema.json"),
               "-o", str(td / "out.json"), "--color", "never", "-"]
        if model:
            cmd[2:2] = ["-m", model]
        r = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, timeout=timeout)
        out = td / "out.json"
        if not out.exists():
            err = r.stderr.decode("utf-8", "replace")
            if AUTH_RE.search(err):
                raise CodexAuthError(AUTH_MSG + f"  (원문: {_tidy(err, 2)})")
            raise RuntimeError(f"codex 실패 (exit {r.returncode}): {_tidy(err)}")
        return json.loads(out.read_text(encoding="utf-8"))


# ---------- 원문 대조 ----------
def _key(s):
    return re.sub(r"\s+", "", s)


def loose(s):
    """공백·줄바꿈 차이를 무시하는 정규식."""
    chars = [re.escape(c) for c in re.sub(r"\s+", "", s)]
    return re.compile(r"\s*".join(chars)) if chars else None


def find(pages, row):
    """old_text 가 있는 쪽과 위치 (판정된 쪽 먼저). 없으면 (None, None)."""
    pat = loose(row["old_text"])
    if not pat:
        return None, None
    for p in sorted(pages, key=lambda p: p["page"] != row["page"]):
        m = pat.search(p["text"])
        if m:
            return p, m
    return None, None


def at_wrap(page, m, old, new):
    """띄어쓰기만 바뀌었고, 새로 넣는 공백이 모두 줄바꿈 이음 자리면 True
    (인쇄본에서 줄끝 공백은 보이지 않으므로 오류가 아니다)."""
    if _key(old) != _key(new):
        return False
    wraps = set(page.get("wraps", []))
    # 원문에서 공백이 아닌 글자들의 절대 위치
    pos = [m.start() + k for k, c in enumerate(page["text"][m.start():m.end()]) if not c.isspace()]

    def spaced(s):  # 공백 아닌 k번째 글자 앞에 공백이 있는지
        out, prev_space, k = set(), False, 0
        for c in s:
            if c.isspace():
                prev_space = True
                continue
            if prev_space and k:
                out.add(k)
            prev_space, k = False, k + 1
        return out

    added = spaced(new) - spaced(old)
    return bool(added) and len(pos) == len(_key(old)) and all(pos[k] in wraps for k in added)


def dedupe(rows):
    """같은 문구를 짚은 항목은 하나로 (수정확률 높은 것). 작은 항목이 큰 항목 안에 들고 고침도 같으면 뺀다."""
    best = {}
    for r in sorted(rows, key=lambda r: -r["p_revise"]):
        k = (r["page"], _key(r["old_text"]), _key(r["new_text"]))
        if k in best:
            best[k].setdefault("related", []).append(r.get("window") or r.get("rule"))
        else:
            best[k] = r
    rows = list(best.values())
    out = []
    for r in rows:
        ko, kn = _key(r["old_text"]), _key(r["new_text"])
        covered = any(b is not r and b["page"] == r["page"] and ko and ko in _key(b["old_text"])
                      and len(ko) < len(_key(b["old_text"])) and kn in _key(b["new_text"])
                      and b["source"] == r["source"] for b in rows)
        if not covered:
            out.append(r)
    return out


def windows(pages):
    out = []
    for k in range(0, len(pages), WINDOW):
        chunk = pages[k:k + WINDOW]
        out.append({"id": f"w{k // WINDOW + 1:02d}", "pages": [chunk[0]["page"], chunk[-1]["page"]], "chunk": chunk})
    return out


def run(uid, workers=3, model=None, limit=None):
    u = config.unit(uid)
    jd = config.stage(uid, "03_판정")
    cache = jd / "cache"
    cache.mkdir(exist_ok=True)
    book = extract_book.load(uid)
    pages = book["pages"]
    appendix = u["id"].startswith("부록") or u.get("part", "").startswith("부록")
    register = "합니다체(부록)" if appendix else "한다체 (부록만 합니다체)"
    wins = windows(pages)[:limit] if limit else windows(pages)

    def make_prompt(w):
        part = "".join(f"\n[p.{p['page']}]\n{p['text']}\n" for p in w["chunk"])
        return PROMPT.format(register=register, title=u["title"], s=w["pages"][0], e=w["pages"][1], part=part)

    def one(w):
        prompt = make_prompt(w)
        key = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12]
        cf = cache / f"{w['id']}_{key}.json"
        if cf.exists():
            print(f"  [cache] {w['id']} p.{w['pages'][0]}–{w['pages'][1]}")
            return w, json.loads(cf.read_text(encoding="utf-8")), False
        print(f"  [codex] {w['id']} p.{w['pages'][0]}–{w['pages'][1]} ...", flush=True)
        try:
            res = ask_codex(prompt, model=model)
        except CodexAuthError:  # 로그인 문제는 모든 구간이 똑같이 실패하므로 바로 멈춘다
            raise
        except Exception as e:  # 한 구간 실패가 전체를 막지 않게
            print(f"  [오류] {w['id']}: {e}")
            return w, {"summary": f"판정 실패: {e}", "items": []}, True
        cf.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  [판정] {w['id']} → {len(res['items'])}건")
        return w, res, False

    print(f"[판정] {uid} 4쪽 구간 {len(wins)}건을 Codex로 판정 (동시 {workers})")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(one, wins))

    rows, summaries, dropped = [], [], 0
    for w, res, failed in results:
        summaries.append({"window": w["id"], "pages": w["pages"], "summary": res["summary"],
                          "n_items": len(res["items"]), "failed": failed})
        for it in res["items"]:
            if _key(it["old_text"]) == _key(it["new_text"]) and it["old_text"].strip() == it["new_text"].strip():
                continue
            r = {**it, "p_revise": round(max(0.0, min(1.0, float(it["p_revise"]))), 2),
                 "source": "codex", "window": w["id"]}
            p, m = find(w["chunk"], r)
            if p is None:
                r["reason"] = "[원문 위치 미확인] " + r["reason"]
                r["p_revise"] = round(r["p_revise"] * 0.5, 2)
            else:
                r["page"] = p["page"]
                if r["judge_type"] == "띄어쓰기" and at_wrap(p, m, r["old_text"], r["new_text"]):
                    dropped += 1  # 줄끝 공백 — 인쇄본에서 보이지 않음
                    continue
            rows.append(r)
    for r in rules.load(uid):
        rows.append({**r, "window": ""})
    rows = dedupe(rows)
    order = {p["page"]: k for k, p in enumerate(pages)}
    rows.sort(key=lambda r: (order.get(r["page"], 999), r["source"] != "rule", -r["p_revise"]))
    for i, r in enumerate(rows):
        r["id"] = i + 1
        r["p_keep"] = round(1 - r["p_revise"], 2)

    out = {"unit": uid, "title": u["title"], "part": u.get("part", ""), "printed": u.get("printed"),
           "windows": summaries, "rows": rows,
           "notes": (config.rules_book() or {}).get("notes", []), "dropped_wrap": dropped}
    (jd / "results.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[판정] 검토 후보 {len(rows)}건 (규칙 {sum(r['source'] == 'rule' for r in rows)} · "
          f"AI {sum(r['source'] == 'codex' for r in rows)}, 줄끝 띄어쓰기 {dropped}건 제외) → {jd / 'results.json'}")
    return out


if __name__ == "__main__":
    import sys
    run(sys.argv[1] if len(sys.argv) > 1 else "1.1")
