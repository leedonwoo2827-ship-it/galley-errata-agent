"""TS 속독 정오표 에이전트 — 웹 콘솔 서버 (127.0.0.1 전용, 표준 라이브러리만 사용).

  GET  /                          콘솔 화면 (tse/console.html)
  GET  /api/state                 책 · 단위 목록 · 진행 현황 · 작업 상태
  GET  /api/unit?id=1.1           단위 상세 (판정 요약 · 폴더별 파일)
  POST /api/new-book              새 책 넣기: 지금 워크스페이스를 backup/ 으로 옮기고 비운다
  POST /api/restore               백업 불러오기 (지금 작업은 먼저 백업)
  POST /api/book/upload           교재 PDF 전체 올리기 → 단위(소절) 자동 추출
  POST /api/book/units            단위 목록 확정 (JSON) → 단위 워크스페이스 생성
  POST /api/book/toc-codex        Codex로 단위 다시 뽑기 (백그라운드)
  POST /api/rules                 규칙 검사만 다시 (백그라운드)
  POST /api/run-all               모든 단위 판정 (백그라운드, {only_new})
  POST /api/export-all            전체 합본 xlsx 다시 만들기
  POST /api/codex/refresh|login|logout
  POST /api/unit/run?id=          추출 → 규칙 → Codex 판정 → 검토 HTML (백그라운드)
  POST /api/unit/meta?id=         담당자 · 마감 저장
  POST /api/unit/open?id=&stage=  탐색기로 폴더 열기
  GET  /review/<id>               검토 화면
  POST /apply                     [반영] → 정오표
  GET  /files/<id>/<path>[?dl=1]  단위 폴더의 파일
  GET  /merged · /merged.html     책 전체 정오표 (xlsx · html)
"""
import json
import mimetypes
import os
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import apply, codex_auth, config, export_xlsx, extract_book, rules, toc
from .pipeline import run as run_pipeline, run_all

CONSOLE = Path(__file__).with_name("console.html")


class Job:
    """한 번에 하나의 백그라운드 작업. print 출력을 로그로 모은다."""

    def __init__(self):
        self.lock = threading.Lock()
        self.name, self.uid, self.running, self.error, self.error_kind = None, None, False, None, None
        self.log, self.started, self.ended = [], None, None

    def start(self, name, uid, fn):
        with self.lock:
            if self.running:
                return False
            self.name, self.uid, self.running, self.error, self.error_kind = name, uid, True, None, None
            self.log, self.started, self.ended = [], datetime.now(), None
        threading.Thread(target=self._run, args=(fn,), daemon=True).start()
        return True

    def _run(self, fn):
        real, job = sys.stdout, self

        class Tee:
            """print 가 여러 조각으로 써도 한 줄로 모아 로그에 넣는다."""
            buf = ""

            def write(self, s):
                real.write(s)
                self.buf += s
                *lines, self.buf = self.buf.split("\n")
                job.log.extend(l for l in lines if l.strip())
                return len(s)

            def flush(self):
                real.flush()

        sys.stdout = Tee()
        try:
            fn()
            print(f"[완료] {self.name}")
        except SystemExit as e:
            self.error = str(e)
            print(f"[중단] {e}")
        except Exception as e:
            self.error, self.error_kind = str(e), getattr(e, "kind", None)
            if self.error_kind == "auth":
                codex_auth.mark_expired()
            print(f"[오류] {e}")
            real.write(traceback.format_exc())
        finally:
            sys.stdout = real
            self.running, self.ended = False, datetime.now()

    def state(self):
        return {"name": self.name, "uid": self.uid, "running": self.running, "error": self.error,
                "error_kind": self.error_kind, "log": self.log[-300:],
                "started": self.started.strftime("%H:%M:%S") if self.started else None,
                "ended": self.ended.strftime("%H:%M:%S") if self.ended else None}


JOB = Job()


def _mtime(p):
    return datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")


def results(uid):
    f = config.stage(uid, "03_판정") / "results.json"
    if not f.exists():
        return None
    d = json.loads(f.read_text(encoding="utf-8"))
    rows = d["rows"]
    by = {}
    for r in rows:
        by[r["judge_type"]] = by.get(r["judge_type"], 0) + 1
    return {"rows": len(rows), "rule": sum(r["source"] == "rule" for r in rows),
            "codex": sum(r["source"] == "codex" for r in rows), "by_type": by,
            "windows": len(d["windows"]), "failed": sum(w["failed"] for w in d["windows"]),
            "dropped_wrap": d.get("dropped_wrap", 0), "updated": _mtime(f)}


def unit_brief(u):
    state, recs, lt = export_xlsx.unit_state(u)
    return {k: u.get(k) for k in ("id", "title", "part", "pages", "printed", "n_pages", "assignee", "due", "order")} | {
        "status": state, "candidates": len(recs),
        "errata": sum(1 for r in recs if export_xlsx.is_errata(r)) if recs else None,  # 검토 전이면 자동 선정 수
        "reviewer": (lt or {}).get("reviewer")}


def app_state():
    pdf = config.book_pdf()
    mx = config.merged_xlsx() if pdf else None
    return {"workspace": str(config.WS),
            "book": {"file": pdf.name, "size": pdf.stat().st_size} if pdf else None,
            "toc": config.toc(),
            "units": [unit_brief(u) for u in config.units() if u.get("include", True)],
            "notes": (config.rules_book() or {}).get("notes", []),
            "merged": {"name": mx.name, "time": _mtime(mx)} if mx and mx.exists() else None,
            "backups": config.backups(), "job": JOB.state(), "codex": codex_auth.state()}


def unit_state(uid):
    u = config.unit(uid)
    d = config.unit_dir(uid)
    stages = []
    for s in config.STAGES:
        files = sorted((p for p in (d / s).rglob("*") if p.is_file() and "cache" not in p.parts),
                       key=lambda p: -p.stat().st_mtime)
        stages.append({"stage": s, "files": [{"name": p.name, "path": str(p.relative_to(d)).replace("\\", "/"),
                                              "time": _mtime(p), "size": p.stat().st_size} for p in files[:40]]})
    return {"unit": u, "brief": unit_brief(u), "dir": str(d), "summary": results(uid),
            "review": any((d / "04_검토").glob("*.html")), "stages": stages, "job": JOB.state()}


# ---------- 동작 ----------
def book_upload(filename, data):
    if Path(filename).suffix.lower() != ".pdf":
        raise ValueError("교재는 PDF로 올려 주세요.")
    config.BOOK_DIR.mkdir(parents=True, exist_ok=True)
    for old in config.BOOK_DIR.glob("*.pdf"):
        old.unlink()
    dest = config.BOOK_DIR / Path(filename).name
    dest.write_bytes(data)
    t = toc.extract(dest, extract_book.load_book())
    config.save_toc(t)
    return t


def units_confirm(body):
    t = config.toc()
    if not t:
        raise ValueError("먼저 교재 PDF를 올려 주세요.")
    orig = {u["id"]: u for u in t.get("units", [])}
    off = t.get("printed_offset", 0)
    units, ids = [], set()
    for k, u in enumerate(body["units"]):
        uid = str(u["id"]).strip()
        if not uid or uid in ids:
            raise ValueError(f"단위 번호가 비었거나 겹칩니다: '{uid}'")
        ids.add(uid)
        s, e = int(u["pages"][0]), int(u["pages"][1])
        o = orig.get(uid, {})
        same = o.get("pages") == [s, e]  # 쪽을 손으로 바꾸면 제목 기준 자르기는 하지 않는다
        units.append({**o, **u, "id": uid, "order": k + 1, "pages": [s, e], "n_pages": e - s + 1,
                      "start_head": o.get("start_head") if same else None,
                      "end_head": o.get("end_head") if same else None,
                      "printed": o.get("printed") if same else [s - off, e - off]})
    t["units"] = units
    if "style" in body:
        t["style"] = body["style"]
    t["confirmed"] = True
    t["confirmed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    config.save_toc(t)
    made = [config.save_unit(u).name for u in units if u.get("include", True)]
    for u in config.units():  # 목록에서 빠진 단위는 지우지 않고 숨긴다
        if u["id"] not in ids or not next(x for x in units if x["id"] == u["id"]).get("include", True):
            config.save_unit({**u, "include": False})
    return made


def unit_meta(uid, body):
    u = config.unit(uid)
    for k in ("assignee", "due"):
        if k in body:
            u[k] = str(body[k]).strip()
    config.save_unit(u)
    t = config.toc()
    if t:
        for x in t.get("units", []):
            if x["id"] == uid:
                x.update({k: u[k] for k in ("assignee", "due")})
        config.save_toc(t)
    return u


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _q(self):
        u = urllib.parse.urlparse(self.path)
        return urllib.parse.unquote(u.path), {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

    def _file(self, f, dl):
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if f.suffix in (".md", ".csv", ".txt", ".json"):
            ctype = "text/plain; charset=utf-8"
        elif ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        extra = {"Content-Disposition": "attachment; filename*=UTF-8''" + urllib.parse.quote(f.name)} if dl else {}
        return self._send(200, f.read_bytes(), ctype, extra)

    def do_GET(self):
        path, q = self._q()
        try:
            if path in ("/", "/index.html"):
                return self._send(200, CONSOLE.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._send(200, app_state())
            if path == "/api/unit":
                return self._send(200, unit_state(q["id"]))
            if path.startswith("/review/"):
                uid = path.split("/")[2]
                f = next(iter(sorted(config.stage(uid, "04_검토").glob("*.html"))), None)
                if f:
                    return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
                return self._send(404, "아직 판정 결과가 없습니다.", "text/plain; charset=utf-8")
            if path in ("/merged", "/merged.html"):
                f = config.merged_xlsx()
                f = f.with_suffix(".html") if path.endswith(".html") else f
                if f.exists():
                    return self._file(f, f.suffix == ".xlsx" or q.get("dl"))
            if path.startswith("/files/"):
                _, _, uid, rel = path.split("/", 3)
                base = config.unit_dir(uid).resolve()
                f = (base / rel).resolve()
                if base in f.parents and f.is_file():
                    return self._file(f, q.get("dl") or f.suffix == ".xlsx")
            self._send(404, "not found", "text/plain; charset=utf-8")
        except SystemExit as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        path, q = self._q()
        uid = q.get("id")
        body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        fname = urllib.parse.unquote(self.headers.get("X-Filename", "upload.pdf"))
        busy = {"ok": False, "error": "다른 작업이 실행 중입니다. 끝난 뒤 다시 누르세요."}

        def job(name, u, fn):
            ok = JOB.start(name, u, fn)
            return self._send(200 if ok else 409, {"ok": True} if ok else busy)

        try:
            if path in ("/api/book/upload", "/api/new-book", "/api/restore") and JOB.running:
                return self._send(409, busy)
            if path == "/api/book/upload":
                return self._send(200, {"ok": True, "toc": book_upload(fname, body)})
            if path == "/api/new-book":
                d = config.backup_current()
                return self._send(200, {"ok": True, "backup": d.name if d else None})
            if path == "/api/restore":
                d = config.restore(json.loads(body)["name"])
                return self._send(200, {"ok": True, "backup": d.name if d else None})
            if path == "/api/book/units":
                return self._send(200, {"ok": True, "made": units_confirm(json.loads(body))})
            if path == "/api/book/toc-codex":
                pdf = config.book_pdf()
                if not pdf:
                    raise ValueError("먼저 교재 PDF를 올려 주세요.")
                return job("Codex로 단위 뽑기", None, lambda: (config.save_toc(toc.codex_extract(pdf)),
                                                         print("[단위] Codex 결과를 불러왔습니다. 확인 후 확정하세요.")))
            if path == "/api/rules":
                return job("규칙 검사", None, rules.run_book)
            if path == "/api/run-all":
                only_new = bool((json.loads(body) if body else {}).get("only_new"))
                return job("전체 판정", None, lambda: run_all(only_new=only_new))
            if path == "/api/export-all":
                return self._send(200, {"ok": True, "file": export_xlsx.merge().name})
            if path == "/api/codex/refresh":
                codex_auth.check_async(deep=True)
                return self._send(200, {"ok": True})
            if path == "/api/codex/login":
                codex_auth.login(relogin=bool((json.loads(body) if body else {}).get("relogin")))
                return self._send(200, {"ok": True})
            if path == "/api/codex/logout":
                return self._send(200, {"ok": True, "out": codex_auth.logout()})
            if path == "/api/unit/run":
                return job("교정 판정", uid, lambda: run_pipeline(uid))
            if path == "/api/unit/meta":
                return self._send(200, {"ok": True, "unit": unit_meta(uid, json.loads(body))})
            if path == "/api/unit/open":
                d = config.unit_dir(uid) / q.get("stage", "")
                os.startfile(str(d if d.exists() else config.unit_dir(uid)))
                return self._send(200, {"ok": True})
            if path == "/apply":
                d = json.loads(body)
                return self._send(200, apply.run(d, d.get("unit")))
            self._send(404, {"error": "not found"})
        except (ValueError, KeyError, SystemExit) as e:
            self._send(400, {"ok": False, "error": str(e)})
        except PermissionError as e:
            self._send(409, {"ok": False, "error": f"파일이 다른 프로그램(엑셀)에서 열려 있습니다: {e.filename}"})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def log_message(self, *a):
        pass


def serve(port=8766, open_browser=True):
    url = f"http://127.0.0.1:{port}/"
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
    except OSError:
        print(f"[server] 이미 실행 중인 것 같습니다 → {url}")
        if open_browser:
            webbrowser.open(url)
        return
    config.WS.mkdir(parents=True, exist_ok=True)
    codex_auth.check_async(deep=False)  # 켜질 때는 가볍게 (실제 호출 확인은 좌하단 [새로고침])
    print(f"[TS 정오표 에이전트] {url}")
    print(f"[TS 정오표 에이전트] 워크스페이스: {config.WS}")
    print("이 창을 닫으면 콘솔도 멈춥니다. (종료: Ctrl+C)")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8766
    serve(port)
