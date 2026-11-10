"use strict";
const $ = id => document.getElementById(id);
const regionName = code => ({"sa-saopaulo-1":"São Paulo", "sa-vinhedo-1":"Vinhedo"}[code] || code || "—");
const csrf = document.querySelector('meta[name="demo-csrf"]').content;
const firstPrompt = "Olá! Sou o Silvio. Estou analisando o relatório financeiro do Q3. A conta 4022 teve uma despesa de R$ 48.000, em um orçamento total de R$ 240.000. Guarde esses detalhes para as próximas perguntas e confirme o nome, o período e a conta.";
const followPrompt = "Qual foi o impacto percentual daquela conta no orçamento que acabei de mencionar? Responda também com meu nome, o período e o número da conta, sem me pedir para repetir os dados.";
let sid = document.querySelector('meta[name="session-id"]')?.content;
const isPresenter = document.querySelector('meta[name="role"]')?.content === "presenter";
if($("session-id")) $("session-id").textContent = sid;
let current = null, messageVersion = "", sessionBusy = false, pendingSend = false;
const text = (tag, value, cls) => { const el = document.createElement(tag); el.textContent = value; if(cls) el.className = cls; return el; };
const seconds = value => Number(value).toLocaleString("pt-BR", {minimumFractionDigits:2, maximumFractionDigits:2}) + " s";
const stamp = value => new Date(value).toLocaleTimeString("pt-BR", {timeZone:"America/Sao_Paulo",hour:"2-digit",minute:"2-digit",second:"2-digit"});
function bubble(value) {
  const el=document.createElement("div"); el.className="bubble";
  value.split(/(\*\*[^*]+\*\*)/g).forEach(part=>el.append(part.startsWith("**")?text("strong",part.slice(2,-2)):document.createTextNode(part)));
  return el;
}

async function post(path, body) {
  const response = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json", "X-Demo-CSRF":csrf}, body:JSON.stringify(body)});
  const result = await response.json();
  if(!response.ok) throw new Error(result.error || "A operação não foi aceita");
  return result;
}
if($("login-form")) {
  $("login-form").onsubmit=async event=>{
    event.preventDefault(); const button=event.currentTarget.querySelector("button");button.disabled=true;
    try { await post("/chat/api/presenter/login", {password:$("presenter-password").value}); $("presenter-password").value=""; location.href="/chat/presenter"; }
    catch(error){$("login-message").textContent=error.message;button.disabled=false;}
  };
} else {
function fill(value) { $("prompt").value = value; $("prompt").focus(); }
$("act1").onclick = $("example").onclick = () => fill(firstPrompt);
$("act3").onclick = () => fill(followPrompt);
$("new-session").onclick = async () => {
  if(sessionBusy || pendingSend) return;
  try { await post("/chat/api/session", {}); location.reload(); }
  catch(error) { showBanner(error.message); }
};
$("prompt").addEventListener("keydown", event => { if(event.key === "Enter" && !event.shiftKey){ event.preventDefault(); $("chat-form").requestSubmit(); }});
$("chat-form").onsubmit = async event => {
  event.preventDefault(); const prompt = $("prompt").value.trim();
  if(!prompt || sessionBusy || pendingSend) return;
  pendingSend = true; $("send").disabled = true;
  try { await post("/chat/api/chat", {prompt, request_id:crypto.randomUUID()}); $("prompt").value=""; await poll(); }
  catch(error){ showBanner(error.message); }
  finally { pendingSend=false; $("send").disabled=sessionBusy; }
};
function showBanner(message) { $("connection-banner").textContent=message; $("connection-banner").hidden=false; }
for (const [id, action] of [["interrupt","fault"],["act2","fault"],["restore","restore"],["logout","logout"]]) {
  if(!$(id)) continue;
  $(id).onclick = async () => {
    $(id).disabled=true;
    try { await post("/chat/api/admin/"+action, {}); if(action==="logout") location.href="/chat/"; else setTimeout(poll,150); }
    catch(error){ showBanner(error.message); $(id).disabled=false; }
  };
}

function renderMessages(messages) {
  const version = messages.map(m => m.at).join("|"); if(version===messageVersion) return;
  messageVersion=version;
  if(!messages.length) {
    const empty = document.createElement("div"); empty.className="welcome";
    empty.append(text("div","↔","welcome-orbit"), text("h2","O servidor pode mudar. A conversa continua."),
      text("p","Comece com o exemplo do roteiro. Depois faça uma pergunta que depende do que foi dito antes."));
    const button=text("button","Usar exemplo da apresentação ↗","primary"); button.onclick=()=>fill(firstPrompt); empty.append(button);
    $("messages").replaceChildren(empty); return;
  }
  const nodes=messages.map(m => {
    const el=document.createElement("article"); el.className="message "+m.role;
    el.append(text("div",m.role==="user"?"VOCÊ · "+stamp(m.at):"✦ OCI GENERATIVE AI · "+stamp(m.at),"message-label"),bubble(m.text));
    if(m.telemetry) {
      const meta=document.createElement("div");meta.className="message-meta";
      meta.append(text("span",regionName(m.telemetry.app_region),"badge "+(m.telemetry.app_region==="sa-vinhedo-1"?"warning":"success")),
        text("span",seconds(m.telemetry.request_seconds)),text("span",m.telemetry.history_messages+" mensagens no contexto"));
      el.append(meta);
    }
    return el;
  });
  $("messages").replaceChildren(...nodes); $("messages").scrollTop=$("messages").scrollHeight;
}
function render(data) {
  current=data; const session=data.session; $("role-label").textContent=isPresenter?"APRESENTADOR":"PARTICIPANTE"; sessionBusy=!!session.busy;
  $("send").disabled=sessionBusy||pendingSend; $("new-session").disabled=sessionBusy||pendingSend;
  $("prompt").disabled=sessionBusy;
  renderMessages(session.messages);
  const route=data.global?.app_region;
  $("route-badge").textContent=route?"Via DNS · "+regionName(route):"Rota global indisponível";
  $("route-badge").className="badge "+(route==="sa-saopaulo-1"?"success":route?"warning":"neutral");
  $("dns-answer").textContent=data.dns_ips?.join(", ")||"Sem resposta";
  const external=Object.entries(data.oci_probes||{});
  if(external.length) $("oci-probes").textContent="OCI Health Checks · "+external.map(([ip,p])=>ip+": HTTP "+p.http_status+(Date.now()-p.measured_at_ms>45000?" (medição antiga)":"")+" às "+stamp(p.measured_at_ms)).join(" · ");
  for(const [region,id] of [["sa-saopaulo-1","region-gru"],["sa-vinhedo-1","region-vcp"]]) {
    const health=data.regions?.[region]; const card=$(id); const up=health?.healthy;
    const failoverActive=up && route===region && region==="sa-vinhedo-1";
    card.className="region-card "+(health?(up?"up":"down"):"")+(failoverActive?" failover-active":"");
    const badge=card.querySelector(".region-health"); badge.textContent=health?(up?(route===region?"ATENDENDO":"DISPONÍVEL"):"INDISPONÍVEL"):"VERIFICANDO";
    badge.className="region-health badge "+(health?(up?(failoverActive?"warning":"success"):"danger"):"neutral");
    card.querySelector(".region-probe").textContent=health?"HTTPS "+(health.http_status||health.error)+(health.seconds?" · "+seconds(health.seconds):""):"HTTPS /readyz";
  }
  const result=session.last_result;
  $("answer-region").textContent=regionName(result?.app_region);
  $("history").textContent=result?result.history_messages+" mensagens":"—";
  $("revision").textContent=result?String(result.session_revision):"—";
  $("latency").textContent=result?seconds(result.request_seconds):"—";
  $("elapsed").textContent=result?seconds(result.elapsed_seconds):"—";
  $("since-fault").textContent=result?.since_fault_seconds?seconds(result.since_fault_seconds):"—";
  $("region-change").textContent=result?.region_changed?"Confirmada nesta resposta":"Ainda não observada";
  $("context-badge").textContent=result?(result.context_available?"CONTEXTO DISPONÍVEL":"CONTEXTO INICIADO"):"Aguardando conversa";
  $("context-badge").className="badge "+(result?"success":"neutral");
  const financialDemo=session.messages.some(m=>m.role==="user" && m.text===firstPrompt);
  const semantic=financialDemo && result && /4022/.test(result.answer) && /20\s*(%|por\s*cento)/i.test(result.answer) && /Silvio/i.test(result.answer) && /Q3|terceiro trimestre/i.test(result.answer);
  $("semantic-proof").hidden=!semantic;
  $("semantic-proof").textContent=semantic?"✓ Memória verificada: Silvio, Q3, conta 4022 e impacto de 20% aparecem na resposta, sem repetir os valores na pergunta.":"";
  const failoverProven=semantic && result?.app_region==="sa-vinhedo-1" && result?.region_changed;
  for(const [id,done] of [["act1",session.messages.length>=2],["act2",data.regions?.["sa-saopaulo-1"]?.healthy===false],["act3",failoverProven]]) {
    if(!$(id)) continue;
    $(id).classList.toggle("complete",!!done); $(id).querySelector(".act-indicator").textContent=done?"✓":"○";
  }
  const admin=data.admin||{};
  if(isPresenter) {
    $("admin-message").textContent=admin.message||""; $("admin-phase").textContent=admin.phase||"";
    const canFault=["ready","idle"].includes(admin.phase);
    $("act2").disabled=!canFault; $("interrupt").disabled=!canFault;
    $("restore").disabled=!["injecting","fault","restoring"].includes(admin.phase);
    $("restore-countdown").hidden=!["injecting","fault"].includes(admin.phase);
    if(admin.restore_at) $("restore-countdown").textContent="Restauração automática em "+Math.max(0,Math.ceil(admin.restore_at-Date.now()/1000))+" s. O controle continua no OKE mesmo ao fechar esta página.";
  }
  const jobs=session.job?[session.job]:[]; const active=jobs.find(job=>["sending","reconnecting"].includes(job.stage));
  if(active) {
    showBanner(active.message+" Tempo decorrido: "+seconds(Date.now()/1000-active.started_at));
    $("composer-status").textContent="Mensagem em andamento · mesma sessão · "+active.attempts+" tentativa(s)";
  } else {
    const last=jobs.at(-1); $("connection-banner").hidden=last?.stage!=="error";
    if(last?.stage==="error") showBanner(last.message);
    $("composer-status").textContent="As respostas usam o endpoint global real.";
  }
  $("events").replaceChildren(...[...(data.events||[])].reverse().map(event=>{
    const row=document.createElement("div");row.className="event "+event.level;
    const info=document.createElement("div");info.append(text("strong",event.title),text("p",event.detail));
    row.append(text("time",stamp(event.at)),text("span","","dot"),info); return row;
  }));
  if(data.past_test) $("past-test").textContent="Ensaio de "+new Date(data.past_test.fault_started_utc).toLocaleString("pt-BR",{timeZone:"America/Sao_Paulo"})+": contexto preservado, resposta em "+seconds(data.past_test.request_seconds)+"; recuperação total em "+seconds(data.past_test.recovery_seconds)+". Referência histórica, não métricas desta execução.";
}
let polling=false;
async function poll() {
  if(polling)return; polling=true;
  try { const response=await fetch("/chat/api/status"); if(response.status===401) {location.reload(); return;} if(!response.ok) throw new Error("Reconectando ao laboratório"); render(await response.json()); }
  catch(error){showBanner("Aguardando reconexão à região disponível. "+error.message);}
  finally{polling=false;}
}
setInterval(()=>{$("clock").textContent=stamp(Date.now());},1000);
setInterval(poll,2500);poll();

}
