"use strict";
const $ = (id) => document.getElementById(id);
const enc = encodeURIComponent;
const esc = (s) => (s == null ? "" : String(s)).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

function inputPath() { return $("input").value.trim(); }

async function jget(path) { const r = await fetch(path); return r.json(); }
async function jsend(method, path, body) {
  const r = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.json();
}

// ── Tabs ──────────────────────────────────────────────────────────────────
document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
  document.querySelectorAll(".panel").forEach(x => x.classList.remove("active"));
  t.classList.add("active");
  $("tab-" + t.dataset.tab).classList.add("active");
  const tab = t.dataset.tab;
  if (tab === "glossary") loadGlossary();
  else if (tab === "revisions") loadRevisions();
  else if (tab === "report") loadReport();
  else if (tab === "state") loadState();
});

// ── 步骤勾选 ────────────────────────────────────────────────────────────────
$("step-all").onchange = (e) => document.querySelectorAll(".step").forEach(c => c.checked = e.target.checked);
document.querySelectorAll(".step").forEach(c => c.onchange = () => {
  $("step-all").checked = [...document.querySelectorAll(".step")].every(x => x.checked);
});

// ── 运行 ────────────────────────────────────────────────────────────────────
function setProgress(done, total, label) {
  const pct = total ? Math.round(done / total * 100) : 0;
  $("bar-fill").style.width = pct + "%";
  $("status-label").textContent = (label || "") + (total ? `  ${done}/${total} 段` : "");
}

$("run").onclick = async () => {
  const input = inputPath();
  if (!input) { alert("请填写输入文件路径"); return; }
  const steps = [...document.querySelectorAll(".step")].filter(c => c.checked).map(c => c.value);
  if (!steps.length) { alert("请至少勾选一个步骤"); return; }
  $("stream").innerHTML = "";
  setProgress(0, 0, "启动中…");
  const res = await jsend("POST", "/api/run", { input, steps, format: $("format").value });
  if (res.error) { $("status-label").textContent = "错误：" + res.error; return; }
  openWS(res.run_id);
};

$("refresh").onclick = () => { loadState(); };

function renderBatch(ev) {
  // 每个批次一块，最新的插到最上面（动态生成在顶部）
  const block = document.createElement("div");
  block.className = "chapter-group";
  block.innerHTML = `<h3>第${ev.chapter}章 ${esc(ev.title || "")} · 批${ev.batch} <span class="muted">(${ev.done}/${ev.total} 段)</span></h3>`;
  const byIdx = {};
  (ev.issues || []).forEach(it => { byIdx[it.index] = it; });
  ev.pairs.forEach((p, i) => {
    const it = byIdx[i];
    const div = document.createElement("div");
    div.className = "pair" + (it ? " flagged" : "");
    let issueHtml = "";
    if (it) {
      const badge = it.fixed ? `<span class="badge fixed">✓已修订</span>` : `<span class="badge sugg">⚠仅建议</span>`;
      issueHtml = `<div class="issue">[${esc(it.type)}] ${esc(it.detail || "")}${it.suggestion ? "　建议：" + esc(it.suggestion) : ""} ${badge}</div>`;
    }
    div.innerHTML = `<div class="src">${esc(p.source)}</div><div class="tgt">${esc(p.target)}${issueHtml}</div>`;
    block.appendChild(div);
  });
  $("stream").prepend(block);
}

function openWS(runId) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/${runId}`);
  ws.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    switch (ev.type) {
      case "progress": setProgress(ev.done, ev.total, ev.label); break;
      case "batch": renderBatch(ev); setProgress(ev.done, ev.total, `第${ev.chapter}章`); break;
      case "step": $("status-label").textContent = `步骤 ${ev.step} ${ev.status === "start" ? "开始" : "完成"}`; break;
      case "audit": if (ev.unifications && ev.unifications.length) $("status-label").textContent = `术语统一 ${ev.unifications.length} 组`; break;
      case "error": $("status-label").textContent = "错误：" + ev.detail; break;
      case "end":
        $("bar-fill").style.width = "100%";
        $("status-label").textContent = "完成 ✓";
        loadState(); loadGlossary(); loadRevisions(); loadReport();
        ws.close();
        break;
    }
  };
  ws.onerror = () => { $("status-label").textContent = "WebSocket 连接失败"; };
}

// ── 术语表（可编辑）──────────────────────────────────────────────────────────
async function loadGlossary() {
  const input = inputPath(); if (!input) return;
  const data = await jget(`/api/glossary?input=${enc(input)}`);
  renderConflicts(data.conflicts || []);
  const tb = $("glossary-table").querySelector("tbody");
  tb.innerHTML = "";
  (data.terms || []).forEach(t => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(t.source)}</td>
      <td><input value="${esc(t.target)}" data-f="target"></td>
      <td><input value="${esc(t.type)}" data-f="type"></td>
      <td><input value="${esc(t.gender)}" data-f="gender" style="width:48px"></td>
      <td style="text-align:center"><input type="checkbox" data-f="lock" ${t.locked ? "checked" : ""}></td>
      <td></td>`;
    const ops = tr.lastElementChild;
    const save = mkBtn("保存", async () => {
      const r = await jsend("PUT", "/api/glossary/term", {
        input, source: t.source,
        target: tr.querySelector('[data-f=target]').value,
        type: tr.querySelector('[data-f=type]').value,
        gender: tr.querySelector('[data-f=gender]').value,
        lock: tr.querySelector('[data-f=lock]').checked,
      });
      if (r.rewritten) $("status-label").textContent = `已保存，并改写正文 ${r.rewritten} 段`;
      loadGlossary();
    });
    const apply = mkBtn("应用到正文", async () => {
      const r = await jsend("POST", "/api/glossary/apply", { input, source: t.source });
      alert(`已改写 ${r.rewritten || 0} 段正文`);
    });
    const trace = mkBtn("溯源", async () => {
      const r = await jget(`/api/glossary/occurrences?input=${enc(input)}&source=${enc(t.source)}`);
      showOccurrences(tr, t.source, r);
    });
    const del = mkBtn("删除", async () => {
      if (!confirm(`删除术语「${t.source}」？`)) return;
      await jsend("DELETE", "/api/glossary/term", { input, source: t.source });
      loadGlossary();
    });
    [save, apply, trace, del].forEach(b => ops.appendChild(b));
    tb.appendChild(tr);
  });
}

function mkBtn(label, fn) { const b = document.createElement("button"); b.className = "sm"; b.textContent = label; b.onclick = fn; return b; }

function showOccurrences(tr, source, r) {
  const next = tr.nextElementSibling;
  if (next && next.classList.contains("occ-row")) { next.remove(); return; } // 再点收起
  const row = document.createElement("tr"); row.className = "occ-row";
  const td = document.createElement("td"); td.colSpan = 6;
  if (!r.count) {
    td.innerHTML = `<span class="muted">未在正文中找到「${esc(source)}」的出现</span>`;
  } else {
    td.innerHTML = `<div class="muted">「${esc(source)}」共 ${r.count} 处　匹配：${esc((r.keys || []).join("、"))}</div>` +
      r.occurrences.map(o =>
        `<div class="occ"><div class="meta">第${o.chapter}章 · 第${o.index}段</div>
         <div class="pair"><div class="src">${esc(o.source)}</div><div class="tgt">${esc(o.target)}</div></div></div>`
      ).join("");
  }
  row.appendChild(td); tr.after(row);
}

function renderConflicts(conflicts) {
  const box = $("glossary-conflicts");
  if (!conflicts.length) { box.innerHTML = ""; return; }
  box.innerHTML = "<h4>待裁决冲突</h4>";
  conflicts.forEach(c => {
    const div = document.createElement("div");
    div.className = "conflict";
    div.innerHTML = `<b>${esc(c.source)}</b>：现有「${esc(c.existing_target)}」 vs 提议「${esc(c.proposed_target)}」（第${c.chapter}章）
      译法 <input value="${esc(c.existing_target)}" style="width:120px">
      <label class="muted"><input type="checkbox"> 改写正文</label> `;
    const target = div.querySelector("input[type=text], input:not([type])");
    const apply = div.querySelector("input[type=checkbox]");
    div.appendChild(mkBtn("裁决", async () => {
      await jsend("POST", "/api/glossary/resolve", {
        input: inputPath(), source: c.source, target: target.value, apply_to_text: apply.checked });
      loadGlossary();
    }));
    box.appendChild(div);
  });
}

$("add-term").onclick = async () => {
  const input = inputPath(); if (!input) return;
  const source = $("new-source").value.trim(); if (!source) return;
  await jsend("PUT", "/api/glossary/term", {
    input, source, target: $("new-target").value.trim(),
    type: $("new-type").value.trim() || "术语", lock: true });
  $("new-source").value = $("new-target").value = $("new-type").value = "";
  loadGlossary();
};

$("reapply").onclick = async () => {
  const input = inputPath(); if (!input) return;
  if (!confirm("把整张术语表的别名/变体在全书正文里统一为当前译法？")) return;
  const r = await jsend("POST", "/api/glossary/reapply", { input });
  $("status-label").textContent = `重新应用术语表：改写正文 ${r.rewritten || 0} 段`;
  loadGlossary();
};

// ── 建议 / 修订 ──────────────────────────────────────────────────────────────
async function loadRevisions() {
  const input = inputPath(); if (!input) return;
  const d = await jget(`/api/revisions?input=${enc(input)}`);
  const box = $("revisions"); box.innerHTML = "";
  const bar = document.createElement("div");
  bar.style.marginBottom = "10px";
  const fixBtn = mkBtn("自动修复一致性（术语类）", async () => {
    if (!confirm("把可安全全局替换的术语/译名不一致改写进正文？代词/语气类不会改。")) return;
    const r = await jsend("POST", "/api/consistency/fix", { input });
    $("status-label").textContent = `一致性自动修复：替换 ${(r.replacements || []).length} 项 · 改写正文 ${r.rewritten || 0} 段`;
    loadRevisions();
  });
  bar.appendChild(fixBtn);
  const note = document.createElement("span");
  note.className = "muted"; note.style.marginLeft = "8px";
  note.textContent = "仅术语/译名类自动改；代词/语气类留作建议（手动处理或用术语表）";
  bar.appendChild(note);
  box.appendChild(bar);
  const section = (title, items, render) => {
    const h = document.createElement("h4"); h.textContent = `${title}（${items.length}）`; box.appendChild(h);
    if (!items.length) { const p = document.createElement("div"); p.className = "muted"; p.textContent = "无"; box.appendChild(p); return; }
    items.forEach(it => { const div = document.createElement("div"); div.className = "rev-item"; div.innerHTML = render(it); box.appendChild(div); });
  };
  section("审校建议", d.review || [], it => {
    const badge = it.fixed ? `<span class="badge fixed">✓已修订</span>` : `<span class="badge sugg">⚠仅建议</span>`;
    return `[${esc(it.type)}] ${esc(it.detail || "")} ${badge}<div class="meta">第${it.chapter}章 · 第${it.index}段${it.suggestion ? " · 建议：" + esc(it.suggestion) : ""}</div>`;
  });
  section("回译疑点", d.backtranslation || [], it => `${esc(it.detail || "")}<div class="meta">第${it.chapter}章</div>`);
  section("一致性问题", d.consistency || [], it => `[${esc(it.type)}] ${esc(it.detail || "")}<div class="meta">${esc(it.where || "")}</div>`);
  section("术语统一（已修订）", d.unifications || [], it =>
    `<b>${esc(it.source)}</b> → ${esc(it.canonical)} <span class="badge fixed">✓已修订</span><div class="meta">替换 ${esc((it.variants || []).join("、"))}${it.reason ? " · " + esc(it.reason) : ""}</div>`);
}

// ── 报告 / 状态 ──────────────────────────────────────────────────────────────
async function loadReport() {
  const input = inputPath(); if (!input) return;
  const r = await jget(`/api/report?input=${enc(input)}`);
  const box = $("report");
  if (!r.summary) { box.innerHTML = `<div class="muted">尚无报告。运行含“报告”步骤后生成。</div>`; return; }
  const s = r.summary;
  box.innerHTML = `
    <div><span class="kpi">章节 <b>${s.chapters_done}/${s.chapters_total}</b></span>
    <span class="kpi">术语 <b>${s.terms}</b></span>
    <span class="kpi">待裁决冲突 <b>${s.open_conflicts}</b></span>
    <span class="kpi">审校问题 <b>${s.review_issues}</b></span>
    <span class="kpi">回译疑点 <b>${s.backtranslation_issues}</b></span>
    <span class="kpi">空译 <b>${s.empty_targets}</b></span></div>`;
}

async function loadState() {
  const input = inputPath(); if (!input) return;
  const d = await jget(`/api/state?input=${enc(input)}`);
  const box = $("state");
  if (!d.exists) { box.innerHTML = `<div class="muted">尚无进度。运行“翻译”后生成状态。</div>`; return; }
  let html = `<div><b>《${esc(d.title)}》</b> <span class="muted">${esc(d.fmt)} · ${esc(d.source_lang)}→${esc(d.target_lang)}</span></div>`;
  html += `<h4>章节</h4><table class="grid"><thead><tr><th></th><th>#</th><th>标题</th><th>状态</th></tr></thead><tbody>`;
  d.chapters.forEach(c => html += `<tr><td>${c.status === "done" ? "✓" : "·"}</td><td>${c.index}</td><td>${esc(c.title)}</td><td>${esc(c.status)}</td></tr>`);
  html += "</tbody></table>";
  const a = d.analysis || {};
  if (a.genre || a.tone || (a.characters || []).length) {
    html += `<h4>风格 / 角色</h4><div class="muted">${esc(a.genre)} · ${esc(a.tone)}</div>`;
    if (a.style_guide) html += `<div class="muted">${esc(a.style_guide)}</div>`;
    if ((a.characters || []).length) {
      html += `<table class="grid"><thead><tr><th>原名</th><th>译名</th><th>性别</th><th>备注</th></tr></thead><tbody>`;
      a.characters.forEach(c => html += `<tr><td>${esc(c.source)}</td><td>${esc(c.target)}</td><td>${esc(c.gender)}</td><td>${esc(c.note)}</td></tr>`);
      html += "</tbody></table>";
    }
  }
  box.innerHTML = html;
}

// ── 初始化 ──────────────────────────────────────────────────────────────────
(async () => {
  const cfg = await jget("/api/config");
  if (cfg.default_input) { $("input").value = cfg.default_input; loadState(); }
})();
