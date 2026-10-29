"""Static page where the tool creator registers MintScope's tools in the ERC-8257 ToolRegistry on Base.

The creator connects their own browser wallet (EIP-1193, e.g. MetaMask or Coinbase Wallet) and signs
the transactions themselves. The server never sees a key and the page contains no secrets: only
public constants (registry address, metadata URIs and the keccak256 manifest hashes computed with
`npx @opensea/tool-sdk hash` against the live manifests).
"""
from __future__ import annotations

import json

CHAIN_ID = 8453  # Base mainnet
REGISTRY = "0x265BB2DBFC0A8165C9A1941Eb1372F349baD2cf1"  # canonical ToolRegistry (@opensea/tool-sdk 0.31.1)
CREATOR = "0x93714D5970f511Ac22f0E5635D3fBF538f717cd4"
NO_PREDICATE = "0x" + "0" * 40  # open access: payment is enforced by x402 at the endpoint
REGISTER_SELECTOR = "fe1d0b16"  # registerTool(string,bytes32,address)
TOOL_REGISTERED_TOPIC = "0xe7be7fd3c802f61682f56ba817276b1cc81fbee7cb50705c8ed7952811dac397"
ORIGIN = "https://mintscope-zrvv.onrender.com"

TOOLS = [
    {
        "key": "analysis",
        "name": "MintScope NFT Collection Analysis",
        "fa": "تحلیل کالکشن NFT",
        "metadata_uri": f"{ORIGIN}/.well-known/ai-tool/mintscope-nft-analysis.json",
        "manifest_hash": "0xbbd9da625c419559bb15cdf6af18a2eb8718cf273c2b061dd4a42316cef045f1",
    },
    {
        "key": "launches",
        "name": "MintScope New NFT Launches",
        "fa": "کالکشن‌های تازه‌منتشرشده",
        "metadata_uri": f"{ORIGIN}/.well-known/ai-tool/mintscope-new-collections.json",
        "manifest_hash": "0xa2c263d6a9f5b07e26b0b7f5994502408fcdfa039125f73b21d1c24b91ef4df6",
    },
]


def encode_register(metadata_uri: str, manifest_hash: str, predicate: str = NO_PREDICATE) -> str:
    """ABI-encode registerTool(string metadataURI, bytes32 manifestHash, address accessPredicate)."""
    uri = metadata_uri.encode()
    h = manifest_hash.lower().removeprefix("0x")
    p = predicate.lower().removeprefix("0x")
    if len(h) != 64 or len(p) != 40:
        raise ValueError("bad hash or address")
    word = lambda n: f"{n:064x}"
    padded = uri.hex() + "00" * ((32 - len(uri) % 32) % 32)
    return "0x" + REGISTER_SELECTOR + word(0x60) + h + p.rjust(64, "0") + word(len(uri)) + padded


def tx_list() -> list[dict]:
    return [{**t, "data": encode_register(t["metadata_uri"], t["manifest_hash"])} for t in TOOLS]


_PAGE = r"""<!doctype html>
<html lang="fa">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>MintScope · Register on OpenSea Tools (Base)</title>
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
 a{color:#7fb2ff} details{margin-top:8px} small{color:#9aa0aa}
</style>
</head>
<body><main>
<h1 class="fa">ثبت ابزارهای MintScope در OpenSea روی شبکه‌ی Base</h1>
<div class="en">Register the two MintScope tools in the ERC-8257 ToolRegistry on Base mainnet</div>

<div class="card">
 <div class="fa">کیف‌پول خودت (MetaMask یا Coinbase Wallet) را وصل کن. همه‌چیز را خودت امضا می‌کنی؛ این سایت هیچ کلیدی نمی‌بیند. هر ثبت حدود ۰٫۰۰۰۰۰۲ اتر (کمتر از یک سنت) هزینه‌ی گس Base دارد.</div>
 <div class="en">Connect your own wallet. You sign everything yourself; this site never sees a key. Each registration costs a tiny Base gas fee (~0.000002 ETH, under one cent). You need a little ETH on Base.</div>
 <button id="connect">اتصال کیف‌پول · Connect wallet</button>
 <button id="switch" disabled>رفتن به شبکه‌ی Base · Switch to Base</button>
 <div id="status" class="mono">—</div>
 <div id="acctWarn" class="warn"></div>
</div>

<div id="tools"></div>

<div class="card">
 <div class="fa"><small>بعد از تأیید هر تراکنش، شماره‌ی ابزار (Tool ID) این‌جا نشان داده می‌شود. هر ابزار را فقط یک بار ثبت کن؛ ثبت دوباره یک ابزار تکراری می‌سازد.</small></div>
 <div class="en"><small>After each transaction confirms, its Tool ID appears here. Register each tool only once; registering again creates a duplicate.</small></div>
 <div class="en"><small>Registry <a target="_blank" rel="noopener" href="https://basescan.org/address/__REGISTRY__"><code>__REGISTRY__</code></a> · function <code>registerTool(string,bytes32,address)</code> · access predicate <code>0x0</code> (open; payments via x402)</small></div>
</div>
</main>
<script>
"use strict";
const CHAIN_HEX = "0x2105";
const REGISTRY = "__REGISTRY__";
const CREATOR = "__CREATOR__";
const TOPIC = "__TOPIC__";
const TOOLS = __TOOLS__;
const ETH_USD = 2300; // rough, for display only
let account = null, chainOk = false;
const $ = (id) => document.getElementById(id);
const eth = window.ethereum;

function setStatus(html){ $("status").innerHTML = html; }
function fmtEth(wei){ return (Number(wei) / 1e18).toFixed(9); }

function renderTools(){
  const box = $("tools");
  for (const t of TOOLS) {
    const prev = localStorage.getItem("mintscope-reg-" + t.key);
    const div = document.createElement("div");
    div.className = "card";
    div.innerHTML =
      '<div class="fa"><b>' + t.fa + '</b></div><div class="en"><b>' + t.name + '</b></div>' +
      '<button id="btn-' + t.key + '" disabled>ثبت · Register ' + t.name + '</button>' +
      '<div id="est-' + t.key + '" class="en"><small></small></div>' +
      '<div id="res-' + t.key + '" class="mono"></div>' +
      '<details><summary class="en"><small>Transaction details</small></summary><div class="mono">to: ' + REGISTRY +
      '<br>metadataURI: ' + t.metadata_uri + '<br>manifestHash: ' + t.manifest_hash +
      '<br>accessPredicate: 0x0000000000000000000000000000000000000000<br>data: ' + t.data + '</div></details>';
    box.appendChild(div);
    if (prev) $("res-" + t.key).innerHTML = '<span class="warn">قبلاً از این مرورگر ارسال شده · Already sent from this browser:</span> ' + txLink(prev);
    $("btn-" + t.key).onclick = () => register(t);
  }
}
function txLink(h){ return '<a target="_blank" rel="noopener" href="https://basescan.org/tx/' + h + '">' + h + '</a>'; }

async function refresh(){
  if (!eth) return;
  const accts = await eth.request({ method: "eth_accounts" });
  account = accts[0] || null;
  const chainId = await eth.request({ method: "eth_chainId" });
  chainOk = chainId.toLowerCase() === CHAIN_HEX;
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
  const mismatch = account && account.toLowerCase() !== CREATOR.toLowerCase();
  $("acctWarn").innerHTML = mismatch ?
    "هشدار: این حساب با سازنده‌ی ثبت‌شده در مانیفست یکی نیست. حساب " + CREATOR + " را انتخاب کن.<br>" +
    "<span class='en'>Warning: connected account differs from the manifest creator " + CREATOR + ". Switch accounts, or OpenSea will reject the listing.</span>" : "";
  for (const t of TOOLS) {
    $("btn-" + t.key).disabled = !(account && chainOk && !mismatch);
    if (account && chainOk && !mismatch) estimate(t);
  }
}

async function estimate(t){
  const el = $("est-" + t.key);
  try {
    const [gas, price] = await Promise.all([
      eth.request({ method: "eth_estimateGas", params: [{ from: account, to: REGISTRY, data: t.data }] }),
      eth.request({ method: "eth_gasPrice" })]);
    const wei = BigInt(gas) * BigInt(price);
    el.innerHTML = "<small>Estimated gas: " + BigInt(gas) + " × " + (Number(BigInt(price)) / 1e9).toFixed(4) +
      " gwei ≈ " + fmtEth(wei) + " ETH (≈ $" + (Number(wei) / 1e18 * ETH_USD).toFixed(4) + ", plus a tiny L1 data fee)</small>";
  } catch (e) {
    el.innerHTML = '<small class="err">Simulation failed: ' + (e && e.message ? e.message : e) + "</small>";
  }
}

async function register(t){
  const btn = $("btn-" + t.key), res = $("res-" + t.key);
  if (localStorage.getItem("mintscope-reg-" + t.key) &&
      !confirm("این ابزار قبلاً از این مرورگر ارسال شده. دوباره ثبت شود؟\nAlready sent from this browser. Register again (duplicate)?")) return;
  btn.disabled = true;
  try {
    const hash = await eth.request({ method: "eth_sendTransaction", params: [{ from: account, to: REGISTRY, data: t.data }] });
    localStorage.setItem("mintscope-reg-" + t.key, hash);
    res.innerHTML = "ارسال شد، در انتظار تأیید · Sent, waiting for confirmation: " + txLink(hash);
    for (let i = 0; i < 100; i++) {
      await new Promise(r => setTimeout(r, 3000));
      const rc = await eth.request({ method: "eth_getTransactionReceipt", params: [hash] });
      if (!rc) continue;
      if (rc.status !== "0x1") { res.innerHTML = '<span class="err">تراکنش ناموفق · Transaction failed</span> ' + txLink(hash); btn.disabled = false; return; }
      const log = (rc.logs || []).find(l => l.address.toLowerCase() === REGISTRY.toLowerCase() && l.topics[0] === TOPIC);
      const id = log ? BigInt(log.topics[1]).toString() : "?";
      localStorage.setItem("mintscope-toolid-" + t.key, id);
      res.innerHTML = '<span class="ok">ثبت شد ✓ · Registered ✓</span> Tool ID: <b>' + id + "</b><br>" + txLink(hash);
      return;
    }
    res.innerHTML += "<br>هنوز تأیید نشده؛ لینک بالا را در Basescan چک کن · Not confirmed yet, check Basescan.";
  } catch (e) {
    res.innerHTML = '<span class="err">' + (e && e.code === 4001 ? "لغو شد · Rejected in wallet" : (e && e.message ? e.message : e)) + "</span>";
    btn.disabled = false;
  }
}

$("connect").onclick = async () => {
  if (!eth) { setStatus('<span class="err">کیف‌پول مرورگر پیدا نشد. MetaMask یا Coinbase Wallet را نصب کن، یا این صفحه را در مرورگر داخل کیف‌پول باز کن.<br>No browser wallet found (window.ethereum).</span>'); return; }
  try { await eth.request({ method: "eth_requestAccounts" }); } catch (e) { setStatus('<span class="err">' + e.message + "</span>"); return; }
  await refresh();
};
$("switch").onclick = async () => {
  try { await eth.request({ method: "wallet_switchEthereumChain", params: [{ chainId: CHAIN_HEX }] }); }
  catch (e) {
    if (e && (e.code === 4902 || (e.data && e.data.originalError && e.data.originalError.code === 4902))) {
      await eth.request({ method: "wallet_addEthereumChain", params: [{ chainId: CHAIN_HEX, chainName: "Base",
        nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 }, rpcUrls: ["https://mainnet.base.org"],
        blockExplorerUrls: ["https://basescan.org"] }] });
    } else { setStatus('<span class="err">' + (e && e.message ? e.message : e) + "</span>"); }
  }
  await refresh();
};
renderTools();
if (eth) { eth.on && eth.on("accountsChanged", refresh); eth.on && eth.on("chainChanged", refresh); refresh(); }
else setStatus("کیف‌پول مرورگر پیدا نشد · No browser wallet detected");
</script>
</body></html>
"""


def page() -> str:
    tools = [{k: t[k] for k in ("key", "name", "fa", "metadata_uri", "manifest_hash", "data")} for t in tx_list()]
    return (_PAGE.replace("__REGISTRY__", REGISTRY).replace("__CREATOR__", CREATOR)
            .replace("__TOPIC__", TOOL_REGISTERED_TOPIC)
            .replace("__TOOLS__", json.dumps(tools, ensure_ascii=False).replace("</", "<\\/")))
