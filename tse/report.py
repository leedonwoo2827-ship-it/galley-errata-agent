"""results.json → 04_검토/검토_<단위>.html (검토 + [반영] 화면, 단일 파일)."""
import html
import json

from . import config

THRESHOLD = 0.6        # 기본 체크 기준 (수정확률)
THRESHOLD_STYLE = 0.8  # 비문은 더 높게 — 문장 다듬기 제안이 많으면 소음이 된다


def checked(r):
    if r.get("bulk"):
        return False  # 일괄 지시는 사람이 보고 켠다
    return r["p_revise"] >= (THRESHOLD_STYLE if r["judge_type"] == "비문" else THRESHOLD)


def run(uid):
    res = json.loads((config.stage(uid, "03_판정") / "results.json").read_text(encoding="utf-8"))
    data = json.dumps(res, ensure_ascii=False).replace("</", "<\\/")
    page = TEMPLATE.replace("__TITLE__", html.escape(f"검토 — {config.label(uid, res['title'])}")) \
                   .replace("__UID__", json.dumps(uid, ensure_ascii=False)) \
                   .replace("__TH__", str(THRESHOLD)).replace("__THS__", str(THRESHOLD_STYLE)) \
                   .replace("__DATA__", data)
    out = config.stage(uid, "04_검토") / f"검토_{config.slug(uid)}.html"
    out.write_text(page, encoding="utf-8")
    print(f"[검토] {out}")
    return out


TEMPLATE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#fff;--fg:#1d2433;--mute:#5b6475;--line:#d9dee7;--head:#f2f5f9;--hi:#b42318;--lo:#05603a;--acc:#1f5eff;--del:#fde8e7;--ins:#e3f6ea;--rule:#eef2ff}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#14171c;--fg:#e6e9ef;--mute:#9aa3b2;--line:#2c323c;--head:#1c2027;--hi:#ff7b72;--lo:#56d364;--acc:#6e9bff;--del:#3a1d1d;--ins:#16301f;--rule:#1d2340}}
:root[data-theme="dark"]{--bg:#14171c;--fg:#e6e9ef;--mute:#9aa3b2;--line:#2c323c;--head:#1c2027;--hi:#ff7b72;--lo:#56d364;--acc:#6e9bff;--del:#3a1d1d;--ins:#16301f;--rule:#1d2340}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 "Malgun Gothic","Apple SD Gothic Neo",system-ui,sans-serif}
.wrap{max-width:1400px;margin:0 auto;padding:20px 16px 80px}
h1{font-size:21px;margin:0 0 4px} .mute{color:var(--mute)}
.kpis{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}
.kpi{border:1px solid var(--line);border-radius:8px;padding:8px 14px;min-width:110px}
.kpi b{display:block;font-size:20px}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;position:sticky;top:0;background:var(--bg);padding:10px 0;z-index:2;border-bottom:1px solid var(--line)}
button,select{font:inherit;border:1px solid var(--line);background:var(--head);color:var(--fg);border-radius:6px;padding:6px 12px;cursor:pointer}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:700}
input[type=text]{font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
.tbl{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:1050px}
th,td{border:1px solid var(--line);padding:6px 8px;vertical-align:top;text-align:left}
th{background:var(--head);white-space:nowrap}
td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.p{font-weight:700}.p.hi{color:var(--hi)}
.meter{height:6px;background:var(--line);border-radius:3px;margin-top:4px;overflow:hidden}
.meter i{display:block;height:100%;background:var(--hi)}
.old{background:var(--del);white-space:pre-wrap} textarea{width:100%;min-height:58px;font:inherit;background:var(--ins);color:var(--fg);border:1px solid var(--line);border-radius:4px;padding:4px}
tr.off td{opacity:.55}
tr.rule td:nth-child(5){background:var(--rule)}
#msg{margin:12px 0;padding:10px 12px;border-radius:6px;display:none;background:var(--head);border:1px solid var(--line);white-space:pre-wrap}
.tag{font-size:12px;border:1px solid var(--line);border-radius:10px;padding:0 6px;white-space:nowrap}
.notes{font-size:13px;margin:6px 0 0;padding-left:18px}
</style></head>
<body><div class="wrap">
<h1 id="h"></h1>
<div class="mute">기존문구(빨강)를 수정문구(초록)로 고칠지 체크하세요. 수정확률 ≥ <span id="th"></span>% (비문은 ≥ <span id="ths"></span>%) 항목이 기본 체크됩니다.
<b>규칙</b> = 책 전체 대조(따옴표·줄표·표/그림 번호·절 표기·목차), <b>AI</b> = Codex 교정. 수정문구를 비우면 '삭제'입니다. 사실·내용 확인은 정독에서 합니다.</div>
<div class="kpis" id="kpis"></div>
<div class="bar">
  <label>반영자 <input type="text" id="reviewer" size="10" placeholder="이름"></label>
  <select id="ftype" onchange="render()"><option value="">유형 전체</option></select>
  <select id="fsrc" onchange="render()"><option value="">규칙+AI</option><option value="rule">규칙만</option><option value="codex">AI만</option></select>
  <label><input type="checkbox" id="hideLow" checked onchange="render()"> 수정확률 20% 미만 숨기기</label>
  <button onclick="setAll(true)">보이는 것 체크</button><button onclick="setAll(false)">보이는 것 해제</button>
  <span style="flex:1"></span>
  <button class="primary" onclick="applyNow()">반영 → 정오표</button>
</div>
<div id="msg"></div>
<div class="tbl"><table>
<thead><tr><th>반영</th><th>#</th><th>쪽</th><th>유형</th><th>출처</th><th>수정확률</th><th style="width:24%">기존문구</th><th style="width:26%">수정문구 (편집 가능)</th><th style="width:22%">사유</th></tr></thead>
<tbody id="tb"></tbody></table></div>
<h2 style="font-size:16px;margin-top:28px">구간별 요약</h2>
<div class="tbl"><table style="min-width:600px"><thead><tr><th>구간</th><th>쪽</th><th>항목</th><th>요약</th></tr></thead><tbody id="wins"></tbody></table></div>
<h2 style="font-size:16px;margin-top:28px">책 전체 점검 메모 <span class="mute" style="font-weight:400">— 단위와 상관없이 한 번에 정할 것</span></h2>
<ul class="notes" id="notes"></ul>
</div>
<script>
const UID=__UID__, TH=__TH__, THS=__THS__, D=__DATA__;
const isOn=r=>!r.bulk && r.p_revise>=(r.judge_type==="비문"?THS:TH);
const st={}; D.rows.forEach(r=>st[r.id]={apply:isOn(r), new_text:r.new_text, note:""});
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
try{document.getElementById("reviewer").value=localStorage.getItem("tse_reviewer")||""}catch(e){}
document.getElementById("h").textContent=`검토 — ${D.title.startsWith(UID)?"":UID+" "}${D.title}`;
document.getElementById("th").textContent=Math.round(TH*100);
document.getElementById("ths").textContent=Math.round(THS*100);
const types=[...new Set(D.rows.map(r=>r.judge_type))];
document.getElementById("ftype").innerHTML+=types.map(t=>`<option>${esc(t)}</option>`).join("");
function visible(){
  const hide=document.getElementById("hideLow").checked, ft=document.getElementById("ftype").value, fs=document.getElementById("fsrc").value;
  return D.rows.filter(r=>!(hide&&r.p_revise<0.2) && (!ft||r.judge_type===ft) && (!fs||r.source===fs));
}
function kpis(){
  const n=D.rows.length, ck=D.rows.filter(r=>st[r.id].apply).length;
  const by=t=>D.rows.filter(r=>r.judge_type===t).length;
  document.getElementById("kpis").innerHTML=[["검토 후보",n],["규칙",D.rows.filter(r=>r.source==="rule").length],["AI",D.rows.filter(r=>r.source==="codex").length],
    ...types.map(t=>[t,by(t)]),["현재 체크",ck]].map(([k,v])=>`<div class="kpi"><span class="mute">${esc(k)}</span><b>${v}</b></div>`).join("");
}
function render(){
  document.getElementById("tb").innerHTML=visible().map(r=>{
    const s=st[r.id], pr=Math.round(r.p_revise*100);
    return `<tr class="${s.apply?"":"off"} ${r.source==="rule"?"rule":""}">
<td><input type="checkbox" ${s.apply?"checked":""} onchange="st[${r.id}].apply=this.checked;render()"></td>
<td>${r.id}</td><td class="num">${r.page}</td>
<td><span class="tag">${esc(r.judge_type)}</span>${r.bulk?' <span class="tag">일괄</span>':""}</td>
<td>${r.source==="rule"?"규칙":"AI"}</td>
<td class="num"><span class="p ${pr>=60?"hi":""}">${pr}%</span><div class="meter"><i style="width:${pr}%"></i></div></td>
<td class="old">${esc(r.old_text)}</td>
<td><textarea oninput="st[${r.id}].new_text=this.value">${esc(s.new_text)}</textarea>
<input type="text" placeholder="메모" style="width:100%;margin-top:4px" value="${esc(s.note)}" oninput="st[${r.id}].note=this.value"></td>
<td>${esc(r.reason)}</td></tr>`}).join("") || `<tr><td colspan="9" class="mute">표시할 항목이 없습니다.</td></tr>`;
  kpis();
}
document.getElementById("wins").innerHTML=(D.windows||[]).map(w=>`<tr><td>${esc(w.window)}</td><td class="num">${w.pages[0]}–${w.pages[1]}</td><td class="num">${w.n_items}${w.failed?" ⚠":""}</td><td>${esc(w.summary)}</td></tr>`).join("");
document.getElementById("notes").innerHTML=(D.notes||[]).map(n=>`<li>${esc(n)}</li>`).join("")||'<li class="mute">없음</li>';
function setAll(v){visible().forEach(r=>st[r.id].apply=v);render()}
function decisions(){
  const reviewer=document.getElementById("reviewer").value.trim();
  try{localStorage.setItem("tse_reviewer",reviewer)}catch(e){}
  return {unit:UID, reviewer, items:D.rows.map(r=>({id:r.id,...st[r.id]}))};
}
function show(t){const m=document.getElementById("msg");m.style.display="block";m.textContent=t}
async function applyNow(){
  const body=decisions();
  if(!body.items.some(i=>i.apply)){show("체크된 항목이 없습니다.");return}
  try{
    const r=await fetch("/apply",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    if(!r.ok){show("반영 실패: "+await r.text());return}
    const j=await r.json();
    try{parent.postMessage({tse:"applied",checked:j.counts.checked},"*")}catch(e){}
    show(`정오표 작성 — 체크한 ${j.counts.checked}건\n`+
      `· 원고에서 위치 확인: ${j.counts.applied}건\n`+
      (j.manual.length?`· 직접 확인 필요: ${j.manual.length}건 (`+j.manual.map(m=>"#"+m.id+" p."+m.page+" "+m.status).join(", ")+`)\n`:"")+
      `\n생성 파일 (05_정오표):\n`+j.files.map(f=>f.split(/[\\/]/).pop()).join("\n"));
  }catch(e){
    // 서버 없이 연 경우: 결정 파일을 내려받아 `python -m tse.apply` 로 처리
    const a=document.createElement("a");
    a.href=URL.createObjectURL(new Blob([JSON.stringify(body,null,1)],{type:"application/json"}));
    a.download=`decisions_${UID}.json`; a.click();
    show("로컬 서버(run.bat)가 실행 중이 아니어서 결정 파일을 내려받았습니다.\n프로젝트 폴더에서: python -m tse.apply decisions_"+UID+".json");
  }
}
render();
</script></body></html>
"""

if __name__ == "__main__":
    import sys
    run(sys.argv[1] if len(sys.argv) > 1 else "1.1")
