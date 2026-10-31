/* ============================================================
 * 小猫桌宠 · 咨询弹窗（自包含：样式 + DOM + 逻辑）
 * 用法：在页面 </body> 前加一行 <script src="/assets/pet.js"></script>
 * 数据：免鉴权 /api/demo/ask（POST {query}），与演示页同源。
 * ============================================================ */
(function () {
  "use strict";
  if (window.__PET_LOADED__) return;
  window.__PET_LOADED__ = true;

  // 小猫 SVG（无外部资源）
  var KITTEN = '' +
    '<svg viewBox="0 0 72 72" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">' +
    '  <path d="M14 30 L10 7 L28 18 Z" fill="#f2a65a"/>' +
    '  <path d="M16 26 L14 13 L24 19 Z" fill="#fbd7a8"/>' +
    '  <path d="M58 30 L62 7 L44 18 Z" fill="#f2a65a"/>' +
    '  <path d="M56 26 L58 13 L48 19 Z" fill="#fbd7a8"/>' +
    '  <circle cx="36" cy="40" r="24" fill="#f5b86a"/>' +
    '  <ellipse cx="36" cy="46" rx="15" ry="12" fill="#fbd7a8"/>' +
    '  <ellipse cx="27" cy="38" rx="3.2" ry="4.6" fill="#3a2d28"/>' +
    '  <ellipse cx="45" cy="38" rx="3.2" ry="4.6" fill="#3a2d28"/>' +
    '  <circle cx="28.2" cy="36.4" r="1.2" fill="#fff"/>' +
    '  <circle cx="46.2" cy="36.4" r="1.2" fill="#fff"/>' +
    '  <path d="M34 44 L38 44 L36 47 Z" fill="#e07a5f"/>' +
    '  <path d="M36 47 Q36 50 33 51 M36 47 Q36 50 39 51" stroke="#3a2d28" stroke-width="1.4" fill="none" stroke-linecap="round"/>' +
    '  <g stroke="#c08a54" stroke-width="1.3" stroke-linecap="round">' +
    '    <line x1="7" y1="42" x2="19" y2="44"/><line x1="7" y1="48" x2="19" y2="47"/>' +
    '    <line x1="65" y1="42" x2="53" y2="44"/><line x1="65" y1="48" x2="53" y2="47"/>' +
    '  </g>' +
    '</svg>';

  var CSS = '' +
    '#pet-root{position:fixed;right:20px;bottom:20px;z-index:9999;font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}' +
    '#pet-fab{width:62px;height:62px;border-radius:50%;background:linear-gradient(145deg,#f5b86a,#f2a65a);' +
    '  box-shadow:0 6px 20px rgba(0,0,0,.22);cursor:pointer;border:2px solid #fff;' +
    '  display:flex;align-items:center;justify-content:center;transition:transform .15s ease}' +
    '#pet-fab svg{width:52px;height:52px}' +
    '#pet-fab:hover{transform:scale(1.08)}' +
    '#pet-fab.idle{animation:pet-bob 2.4s ease-in-out infinite}' +
    '@keyframes pet-bob{0%,100%{transform:translateY(0)}50%{transform:translateY(-6px)}}' +
    '#pet-bubble{position:absolute;right:72px;bottom:8px;max-width:220px;background:#fff;' +
    '  border:1px solid #e2e6eb;border-radius:12px;padding:9px 13px;font-size:13px;color:#4b5563;' +
    '  box-shadow:0 4px 14px rgba(0,0,0,.12);opacity:0;pointer-events:none;transition:opacity .25s;white-space:nowrap}' +
    '#pet-bubble.show{opacity:1}' +
    '#pet-bubble::after{content:"";position:absolute;right:-6px;bottom:14px;width:12px;height:12px;' +
    '  background:#fff;border-right:1px solid #e2e6eb;border-bottom:1px solid #e2e6eb;transform:rotate(-45deg)}' +
    '#pet-panel{position:absolute;right:0;bottom:74px;width:340px;max-width:calc(100vw - 40px);height:460px;' +
    '  max-height:calc(100vh - 120px);background:#fff;border:1px solid #e2e6eb;border-radius:14px;' +
    '  box-shadow:0 12px 40px rgba(0,0,0,.22);display:none;flex-direction:column;overflow:hidden}' +
    '#pet-panel.open{display:flex}' +
    '#pet-head{display:flex;align-items:center;gap:10px;padding:12px 14px;background:linear-gradient(145deg,#f5b86a,#f2a65a);color:#fff}' +
    '#pet-head .avatar{width:34px;height:34px;border-radius:50%;background:#fff;display:flex;align-items:center;justify-content:center;flex:0 0 auto}' +
    '#pet-head .avatar svg{width:28px;height:28px}' +
    '#pet-head .t{flex:1;min-width:0}' +
    '#pet-head .t b{display:block;font-size:15px;letter-spacing:.3px}' +
    '#pet-head .t span{display:block;font-size:11.5px;opacity:.85}' +
    '#pet-close{background:none;border:none;color:#fff;font-size:22px;cursor:pointer;line-height:1;padding:2px 6px;border-radius:6px}' +
    '#pet-close:hover{background:rgba(255,255,255,.2)}' +
    '#pet-msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px;background:#f7f8fa}' +
    '.pet-msg{max-width:82%;padding:9px 12px;border-radius:12px;font-size:13.5px;line-height:1.6;white-space:pre-wrap;word-break:break-word}' +
    '.pet-msg.user{align-self:flex-end;background:#2b5c9b;color:#fff;border-bottom-right-radius:4px}' +
    '.pet-msg.bot{align-self:flex-start;background:#fff;border:1px solid #e2e6eb;color:#15202b;border-bottom-left-radius:4px}' +
    '.pet-msg.bot .risk{display:inline-block;font-size:11px;font-weight:640;padding:1px 7px;border-radius:4px;margin-bottom:6px}' +
    '.pet-msg.bot .risk.l1{background:#eef7f1;color:#1f7a4d}.pet-msg.bot .risk.l2{background:#fdf6e6;color:#8a6100}.pet-msg.bot .risk.l3{background:#fdf2f3;color:#a4232b}' +
    '.pet-msg.bot .note{margin-top:6px;font-size:12px;color:#7b8794}' +
    '.pet-msg.bot .clause{margin-top:8px;font-size:12px;color:#4b5563;border-top:1px dashed #e2e6eb;padding-top:6px}' +
    '.pet-msg.bot .clause b{color:#15202b}' +
    '#pet-input{display:flex;gap:8px;padding:11px 12px;border-top:1px solid #e2e6eb;background:#fff}' +
    '#pet-input textarea{flex:1;min-height:38px;max-height:90px;resize:none;border:1px solid #e2e6eb;border-radius:8px;padding:9px 11px;font:inherit;font-size:13.5px;outline:none;color:#15202b}' +
    '#pet-input textarea:focus{border-color:#2b5c9b}' +
    '#pet-send{background:#2b5c9b;border:none;color:#fff;border-radius:8px;padding:0 16px;cursor:pointer;font-size:13.5px;font-weight:600}' +
    '#pet-send:hover{background:#24507f}' +
    '#pet-send:disabled{opacity:.5;cursor:not-allowed}';

  var style = document.createElement('style');
  style.textContent = CSS;
  document.head.appendChild(style);

  var root = document.createElement('div');
  root.id = 'pet-root';
  root.innerHTML =
    '<div id="pet-bubble"></div>' +
    '<div id="pet-fab" class="idle" title="小猫助手 · 点我咨询">' + KITTEN + '</div>' +
    '<div id="pet-panel">' +
    '  <div id="pet-head">' +
    '    <div class="avatar">' + KITTEN + '</div>' +
    '    <div class="t"><b>小猫助手</b><span>规则合规咨询 · 引用可审计</span></div>' +
    '    <button id="pet-close" title="收起">×</button>' +
    '  </div>' +
    '  <div id="pet-msgs"></div>' +
    '  <div id="pet-input"><textarea id="pet-q" rows="1" placeholder="问规则，例如：七天无理由退货运费谁承担？"></textarea>' +
    '    <button id="pet-send">发送</button></div>' +
    '</div>';
  document.body.appendChild(root);

  var fab = document.getElementById('pet-fab');
  var panel = document.getElementById('pet-panel');
  var bubble = document.getElementById('pet-bubble');
  var msgs = document.getElementById('pet-msgs');
  var ta = document.getElementById('pet-q');
  var send = document.getElementById('pet-send');

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  }); }

  function bubbleSay(t) { bubble.textContent = t; bubble.classList.add('show'); }

  function addMsg(role, html) {
    var d = document.createElement('div');
    d.className = 'pet-msg ' + role;
    d.innerHTML = html;
    msgs.appendChild(d);
    msgs.scrollTop = msgs.scrollHeight;
    return d;
  }

  function riskBadge(lv) {
    var l = (lv || 'L1').toUpperCase();
    return '<span class="risk ' + l.toLowerCase() + '">' + esc(lv || 'L1') + '</span>';
  }

  function renderBot(r) {
    var html = riskBadge(r.risk_level) + '<div>' + esc(r.conclusion || '(空答复)') + '</div>';
    if (r.notice) html += '<div class="note">' + esc(r.notice) + '</div>';
    if (r.handoff) html += '<div class="note">🔔 已转人工：' + esc(r.handoff_reason || '高风险事项') + '</div>';
    if (r.clauses && r.clauses.length) {
      html += '<div class="clause">依据：' +
        r.clauses.slice(0, 3).map(function (c) {
          return '<b>《' + esc(c.doc_name) + '》' + esc(c.clause_no) + '</b>';
        }).join('、') + '</div>';
    }
    return addMsg('bot', html);
  }

  function ask(q) {
    q = (q || '').trim();
    if (!q) return;
    addMsg('user', esc(q));
    ta.value = '';
    send.disabled = true;
    var pending = addMsg('bot', '<span style="color:#7b8794">喵…正在翻规则</span>');
    fetch('/api/demo/ask', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: q })
    }).then(function (res) {
      if (res.status === 429) throw new Error('问太快啦，稍等再试');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    }).then(function (r) {
      pending.remove();
      renderBot(r);
    }).catch(function (e) {
      pending.remove();
      addMsg('bot', '喵…出错了：' + esc(e.message));
    }).finally(function () { send.disabled = false; ta.focus(); });
  }

  fab.addEventListener('click', function () {
    panel.classList.toggle('open');
    bubble.classList.remove('show');
    if (panel.classList.contains('open')) {
      ta.focus();
      if (!msgs.children.length) {
        addMsg('bot', '喵～ 我是小猫助手。问我规则问题，我会给你<b>带条款号的答案</b>，涉及处罚/定性会转人工。');
      }
    }
  });
  document.getElementById('pet-close').addEventListener('click', function () {
    panel.classList.remove('open');
  });
  send.addEventListener('click', function () { ask(ta.value); });
  ta.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(ta.value); }
  });

  // 首次进入 1.5s 后喵一声，之后 hover 再提示
  setTimeout(function () { if (!panel.classList.contains('open')) bubbleSay('喵～ 有规则问题点我问'); }, 1500);
  setTimeout(function () { bubble.classList.remove('show'); }, 7000);
  fab.addEventListener('mouseenter', function () { if (!panel.classList.contains('open')) bubbleSay('点我咨询'); });
  fab.addEventListener('mouseleave', function () { bubble.classList.remove('show'); });
})();
