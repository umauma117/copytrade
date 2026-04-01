# -*- coding: utf-8 -*-
"""简易网页：交易流水 + 运行时开关。默认仅监听本机，生产请设 DASHBOARD_TOKEN 并限制安全组。"""
from __future__ import annotations

from typing import Any

from flask import Flask, abort, jsonify, request

import config
import runtime_controls as rc


def _check_token(expected: str) -> None:
    if not expected:
        return
    auth = request.headers.get("Authorization", "")
    if auth == f"Bearer {expected}":
        return
    if request.args.get("token") == expected:
        return
    abort(403)


def create_app(tracker: Any) -> Flask:
    app = Flask(__name__)
    app.config["TRACKER"] = tracker

    INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>跟单控制台</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 920px; margin: 24px auto; padding: 0 16px; color: #1a1a1a; }
    h1 { font-size: 1.35rem; }
    .card { border: 1px solid #ddd; border-radius: 8px; padding: 16px; margin-bottom: 16px; background: #fafafa; }
    .row { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin: 8px 0; }
    label { min-width: 140px; font-weight: 600; }
    button { padding: 8px 14px; border-radius: 6px; border: 1px solid #333; background: #fff; cursor: pointer; }
    button.primary { background: #111; color: #fff; }
    button.ghost { border-color: #999; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid #eee; vertical-align: top; }
    th { background: #f0f0f0; }
    .muted { color: #666; font-size: 12px; }
    .ok { color: #0a7; }
    .warn { color: #c60; }
    code { font-size: 12px; word-break: break-all; }
  </style>
</head>
<body>
  <h1>跟单控制台</h1>
  <p class="muted">刷新页面可更新数据。若设置了访问口令，请在下方填写后点「保存口令到本页」。</p>

  <div class="card">
    <div class="row">
      <label>访问口令</label>
      <input type="password" id="token" placeholder="与 .env 中 DASHBOARD_TOKEN 一致" style="flex:1;max-width:320px;padding:8px"/>
      <button type="button" class="ghost" onclick="saveToken()">保存口令到本页</button>
    </div>
  </div>

  <div class="card">
    <h2 style="margin-top:0;font-size:1.1rem">开关（覆盖 .env，选「跟随配置」则恢复文件里的值）</h2>
    <div class="row">
      <label>跟单买入</label>
      <select id="ex">
        <option value="">跟随 .env</option>
        <option value="true">强制 开</option>
        <option value="false">强制 关</option>
      </select>
      <span class="muted">当前生效：<strong id="exEff">-</strong>（.env：<span id="exEnv">-</span>）</span>
    </div>
    <div class="row">
      <label>领袖跟卖</label>
      <select id="cs">
        <option value="">跟随 .env</option>
        <option value="true">强制 开</option>
        <option value="false">强制 关</option>
      </select>
      <span class="muted">当前生效：<strong id="csEff">-</strong>（.env：<span id="csEnv">-</span>）</span>
    </div>
    <div class="row">
      <button type="button" class="primary" onclick="applyToggles()">应用开关</button>
      <span class="muted" id="applyMsg"></span>
    </div>
  </div>

  <div class="card">
    <h2 style="margin-top:0;font-size:1.1rem">当前持仓（内存 + 已恢复快照）</h2>
    <div id="pos" class="muted">加载中…</div>
  </div>

  <div class="card">
    <h2 style="margin-top:0;font-size:1.1rem">最近事件</h2>
    <button type="button" class="ghost" onclick="loadAll()">刷新</button>
    <table style="margin-top:12px">
      <thead><tr><th>时间</th><th>类型</th><th>说明</th><th>详情</th></tr></thead>
      <tbody id="ev"></tbody>
    </table>
  </div>

<script>
function getToken() {
  return localStorage.getItem('dash_token') || '';
}
function saveToken() {
  localStorage.setItem('dash_token', document.getElementById('token').value.trim());
  loadAll();
}
function authQuery() {
  const t = getToken();
  return t ? ('?token=' + encodeURIComponent(t)) : '';
}
function authHeaders() {
  const t = getToken();
  return t ? { 'Authorization': 'Bearer ' + t, 'Content-Type': 'application/json' } : { 'Content-Type': 'application/json' };
}
async function loadAll() {
  document.getElementById('token').value = getToken();
  try {
    const r = await fetch('/api/state' + authQuery(), { headers: authHeaders() });
    if (!r.ok) { document.getElementById('pos').textContent = '无权限或接口错误 ' + r.status; return; }
    const s = await r.json();
    document.getElementById('exEnv').textContent = s.env_execute_copy ? '开' : '关';
    document.getElementById('csEnv').textContent = s.env_copy_sell ? '开' : '关';
    document.getElementById('exEff').textContent = s.effective_execute_copy ? '开' : '关';
    document.getElementById('csEff').textContent = s.effective_copy_sell ? '开' : '关';
    const ox = s.override_execute_copy, oc = s.override_copy_sell;
    document.getElementById('ex').value = ox === null || ox === undefined ? '' : (ox ? 'true' : 'false');
    document.getElementById('cs').value = oc === null || oc === undefined ? '' : (oc ? 'true' : 'false');
    const p = s.positions || [];
    if (!p.length) document.getElementById('pos').innerHTML = '<span class="muted">无未平仓</span>';
    else document.getElementById('pos').innerHTML = '<table><thead><tr><th>代币</th><th>成本(BNB)</th><th>路由</th></tr></thead><tbody>' +
      p.map(x => '<tr><td><code>'+x.token+'</code></td><td>'+x.cost_bnb+'</td><td><code>'+x.router+'</code></td></tr>').join('') + '</tbody></table>';
    const ev = await fetch('/api/events' + authQuery(), { headers: authHeaders() });
    const events = await ev.json();
    document.getElementById('ev').innerHTML = (events || []).map(e => {
      const extra = Object.keys(e).filter(k => !['ts','ts_local','kind','message'].includes(k))
        .map(k => k+'='+JSON.stringify(e[k])).join(' ');
      return '<tr><td>'+e.ts_local+'</td><td>'+e.kind+'</td><td>'+e.message+'</td><td><code>'+extra+'</code></td></tr>';
    }).join('') || '<tr><td colspan="4" class="muted">暂无</td></tr>';
  } catch (e) {
    document.getElementById('pos').textContent = '请求失败：' + e;
  }
}
async function applyToggles() {
  const ex = document.getElementById('ex').value;
  const cs = document.getElementById('cs').value;
  const body = {};
  if (ex === '') body.execute_copy = null;
  else body.execute_copy = ex === 'true';
  if (cs === '') body.copy_sell = null;
  else body.copy_sell = cs === 'true';
  const r = await fetch('/api/control' + authQuery(), { method: 'POST', headers: authHeaders(), body: JSON.stringify(body) });
  document.getElementById('applyMsg').textContent = r.ok ? '已保存' : '失败 ' + r.status;
  loadAll();
}
loadAll();
setInterval(loadAll, 15000);
</script>
</body>
</html>"""

    @app.get("/")
    def index() -> str:
        _check_token(config.DASHBOARD_TOKEN)
        return INDEX_HTML

    @app.get("/api/state")
    def api_state():
        _check_token(config.DASHBOARD_TOKEN)
        tracker = app.config["TRACKER"]
        o = rc.get_overrides()
        positions = []
        try:
            for p in tracker.get_open_positions():
                positions.append(
                    {
                        "token": p.token_address,
                        "cost_bnb": f"{p.cost_bnb / 1e18:.6f}",
                        "router": (p.router_address or "")[:16] + "…",
                    }
                )
        except Exception:
            pass
        return jsonify(
            {
                "env_execute_copy": config.EXECUTE_COPY,
                "env_copy_sell": config.COPY_SELL_ACTIONS,
                "override_execute_copy": o["execute_copy"],
                "override_copy_sell": o["copy_sell"],
                "effective_execute_copy": rc.effective_execute_copy(),
                "effective_copy_sell": rc.effective_copy_sell(),
                "positions": positions,
            }
        )

    @app.get("/api/events")
    def api_events():
        _check_token(config.DASHBOARD_TOKEN)
        return jsonify(rc.get_events(200))

    @app.post("/api/control")
    def api_control():
        _check_token(config.DASHBOARD_TOKEN)
        data = request.get_json(silent=True) or {}
        if "execute_copy" in data:
            v = data["execute_copy"]
            rc.set_execute_copy_override(v if v is None else bool(v))
        if "copy_sell" in data:
            v = data["copy_sell"]
            rc.set_copy_sell_override(v if v is None else bool(v))
        extra = {k: data[k] for k in ("execute_copy", "copy_sell") if k in data}
        rc.record_event("控制台", "已更新运行时开关", **extra)
        return jsonify({"ok": True})

    return app


def start_dashboard_background(tracker: Any) -> None:
    from threading import Thread

    app = create_app(tracker)

    def _run() -> None:
        app.run(
            host=config.DASHBOARD_HOST,
            port=config.DASHBOARD_PORT,
            threaded=True,
            use_reloader=False,
        )

    t = Thread(target=_run, name="dashboard", daemon=True)
    t.start()
