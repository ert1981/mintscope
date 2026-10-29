"""Page where the tool creator deregisters the duplicate MintScope tools in the ERC-8257 ToolRegistry on Base.

Reza accidentally registered both tools twice. Tool IDs 814 (nft-analysis) and 815 (new-collections) are kept;
the duplicates 816 and 817 are removed with `deregisterTool(uint256)` (ABI from @opensea/tool-sdk 0.31.1,
creator-only: other callers revert). The creator signs in their own browser wallet; the server sees no key.
Only the two duplicate IDs are ever encoded, so the kept tools cannot be deregistered from this page.
"""
from __future__ import annotations

import json

from register_page import CHAIN_ID, CREATOR, REGISTRY  # noqa: F401  (CHAIN_ID re-exported for tests)

DEREGISTER_SELECTOR = "9148e6ca"  # deregisterTool(uint256)
GET_CONFIG_SELECTOR = "a0178453"  # getToolConfig(uint256)
TOOL_DEREGISTERED_TOPIC = "0x9add33e854e243f868ff7cacac076d65b1d56fb593e2bb891e76e2e8d5ddd034"
KEEP_IDS = (814, 815)

DUPLICATES = [
    {"id": 816, "keeps": 814, "name": "MintScope NFT Collection Analysis (duplicate)", "fa": "تحلیل کالکشن NFT (نسخه‌ی تکراری)",
     "reg_tx": "0xab2dd5fa43dfa0e451b419b7c2b74cb3f1f9bd6f4b7fc8476586fd2398bc4ebe"},
    {"id": 817, "keeps": 815, "name": "MintScope New NFT Launches (duplicate)", "fa": "کالکشن‌های تازه (نسخه‌ی تکراری)",
     "reg_tx": "0x345747a6728b85bf389da2b8b969b929702c857399b341b56be465887b647194"},
]


def _uint(n: int) -> str:
    if n < 0:
        raise ValueError("negative")
    return f"{n:064x}"


def encode_deregister(tool_id: int) -> str:
    if tool_id in KEEP_IDS:
        raise ValueError("refusing to encode a kept tool")
    return "0x" + DEREGISTER_SELECTOR + _uint(tool_id)


def encode_get_config(tool_id: int) -> str:
    return "0x" + GET_CONFIG_SELECTOR + _uint(tool_id)


def tx_list() -> list[dict]:
    return [{**d, "data": encode_deregister(d["id"]), "cfg": encode_get_config(d["id"])} for d in DUPLICATES]


_PAGE = r"""<!doctype html>
<html lang="fa">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>MintScope · Remove duplicate tools (Base)</title>
<style>
 body{font-family:system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif;background:#0f1115;color:#e8e8ea;margin:0;padding:24px;line-height:1.6}
 main{max-width:760px;margin:0 auto}
 h1{font-size:1.4rem;margin:0 0 4px} .en{color:#9aa0aa;font-size:.92em;direction:ltr;text-align:left}
 .fa{direction:rtl;text-align:right}
 .card{background:#181b22;border:1px solid #2a2f3a;border-radius:12px;padding:16px;margin:14px 0}
 button{background:#d9534f;color:#fff;border:0;border-radius:8px;padding:10px 16px;font-size:1rem;cursor:pointer;margin:6px 0}
 button.blue{background:#2f6fed}
 button:disabled{background:#3a3f4a;color:#8a8f99;cursor:not-allowed}
 code,.mono{font-family:ui-monospace,Menlo,monospace;font-size:.85em;word-break:break-all;direction:ltr}
 .warn{color:#ffb84d} .ok{color:#4ddc8a} .err{color:#ff6b6b}
 a{color:#7fb2ff} details{margin-top:8px} small{color:#9aa0aa}
</style>
</head>
<body><main>
<h1 class="fa">حذف ابزارهای تکراری MintScope از OpenSea (شبکه‌ی Base)</h1>
<div class="en">Deregister the duplicate tools #816 and #817. Tools #814 and #815 stay registered.</div>

<div class="card">
 <div class="fa">کیف‌پول خودت را با آدرس سازنده وصل کن و روی Base برو. برای هر نسخه‌ی تکراری یک دکمه هست؛ هر کدام را یک بار بزن و در کیف‌پول تأیید کن. هزینه‌ی هر کدام کمتر از یک سنت است. ابزارهای ۸۱۴ و ۸۱۵ دست نمی‌خورند.</div>
 <div class="en">Connect the creator wallet, switch to Base, then press each button once and approve it in your wallet. Each costs well under one cent. Tools 814 and 815 are not touched.</div>
 <button id="connect" class="blue">اتصال کیف‌پول · Connect wallet</button>
 <button id="switch" class="blue" disabled>رفتن به شبکه‌ی Base · Switch to Base</button>
 <div id="status" class="mono">—</div>
 <div id="acctWarn" class="warn"></div>
</div>

<div id="tools"></div>

<div class="card">
 <div class="en"><small>Registry <a target="_blank" rel="noopener" href="https://basescan.org/address/__REGISTRY__"><code>__REGISTRY__</code></a> · function <code>deregisterTool(uint256)</code> (creator only)</small></div>
</div>
</main>
<script>
"use strict";
const CHAIN_HEX = "0x2105";
const REGISTRY = "__REGISTRY__";
const CREATOR = "__CREATOR__";
const TOPIC = "__TOPIC__";
const DUPS = __DUPS__;
const ETH_USD = 2300; // rough, display only
let account = null, chainOk = false;
const $ = (id) => document.getElementById(id);
const eth = window.ethereum;
function setStatus(h){ $("status").innerHTML = h; }
function fmtEth(wei){ return (Number(wei) / 1e18).toFixed(9); }
function txLink(h){ return '<a target="_blank" rel="noopener" href="https://basescan.org/tx/' + h + '">' + h + '</a>'; }

function render(){
  for (const d of DUPS) {
    const div = document.createElement("div");
    div.className = "card";
    div.innerHTML =
      '<div class="fa"><b>' + d.fa + ' — #' + d.id + '</b> (نسخه‌ی اصلی #' + d.keeps + ' می‌ماند)</div>' +
      '<div class="en"><b>' + d.name + ' — Tool #' + d.id + '</b> (original #' + d.keeps + ' stays). Registered in ' + txLink(d.reg_tx) + '</div>' +
      '<button id="btn-' + d.id + '" disabled>حذف #' + d.id + ' · Deregister #' + d.id + '</button>' +
      '<div id="chk-' + d.id + '" class="en"><small></small></div>' +
      '<div id="res-' + d.id + '" class="mono"></div>' +
      '<details><summary class="en"><small>Transaction details</small></summary><div class="mono">to: ' + REGISTRY +
      '<br>function: deregisterTool(' + d.id + ')<br>data: ' + d.data + '</div></details>';
    $("tools").appendChild(div);
    $("btn-" + d.id).onclick = () => deregister(d);
  }
}

async function check(d){
  const el = $("chk-" + d.id), btn = $("btn-" + d.id);
  try {
    const cfg = await eth.request({ method: "eth_call", params: [{ to: REGISTRY, data: d.cfg }, "latest"] });
    const creator = "0x" + cfg.slice(2 + 64 + 24, 2 + 128);
    if (creator.toLowerCase() !== CREATOR.toLowerCase()) { el.innerHTML = '<small class="err">Creator on-chain is ' + creator + ', not you.</small>'; btn.disabled = true; return; }
  } catch (e) { /* may revert once deregistered; the gas estimate below decides */ }
  try {
    const [gas, price] = await Promise.all([
      eth.request({ method: "eth_estimateGas", params: [{ from: account, to: REGISTRY, data: d.data }] }),
      eth.request({ method: "eth_gasPrice" })]);
    const wei = BigInt(gas) * BigInt(price);
    el.innerHTML = '<small class="ok">Simulation OK.</small> <small>Gas ' + BigInt(gas) + " × " + (Number(BigInt(price)) / 1e9).toFixed(4) +
      " gwei ≈ " + fmtEth(wei) + " ETH (≈ $" + (Number(wei) / 1e18 * ETH_USD).toFixed(4) + ", plus a tiny L1 data fee)</small>";
    btn.disabled = false;
  } catch (e) {
    el.innerHTML = '<small class="warn">احتمالاً قبلاً حذف شده · Simulation reverted (probably already deregistered): ' + (e && e.message ? e.message : e) + "</small>";
    btn.disabled = true;
  }
}

async function refresh(){
  if (!eth) return;
  const accts = await eth.request({ method: "eth_accounts" });
  account = accts[0] || null;
  const chainId = await eth.request({ method: "eth_chainId" });
  chainOk = chainId.toLowerCase() === CHAIN_HEX;
  $("switch").disabled = !account || chainOk;
  let s = account ? "حساب · Account: " + account : "وصل نیست · Not connected";
  s += "<br>شبکه · Network: " + (chainOk ? '<span class="ok">Base ✓</span>' : '<span class="warn">' + chainId + ' (not Base)</span>');
  setStatus(s);
  const mismatch = account && account.toLowerCase() !== CREATOR.toLowerCase();
  $("acctWarn").innerHTML = mismatch ? "هشدار: فقط سازنده (" + CREATOR + ") می‌تواند حذف کند. حساب را عوض کن.<br><span class='en'>Only the creator " + CREATOR + " can deregister. Switch accounts.</span>" : "";
  for (const d of DUPS) {
    $("btn-" + d.id).disabled = true;
    if (account && chainOk && !mismatch) check(d);
  }
}

async function deregister(d){
  const btn = $("btn-" + d.id), res = $("res-" + d.id);
  if (!confirm("ابزار تکراری #" + d.id + " حذف شود؟ (#" + d.keeps + " می‌ماند)\nDeregister duplicate tool #" + d.id + "? (#" + d.keeps + " stays)")) return;
  btn.disabled = true;
  try {
    const hash = await eth.request({ method: "eth_sendTransaction", params: [{ from: account, to: REGISTRY, data: d.data }] });
    res.innerHTML = "ارسال شد، در انتظار تأیید · Sent, waiting: " + txLink(hash);
    for (let i = 0; i < 100; i++) {
      await new Promise(r => setTimeout(r, 3000));
      const rc = await eth.request({ method: "eth_getTransactionReceipt", params: [hash] });
      if (!rc) continue;
      if (rc.status !== "0x1") { res.innerHTML = '<span class="err">تراکنش ناموفق · Failed</span> ' + txLink(hash); btn.disabled = false; return; }
      const ok = (rc.logs || []).some(l => l.address.toLowerCase() === REGISTRY.toLowerCase() && l.topics[0] === TOPIC && BigInt(l.topics[1]) === BigInt(d.id));
      res.innerHTML = (ok ? '<span class="ok">حذف شد ✓ · Deregistered ✓</span> ' : '<span class="warn">Confirmed (event not found)</span> ') + txLink(hash);
      return;
    }
    res.innerHTML += "<br>هنوز تأیید نشده؛ در Basescan چک کن · Not confirmed yet, check Basescan.";
  } catch (e) {
    res.innerHTML = '<span class="err">' + (e && e.code === 4001 ? "لغو شد · Rejected in wallet" : (e && e.message ? e.message : e)) + "</span>";
    btn.disabled = false;
  }
}

$("connect").onclick = async () => {
  if (!eth) { setStatus('<span class="err">کیف‌پول مرورگر پیدا نشد · No browser wallet found (window.ethereum).</span>'); return; }
  try { await eth.request({ method: "eth_requestAccounts" }); } catch (e) { setStatus('<span class="err">' + e.message + "</span>"); return; }
  await refresh();
};
$("switch").onclick = async () => {
  try { await eth.request({ method: "wallet_switchEthereumChain", params: [{ chainId: CHAIN_HEX }] }); }
  catch (e) { setStatus('<span class="err">' + (e && e.message ? e.message : e) + "</span>"); }
  await refresh();
};
render();
if (eth) { eth.on && eth.on("accountsChanged", refresh); eth.on && eth.on("chainChanged", refresh); refresh(); }
else setStatus("کیف‌پول مرورگر پیدا نشد · No browser wallet detected");
</script>
</body></html>
"""


def page() -> str:
    return (_PAGE.replace("__REGISTRY__", REGISTRY).replace("__CREATOR__", CREATOR)
            .replace("__TOPIC__", TOOL_DEREGISTERED_TOPIC)
            .replace("__DUPS__", json.dumps(tx_list(), ensure_ascii=False).replace("</", "<\\/")))
