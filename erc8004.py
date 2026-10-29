"""ERC-8004 (Trustless Agents) identity for MintScope.

* `registration(...)` builds the agent registration file served at /.well-known/agent-registration.json
  (also /agent-registration.json). Its URL is the agentURI passed to `register(string)`.
* `page(...)` is a static page where the owner registers the agent in the Identity Registry on Base with
  their own injected wallet (same safety model as /register-onchain: no keys, exact calldata shown,
  Base chain check, expected-owner check, duplicate warnings).

Identity Registry (Base mainnet, from github.com/erc-8004/erc-8004-contracts):
0x8004A169FB4a3325136EB29fA0ceB6D2e539a432, `register(string agentURI) returns (uint256 agentId)`,
event `Registered(uint256 indexed agentId, string agentURI, address indexed owner)`.
"""
from __future__ import annotations

import html
import json
import os
import re

CHAIN_ID = 8453
IDENTITY_REGISTRY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
OWNER = "0x93714D5970f511Ac22f0E5635D3fBF538f717cd4"
REGISTER_SELECTOR = "f2c298be"  # register(string)
REGISTRATION_TYPE = "https://eips.ethereum.org/EIPS/eip-8004#registration-v1"
WELL_KNOWN = "/.well-known/agent-registration.json"
ENV_AGENT_ID = "MINTSCOPE_ERC8004_AGENT_ID"
MCP_PROTOCOL = "2025-06-18"


def agent_id(env=None) -> str | None:
    value = ((env if env is not None else os.environ).get(ENV_AGENT_ID) or "").strip()
    return value if re.fullmatch(r"[0-9]{1,30}", value) else None


def encode_register(agent_uri: str) -> str:
    """ABI-encode register(string agentURI)."""
    uri = agent_uri.encode()
    padded = uri.hex() + "00" * ((32 - len(uri) % 32) % 32)
    return "0x" + REGISTER_SELECTOR + f"{0x20:064x}" + f"{len(uri):064x}" + padded


def registration(base: str, env=None) -> dict:
    aid = agent_id(env)
    return {
        "type": REGISTRATION_TYPE,
        "name": "MintScope",
        "description": "Evidence-based NFT collection analysis and new-launch discovery on Ethereum and Base: "
                       "verified OpenSea market data, realized sales versus listings, creator evidence, risk "
                       "flags and confidence. Pay per call with x402 (USDC on Base). Not a profit prediction.",
        "image": f"{base}/static/icon.png",
        "services": [
            {"name": "web", "endpoint": f"{base}/"},
            {"name": "MCP", "endpoint": f"{base}/mcp", "version": MCP_PROTOCOL},
            {"name": "x402", "endpoint": f"{base}/analyze"},
            {"name": "x402", "endpoint": f"{base}/discover"},
            {"name": "OpenAPI", "endpoint": f"{base}/openapi.json"},
            {"name": "ERC-8257", "endpoint": f"{base}/.well-known/ai-tool/mintscope-nft-analysis.json"},
            {"name": "ERC-8257", "endpoint": f"{base}/.well-known/ai-tool/mintscope-new-collections.json"},
        ],
        "x402Support": True,
        "active": True,
        "registrations": ([{"agentId": int(aid), "agentRegistry": f"eip155:{CHAIN_ID}:{IDENTITY_REGISTRY}"}]
                          if aid else []),
        "supportedTrust": ["reputation"],
    }


_PAGE = r"""<!doctype html>
<html lang="fa">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>__NAME__ · Register in the ERC-8004 Identity Registry (Base)</title>
<style>
 body{font-family:system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif;background:#0f1115;color:#e8e8ea;margin:0;padding:24px;line-height:1.6}
 main{max-width:760px;margin:0 auto}
 h1{font-size:1.4rem;margin:0 0 4px} .en{color:#9aa0aa;font-size:.92em;direction:ltr;text-align:left}
 .fa{direction:rtl;text-align:right}
 .card{background:#181b22;border:1px solid #2a2f3a;border-radius:12px;padding:16px;margin:14px 0}
 button{background:#2f6fed;color:#fff;border:0;border-radius:8px;padding:10px 16px;font-size:1rem;cursor:pointer;margin:6px 0}
 button:disabled{background:#3a3f4a;color:#8a8f99;cursor:not-allowed}
 code,.mono{font-family:ui-monospace,Menlo,monospace;font-size:.85em;word-break:break-all;direction:ltr}
 .warn{color:#ffb84d} .ok{color:#4ddc8a} .err{color:#ff6b6b}
 a{color:#7fb2ff} small{color:#9aa0aa}
</style>
</head>
<body><main>
<h1 class="fa">ثبت __NAME__ در رجیستری هویت ERC-8004 روی Base</h1>
<div class="en">Register __NAME__ as an agent in the ERC-8004 Identity Registry on Base mainnet</div>

<div class="card">
 <div class="fa">کیف‌پول خودت را وصل کن و خودت امضا کن؛ این سایت هیچ کلیدی نمی‌بیند. یک تراکنش، گس حدود ۱۳۵ هزار (کمتر از یک سنت روی Base). این کار یک NFT هویت (AGENT) به کیف‌پولت می‌دهد. فقط یک بار ثبت کن.</div>
 <div class="en">Connect your own wallet; you sign yourself and this site never sees a key. One transaction (~135k gas, under one cent on Base). It mints an agent identity NFT (AGENT) to your wallet. Register only once.</div>
 <button id="connect">اتصال کیف‌پول · Connect wallet</button>
 <button id="switch" disabled>رفتن به شبکه‌ی Base · Switch to Base</button>
 <div id="status" class="mono">—</div>
 <div id="acctWarn" class="warn"></div>
</div>

<div class="card">
 <div class="fa"><b>تراکنش دقیق</b></div><div class="en"><b>Exact transaction</b></div>
 <div class="mono">chain: Base (8453)<br>to: <a target="_blank" rel="noopener" href="https://basescan.org/address/__REGISTRY__">__REGISTRY__</a> (IdentityRegistry, proxy)<br>
 function: register(string agentURI) · selector 0xf2c298be<br>
 agentURI: <a target="_blank" rel="noopener" href="__AGENT_URI__">__AGENT_URI__</a><br>value: 0<br>data: <span id="calldata">__CALLDATA__</span></div>
 <div id="dup" class="warn"></div>
 <button id="register" disabled>ثبت · Register agent</button>
 <div id="est" class="en"><small></small></div>
 <div id="res" class="mono"></div>
</div>

<div class="card">
 <div class="fa"><small>بعد از تأیید، شماره‌ی عامل (agentId) این‌جا نشان داده می‌شود. آن را در Render به متغیر <code>__ENV__</code> بده تا فایل ثبت، بخش registrations را نشان دهد.</small></div>
 <div class="en"><small>After confirmation the agentId appears here. Set it on Render as <code>__ENV__</code> so the registration file lists it under <code>registrations</code>.</small></div>
</div>
</main>
<script>
"use strict";
const CHAIN_HEX = "0x2105";
const REGISTRY = "__REGISTRY__";
const OWNER = "__OWNER__";
const AGENT_URI = "__AGENT_URI__";
const CALLDATA = "__CALLDATA__";
const TOPIC = "0xca52e62c367d81bb2e328eb795f7c7ba24afb478408a26c0e201d155c449bc4a";
const DEPLOY_BLOCK = "0x27bbd27";
const KNOWN_ID = "__AGENT_ID__";
const LS = "erc8004-reg-" + AGENT_URI;
let account = null, chainOk = false, dupBlock = false, dupFound = false;
const $ = (id) => document.getElementById(id);
const eth = window.ethereum;
function setStatus(html){ $("status").innerHTML = html; }
function fmtEth(wei){ return (Number(wei) / 1e18).toFixed(9); }
function txLink(h){ return '<a target="_blank" rel="noopener" href="https://basescan.org/tx/' + h + '">' + h + '</a>'; }
function nftLink(id){ return '<a target="_blank" rel="noopener" href="https://basescan.org/nft/' + REGISTRY + '/' + id + '">#' + id + '</a>'; }
function esc(s){ return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
function decodeUri(data){
  try { const h = data.slice(2); const len = parseInt(h.slice(64, 128), 16); const hex = h.slice(128, 128 + len * 2);
    const bytes = new Uint8Array(hex.match(/../g).map(b => parseInt(b, 16))); return new TextDecoder().decode(bytes);
  } catch (e) { return "?"; }
}

async function checkDuplicates(){
  const out = [];
  dupBlock = false; dupFound = false;
  if (KNOWN_ID) { out.push("این سرویس قبلاً با agentId " + nftLink(KNOWN_ID) + " ثبت شده · Already registered as agent " + nftLink(KNOWN_ID) + " (server config). Registering again creates a duplicate."); dupBlock = true; }
  const prev = localStorage.getItem(LS);
  if (prev) { out.push("قبلاً از این مرورگر ارسال شده · Already sent from this browser: " + txLink(prev)); dupFound = true; }
  try {
    const data = "0x70a08231" + account.slice(2).toLowerCase().padStart(64, "0");
    const bal = BigInt(await eth.request({ method: "eth_call", params: [{ to: REGISTRY, data }, "latest"] }));
    if (bal > 0n) { out.push("این کیف‌پول همین حالا " + bal + " عامل ERC-8004 دارد · This wallet already owns " + bal + " ERC-8004 agent(s)."); dupFound = true; }
  } catch (e) { out.push("بررسی موجودی ممکن نشد · balanceOf check failed: " + (e && e.message ? e.message : e)); }
  try {
    const logs = await eth.request({ method: "eth_getLogs", params: [{ address: REGISTRY, fromBlock: DEPLOY_BLOCK, toBlock: "latest",
      topics: [TOPIC, null, "0x" + account.slice(2).toLowerCase().padStart(64, "0")] }] });
    for (const l of logs || []) {
      const uri = decodeUri(l.data), id = BigInt(l.topics[1]).toString();
      const same = uri === AGENT_URI;
      out.push((same ? '<span class="err">همین آدرس قبلاً ثبت شده · Same agentURI already registered</span> ' : "") + "agent " + nftLink(id) + " → " + esc(uri));
      if (same) dupBlock = true; else dupFound = true;
    }
    if (!logs || !logs.length) out.push('<span class="ok">ثبت قبلی از این کیف‌پول پیدا نشد · No earlier registration by this wallet found.</span>');
  } catch (e) { out.push("جست‌وجوی رویدادهای قبلی ممکن نشد (RPC کیف‌پول محدودیت دارد) · Could not scan earlier Registered events: " + (e && e.message ? e.message : e)); }
  $("dup").innerHTML = out.join("<br>");
}

async function refresh(){
  if (!eth) return;
  const accts = await eth.request({ method: "eth_accounts" });
  account = accts[0] || null;
  const chainId = await eth.request({ method: "eth_chainId" });
  chainOk = String(chainId).toLowerCase() === CHAIN_HEX;
  $("switch").disabled = !account || chainOk;
  let s = account ? "حساب · Account: " + account : "وصل نیست · Not connected";
  s += "<br>شبکه · Network: " + (chainOk ? '<span class="ok">Base ✓</span>' : '<span class="warn">' + chainId + ' (not Base)</span>');
  if (account && chainOk) {
    try { const bal = await eth.request({ method: "eth_getBalance", params: [account, "latest"] });
      s += "<br>موجودی · Balance: " + fmtEth(BigInt(bal)) + " ETH";
      if (BigInt(bal) === 0n) s += ' <span class="err">— برای گس کمی ETH روی Base لازم است · you need a little ETH on Base for gas</span>';
    } catch (e) {}
  }
  setStatus(s);
  const mismatch = account && account.toLowerCase() !== OWNER.toLowerCase();
  $("acctWarn").innerHTML = mismatch ? "هشدار: این حساب با مالک مورد انتظار یکی نیست؛ حساب " + OWNER + " را انتخاب کن.<br><span class='en'>Warning: connected account differs from the expected owner " + OWNER + ". The agent NFT would belong to this other account.</span>" : "";
  $("register").disabled = true;
  if (account && chainOk) {
    await checkDuplicates();
    $("register").disabled = mismatch || dupBlock;
    if (!mismatch && !dupBlock) estimate();
  }
}

async function estimate(){
  try {
    const [gas, price] = await Promise.all([
      eth.request({ method: "eth_estimateGas", params: [{ from: account, to: REGISTRY, data: CALLDATA }] }),
      eth.request({ method: "eth_gasPrice" })]);
    const wei = BigInt(gas) * BigInt(price);
    $("est").innerHTML = "<small>Estimated gas: " + BigInt(gas) + " × " + (Number(BigInt(price)) / 1e9).toFixed(4) + " gwei ≈ " + fmtEth(wei) + " ETH (plus a tiny L1 data fee)</small>";
  } catch (e) { $("est").innerHTML = '<small class="err">Simulation failed: ' + (e && e.message ? e.message : e) + "</small>"; }
}

$("register").onclick = async () => {
  const btn = $("register"), res = $("res");
  if (dupFound && !confirm("ثبت قبلی پیدا شد. دوباره ثبت شود (عامل تکراری)؟\nAn earlier registration was found. Register again (duplicate agent)?")) return;
  btn.disabled = true;
  try {
    const hash = await eth.request({ method: "eth_sendTransaction", params: [{ from: account, to: REGISTRY, data: CALLDATA, value: "0x0" }] });
    localStorage.setItem(LS, hash);
    res.innerHTML = "ارسال شد، در انتظار تأیید · Sent, waiting for confirmation: " + txLink(hash);
    for (let i = 0; i < 100; i++) {
      await new Promise(r => setTimeout(r, 3000));
      const rc = await eth.request({ method: "eth_getTransactionReceipt", params: [hash] });
      if (!rc) continue;
      if (rc.status !== "0x1") { res.innerHTML = '<span class="err">تراکنش ناموفق · Transaction failed</span> ' + txLink(hash); btn.disabled = false; return; }
      const log = (rc.logs || []).find(l => l.address.toLowerCase() === REGISTRY.toLowerCase() && l.topics[0] === TOPIC);
      const id = log ? BigInt(log.topics[1]).toString() : "?";
      localStorage.setItem(LS + "-id", id);
      res.innerHTML = '<span class="ok">ثبت شد ✓ · Registered ✓</span> agentId: <b id="agentId">' + id + "</b> " + (id !== "?" ? nftLink(id) : "") + "<br>" + txLink(hash) +
        "<br>Render env: <code>__ENV__=" + id + "</code>";
      return;
    }
    res.innerHTML += "<br>هنوز تأیید نشده؛ Basescan را چک کن · Not confirmed yet, check Basescan.";
  } catch (e) {
    res.innerHTML = '<span class="err">' + (e && e.code === 4001 ? "لغو شد · Rejected in wallet" : (e && e.message ? e.message : e)) + "</span>";
    btn.disabled = false;
  }
};
$("connect").onclick = async () => {
  if (!eth) { setStatus('<span class="err">کیف‌پول مرورگر پیدا نشد · No browser wallet found (window.ethereum).</span>'); return; }
  try { await eth.request({ method: "eth_requestAccounts" }); } catch (e) { setStatus('<span class="err">' + e.message + "</span>"); return; }
  await refresh();
};
$("switch").onclick = async () => {
  try { await eth.request({ method: "wallet_switchEthereumChain", params: [{ chainId: CHAIN_HEX }] }); }
  catch (e) {
    if (e && (e.code === 4902 || (e.data && e.data.originalError && e.data.originalError.code === 4902))) {
      await eth.request({ method: "wallet_addEthereumChain", params: [{ chainId: CHAIN_HEX, chainName: "Base",
        nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 }, rpcUrls: ["https://mainnet.base.org"], blockExplorerUrls: ["https://basescan.org"] }] });
    } else { setStatus('<span class="err">' + (e && e.message ? e.message : e) + "</span>"); }
  }
  await refresh();
};
if (eth) { eth.on && eth.on("accountsChanged", refresh); eth.on && eth.on("chainChanged", refresh); refresh(); }
else setStatus("کیف‌پول مرورگر پیدا نشد · No browser wallet detected");
</script>
</body></html>
"""


def page(base: str, env=None) -> str:
    agent_uri = f"{base}{WELL_KNOWN}"
    subs = {"__NAME__": "MintScope", "__REGISTRY__": IDENTITY_REGISTRY, "__OWNER__": OWNER,
            "__AGENT_URI__": html.escape(agent_uri, quote=True), "__CALLDATA__": encode_register(agent_uri),
            "__AGENT_ID__": agent_id(env) or "", "__ENV__": ENV_AGENT_ID}
    out = _PAGE
    for k, v in subs.items():
        out = out.replace(k, v)
    return out
