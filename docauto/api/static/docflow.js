/* OneOffice DocFlow UI: One View, One Inbox, OneFlow (builder), One Library (+ Teach One),
   One Desk, One Source. Loaded after app.html's main script and reuses its helpers. */
const DFAPI = "/df";
const DST = {
  queued:{t:"Queued",c:"mute"}, processing:{t:"Processing",c:"mute"}, success:{t:"Auto-processed",c:"ok"},
  validation:{t:"In review",c:"warn"}, validated:{t:"Reviewed",c:"ok"}, invalid:{t:"Rejected",c:"bad"},
  failed:{t:"Failed",c:"bad"}, split:{t:"Split batch",c:"info"}
};
const CAT_ICON = {invoice:"🧾", purchase_order:"📦", receipt:"🧾", bank_statement:"🏦", id_document:"🪪", custom:"📄"};
const OPT_TXT = {tier1_opensource:"OneCore · Open source", tier2_cloud:"OneConnect · Cloud", tier3_ai:"OneMind · AI"};
const OPT_NAME = {tier1_opensource:"OneCore", tier2_cloud:"OneConnect", tier3_ai:"OneMind"};
const dstat = s => { const m = DST[s] || {t:s||"–", c:"mute"}; return `<span class="pill ${m.c}">${esc(m.t)}</span>`; };
const fmtDate = s => s ? esc(String(s).slice(0,16).replace("T"," ")) : "–";
const ms = v => v==null ? "–" : v < 1000 ? `${v} ms` : `${(v/1000).toFixed(1)} s`;
const age = h => h < 1 ? `${Math.round(h*60)} min` : h < 48 ? `${h.toFixed(1)} h` : `${Math.round(h/24)} d`;
const dfa = (p, o) => api(DFAPI + p, o);
const uid = () => Math.random().toString(36).slice(2, 8);
function kpi(label, value, sub="", cls=""){ return `<div class="card pad kpi ${cls}"><div class="muted small">${esc(label)}</div><div class="big">${value}</div><div class="muted small">${sub}</div></div>`; }
function download(path){
  const headers = token()?{Authorization:"Bearer "+token()}:{};
  fetch(API+path,{headers}).then(async r=>{ if(!r.ok) throw new Error(await r.text());
    const name=(r.headers.get("content-disposition")||"").match(/filename="([^"]+)"/)?.[1]||"export";
    const url=URL.createObjectURL(await r.blob()); const a=document.createElement("a"); a.href=url; a.download=name; a.click();
    setTimeout(()=>URL.revokeObjectURL(url),2000); }).catch(e=>toast(e.message,true));
}
async function postFiles(path, files, extra={}){
  const fd=new FormData(); files.forEach(f=>fd.append("files", f)); Object.entries(extra).forEach(([k,v])=>fd.append(k,v));
  const headers=token()?{Authorization:"Bearer "+token()}:{};
  const r=await fetch(API+path,{method:"POST",body:fd,headers}); const j=await r.json();
  if(!r.ok) throw new Error(j.message||j.detail||r.statusText); return j;
}
function dropZone(el, onFiles, label){
  el.innerHTML = `<div class="dz-ico">⬆</div><b>${label||"Drop documents here"}</b><div class="muted small">or click to choose · PDF, PNG, JPG, TIFF · several files at once</div><input type="file" multiple hidden accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp">`;
  const inp = el.querySelector("input");
  el.onclick = () => inp.click(); inp.onchange = () => inp.files.length && onFiles([...inp.files]);
  el.ondragover = e => { e.preventDefault(); el.classList.add("over"); }; el.ondragleave = () => el.classList.remove("over");
  el.ondrop = e => { e.preventDefault(); el.classList.remove("over"); if (e.dataTransfer.files.length) onFiles([...e.dataTransfer.files]); };
}
let DT_CACHE = null;
async function doctypesList(force){ if(!DT_CACHE||force) DT_CACHE = await dfa("/doctypes"); return DT_CACHE; }
const dtName = id => (DT_CACHE||[]).find(d=>d.id===id)?.name || (id ? id : "Unclassified");

/* ============================================================== HOME */
routes.home = async (el) => {
  const [ov] = await Promise.all([dfa("/overview"), doctypesList(true)]);
  const T = ov.totals, b = T.byStatus;
  el.innerHTML = `
  <div class="hero card"><div><div class="eyebrow">OneOffice DocFlow · One View</div><h1>Documents in, clean data out.</h1>
    <p>Drop any document into One Inbox, or build a flow for a business process. DocFlow recognises, splits, extracts, checks and delivers. People only see what needs a decision.</p>
    <div class="row" style="margin-top:12px"><a class="btn primary" href="#/inbox">Open One Inbox</a><a class="btn" href="#/flow/new">+ New flow</a><a class="btn" href="#/teach">✨ Teach One</a></div></div>
    <div class="hero-art">${["Intake","Classify","Extract","Check","Review","Deliver"].map((s,i)=>`<div class="ha"><span>${i+1}</span>${s}</div>`).join("")}</div></div>
  <div class="grid kpis" style="margin-top:14px">
    ${kpi("Documents", T.total, `${T.processed} finished`)}
    ${kpi("Auto-processed", T.stpRate==null?"–":pct(T.stpRate), "no human touch")}
    ${kpi("In review", ov.review, ov.overdue?`<span class="badc">${ov.overdue} past SLA</span> · <a href="#/review">open desk</a>`:'<a href="#/review">open One Desk</a>', ov.overdue?"badk":ov.review?"warnk":"")}
    ${kpi("Field accuracy", T.fieldAccuracy==null?"–":pct(T.fieldAccuracy), "fields reviewers did not change")}
    ${kpi("Delivery issues", ov.deliveryFailures, ov.deliveryFailures?"see flow delivery logs":"all deliveries OK", ov.deliveryFailures?"badk":"")}
  </div>
  <div class="grid g2" style="margin-top:14px">
    <div class="card"><div class="pad row"><b>Flows</b><span class="sp"></span><a class="small" href="#/flows">All flows →</a></div>
      <table>${ov.flows.map(f=>`<tr><td><a href="#/flow/${f.id}"><b>${f.isInbox?"📥 ":""}${esc(f.name)}</b></a></td>
        <td>${f.status==="live"?'<span class="pill ok">Live</span>':'<span class="pill mute">Draft</span>'}</td>
        <td class="right">${f.stats.total} docs</td><td style="width:140px">${bar(f.stats.stpRate)}</td>
        <td>${f.stats.byStatus.validation?`<a class="pill warn" href="#/review/${f.id}">${f.stats.byStatus.validation} in review</a>`:""}</td></tr>`).join("")}</table></div>
    <div class="card"><div class="pad"><b>Recent activity</b></div>
      ${ov.recent.length?`<table>${ov.recent.map(d=>`<tr><td><a href="#/doc/${d.id}">${esc(d.file_name)}</a><div class="small muted">${esc(d.instance_id)} · ${d.status==="split"?"batch":esc(dtName(d.doctype_id))}</div></td><td>${dstat(d.status)}</td><td class="small muted">${fmtDate(d.created_at)}</td></tr>`).join("")}</table>`
        :'<div class="empty">Nothing processed yet. Drop a document into <a href="#/inbox">One Inbox</a>.</div>'}</div></div>`;
};

/* ============================================================== SMART INBOX */
routes.inbox = async (el) => {
  await doctypesList();
  el.innerHTML = `<div class="head"><div class="grow"><h1>📥 <span class="onet">One</span> Inbox</h1><p>Drop anything: invoices, POs, IDs, statements, even one scanned PDF with all of them. Each page is recognised against your <a href="#/doctypes">document types</a>, batches are split, and every part is extracted and checked.</p></div>
    <a class="btn" href="#/flow/smart-inbox/builder">Inbox settings</a></div>
    <div class="card pad dz" id="dz"></div>
    <div class="row" style="margin:14px 0 8px"><b>Latest</b><span class="sp"></span><button class="btn sm" id="ref">Refresh</button></div>
    <div class="card" id="ilist"><div class="empty"><span class="spin"></span></div></div>`;
  dropZone($("#dz"), async files => {
    try { const r = await postFiles(`${DFAPI}/flows/smart-inbox/documents?source=upload`, files); toast(`${r.documents.length} document(s) received`); load(); }
    catch(e){ toast(e.message,true); } }, "Drop documents into One Inbox");
  const load = async () => {
    const docs = await dfa("/flows/smart-inbox/documents?limit=200");
    const parents = docs.filter(d=>!d.parent_id), kids = id => docs.filter(d=>d.parent_id===id);
    $("#ilist").innerHTML = parents.length ? `<table><tr><th>Document</th><th>Recognised as</th><th>Status</th><th>Received</th><th></th></tr>
      ${parents.map(p=>{ const ch = kids(p.id); return `<tr><td>${p.status==="split"?"🗂 ":""}<a href="#/doc/${p.id}">${esc(p.file_name)}</a></td>
        <td>${p.status==="split"?`<span class="muted small">${ch.length} document${ch.length===1?"":"s"}</span>`:esc(dtName(p.doctype_id))}</td><td>${dstat(p.status)}</td>
        <td class="small muted">${fmtDate(p.created_at)}</td><td class="right">${p.status==="validation"?`<a class="btn sm primary" href="#/review/smart-inbox/${p.id}">Review</a>`:""}</td></tr>
        ${ch.map(c=>`<tr class="child"><td><span class="tree">└</span><a href="#/doc/${c.id}">${esc(c.file_name)}</a></td><td>${CAT_ICON[(DT_CACHE.find(d=>d.id===c.doctype_id)||{}).category]||"❔"} ${esc(dtName(c.doctype_id))}</td>
          <td>${dstat(c.status)}</td><td class="small muted">${ms(c.processing_ms)}</td><td class="right">${c.status==="validation"?`<a class="btn sm primary" href="#/review/smart-inbox/${c.id}">Review</a>`:`<a class="btn sm" href="#/doc/${c.id}">View</a>`}</td></tr>`).join("")}`; }).join("")}</table>`
      : '<div class="empty">The inbox is empty.</div>';
    if (docs.some(d=>d.status==="queued"||d.status==="processing")) { clearTimeout(window.__it); window.__it=setTimeout(()=>{ if(location.hash.startsWith("#/inbox")) load(); },1500); }
  };
  $("#ref").onclick = load; load();
};

/* ============================================================== FLOWS LIST */
routes.flows = async (el) => {
  const flows = await dfa("/flows");
  el.innerHTML = `<div class="head"><div class="grow"><h1><span class="onet">One</span>Flow</h1><p>A flow is a business process: where documents come from, which types it accepts, which engine reads them, which rules check them, who reviews them and where results go.</p></div>
    <a class="btn primary" href="#/flow/new">+ New flow</a></div>
  <div class="card"><table class="itable"><tr><th>Flow</th><th>Document types</th><th>Engine</th><th>Status</th><th class="right">Documents</th><th>Auto-processed</th><th>In review</th></tr>
    ${flows.map(f=>`<tr><td><a href="#/flow/${f.id}"><b>${f.isInbox?"📥 ":""}${esc(f.name)}</b></a><div class="small muted">${esc(f.description||"")}</div></td>
      <td class="small">${f.isInbox&&!f.docTypes.length?"All document types":esc(f.docTypeNames.join(", "))}</td>
      <td><span class="pill ${PACK[f.engine.option].cls}">${OPT_TXT[f.engine.option]}</span></td>
      <td>${f.status==="live"?'<span class="pill ok">Live</span>':'<span class="pill mute">Draft</span>'} <span class="small muted">v${f.version}</span></td>
      <td class="right">${f.stats.total}</td><td style="width:150px">${bar(f.stats.stpRate)}</td>
      <td>${f.stats.byStatus.validation?`<a class="pill warn" href="#/review/${f.id}">${f.stats.byStatus.validation}</a>`:'<span class="muted">–</span>'}</td></tr>`).join("")}
  </table></div>`;
};

/* ============================================================== FLOW WORKSPACE / BUILDER */
const STAGES = [["intake","Intake","⬇"],["classify","Classify & split","🔎"],["extract","Extract","⚙"],["rules","Rules & lookups","✔"],["review","Review","👤"],["deliver","Deliver","➜"]];
let FB = null;
function blankFlow(){ return {name:"", description:"", docTypes:["invoice"], classify:{mode:"single",split:false,minScore:2},
  engine:{option:"tier1_opensource",ocrProvider:"auto",extractionProvider:"auto",capabilities:["ocr_printed"]},
  review:{fieldConfidence:0.85,documentConfidence:0.8,slaHours:24,slaWebhook:""}, rules:[], deliveries:[], intake:{inputFolder:"",pollSeconds:30}, status:"draft"}; }
routes.flow = async (el, rest) => {
  const [id, tab="builder"] = rest.split("/");
  await doctypesList();
  const isNew = id==="new";
  const fl = isNew ? blankFlow() : await dfa(`/flows/${id}`);
  const st = isNew ? null : await dfa(`/flows/${id}/stats`);
  FB = { fl: JSON.parse(JSON.stringify(fl)), stage: FB && FB.id===id ? FB.stage : "intake", id, isNew, refs: await dfa("/reftables") };
  const waiting = st ? st.byStatus.validation : 0;
  const tabs = isNew ? [] : [["builder","Builder"],["documents","Documents"],["dashboard","Dashboard"],["deliveries","Delivery log"],["connect","Connect a bot"]];
  el.innerHTML = `<div class="crumb"><a href="#/flows">OneFlow</a> / ${isNew?"New flow":esc(fl.name)}</div>
  <div class="head"><div class="grow">${isNew?`<h1>New flow</h1><p>Set the six stages, then create the flow. You can test it while it is a draft.</p>`
     :`<div class="row"><h1>${fl.isInbox?"📥 ":""}${esc(fl.name)}</h1>${fl.status==="live"?'<span class="pill ok">Live</span>':'<span class="pill mute">Draft</span>'}<span class="pill mute">v${fl.version}</span></div><p>${esc(fl.description||"")}</p>`}</div>
    ${isNew?"":`<div class="row"><button class="btn primary" id="fUp">Upload documents</button>
      <a class="btn ${waiting?"":"disabled"}" href="#/review/${fl.id}">Review ${waiting?`<span class="pill warn">${waiting}</span>`:""}</a>
      ${fl.status==="live"?'<button class="btn" id="fDraft">Set to draft</button>':'<button class="btn golive" id="fLive">Go live</button>'}
      <div class="dd"><button class="btn" id="expBtn">Export ▾</button><div class="ddm" id="expMenu" hidden><button data-exp="xlsx">Excel</button><button data-exp="csv">CSV</button></div></div></div>`}</div>
  ${tabs.length?`<div class="tabs itabs">${tabs.map(([k,v])=>`<button data-tab="${k}" class="${k===tab?"on":""}">${v}</button>`).join("")}</div>`:""}
  <div id="flowtab"></div>`;
  document.querySelectorAll("[data-tab]").forEach(b=>b.onclick=()=>location.hash=`#/flow/${id}/${b.dataset.tab}`);
  if (!isNew){
    $("#fUp").onclick = () => { const i=document.createElement("input"); i.type="file"; i.multiple=true; i.onchange=async()=>{ try{ const r=await postFiles(`${DFAPI}/flows/${id}/documents?source=upload`,[...i.files]); toast(`${r.documents.length} document(s) received`); location.hash=`#/flow/${id}/documents`; route(); }catch(e){toast(e.message,true)} }; i.click(); };
    if ($("#fLive")) $("#fLive").onclick=async()=>{ try{ await dfa(`/flows/${id}/live?live=true`,{method:"POST"}); toast("Flow is live: bots and folders can use it"); route(); }catch(e){toast(e.message,true)} };
    if ($("#fDraft")) $("#fDraft").onclick=async()=>{ await dfa(`/flows/${id}/live?live=false`,{method:"POST"}); toast("Flow set to draft"); route(); };
    $("#expBtn").onclick=()=>{ $("#expMenu").hidden=!$("#expMenu").hidden; };
    document.querySelectorAll("[data-exp]").forEach(b=>b.onclick=()=>{ $("#expMenu").hidden=true; download(`${DFAPI}/flows/${id}/export?format=${b.dataset.exp}`); });
  }
  const box = $("#flowtab");
  if (isNew || tab==="builder") drawBuilder(box);
  else if (tab==="documents") flowDocuments(box, fl);
  else if (tab==="dashboard") flowDashboard(box, fl, st);
  else if (tab==="deliveries") flowDeliveries(box, fl);
  else flowConnect(box, fl);
};

function stageSummary(k){
  const f = FB.fl;
  return ({
    intake: `Upload, bot${f.intake.inputFolder?", folder":""}`,
    classify: f.classify.mode==="auto" ? `Auto-recognise ${f.isInbox&&!f.docTypes.length?"all types":f.docTypes.length+" type(s)"}${f.classify.split?" · split":""}` : esc(dtName(f.docTypes[0])),
    extract: OPT_TXT[f.engine.option],
    rules: f.rules.length ? `${f.rules.filter(r=>r.enabled!==false).length} rule(s)` : "No extra rules",
    review: `Below ${pct(f.review.fieldConfidence)} · SLA ${f.review.slaHours} h`,
    deliver: f.deliveries.length ? `${f.deliveries.length} destination(s)` : "Keep in DocFlow"
  })[k];
}
function drawBuilder(box){
  const f = FB.fl;
  box.innerHTML = `${FB.isNew?`<div class="card pad" style="margin-bottom:12px"><div class="grid g2"><label class="f"><span>Flow name</span><input type="text" id="bName" value="${esc(f.name)}" placeholder="e.g. Accounts payable invoices"></label>
      <label class="f"><span>Description</span><input type="text" id="bDesc" value="${esc(f.description)}" placeholder="What this process does"></label></div></div>`:""}
    <div class="pipe">${STAGES.map(([k,t,ic],i)=>`<button class="stage ${FB.stage===k?"on":""}" data-st="${k}"><div class="st-top"><span class="st-ic">${ic}</span><span class="st-n">${i+1}</span></div><b>${t}</b><div class="small muted">${stageSummary(k)}</div></button>${i<5?'<div class="pipe-arrow">›</div>':""}`).join("")}</div>
    <div class="card pad" id="stage"></div>
    <div class="row" style="margin-top:12px"><span class="muted small" id="dirty"></span><span class="sp"></span>
      ${FB.isNew?'<a class="btn" href="#/flows">Cancel</a><button class="btn primary" id="bSave">Create flow</button>':`${f.isInbox?"":'<button class="btn danger" id="bDel">Delete flow</button>'}<button class="btn primary" id="bSave">Save flow</button>`}</div>`;
  const keepHeader = () => { if ($("#bName")) { f.name=$("#bName").value.trim(); f.description=$("#bDesc").value.trim(); } };
  box.querySelectorAll("[data-st]").forEach(b=>b.onclick=()=>{ readStage(); keepHeader(); FB.stage=b.dataset.st; drawBuilder(box); });
  ({intake:stIntake, classify:stClassify, extract:stExtract, rules:stRules, review:stReview, deliver:stDeliver}[FB.stage])($("#stage"));
  $("#bSave").onclick = async () => { readStage(); keepHeader(); if (FB.isNew && !f.name) return toast("Give the flow a name",true);
    try { const r = FB.isNew ? await dfa("/flows",{method:"POST",json:f}) : await dfa(`/flows/${FB.id}`,{method:"PUT",json:f});
      toast(FB.isNew?`Flow "${r.name}" created as draft`:"Flow saved"); FB.id=r.id; location.hash=`#/flow/${r.id}/builder`; route(); } catch(e){ toast(e.message,true); } };
  if ($("#bDel")) { const del=$("#bDel"); del.onclick=async()=>{ if(!del.dataset.arm){ del.dataset.arm=1; del.textContent="Click again to delete"; setTimeout(()=>{del.dataset.arm="";del.textContent="Delete flow"},4000); return; }
    await dfa(`/flows/${FB.id}`,{method:"DELETE"}); toast("Flow deleted"); location.hash="#/flows"; }; }
}
let readStage = () => {};
function stIntake(b){
  const f=FB.fl;
  b.innerHTML = `<h3>⬇ Intake</h3><p class="muted small">Where documents come from. Upload and bots are always available; add a watched folder for scanners or shared drives.</p>
    <div class="grid g3"><div class="card pad mini"><b>Upload</b><div class="small muted">Drag and drop in DocFlow (Upload documents button).</div></div>
      <div class="card pad mini"><b>Bots & API</b><div class="small muted">Action <i>Send document to flow</i> with flow id <code>${esc(FB.id==="new"?"(after create)":FB.id)}</code>. Bots only use live flows.</div></div>
      <div class="card pad mini"><b>Watched folder</b><div class="small muted">New files are picked up while the flow is live and moved to <code>processed</code>.</div></div></div>
    <div class="grid g2" style="margin-top:12px"><label class="f"><span>Input folder (optional)</span><input type="text" id="iIn" value="${esc(f.intake.inputFolder)}" placeholder="C:\\Scans\\AP"></label>
      <label class="f"><span>Check every (seconds)</span><input type="number" min="5" id="iPoll" value="${f.intake.pollSeconds}"></label></div>
    ${!FB.isNew&&f.intake.inputFolder?'<button class="btn sm" id="iScan">Pick up files now</button>':""}`;
  readStage = () => { f.intake.inputFolder=$("#iIn").value.trim(); f.intake.pollSeconds=parseInt($("#iPoll").value||"30",10); };
  if ($("#iScan")) $("#iScan").onclick=async()=>{ try{ const r=await dfa(`/flows/${FB.id}/intake/scan`,{method:"POST"}); toast(`${r.ingested.length} file(s) picked up`); }catch(e){toast(e.message,true)} };
}
function stClassify(b){
  const f=FB.fl, all=DT_CACHE;
  b.innerHTML = `<h3>🔎 Classify & split</h3><p class="muted small">Which document types this flow accepts and how each document is recognised.</p>
    <span class="flabel">Document types ${f.isInbox?'<span class="muted small">(none ticked = all types, including new ones)</span>':""}</span>
    <div class="grid tplgrid">${all.map(d=>`<label class="card pad dtpick ${f.docTypes.includes(d.id)?"sel":""}"><input type="checkbox" name="cdt" value="${d.id}" ${f.docTypes.includes(d.id)?"checked":""}> <span>${CAT_ICON[d.category]||"📄"} <b>${esc(d.name)}</b><div class="small muted">${d.fields.length} fields</div></span></label>`).join("")}
      <a class="card pad dtpick add" href="#/teach">＋ Teach a new type</a></div>
    <div class="grid g2" style="margin-top:14px">
      <label class="f"><span>Recognition</span><select id="cMode"><option value="single" ${f.classify.mode==="single"?"selected":""}>Every document is the first ticked type</option><option value="auto" ${f.classify.mode==="auto"?"selected":""}>Recognise the type of each document automatically</option></select></label>
      <label class="f"><span>Mixed batches</span><label class="row" style="gap:8px;margin-top:6px"><label class="switch"><input type="checkbox" id="cSplit" ${f.classify.split?"checked":""}><span></span></label> Split a scanned batch into one document per part</label></label></div>
    <label class="f"><span>Minimum recognition score</span><input type="number" step="0.5" min="0" id="cMin" value="${f.classify.minScore}" style="max-width:160px"><em>Pages scoring lower go to One Desk as "Unclassified" so a person can pick the type.</em></label>`;
  b.querySelectorAll("[name=cdt]").forEach(c=>c.onchange=()=>c.closest(".dtpick").classList.toggle("sel",c.checked));
  readStage = () => { f.docTypes=[...b.querySelectorAll("[name=cdt]:checked")].map(x=>x.value); f.classify.mode=$("#cMode").value; f.classify.split=$("#cSplit").checked; f.classify.minScore=parseFloat($("#cMin").value||"2"); };
}
function engineChoices(option){
  const ocr = S.providers.filter(p=>p.kind==="ocr" && p.id!=="pdf-text" && (option==="tier3_ai" || p.optionPack===option));
  const ex = S.providers.filter(p=>p.kind==="extraction" && (option==="tier3_ai" ? p.optionPack==="tier3_ai" : p.optionPack==="core"));
  return {ocr, ex};
}
function stExtract(b){
  const e=FB.fl.engine, {ocr, ex}=engineChoices(e.option);
  const card = pid => { const engines=S.providers.filter(p=>p.optionPack===pid); const rdy=engines.filter(p=>p.available&&p.allowedByPolicy).length;
    return `<button class="card pad optpick ${e.option===pid?"sel":""}" data-opt="${pid}" style="--c:${PACK[pid].c}"><span class="pill ${PACK[pid].cls}">${OPT_NAME[pid]}</span> <b>${OPT_TXT[pid].split(" · ")[1]}</b><div class="small muted" style="margin-top:4px">${esc(S.settings.packs[pid].summary)}</div>
      <div class="small" style="margin-top:6px">${rdy?`<span class="pill ok">${rdy} ready</span>`:'<span class="pill warn">Not configured</span>'}</div></button>`; };
  const opt = p => `<option value="${p.id}" ${p.id===e.ocrProvider||p.id===e.extractionProvider?"selected":""}>${esc(p.displayName)}${p.available&&p.allowedByPolicy?"":" (not ready)"}</option>`;
  b.innerHTML = `<h3>⚙ Extract</h3><p class="muted small">Which engine reads the pages and fills the fields. Configure engines under <a href="#/overview">Engines</a>.</p>
    <div class="grid g3">${["tier1_opensource","tier2_cloud","tier3_ai"].map(card).join("")}</div>
    <div class="grid g2" style="margin-top:14px"><label class="f"><span>OCR engine</span><select id="eOcr"><option value="auto">Auto (best allowed)</option>${ocr.map(opt).join("")}</select></label>
      <label class="f"><span>Field extraction</span><select id="eEx"><option value="auto">Auto</option>${ex.map(opt).join("")}</select><em>${e.option==="tier3_ai"?"AI reads the text (and page images) against the document type's fields.":"Labels, patterns and labels learned from reviewers."}</em></label></div>
    <label class="f"><span>Features</span><div class="row">${FEATURES.map(([k,v])=>`<label class="row" style="gap:4px"><input type="checkbox" name="efeat" value="${k}" ${e.capabilities.includes(k)?"checked":""}>${v}</label>`).join("")}</div></label>`;
  readStage = () => { e.ocrProvider=$("#eOcr").value; e.extractionProvider=$("#eEx").value; e.capabilities=["ocr_printed",...[...b.querySelectorAll("[name=efeat]:checked")].map(x=>x.value)]; };
  b.querySelectorAll("[data-opt]").forEach(x=>x.onclick=()=>{ readStage(); e.option=x.dataset.opt; e.ocrProvider="auto"; e.extractionProvider="auto"; stExtract(b); });
}
const RULE_TYPES = {
  duplicate:{t:"Duplicate check", d:"Flag a document whose key fields were already seen in this flow.", p:{fields:["supplier_vat","invoice_number"]}},
  lookup:{t:"Reference lookup", d:"Check a value against reference data (e.g. supplier master) and fill other fields from it.", p:{field:"supplier_vat",table:"",column:"",enrich:{}}},
  ksa_vat:{t:"KSA VAT (ZATCA) check", d:"VAT number format, VAT = 15% of subtotal, total = subtotal + VAT.", p:{vatNumberField:"supplier_vat",subtotalField:"subtotal",vatField:"vat_amount",totalField:"total",rate:0.15}},
  threshold:{t:"Amount threshold / approval", d:"Route documents above (or below) an amount to review.", p:{field:"total",op:">",value:50000,message:"Approval needed above 50,000"}},
  required:{t:"Required fields", d:"Fields that must be present for this process.", p:{fields:["po_number"]}},
  expression:{t:"Custom check", d:"Any arithmetic/logic over field names.", p:{expression:"total > 0",message:""}}
};
function stRules(b){
  const f=FB.fl;
  const refOpts = sel => `<option value="">– choose –</option>${FB.refs.map(r=>`<option value="${r.id}" ${r.id===sel?"selected":""}>${esc(r.name)} (${r.rowCount} rows)</option>`).join("")}`;
  const params = (r,i) => { const p=r.params;
    if (r.type==="duplicate"||r.type==="required") return `<label class="f"><span>Fields</span><input type="text" data-p="fields" value="${esc((p.fields||[]).join(", "))}"><em>comma separated field names</em></label>`;
    if (r.type==="lookup"){ const t=FB.refs.find(x=>x.id===p.table);
      return `<div class="grid g3"><label class="f"><span>Field</span><input type="text" data-p="field" value="${esc(p.field||"")}"></label>
        <label class="f"><span>Reference table</span><select data-p="table">${refOpts(p.table)}</select>${FB.refs.length?"":'<em><a href="#/refdata">Upload reference data first</a></em>'}</label>
        <label class="f"><span>Match column</span>${t?`<select data-p="column">${t.columns.map(c=>`<option ${c===p.column?"selected":""}>${esc(c)}</option>`).join("")}</select>`:`<input type="text" data-p="column" value="${esc(p.column||"")}">`}</label></div>
        <label class="f"><span>Fill fields from the matched row (optional)</span><input type="text" data-p="enrich" value="${esc(Object.entries(p.enrich||{}).map(([k,v])=>k+"="+v).join(", "))}" placeholder="supplier_name=name, payment_terms=terms"></label>`; }
    if (r.type==="ksa_vat") return `<div class="grid g3">${["vatNumberField","subtotalField","vatField","totalField"].map(k=>`<label class="f"><span>${k.replace("Field"," field")}</span><input type="text" data-p="${k}" value="${esc(p[k]||"")}"></label>`).join("")}<label class="f"><span>Rate</span><input type="number" step="0.01" data-p="rate" value="${p.rate??0.15}"></label></div>`;
    if (r.type==="threshold") return `<div class="grid g3"><label class="f"><span>Field</span><input type="text" data-p="field" value="${esc(p.field||"")}"></label>
      <label class="f"><span>Condition</span><select data-p="op">${[">",">=","<","<=","=="].map(o=>`<option ${o===p.op?"selected":""}>${o}</option>`).join("")}</select></label>
      <label class="f"><span>Value</span><input type="number" data-p="value" value="${p.value??0}"></label></div><label class="f"><span>Message for the reviewer</span><input type="text" data-p="message" value="${esc(p.message||"")}"></label>`;
    return `<label class="f"><span>Expression</span><input type="text" class="mono" data-p="expression" value="${esc(p.expression||"")}"></label><label class="f"><span>Message</span><input type="text" data-p="message" value="${esc(p.message||"")}"></label>`;
  };
  b.innerHTML = `<h3>✔ Rules & lookups</h3><p class="muted small">Business checks after extraction. Each rule can send the document to review, reject it, or just add a warning. Document-type checks (e.g. totals) always run too.</p>
    <div id="rl">${f.rules.map((r,i)=>`<div class="card pad rulec" data-i="${i}"><div class="row"><label class="switch"><input type="checkbox" data-k="enabled" ${r.enabled!==false?"checked":""}><span></span></label>
      <b>${esc(RULE_TYPES[r.type].t)}</b><input type="text" data-k="name" value="${esc(r.name||"")}" placeholder="name" style="max-width:240px"><span class="sp"></span>
      <select data-k="action"><option value="review" ${r.action==="review"?"selected":""}>Send to review</option><option value="reject" ${r.action==="reject"?"selected":""}>Reject</option><option value="warn" ${r.action==="warn"?"selected":""}>Warn only</option></select>
      <button class="btn sm ghost" data-del="${i}">✕</button></div><div class="small muted" style="margin:4px 0 8px">${esc(RULE_TYPES[r.type].d)}</div>${params(r,i)}</div>`).join("")||'<div class="empty">No rules yet.</div>'}</div>
    <div class="row" style="margin-top:10px"><span class="muted small">Add rule:</span>${Object.entries(RULE_TYPES).map(([k,v])=>`<button class="btn sm" data-add="${k}">${esc(v.t)}</button>`).join("")}</div>`;
  readStage = () => { b.querySelectorAll(".rulec").forEach(c=>{ const r=f.rules[+c.dataset.i];
    r.enabled=c.querySelector("[data-k=enabled]").checked; r.name=c.querySelector("[data-k=name]").value.trim(); r.action=c.querySelector("[data-k=action]").value;
    c.querySelectorAll("[data-p]").forEach(inp=>{ const k=inp.dataset.p, v=inp.value.trim();
      if (k==="fields") r.params.fields=v.split(",").map(s=>s.trim()).filter(Boolean);
      else if (k==="enrich") r.params.enrich=Object.fromEntries(v.split(",").map(s=>s.split("=").map(x=>x.trim())).filter(x=>x[0]&&x[1]));
      else if (k==="value"||k==="rate") r.params[k]=parseFloat(v||"0"); else r.params[k]=v; }); }); };
  b.querySelectorAll("[data-add]").forEach(x=>x.onclick=()=>{ readStage(); const t=RULE_TYPES[x.dataset.add]; f.rules.push({type:x.dataset.add, name:t.t, enabled:true, action:"review", docTypes:[], params:JSON.parse(JSON.stringify(t.p))}); stRules(b); });
  b.querySelectorAll("[data-del]").forEach(x=>x.onclick=()=>{ readStage(); f.rules.splice(+x.dataset.del,1); stRules(b); });
  b.querySelectorAll("[data-p=table]").forEach(x=>x.onchange=()=>{ readStage(); stRules(b); });
}
function stReview(b){
  const r=FB.fl.review;
  b.innerHTML = `<h3>👤 Review</h3><p class="muted small">When a person must look at a document, and how fast. Reviews happen in <a href="#/review">One Desk</a>.</p>
    <div class="grid g2"><label class="f"><span>Review a field below this confidence</span><input type="number" step="0.01" min="0" max="1" id="rF" value="${r.fieldConfidence}"></label>
      <label class="f"><span>Review a document below this confidence</span><input type="number" step="0.01" min="0" max="1" id="rD" value="${r.documentConfidence}"></label>
      <label class="f"><span>Review SLA (hours)</span><input type="number" step="0.5" min="0" id="rS" value="${r.slaHours}"><em>Documents waiting longer are marked overdue.</em></label>
      <label class="f"><span>Overdue alert webhook (optional)</span><input type="text" id="rW" value="${esc(r.slaWebhook)}" placeholder="https://… (Teams / Slack / your RPA tool)"></label></div>`;
  readStage = () => { r.fieldConfidence=parseFloat($("#rF").value); r.documentConfidence=parseFloat($("#rD").value); r.slaHours=parseFloat($("#rS").value||"24"); r.slaWebhook=$("#rW").value.trim(); };
}
const DLV = {
  webhook:{t:"Webhook", d:"POST the result as JSON to your ERP, middleware or integration.", p:{url:"",authHeader:""}},
  start_bot:{t:"Start a bot", d:"Call your RPA tool to start a bot with the extracted data as input.", p:{url:"",bot:""}},
  excel:{t:"Excel register", d:"Append one row per document to an Excel file (one sheet per document type).", p:{path:""}},
  folder:{t:"Folder", d:"Write JSON + the original file into status subfolders.", p:{path:""}}
};
function stDeliver(b){
  const f=FB.fl, sts=["success","validated","validation","invalid","failed"];
  b.innerHTML = `<h3>➜ Deliver</h3><p class="muted small">Where finished documents go. Every delivery is logged and retried once.</p>
    <div>${f.deliveries.map((d,i)=>`<div class="card pad rulec" data-i="${i}"><div class="row"><label class="switch"><input type="checkbox" data-k="enabled" ${d.enabled!==false?"checked":""}><span></span></label>
      <b>${esc(DLV[d.type].t)}</b><input type="text" data-k="name" value="${esc(d.name||"")}" placeholder="name" style="max-width:240px"><span class="sp"></span>
      ${FB.isNew||!d.id?"":`<button class="btn sm" data-test="${d.id}">Send test</button>`}<button class="btn sm ghost" data-del="${i}">✕</button></div>
      <div class="small muted" style="margin:4px 0 8px">${esc(DLV[d.type].d)}</div>
      <div class="grid g2">${Object.keys(DLV[d.type].p).map(k=>`<label class="f"><span>${{url:"URL",authHeader:"Authorization header (optional)",bot:"Bot name",path:d.type==="excel"?"Excel file path":"Folder path"}[k]}</span><input type="text" data-p="${k}" value="${esc(d.params[k]||"")}" placeholder="${k==="path"?(d.type==="excel"?"C:\\\\Reports\\\\ap-register.xlsx":"C:\\\\DocFlow\\\\out"):k==="url"?"https://":""}"></label>`).join("")}</div>
      <div class="row small"><span class="muted">Send when:</span>${sts.map(s=>`<label class="row" style="gap:4px"><input type="checkbox" data-on="${s}" ${d.on.includes(s)?"checked":""}>${DST[s].t}</label>`).join("")}</div></div>`).join("")||'<div class="empty">Results stay in DocFlow (export any time).</div>'}</div>
    <div class="row" style="margin-top:10px"><span class="muted small">Add destination:</span>${Object.entries(DLV).map(([k,v])=>`<button class="btn sm" data-add="${k}">${v.t}</button>`).join("")}</div>`;
  readStage = () => { b.querySelectorAll(".rulec").forEach(c=>{ const d=f.deliveries[+c.dataset.i];
    d.enabled=c.querySelector("[data-k=enabled]").checked; d.name=c.querySelector("[data-k=name]").value.trim();
    c.querySelectorAll("[data-p]").forEach(inp=>d.params[inp.dataset.p]=inp.value.trim());
    d.on=[...c.querySelectorAll("[data-on]:checked")].map(x=>x.dataset.on); }); };
  b.querySelectorAll("[data-add]").forEach(x=>x.onclick=()=>{ readStage(); const t=DLV[x.dataset.add]; f.deliveries.push({type:x.dataset.add, name:t.t, enabled:true, on:["success","validated"], params:JSON.parse(JSON.stringify(t.p))}); stDeliver(b); });
  b.querySelectorAll("[data-del]").forEach(x=>x.onclick=()=>{ readStage(); f.deliveries.splice(+x.dataset.del,1); stDeliver(b); });
  b.querySelectorAll("[data-test]").forEach(x=>x.onclick=async()=>{ try{ const r=await dfa(`/flows/${FB.id}/deliveries/${x.dataset.test}/test`,{method:"POST"}); const o=r[0]; toast(o?`${o.delivery}: ${o.ok?"delivered":"failed"} (${o.detail})`:"Nothing sent", o&&!o.ok); }catch(e){toast(e.message,true)} });
}
async function flowDocuments(box, fl){
  box.innerHTML = `<div class="card pad dz" id="fdz"></div><div class="row" style="margin:12px 0" id="chips">${["",...Object.keys(DST)].map(k=>`<button class="chipb ${k?"":"on"}" data-s="${k}">${k?DST[k].t:"All"}</button>`).join("")}</div><div class="card" id="dl"></div>`;
  dropZone($("#fdz"), async files=>{ try{ const r=await postFiles(`${DFAPI}/flows/${fl.id}/documents?source=upload`, files); toast(`${r.documents.length} document(s) received`); load(); }catch(e){toast(e.message,true)} });
  let cur="";
  const load=async()=>{ const docs=await dfa(`/flows/${fl.id}/documents?limit=300${cur?"&status="+cur:""}`);
    $("#dl").innerHTML = docs.length?`<table><tr><th>Document</th><th>Type</th><th>Status</th><th>Source</th><th>Received</th><th>Time</th><th>Reviewed by</th><th></th></tr>
      ${docs.map(d=>`<tr><td>${d.parent_id?'<span class="tree">└</span>':""}<a href="#/doc/${d.id}">${esc(d.file_name)}</a><div class="small muted mono">${d.id.slice(0,8)} · v${d.instance_version||"–"}</div></td>
        <td class="small">${d.status==="split"?"batch":esc(dtName(d.doctype_id))}</td><td>${dstat(d.status)}${(d.flags&&JSON.parse(d.flags).length)?` <span class="small warnc" title="${esc(JSON.parse(d.flags).map(x=>x.message).join("\n"))}">⚑ ${JSON.parse(d.flags).length}</span>`:""}</td>
        <td class="small">${esc(d.source||"–")}</td><td class="small">${fmtDate(d.created_at)}</td><td class="small">${ms(d.processing_ms)}</td>
        <td class="small">${esc(d.validated_by||"–")}${d.corrected_count?` <span class="muted">(${d.corrected_count} fixed)</span>`:""}</td>
        <td class="right">${d.status==="validation"?`<a class="btn sm primary" href="#/review/${fl.id}/${d.id}">Review</a>`:`<a class="btn sm" href="#/doc/${d.id}">View</a>`}</td></tr>`).join("")}</table>`:'<div class="empty">No documents.</div>';
    if (docs.some(d=>d.status==="queued"||d.status==="processing")){ clearTimeout(window.__ft); window.__ft=setTimeout(()=>{ if(location.hash.includes("/documents")) load(); },1500); } };
  box.querySelectorAll("[data-s]").forEach(b=>b.onclick=()=>{ cur=b.dataset.s; box.querySelectorAll("[data-s]").forEach(x=>x.classList.toggle("on",x===b)); load(); });
  load();
}
function flowDashboard(box, fl, st){
  const b=st.byStatus, max=Math.max(1,...st.daily.map(d=>d.total));
  const seg=(n,c,l)=> n?`<i style="flex:${n};background:${c}" title="${l}: ${n}"></i>`:"";
  box.innerHTML = `<div class="grid kpis">${kpi("Documents", st.total, `${st.processed} finished`)}${kpi("Auto-processed", st.stpRate==null?"–":pct(st.stpRate), "no human touch")}
    ${kpi("In review", b.validation, b.validation?`<a href="#/review/${fl.id}">open →</a>`:"empty", b.validation?"warnk":"")}${kpi("Field accuracy", st.fieldAccuracy==null?"–":pct(st.fieldAccuracy), "not changed by reviewers")}
    ${kpi("Avg. time", ms(st.avgProcessingMs), `${st.pages} pages · ${st.credits} credits`)}</div>
  <div class="grid g2" style="margin-top:14px"><div class="card pad"><b>Status</b><div class="stack" style="margin:12px 0">${seg(b.success,"var(--ok)","Auto")}${seg(b.validated,"var(--accent)","Reviewed")}${seg(b.validation,"var(--warn)","In review")}${seg(b.invalid,"var(--t3)","Rejected")}${seg(b.failed,"var(--bad)","Failed")}</div>
    <table>${["success","validation","validated","invalid","failed"].map(k=>`<tr><td>${dstat(k)}</td><td class="right">${b[k]||0}</td></tr>`).join("")}</table></div>
    <div class="card pad"><b>Last 14 days</b>${st.daily.length?`<div class="chart">${st.daily.map(d=>`<div class="col" title="${d.day}: ${d.total}"><div class="bars" style="height:${Math.round(d.total/max*140)}px">${seg(d.stp,"var(--ok)","Auto")}${seg(d.human,"var(--warn)","Review")}${seg(d.failed,"var(--bad)","Failed")}${seg(d.total-d.stp-d.human-d.failed,"var(--line)","Other")}</div><div class="small muted">${d.day.slice(5)}</div></div>`).join("")}</div>`:'<div class="empty">No documents yet.</div>'}</div></div>`;
}
async function flowDeliveries(box, fl){
  const log = await dfa(`/flows/${fl.id}/deliveries`);
  box.innerHTML = `<div class="card">${log.length?`<table><tr><th>When</th><th>Destination</th><th>Document</th><th>Result</th></tr>${log.map(l=>`<tr><td class="small">${fmtDate(l.ts)}</td><td>${esc(l.delivery)} <span class="small muted">${esc(l.type)}</span></td><td class="small"><a href="#/doc/${l.document_id}">${l.document_id.slice(0,8)}</a></td><td>${l.ok?'<span class="pill ok">Delivered</span>':'<span class="pill bad">Failed</span>'} <span class="small muted">${esc(l.detail)}</span></td></tr>`).join("")}</table>`:'<div class="empty">Nothing delivered yet. Add destinations in Builder → Deliver.</div>'}</div>`;
}
function flowConnect(box, fl){
  const base=location.origin;
  box.innerHTML = `<div class="card pad"><b>Use this flow from a bot</b><p class="muted small">In the bot designer add actions from the <b>DocFlow</b> package and choose flow <code>${esc(fl.id)}</code>. ${fl.status!=="live"?'<span class="warnc">Set the flow live first.</span>':""}</p>
    <table><tr><th>Action</th><th>Result</th></tr><tr><td><b>Send document to flow</b></td><td><code>documentId</code></td></tr><tr><td><b>Wait for document</b></td><td>status + fields (optionally waits for review)</td></tr>
    <tr><td><b>Get document result</b></td><td>fields, confidence, flags, split children</td></tr><tr><td><b>Export flow results</b></td><td>Excel / CSV file</td></tr></table></div>
  <div class="grid g2" style="margin-top:12px"><div class="card pad"><b>REST</b><pre>curl -F "files=@scan.pdf" "${base}${API}${DFAPI}/flows/${fl.id}/documents?source=bot&requireLive=true"
curl ${base}${API}${DFAPI}/documents/&lt;documentId&gt;</pre></div>
  <div class="card pad"><b>Java</b><pre>DocAutoClient c = new DocAutoClient("${base}", token);
String id = DocAutoClient.jsonString(c.sendToFlow("${esc(fl.id)}", Path.of("scan.pdf")), "documentId");
String doc = c.waitForDocument(id, Duration.ofMinutes(5), true);</pre></div></div>`;
}

/* ============================================================== DOCUMENT DETAIL (read-only) */
routes.doc = async (el, id) => {
  await doctypesList();
  const d = await dfa(`/documents/${id}`);
  if (d.status==="validation"){ location.hash=`#/review/${d.flowId}/${id}`; return; }
  el.innerHTML = `<div class="crumb"><a href="#/flow/${d.flowId}/documents">${esc(d.flowId)}</a> / document</div>
  <div class="head"><div class="grow"><div class="row"><h1 style="font-size:19px">${esc(d.file_name)}</h1>${dstat(d.status)}</div><p>${esc(dtName(d.doctype_id))} · received ${fmtDate(d.created_at)} via ${esc(d.source||"?")}${d.validated_by?` · reviewed by ${esc(d.validated_by)}`:""}</p></div>
    <div class="row">${["success","validated","invalid"].includes(d.status)?`<a class="btn" href="#/review/${d.flowId}/${id}">Open in One Desk</a>`:""}<a class="btn" href="${API}/documents/${id}/file" target="_blank">Original file</a></div></div>
  ${d.error?`<div class="result bad">${esc(d.error.code)}: ${esc(d.error.message)}</div>`:""}
  ${d.flags.length?`<div class="card pad" style="margin-bottom:12px"><b>Flags</b>${d.flags.map(f=>`<div class="small"><span class="pill ${f.action==="reject"?"bad":f.action==="review"?"warn":"mute"}">${esc(f.action)}</span> <b>${esc(f.rule)}</b>: ${esc(f.message)}</div>`).join("")}</div>`:""}
  ${d.children.length?`<div class="card"><div class="pad"><b>Split into ${d.children.length} documents</b></div><table>${d.children.map(c=>`<tr><td><a href="#/doc/${c.id}">${esc(c.file_name)}</a></td><td>${esc(dtName(c.doctype_id))}</td><td>${dstat(c.status)}</td></tr>`).join("")}</table></div>`
   :`<div class="card"><table><tr><th>Field</th><th>Value</th><th>Confidence</th></tr>${Object.entries(d.fields).map(([k,v])=>`<tr><td><b>${esc(k)}</b></td><td>${typeof v==="object"&&v?`<pre>${esc(JSON.stringify(v,null,1))}</pre>`:`<span dir="auto">${esc(v??"–")}</span>`}</td><td>${bar(d.confidence[k])}</td></tr>`).join("")||'<tr><td class="empty">No fields</td></tr>'}</table></div>`}`;
};

/* ============================================================== DOC TYPES */
routes.doctypes = async (el) => {
  const list = await doctypesList(true);
  el.innerHTML = `<div class="head"><div class="grow"><h1><span class="onet">One</span> Library</h1><p>Reusable definitions of what to extract. Any flow can use them, and One Inbox recognises all of them.</p></div>
    <div class="row"><a class="btn" href="#/doctype/new">+ Blank type</a><a class="btn primary" href="#/teach">✨ Teach from samples</a></div></div>
  <div class="grid tplgrid">${list.map(d=>`<a class="card pad dtcard" href="#/doctype/${d.id}"><div class="row"><span style="font-size:24px">${CAT_ICON[d.category]||"📄"}</span><span class="sp"></span><span class="pill mute">v${d.version}</span></div>
    <b>${esc(d.name)}</b><div class="small muted">${esc(d.description||"")}</div>
    <div class="small" style="margin-top:8px">${d.fields.length} fields · ${d.usedBy.length?`used by ${esc(d.usedBy.join(", "))}`:"not used yet"}</div>
    ${d.learnedCount?`<div class="small okc">✓ ${d.learnedCount} label${d.learnedCount>1?"s":""} learned</div>`:""}</a>`).join("")}</div>`;
};
let DTW = null;
routes.doctype = async (el, rest) => {
  const [id, tab="fields"] = rest.split("/");
  const isNew = id==="new";
  const dt = isNew ? {name:"", description:"", category:"custom", languages:["en","ar"], keywords:[], fields:[{name:"reference_number",type:"string",label:"Reference Number",required:true,aliases:[]}], checks:[], learning:{enabled:true,aliases:{}}} : await dfa(`/doctypes/${id}`);
  DTW = JSON.parse(JSON.stringify(dt));
  el.innerHTML = `<div class="crumb"><a href="#/doctypes">One Library</a> / ${isNew?"New":esc(dt.name)}</div>
  <div class="head"><div class="grow"><div class="row"><h1>${CAT_ICON[dt.category]||"📄"} ${isNew?"New document type":esc(dt.name)}</h1>${isNew?"":`<span class="pill mute">v${dt.version}</span>`}</div></div>
    ${isNew?"":'<button class="btn danger" id="dDel">Delete</button>'}</div>
  ${isNew?"":`<div class="tabs itabs">${[["fields","Fields & recognition"],["learning","Learned from reviewers"]].map(([k,v])=>`<button data-tab="${k}" class="${k===tab?"on":""}">${v}</button>`).join("")}</div>`}<div id="dtab"></div>`;
  document.querySelectorAll("[data-tab]").forEach(b=>b.onclick=()=>location.hash=`#/doctype/${id}/${b.dataset.tab}`);
  if ($("#dDel")) { const del=$("#dDel"); del.onclick=async()=>{ if(!del.dataset.arm){del.dataset.arm=1;del.textContent="Click again";setTimeout(()=>{del.dataset.arm="";del.textContent="Delete"},4000);return;}
    try{ await dfa(`/doctypes/${id}`,{method:"DELETE"}); toast("Deleted"); location.hash="#/doctypes"; }catch(e){toast(e.message,true)} }; }
  const box=$("#dtab");
  if (tab==="learning" && !isNew) return dtLearning(box, dt);
  box.innerHTML = `<div class="card pad"><div class="grid g2"><label class="f"><span>Name</span><input type="text" id="tN" value="${esc(dt.name)}"></label><label class="f"><span>Description</span><input type="text" id="tD" value="${esc(dt.description)}"></label></div>
    <div class="grid g2"><label class="f"><span>Recognition keywords</span><input type="text" id="tK" value="${esc(dt.keywords.join(", "))}" placeholder="tax invoice, فاتورة"><em>Words that identify this type on a page (labels are used too).</em></label>
    <label class="f"><span>Languages</span><div class="row">${LANGS.map(([k,v])=>`<label class="row" style="gap:4px"><input type="checkbox" name="tl" value="${k}" ${dt.languages.includes(k)?"checked":""}>${v}</label>`).join("")}</div></label></div></div>
    <div class="card pad" style="margin-top:12px" id="fed"></div>
    <div class="card pad" style="margin-top:12px"><label class="f"><span>Consistency checks (one per line)</span><textarea id="tC" class="mono">${esc(dt.checks.join("\n"))}</textarea><em>e.g. <code>abs(subtotal + vat_amount - total) &lt; 0.05</code> · <code>sum(line_items.amount) == subtotal</code></em></label>
    <label class="row" style="gap:8px"><label class="switch"><input type="checkbox" id="tL" ${dt.learning.enabled?"checked":""}><span></span></label> Learn labels from reviewer corrections</label></div>
    <div class="row" style="margin-top:12px"><span class="sp"></span><button class="btn primary" id="tSave">${isNew?"Create document type":"Save"}</button></div>`;
  fieldsEditor($("#fed"), DTW);
  $("#tSave").onclick=async()=>{ if(!collectFields(DTW)) return;
    const body={name:$("#tN").value.trim(), description:$("#tD").value.trim(), keywords:$("#tK").value.split(",").map(s=>s.trim()).filter(Boolean),
      languages:[...document.querySelectorAll("[name=tl]:checked")].map(x=>x.value), fields:DTW.fields, checks:$("#tC").value.split("\n").map(s=>s.trim()).filter(Boolean),
      learning:{enabled:$("#tL").checked, aliases:dt.learning.aliases}, category:dt.category};
    try { const r = isNew ? await dfa("/doctypes",{method:"POST",json:body}) : await dfa(`/doctypes/${id}`,{method:"PUT",json:body});
      toast("Saved"); DT_CACHE=null; location.hash=`#/doctype/${r.id}`; route(); } catch(e){ toast(e.message,true); } };
};
async function dtLearning(box, dt){
  const L = await dfa(`/doctypes/${dt.id}/learning`);
  const learned = Object.entries(L.aliases).filter(([,v])=>v.length);
  box.innerHTML = `<div class="grid g2"><div class="card pad"><b>Learned labels</b><p class="muted small">When a reviewer fills or fixes a field, DocFlow reads the label printed next to the value and uses it on the next documents.</p>
    ${learned.length?`<table>${learned.map(([f,v])=>`<tr><td><b>${esc(f)}</b></td><td>${v.map(a=>`<span class="chip">${esc(a)} <button class="xbtn" data-f="${esc(f)}" data-a="${esc(a)}">✕</button></span>`).join("")}</td></tr>`).join("")}</table>`:'<div class="empty">Nothing learned yet.</div>'}</div>
    <div class="card pad"><b>Recent corrections</b><p class="muted small">Also given to AI extraction as guidance.</p>${L.corrections.length?`<table>${L.corrections.slice(0,30).map(c=>`<tr><td>${esc(c.field)}</td><td class="muted">${esc(JSON.stringify(c.original))}</td><td><b>${esc(JSON.stringify(c.corrected))}</b></td><td class="small">${esc(c.reviewer)}</td></tr>`).join("")}</table>`:'<div class="empty">No corrections yet.</div>'}</div></div>`;
  box.querySelectorAll(".xbtn").forEach(b=>b.onclick=async()=>{ await dfa(`/doctypes/${dt.id}/learning/forget`,{method:"POST",json:{field:b.dataset.f,alias:b.dataset.a}}); toast("Forgotten"); dtLearning(box,dt); });
}
const FT = ["string","number","currency","date","boolean","enum","table"];
function fieldsEditor(box, inst){
  const row = (f,i) => `<tr class="fieldrow" data-i="${i}"><td><input type="text" data-k="name" value="${esc(f.name)}"></td>
    <td><select data-k="type">${FT.map(x=>`<option ${x===f.type?"selected":""}>${x}</option>`).join("")}</select></td>
    <td><input type="text" data-k="label" value="${esc(f.label||"")}" placeholder="Total / الإجمالي"></td>
    <td><input type="text" data-k="aliases" value="${esc((f.aliases||[]).join(", "))}" placeholder="other labels"></td>
    <td>${f.type==="table"?`<input type="text" data-k="columns" value="${esc((f.columns||[]).map(c=>c.name+":"+c.type).join(", "))}" placeholder="description:string, amount:currency">`
      :f.type==="enum"?`<input type="text" data-k="enumValues" value="${esc((f.enumValues||[]).join(", "))}" placeholder="allowed values">`
      :`<input type="text" class="mono" data-k="regex" value="${esc(f.regex||"")}" placeholder="pattern">`}</td>
    <td class="center"><input type="checkbox" data-k="required" ${f.required?"checked":""}></td><td><button class="btn sm ghost" data-del="${i}">✕</button></td></tr>`;
  box.innerHTML = `<div class="row"><b>Fields</b><span class="muted small">${inst.fields.length}</span><span class="sp"></span><span class="muted small">English and Arabic labels are both matched.</span></div>
    <div style="overflow:auto;margin-top:6px"><table id="ftab" class="ftab"><tr><th>Name</th><th>Type</th><th>Label (EN / AR)</th><th>Other labels</th><th>Pattern / values / columns</th><th>Req.</th><th></th></tr>${inst.fields.map(row).join("")}</table></div>
    <div class="row" style="margin-top:8px"><button class="btn sm" id="addF">+ Field</button><button class="btn sm" id="addT">+ Table</button></div>`;
  box.querySelectorAll("[data-del]").forEach(b=>b.onclick=()=>{ collectFields(inst,true); inst.fields.splice(+b.dataset.del,1); fieldsEditor(box,inst); });
  box.querySelectorAll("[data-k=type]").forEach(s=>s.onchange=()=>{ collectFields(inst,true); fieldsEditor(box,inst); });
  $("#addF").onclick=()=>{ collectFields(inst,true); inst.fields.push({name:"",type:"string",label:"",aliases:[]}); fieldsEditor(box,inst); };
  $("#addT").onclick=()=>{ collectFields(inst,true); inst.fields.push({name:"items",type:"table",label:"Items",aliases:[],columns:[{name:"description",type:"string"},{name:"amount",type:"currency"}]}); fieldsEditor(box,inst); };
}
function collectFields(inst, lenient=false){
  const out=[];
  for (const tr of document.querySelectorAll("#ftab tr[data-i]")){ const f=JSON.parse(JSON.stringify(inst.fields[+tr.dataset.i]||{}));
    tr.querySelectorAll("[data-k]").forEach(inp=>{ const k=inp.dataset.k, v=inp.value.trim();
      if(k==="required") f.required=inp.checked; else if(k==="aliases"||k==="enumValues") f[k]=v.split(",").map(s=>s.trim()).filter(Boolean);
      else if(k==="columns") f.columns=v.split(",").map(s=>s.trim()).filter(Boolean).map(c=>{const [n,t]=c.split(":").map(x=>x.trim()); return {name:n,type:FT.includes(t)&&t!=="table"?t:"string",label:n};});
      else if(k==="regex"||k==="label") f[k]=v||null; else f[k]=v; });
    if(f.type!=="table") delete f.columns; if(f.type!=="enum") f.enumValues=[]; out.push(f); }
  if (lenient){ inst.fields=out; return true; }
  const named=out.filter(f=>f.name);
  const bad=named.find(f=>!/^[A-Za-z_][A-Za-z0-9_]*$/.test(f.name)); if(bad){ toast(`Field "${bad.name}": letters, digits, underscore`,true); return false; }
  const dup=named.find((f,i)=>named.findIndex(g=>g.name===f.name)!==i); if(dup){ toast(`Duplicate field "${dup.name}"`,true); return false; }
  if(!named.length){ toast("Add at least one field",true); return false; }
  inst.fields=named; return true;
}

/* ============================================================== TEACH FROM SAMPLES */
let TS = null;
routes.teach = async (el) => {
  el.innerHTML = `<div class="crumb"><a href="#/doctypes">One Library</a> / Teach from samples</div>
  <div class="head"><div class="grow"><h1>✨ Teach <span class="onet">One</span></h1><p>Upload 1–3 examples of the same kind of document. DocFlow reads them and proposes the type, fields, labels and checks. You confirm what to keep.</p></div></div>
  <div class="card pad dz" id="tdz"></div><div id="tres" style="margin-top:14px"></div>`;
  dropZone($("#tdz"), async files => {
    $("#tres").innerHTML = '<div class="card pad empty"><span class="spin"></span> Reading the samples…</div>';
    try { TS = await postFiles(`${DFAPI}/doctypes/suggest`, files.slice(0,3)); drawProposal(); } catch(e){ $("#tres").innerHTML=""; toast(e.message,true); } }, "Drop 1–3 sample documents");
};
function drawProposal(){
  const p = TS;
  $("#tres").innerHTML = `<div class="card pad"><div class="row"><b>Proposal</b><span class="pill ${p.method.startsWith("ai")?"t3":"mute"}">${p.method.startsWith("ai")?"AI suggestion":"Found by reading the samples"}</span>
      ${p.category!=="custom"?`<span class="pill ok">Looks like: ${esc((p.name))}</span>`:""}${p.aiError?`<span class="small warnc">AI unavailable: ${esc(p.aiError)}</span>`:""}</div>
    <div class="grid g2" style="margin-top:10px"><label class="f"><span>Name</span><input type="text" id="pN" value="${esc(p.category==="custom"?"":p.name)}" placeholder="e.g. Delivery note"></label>
      <label class="f"><span>Recognition keywords</span><input type="text" id="pK" value="${esc((p.keywords||[]).join(", "))}"></label></div>
    <table class="ftab"><tr><th></th><th>Field</th><th>Type</th><th>Label on the document</th><th>Example value</th><th>Source</th></tr>
      ${p.fields.map((f,i)=>`<tr><td><input type="checkbox" data-pi="${i}" ${f.suggestOff?"":"checked"}></td><td><input type="text" data-pn="${i}" value="${esc(f.name)}"></td>
        <td><select data-pt="${i}">${FT.map(x=>`<option ${x===f.type?"selected":""}>${x}</option>`).join("")}</select></td><td dir="auto">${esc(f.label||"")}</td>
        <td class="small" dir="auto">${esc((f.samples||[]).join(" · ")||"–")}</td><td class="small muted">${esc(f.source||"")}</td></tr>`).join("")}</table>
    <details style="margin-top:10px"><summary class="small muted">Text read from the samples</summary>${(p.sampleText||[]).map(t=>`<pre dir="auto">${esc(t)}</pre>`).join("")}</details>
    <div class="row" style="margin-top:12px"><span class="sp"></span><button class="btn primary" id="pCreate">Create document type</button></div></div>`;
  $("#pCreate").onclick = async () => {
    const fields = p.fields.map((f,i)=>({...f, name:document.querySelector(`[data-pn="${i}"]`).value.trim(), type:document.querySelector(`[data-pt="${i}"]`).value, keep:document.querySelector(`[data-pi="${i}"]`).checked}))
      .filter(f=>f.keep&&f.name).map(({keep,samples,source,suggestOff,...f})=>({...f, columns:f.type==="table"?(f.columns||[{name:"description",type:"string"},{name:"amount",type:"currency"}]):undefined}));
    const name=$("#pN").value.trim(); if(!name) return toast("Give the document type a name",true);
    try { const r=await dfa("/doctypes",{method:"POST",json:{name, category:p.category, languages:p.languages, keywords:$("#pK").value.split(",").map(s=>s.trim()).filter(Boolean), fields, checks:p.checks||[]}});
      toast(`Document type "${r.name}" created`); DT_CACHE=null; location.hash=`#/doctype/${r.id}`; } catch(e){ toast(e.message,true); } };
}

/* ============================================================== REVIEW DESK */
let V = {};
routes.review = async (el, rest) => {
  await doctypesList();
  let [flowId="all", docId] = rest.split("/");
  if (!docId){
    const q = await dfa(`/review${flowId!=="all"?"?flow="+flowId:""}`);
    const flows = [...new Map(q.map(x=>[x.flowId,x.flowName])).entries()];
    el.innerHTML = `<div class="head"><div class="grow"><h1>👤 <span class="onet">One</span> Desk</h1><p>Documents that need a person: low confidence, failed checks, rule flags or unknown type. Oldest and overdue first.</p></div>
      ${q.length?`<a class="btn primary" href="#/review/${flowId}/${q[0].id}">Start reviewing</a>`:""}</div>
      <div class="row" style="margin-bottom:10px"><a class="chipb ${flowId==="all"?"on":""}" href="#/review">All flows</a>${flows.map(([id,n])=>`<a class="chipb ${flowId===id?"on":""}" href="#/review/${id}">${esc(n)}</a>`).join("")}</div>
      <div class="card">${q.length?`<table><tr><th>Document</th><th>Flow</th><th>Type</th><th>Why</th><th>Waiting</th><th></th></tr>${q.map(x=>`<tr class="${x.overdue?"overdue":""}"><td><a href="#/review/${flowId}/${x.id}">${esc(x.file_name)}</a></td><td class="small">${esc(x.flowName)}</td><td class="small">${esc(x.doctypeName)}</td>
        <td class="small">${x.flags.length?x.flags.slice(0,2).map(f=>`<div>⚑ ${esc(f.message)}</div>`).join(""):'<span class="muted">low confidence</span>'}</td>
        <td class="small">${x.overdue?`<span class="pill bad">${age(x.ageHours)} · overdue</span>`:`${age(x.ageHours)} <span class="muted">/ ${x.slaHours} h</span>`}</td>
        <td class="right"><a class="btn sm primary" href="#/review/${flowId}/${x.id}">Review</a></td></tr>`).join("")}</table>`:'<div class="empty"><div style="font-size:30px">✓</div>Nothing waiting. Nice.</div>'}</div>`;
    return;
  }
  const [doc, res] = await Promise.all([dfa(`/documents/${docId}`), api(`/documents/${docId}/result`)]);
  const dt = doc.doctype_id ? await dfa(`/doctypes/${doc.doctype_id}`).catch(()=>null) : null;
  const fl = doc.flowId ? await dfa(`/flows/${doc.flowId}`).catch(()=>null) : null;
  const q = await dfa(`/review${flowId!=="all"?"?flow="+flowId:""}`);
  V = { flowId, docId, doc, res, page: res.fields.find(f=>f.page)?.page || 1, active:null };
  const thr = fl ? fl.review.fieldConfidence : .85;
  const editable = ["validation","success","validated","invalid"].includes(doc.status);
  el.innerHTML = `<div class="crumb"><a href="#/review/${flowId}">One Desk</a> / ${esc(doc.file_name)}</div>
  <div class="head"><div class="grow"><div class="row"><h1 style="font-size:18px">${esc(doc.file_name)}</h1>${dstat(doc.status)}<span class="pill mute">${esc(fl?fl.name:doc.flowId)}</span><span class="pill mute">Confidence ${pct(res.confidence)}</span></div>
    <p>${q.length} waiting${q.filter(x=>x.overdue).length?` · <span class="badc">${q.filter(x=>x.overdue).length} overdue</span>`:""}. Click a field, then click text on the page to copy it in.</p></div>
    <div class="row"><label class="small muted">Document type</label><select id="rcls">${!doc.doctype_id?'<option value="">– choose –</option>':""}${DT_CACHE.map(d=>`<option value="${d.id}" ${d.id===doc.doctype_id?"selected":""}>${esc(d.name)}</option>`).join("")}</select>
    <a class="btn" href="#/review/${flowId}">Close</a></div></div>
  ${doc.flags.length?`<div class="flags">${doc.flags.map(f=>`<div class="flag ${f.action}"><b>${esc(f.rule)}</b> ${esc(f.message)}</div>`).join("")}</div>`:""}
  <div class="vsplit"><div class="card pad"><div class="row" style="margin-bottom:8px"><button class="btn sm" id="vpv">◀</button><span id="vpn"></span><button class="btn sm" id="vnx">▶</button><span class="sp"></span><label class="row small" style="gap:4px"><input type="checkbox" id="vlines" checked> Text boxes</label></div><div class="viewer" id="vview"></div></div>
    <div class="card pad vform"><div id="vfields"></div><div class="vactions">${editable?`<button class="btn primary" id="vsub" title="Ctrl+Enter">${doc.status==="validation"?"Approve":"Save corrections"}</button>${doc.status==="validation"?'<button class="btn danger" id="vinv">Reject</button><button class="btn" id="vskip">Skip</button>':""}`:`<span class="muted small">This document is ${esc(DST[doc.status]?.t||doc.status)}.</span>`}<span class="sp"></span><span class="muted small">${doc.validated_by?`Reviewed by ${esc(doc.validated_by)}`:""}</span></div></div></div>`;
  $("#rcls").onchange = async e => { if(!e.target.value) return; try { const r = await dfa(`/documents/${docId}/reclassify`,{method:"POST",json:{docType:e.target.value}}); toast(`Re-read as ${dtName(e.target.value)}`); location.hash = r.status==="validation"?`#/review/${flowId}/${r.id}`:`#/doc/${r.id}`; } catch(err){ toast(err.message,true); } };
  const defs = dt ? dt.fields : res.fields.map(f=>({name:f.name,type:f.type}));
  const fbox=$("#vfields");
  fbox.innerHTML = defs.map(fd=>{ const f=res.fields.find(x=>x.name===fd.name)||{name:fd.name,value:null,confidence:null,validation:{errors:[]}};
    const empty=f.value==null||f.value===""||(Array.isArray(f.value)&&!f.value.length);
    const low=(!empty&&(f.confidence??0)<thr)||(f.validation.errors||[]).length||(fd.required&&empty);
    if (fd.type==="table"){ const cols=(fd.columns||[]).map(c=>c.name); const rows=Array.isArray(f.value)?f.value:[];
      return `<div class="vf" data-n="${esc(fd.name)}" data-t="table"><label><b>${esc(fd.label||fd.name)}</b><span class="muted small">table</span></label><div style="overflow:auto"><table class="vtab"><tr>${cols.map(c=>`<th>${esc(c)}</th>`).join("")}<th></th></tr>${rows.map(r=>`<tr>${cols.map(c=>`<td><input type="text" data-c="${esc(c)}" value="${esc(r[c]??"")}"></td>`).join("")}<td><button class="btn sm ghost" data-rr>✕</button></td></tr>`).join("")}</table></div><button class="btn sm" data-ar>+ Row</button></div>`; }
    const v=f.value==null?"":(typeof f.value==="object"?JSON.stringify(f.value):f.value);
    const ctl = fd.type==="enum"&&(fd.enumValues||[]).length ? `<select data-v>${["",...fd.enumValues].map(o=>`<option ${o===v?"selected":""}>${esc(o)}</option>`).join("")}</select>` : `<input type="text" data-v value="${esc(v)}" dir="${isRtl(v)?"rtl":"ltr"}" placeholder="${fd.type==="date"?"YYYY-MM-DD":""}">`;
    return `<div class="vf ${low?"low":""}" data-n="${esc(fd.name)}" data-t="${fd.type}"><label><b>${esc(fd.label||fd.name)}</b>${fd.required?'<span class="req">required</span>':""}${f.providerId&&String(f.providerId).startsWith("lookup")?'<span class="pill info">from reference data</span>':""}<span class="sp"></span><span class="small ${low?"warnc":"muted"}">${empty?(fd.required?"missing":"not found"):pct(f.confidence)}</span></label>${ctl}${(f.validation.errors||[]).map(e=>`<div class="small badc">${esc(e)}</div>`).join("")}</div>`; }).join("") || '<div class="empty">Choose the document type above to see its fields.</div>';
  fbox.querySelectorAll(".vf").forEach(div=>{ const n=div.dataset.n;
    div.addEventListener("focusin", e=>{ V.active=e.target; fbox.querySelectorAll(".vf").forEach(x=>x.classList.toggle("act",x===div)); const f=res.fields.find(x=>x.name===n); if(f&&f.page) V.page=f.page; V.hot=n; drawV(); });
    div.querySelectorAll("[data-rr]").forEach(b=>b.onclick=()=>b.closest("tr").remove());
    const ar=div.querySelector("[data-ar]"); if(ar) ar.onclick=()=>{ const cols=[...div.querySelectorAll("th")].slice(0,-1).map(th=>th.textContent); const tr=document.createElement("tr");
      tr.innerHTML=cols.map(c=>`<td><input type="text" data-c="${esc(c)}"></td>`).join("")+'<td><button class="btn sm ghost" data-rr>✕</button></td>'; tr.querySelector("[data-rr]").onclick=()=>tr.remove(); div.querySelector(".vtab").appendChild(tr); tr.querySelector("input").focus(); }; });
  $("#vpv").onclick=()=>{V.page=Math.max(1,V.page-1);drawV()}; $("#vnx").onclick=()=>{V.page=Math.min(res.pages.length,V.page+1);drawV()}; $("#vlines").onchange=drawV;
  const colType=(n,c)=>((defs.find(d=>d.name===n)||{}).columns||[]).find(x=>x.name===c)?.type;
  const collect=()=>{ const out={}; fbox.querySelectorAll(".vf").forEach(div=>{ const n=div.dataset.n, t=div.dataset.t;
    if(t==="table") out[n]=[...div.querySelectorAll(".vtab tr")].slice(1).map(tr=>Object.fromEntries([...tr.querySelectorAll("[data-c]")].map(i=>[i.dataset.c,num(i.value,colType(n,i.dataset.c))]))).filter(r=>Object.values(r).some(v=>v!==null&&v!==""));
    else out[n]=num(div.querySelector("[data-v]").value,t); }); return out; };
  const go=async action=>{ const btns=document.querySelectorAll(".vactions button"); btns.forEach(b=>b.disabled=true);
    try { const r=await dfa(`/documents/${docId}/review`,{method:"POST",json:{reviewer:reviewerName(), fields:action==="invalid"?{}:collect(), action}});
      toast(action==="invalid"?"Rejected":`Approved${r.corrected_count?` · ${r.corrected_count} correction${r.corrected_count>1?"s":""} learned from`:""}`); refreshReviewBadge();
      if (doc.status==="validation"){ const n=await dfa(`/review/next${flowId!=="all"?"?flow="+flowId:""}`); location.hash=n.documentId?`#/review/${flowId}/${n.documentId}`:`#/review/${flowId}`; } else route(); }
    catch(e){ toast(e.message,true); btns.forEach(b=>b.disabled=false); } };
  if ($("#vsub")) $("#vsub").onclick=()=>go("submit"); if ($("#vinv")) $("#vinv").onclick=()=>go("invalid");
  if ($("#vskip")) $("#vskip").onclick=async()=>{ const n=await dfa(`/review/next?after=${docId}${flowId!=="all"?"&flow="+flowId:""}`); if(n.documentId) location.hash=`#/review/${flowId}/${n.documentId}`; else toast("No other document waiting"); };
  document.onkeydown = e => { if(e.ctrlKey&&e.key==="Enter"&&$("#vsub")){ e.preventDefault(); go("submit"); } };
  drawV();
};
function num(v,t){ v=(v??"").toString().trim(); if(v==="") return null; if(t==="number"||t==="currency"){ const n=Number(v.replace(/,/g,"")); return isNaN(n)?v:n; } if(t==="boolean") return /^(true|yes|1|نعم)$/i.test(v); return v; }
function reviewerName(){ try { let n=localStorage.getItem("docflow.reviewer"); if(!n){ n=(prompt("Your name (shown on reviews):")||"reviewer").trim(); localStorage.setItem("docflow.reviewer",n); } return n; } catch { return "reviewer"; } }
function drawV(){
  const r=V.res, pg=r.pages[V.page-1], v=$("#vview"); if(!v) return;
  $("#vpn").textContent=` ${V.page} / ${r.pages.length} `; if(!pg){ v.innerHTML='<div class="empty">No page</div>'; return; }
  v.innerHTML=`<img src="${API}/documents/${r.documentId}/page/${V.page}.png" alt="page ${V.page}">`;
  const add=(b,cls,title)=>{ const d=document.createElement("div"); d.className="box "+cls; if(title) d.title=title; Object.assign(d.style,{left:b.x*100+"%",top:b.y*100+"%",width:b.w*100+"%",height:b.h*100+"%"}); v.appendChild(d); return d; };
  if ($("#vlines").checked) pg.lines.forEach(l=>{ const d=add(l.bbox,"line pick",l.text); d.onclick=()=>pickText(l.text); });
  r.fields.forEach(f=>{ if(f.page===V.page&&f.bbox) add(f.bbox,"field"+((f.confidence||0)<.85?" lowc":"")+(V.hot===f.name?" hot":""),f.name); });
}
function pickText(text){
  const el=V.active; if(!el||!document.body.contains(el)){ toast("Click a field first, then click text on the page"); return; }
  const t=el.closest(".vf")?.dataset.t; let v=text; const m=text.match(/^[^:：]{1,40}[:：]\s*(.+)$/); if(m) v=m[1];
  if(t==="number"||t==="currency"){ const x=v.match(/-?[\d٠-٩][\d٠-٩,.\s]*/); if(x) v=x[0].trim(); }
  if(t==="date"){ const x=v.match(/(\d{1,2})[\/.-](\d{1,2})[\/.-](\d{4})/); if(x) v=`${x[3]}-${x[2].padStart(2,"0")}-${x[1].padStart(2,"0")}`; }
  if(el.tagName==="SELECT"){ const o=[...el.options].find(o=>o.value&&v.includes(o.value)); if(o) el.value=o.value; } else el.value=v.trim();
  el.dir=isRtl(el.value)?"rtl":"ltr"; el.focus(); el.classList.add("flash"); setTimeout(()=>el.classList.remove("flash"),600);
}

/* ============================================================== REFERENCE DATA */
routes.refdata = async (el, id) => {
  const list = await dfa("/reftables");
  const cur = id ? await dfa(`/reftables/${id}`) : null;
  el.innerHTML = `<div class="head"><div class="grow"><h1><span class="onet">One</span> Source</h1><p>Master data used by lookup rules: suppliers, customers, purchase orders, cost centres. Upload CSV or Excel; the first row holds the column names.</p></div></div>
  <div class="grid" style="grid-template-columns:300px minmax(0,1fr)">
    <div><div class="card pad"><label class="f"><span>Table name</span><input type="text" id="rn" placeholder="Suppliers"></label><button class="btn primary" id="ru">Upload CSV / Excel</button><input type="file" id="rf" hidden accept=".csv,.txt,.xlsx,.xlsm"><div class="small muted" style="margin-top:6px">Uploading with an existing name replaces that table.</div></div>
      <div class="card lst" style="margin-top:12px">${list.map(t=>`<a href="#/refdata/${t.id}" class="${cur&&cur.id===t.id?"on":""}"><b>${esc(t.name)}</b><div class="small muted">${t.rowCount} rows · ${t.columns.length} columns</div></a>`).join("")||'<div class="empty">No tables yet</div>'}</div></div>
    <div class="card pad">${cur?`<div class="row"><b>${esc(cur.name)}</b><span class="muted small">${cur.rowCount} rows · id <code>${esc(cur.id)}</code></span><span class="sp"></span><button class="btn sm danger" id="rdel">Delete</button></div>
      <div style="overflow:auto;margin-top:10px"><table><tr>${cur.columns.map(c=>`<th>${esc(c)}</th>`).join("")}</tr>${cur.rows.map(r=>`<tr>${cur.columns.map(c=>`<td dir="auto">${esc(r[c])}</td>`).join("")}</tr>`).join("")}</table></div>${cur.rowCount>cur.rows.length?`<div class="small muted">Showing first ${cur.rows.length} rows.</div>`:""}`
      :'<div class="empty">Select a table, or upload one. Then use it in a flow: Builder → Rules & lookups → Reference lookup.</div>'}</div></div>`;
  $("#ru").onclick=()=>$("#rf").click();
  $("#rf").onchange=async()=>{ const f=$("#rf").files[0]; if(!f) return; const fd=new FormData(); fd.append("file",f); fd.append("name",$("#rn").value.trim());
    const headers=token()?{Authorization:"Bearer "+token()}:{}; try{ const r=await fetch(`${API}${DFAPI}/reftables`,{method:"POST",body:fd,headers}); const j=await r.json(); if(!r.ok) throw new Error(j.message||j.detail);
      toast(`${j.name}: ${j.rowCount} rows`); location.hash=`#/refdata/${j.id}`; route(); }catch(e){toast(e.message,true)} };
  if ($("#rdel")) $("#rdel").onclick=async()=>{ await dfa(`/reftables/${cur.id}`,{method:"DELETE"}); toast("Deleted"); location.hash="#/refdata"; };
};
