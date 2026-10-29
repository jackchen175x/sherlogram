"""调查报告：把一次溯源 / 卖家卡片做成一个可分享的网页文件。

互动图谱（能拖、能缩放、按类型筛、一键存图、导出 GEXF 给 Gephi、一键打码）+ 汇聚点 + 卖家 / 网站 / 钱包表格 + 卡片证据原话。
没接 MCP 的人也能用：把工具返回的 JSON 存成文件，然后 ``python -m tg_mcp.report trace.json [out.html]``。
本模块不连 Telegram，纯文本处理；图形库从公共 CDN 加载，所以打开报告要联网。
"""

import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_NODE = re.compile(r'^\s*(n\d+)\["(.*)"\](?::::(\w+))?\s*$')
_EDGE = re.compile(r"^\s*(n\d+) -->\|(.*)\| (n\d+)\s*$")
_SHORT_WALLET = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{5}…[1-9A-HJ-NP-Za-km-z]{4}$")


def graph_from_mermaid(text: str) -> Dict[str, list]:
    """从工具返回的 Mermaid 文本还原节点和边（命令行用；类型按标签猜，不如工具里直接出的准）。"""
    nodes, edges = [], []
    for line in text.splitlines():
        m = _NODE.match(line)
        if m:
            parts = m.group(2).split("<br/>")
            cls, label = m.group(3) or "", parts[0]
            if cls == "seller":
                kind = "seller"
            elif cls == "wallet" or _SHORT_WALLET.match(label):
                kind = "wallet"
            elif cls == "site" or (not label.startswith("@") and "." in label):
                kind = "site"
            else:
                kind = "handle"
            nodes.append({"id": m.group(1), "label": label, "sub": " · ".join(parts[1:]), "kind": kind, "source": cls == "source", "bad": cls == "bad"})
            continue
        m = _EDGE.match(line)
        if m:
            edges.append({"from": m.group(1), "to": m.group(3), "label": m.group(2)})
    return {"nodes": nodes, "edges": edges}


def _e(x: Any) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def _table(headers: List[str], rows: List[List[Any]]) -> str:
    if not rows:
        return "<p class='muted'>（无）</p>"
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c if isinstance(c, _Raw) else _e(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


class _Raw(str):
    """已经是 HTML 的单元格，别再转义。"""


def _link(url: str, text: str) -> _Raw:
    return _Raw(f'<a href="{_e(url)}" target="_blank" rel="noopener">{_e(text)}</a>')


def _deep_summary(deep: Optional[Dict[str, Any]]) -> str:
    """网站深挖结果压成一句：注册商 / 注册日期 / 同证书的站 / 同服务器的站。"""
    if not deep:
        return ""
    bits = []
    if deep.get("registrar"):
        bits.append("注册商 " + deep["registrar"])
    if deep.get("registrant"):
        bits.append("注册人 " + deep["registrant"])
    if deep.get("created"):
        bits.append("注册于 " + deep["created"])
    elif deep.get("first_cert"):
        bits.append("最早证书 " + deep["first_cert"])
    if deep.get("cert_siblings"):
        bits.append("同证书：" + "、".join(deep["cert_siblings"][:5]))
    if deep.get("same_ip"):
        bits.append("同服务器：" + "、".join(deep["same_ip"][:5]))
    for k in ("note", "ip_note", "cert_error", "ip_error", "rdap_error", "error"):
        if deep.get(k):
            bits.append(f"{k}: {deep[k]}")
    return "；".join(bits)


def _card(c: Dict[str, Any]) -> str:
    tier = c.get("tier", "?")
    flags = "".join(f"<li>{_e(f)}</li>" for f in c.get("red_flags", [])) or "<li class='muted'>没有红旗</li>"
    ev = "".join(
        f"<blockquote><b>{_e(e.get('dimension'))}</b> · {_e(e.get('chat'))} · {_e(e.get('date') or '')}<br/>{_e(e.get('quote'))}</blockquote>"
        for e in c.get("evidence", [])
    )
    kv = []
    for section in ("profile", "activity", "provenance"):
        for k, v in (c.get(section) or {}).items():
            if v not in (None, [], {}, ""):
                kv.append([f"{section}.{k}", json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v])
    money_rows = [
        [_link(m.get("explorer", "#"), a), m.get("in_usdt"), m.get("out_usdt"), m.get("payers"), m.get("payees"), f"{m.get('first')} ～ {m.get('last')}",
         "；".join(f"{r['address'][:8]}… {r['usdt']}U/{r['count']}笔" for r in m.get("top_out", []))]
        for a, m in (c.get("money") or {}).items() if not m.get("error")
    ]
    related = "、".join(f"{r['handle']}（{r['why']}）" for r in c.get("related_accounts", []))
    return f"""
<section class="card">
  <h3><span class="tier tier-{_e(tier)}">{_e(tier)}</span> {_e(c.get('handle'))} <small>{_e(c.get('tier_definition'))}</small></h3>
  <h4>红旗</h4><ul>{flags}</ul>
  {('<h4>证据原话</h4>' + ev) if ev else ''}
  {('<h4>关联账号</h4><p>' + _e(related) + '</p>') if related else ''}
  {('<h4>钱的情况（USDT）</h4>' + _table(['收款地址', '进', '出', '打款人数', '收款人数', '时间', '钱主要转去'], money_rows)) if money_rows else ''}
  <details><summary>资料与活跃度</summary>{_table(['项', '值'], kv)}</details>
  <p class="muted">可信度：{_e(c.get('confidence'))} · {_e(c.get('disclaimer'))}</p>
</section>"""


def render_html(result: Dict[str, Any], graph: Optional[Dict[str, list]] = None, cards: Optional[List[Dict[str, Any]]] = None, title: str = "调查报告") -> str:
    """把溯源结果（和 / 或卖家卡片）渲染成一个自包含的网页。"""
    cards = cards or []
    parts = [f"<p class='summary'>{_e(result.get('summary', ''))}</p>"]
    if result.get("truncated"):
        parts.append("<p class='muted'>没做完的：" + "；".join(_e(x) for x in result["truncated"]) + "</p>")
    if graph and graph.get("nodes"):
        parts.append("""
<div class="toolbar">
  <label><input type="checkbox" class="kind" value="seller" checked> 卖家</label>
  <label><input type="checkbox" class="kind" value="handle" checked> @号</label>
  <label><input type="checkbox" class="kind" value="site" checked> 网站</label>
  <label><input type="checkbox" class="kind" value="wallet" checked> 钱包</label>
  <label><input type="checkbox" id="onlySource"> 只看汇聚点周围</label>
  <label><input type="checkbox" id="mask"> 打码</label>
  <span class="grow"></span>
  <button onclick="savePng()">存成图片</button>
  <button onclick="saveGexf()">导出 GEXF</button>
  <button onclick="network.stabilize()">重新排布</button>
</div>
<div id="graph"></div>
<p class="legend"><i class="sw seller"></i>卖家 <i class="sw handle"></i>@号 <i class="sw site"></i>网站 <i class="sw wallet"></i>钱包 <i class="sw source"></i>线索汇聚点 <i class="sw bad"></i>钓鱼 / 官方标记 · 拖动节点、滚轮缩放、双击打开</p>""")
    if result.get("candidates"):
        rows = [[c.get("node"), c.get("kind"), c.get("score"), "；".join(c.get("reasons", []))] for c in result["candidates"]]
        parts.append("<h2>线索汇聚点</h2>" + _table(["节点", "类型", "被指次数", "理由"], rows))
    if result.get("sellers"):
        rows = [[s.get("handle"), s.get("name"), s.get("posts"), "、".join(s.get("sites", [])), "、".join(s.get("mentions", []))] for s in result["sellers"]]
        parts.append("<h2>卖家</h2>" + _table(["账号", "名字", "帖数", "贴过的站", "提到的号"], rows))
    if result.get("sites"):
        rows = [[_link(f"https://{h}", h), s.get("verdict"), s.get("title"), s.get("platform"), "、".join(s.get("contacts", [])), "、".join(s.get("calls", [])), _deep_summary(s.get("deep"))] for h, s in result["sites"].items()]
        parts.append("<h2>网站</h2>" + _table(["域名", "探测结果", "标题", "平台", "站上留的联系方式", "调用的域名", "背后"], rows))
    if result.get("wallets"):
        rows = [
            [_link(w.get("explorer", "#"), a), w.get("in_usdt"), w.get("out_usdt"), w.get("payers"), w.get("payees"), f"{w.get('first') or ''} ～ {w.get('last') or ''}",
             "；".join(f"{r['address'][:8]}… {r['usdt']}U/{r['count']}笔" for r in w.get("top_out", [])), w.get("note") or w.get("error") or ""]
            for a, w in result["wallets"].items()
        ]
        parts.append("<h2>钱包（USDT）</h2>" + _table(["地址", "进", "出", "打款人数", "收款人数", "时间", "钱主要转去", "备注"], rows))
    if result.get("handles"):
        rows = [[h, v.get("kind"), v.get("label"), "、".join(v.get("flags", [])), v.get("bio") or "", v.get("error") or ""] for h, v in result["handles"].items()]
        parts.append("<h2>核对过的 @号</h2>" + _table(["账号", "类型", "名字", "标记", "简介", "错误"], rows))
    parts += [_card(c) for c in cards]
    data = json.dumps({"graph": graph or {"nodes": [], "edges": []}}, ensure_ascii=False).replace("</", "<\\/")
    return (
        _TEMPLATE.replace("__TITLE__", _e(title))
        .replace("__DATE__", f"{datetime.now(timezone.utc).astimezone():%Y-%m-%d %H:%M}")
        .replace("__BODY__", "\n".join(parts))
        .replace("__DATA__", data)
    )


_TEMPLATE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#fafaf9;--fg:#1c1917;--muted:#78716c;--line:#e7e5e4;--accent:#ea580c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",Segoe UI,Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 60px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px;border-left:4px solid var(--accent);padding-left:10px}
h3{font-size:17px;margin:0 0 8px}h4{font-size:14px;margin:14px 0 4px;color:var(--muted)}.date,.muted{color:var(--muted);font-size:13px}.summary{font-size:16px;background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px 14px}
#graph{height:640px;background:#fff;border:1px solid var(--line);border-radius:10px}.toolbar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin:16px 0 8px;font-size:14px}.toolbar .grow{flex:1}
button{background:#fff;border:1px solid var(--line);border-radius:8px;padding:6px 12px;font:inherit;cursor:pointer}button:hover{border-color:var(--accent)}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13px}th,td{padding:7px 9px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line);word-break:break-all}th{background:#f5f5f4;font-weight:600}tr:last-child td{border-bottom:0}
.legend{font-size:13px;color:var(--muted)}.sw{display:inline-block;width:12px;height:12px;border-radius:3px;margin:0 4px 0 10px;vertical-align:-1px;border:1px solid #999}.sw.seller{background:#f4f4f4}.sw.handle{background:#fff;border-color:#a78bfa}.sw.site{background:#e8f1ff;border-color:#3b82f6}.sw.wallet{background:#ede9fe;border-color:#7c3aed}.sw.source{background:#ffedd5;border:2px solid #ea580c}.sw.bad{background:#fee2e2;border-color:#dc2626}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-top:20px}.tier{display:inline-block;min-width:30px;text-align:center;border-radius:6px;padding:0 8px;color:#fff;font-weight:700;margin-right:6px}.tier-A{background:#16a34a}.tier-B{background:#2563eb}.tier-C{background:#d97706}.tier-D{background:#dc2626}
blockquote{margin:6px 0;padding:8px 12px;background:#fafaf9;border-left:3px solid var(--line);font-size:14px}details{margin-top:10px}summary{cursor:pointer;color:var(--muted)}a{color:#2563eb}
</style></head><body><main>
<h1>__TITLE__</h1><p class="date">生成于 __DATE__ · 由 Sherlogram 生成 · 只做相对可核查度分层，不构成任何背书或指控</p>
__BODY__
</main>
<script>const DATA = __DATA__;</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.9/standalone/umd/vis-network.min.js" onerror="this.onerror=null;this.src='https://cdn.jsdelivr.net/npm/vis-network@9.1.9/standalone/umd/vis-network.min.js'"></script>
<script>
let network = null;
const STYLE = {seller:['#f4f4f4','#999'], handle:['#ffffff','#a78bfa'], site:['#e8f1ff','#3b82f6'], wallet:['#ede9fe','#7c3aed']};
function maskText(s, kind){ if(!s) return s; if(kind==='wallet') return s.slice(0,2)+'****'+s.slice(-2); if(s.startsWith('@')) return '@'+s.slice(1,3)+'***'; if(s.includes('.')) return s.split('.').map((p,i,a)=> i===a.length-1?p:p.slice(0,2)+'**').join('.'); return s.slice(0,1)+'**'; }
function build(){
  const el = document.getElementById('graph'); if(!el || !window.vis) return;
  const mask = document.getElementById('mask').checked;
  const kinds = new Set([...document.querySelectorAll('.kind:checked')].map(c=>c.value));
  const only = document.getElementById('onlySource').checked;
  const keep = new Set(); const src = new Set(DATA.graph.nodes.filter(n=>n.source).map(n=>n.id));
  if(only){ for(const e of DATA.graph.edges){ if(src.has(e.from)||src.has(e.to)){ keep.add(e.from); keep.add(e.to);} } for(const s of src) keep.add(s); }
  const nodes = DATA.graph.nodes.filter(n=>kinds.has(n.kind) && (!only || keep.has(n.id))).map(n=>{
    const [bg,border] = n.bad?['#fee2e2','#dc2626']: n.source?['#ffedd5','#ea580c']: STYLE[n.kind]||STYLE.handle;
    const label = (mask?maskText(n.label,n.kind):n.label) + (n.sub? '\\n'+(mask && n.kind!=='wallet' && !/^[进出]/.test(n.sub)?maskText(n.sub,'name'):n.sub):'');
    return {id:n.id, label, shape:'box', margin:8, color:{background:bg,border,highlight:{background:bg,border:'#ea580c'}}, borderWidth:n.source?3:1, font:{size:14,face:'PingFang SC, Microsoft YaHei, sans-serif'}, title:n.url||undefined, url:n.url};
  });
  const ids = new Set(nodes.map(n=>n.id));
  const edges = DATA.graph.edges.filter(e=>ids.has(e.from)&&ids.has(e.to)).map(e=>({from:e.from,to:e.to,label:e.label,arrows:'to',font:{size:11,color:'#78716c',align:'middle',strokeWidth:3,strokeColor:'#fff'},color:{color:'#c7c2bd',highlight:'#ea580c'},smooth:{type:'continuous'}}));
  if(network) network.destroy();
  network = new vis.Network(el, {nodes:new vis.DataSet(nodes), edges:new vis.DataSet(edges)}, {physics:{solver:'forceAtlas2Based',forceAtlas2Based:{gravitationalConstant:-60,springLength:140},stabilization:{iterations:250}},interaction:{hover:true,tooltipDelay:120},layout:{improvedLayout:true}});
  network.on('doubleClick', p=>{ const n = nodes.find(x=>x.id===p.nodes[0]); if(n&&n.url) window.open(n.url,'_blank'); });
}
function download(blob, name){ const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download=name; a.click(); setTimeout(()=>URL.revokeObjectURL(a.href),1000); }
function savePng(){ const c=document.querySelector('#graph canvas'); if(!c) return; const o=document.createElement('canvas'); o.width=c.width; o.height=c.height; const g=o.getContext('2d'); g.fillStyle='#fff'; g.fillRect(0,0,o.width,o.height); g.drawImage(c,0,0); o.toBlob(b=>download(b,'graph.png')); }
function saveGexf(){ const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'); let x='<?xml version="1.0" encoding="UTF-8"?>\\n<gexf xmlns="http://www.gexf.net/1.2draft" version="1.2"><graph defaultedgetype="directed"><attributes class="node"><attribute id="0" title="kind" type="string"/><attribute id="1" title="source" type="boolean"/></attributes><nodes>';
  for(const n of DATA.graph.nodes) x+=`<node id="${n.id}" label="${esc(n.label)}"><attvalues><attvalue for="0" value="${n.kind}"/><attvalue for="1" value="${!!n.source}"/></attvalues></node>`;
  x+='</nodes><edges>'; DATA.graph.edges.forEach((e,i)=>{ x+=`<edge id="${i}" source="${e.from}" target="${e.to}" label="${esc(e.label)}"/>`; }); x+='</edges></graph></gexf>'; download(new Blob([x],{type:'application/xml'}),'graph.gexf'); }
document.querySelectorAll('.kind, #onlySource, #mask').forEach(c=>c.addEventListener('change', build));
window.addEventListener('load', ()=>{ if(!window.vis){ const el=document.getElementById('graph'); if(el) el.innerHTML='<p style="padding:20px;color:#78716c">图形库没加载到（需要联网）。表格和证据不受影响。</p>'; } else build(); });
</script></body></html>
"""


def main(argv: Optional[List[str]] = None) -> Path:
    """命令行：python -m tg_mcp.report 工具结果.json [输出.html]"""
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        sys.exit(__doc__)
    src = Path(argv[0])
    data = json.loads(src.read_text(encoding="utf-8"))
    out = Path(argv[1]) if len(argv) > 1 else src.with_suffix(".html")
    cards = [data] if "tier" in data else []
    graph = graph_from_mermaid(data["mermaid"]) if data.get("mermaid") else None
    out.write_text(render_html(data, graph, cards, title=data.get("handle") or data.get("summary", "调查报告")[:40]), encoding="utf-8")
    print(f"写入 {out}")
    return out


if __name__ == "__main__":
    main()
