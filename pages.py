"""Public English landing / pricing page (served at / and /pricing)."""
from html import escape

from config import Settings


def landing_page(settings: Settings) -> str:
    price = escape(settings.price_usdc)
    free_n = settings.free_analyses_per_month
    network = "Base" if settings.x402_network == "base" else "Base Sepolia (test network)"
    paid_status = ("" if settings.paid_enabled else
                   "<p class='note'>The paid tier is not enabled on this server yet.</p>")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>MintScope AI - NFT risk analysis and new-launch discovery</title>
<meta name="description" content="Evidence-based risk analysis of NFT collections and discovery of new NFT launches.">
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:880px;margin:0 auto;padding:32px 20px;color:#1b1f24;line-height:1.55}}
h1{{font-size:2rem;margin-bottom:.2rem}} .sub{{color:#57606a;margin-top:0}}
.tiers{{display:flex;gap:16px;flex-wrap:wrap;margin:24px 0}}
.tier{{flex:1 1 240px;border:1px solid #d0d7de;border-radius:10px;padding:18px}}
.tier h3{{margin-top:0}} .price{{font-size:1.6rem;font-weight:700}}
.soon{{opacity:.7;border-style:dashed}} .note{{color:#57606a;font-size:.9rem}}
.disclaimer{{background:#fff8c5;border:1px solid #d4a72c;border-radius:8px;padding:12px 16px}}
code{{background:#f6f8fa;padding:1px 5px;border-radius:4px}}
</style></head><body>
<h1>MintScope AI</h1>
<p class="sub">Evidence-based NFT risk analysis and new-launch discovery, built as an AI-agent tool for OpenSea Tools.</p>

<h2>What it does</h2>
<ul>
<li><b>Analyze a collection</b> (<code>POST /analyze</code>): reads market and creator data for an NFT collection
(OpenSea slug or contract) and returns a 0-100 evidence score, risk flags with evidence, and what data is missing.
Every value is labeled with its source; unknown data is shown as unknown, never guessed.</li>
<li><b>Discover new launches</b> (<code>/discover</code>): finds NFT collections that started minting today, this week
or this month on Ethereum and Base from public onchain mint events, ranks them by early momentum and distribution,
and flags warning signs such as wallet concentration, suspected wash trading or unverified contracts.</li>
</ul>

<h2>Pricing</h2>
<div class="tiers">
<div class="tier"><h3>Free</h3><div class="price">$0</div>
<p>{free_n} analyses per month per caller (wallet or IP).</p>
<p>Overall score and a short project summary. Each response shows how many free analyses are left.</p></div>
<div class="tier"><h3>Full report</h3><div class="price">${price} <span class="note">per report</span></div>
<p>The detailed risk report: every metric with its source, all red flags with evidence, weaknesses,
missing data, and the full new-launch discovery data.</p>
<p class="note">Paid per request in USDC on {network} with the x402 protocol (HTTP 402). You are charged only
when the report is delivered.</p>{paid_status}</div>
<div class="tier soon"><h3>Monthly plan</h3><div class="price">Coming soon</div>
<p>A monthly plan is planned. It is not available yet.</p></div>
</div>

<h2>How paying works</h2>
<p>Send the request with <code>"tier": "full"</code>. The server answers <code>402 Payment Required</code> with x402
payment requirements. An x402-capable client or agent wallet signs a USDC transfer authorization and repeats the
request with an <code>X-Payment</code> header. A payment facilitator verifies it, the report is produced, the payment
is settled onchain, and the response includes the transaction hash in <code>X-PAYMENT-RESPONSE</code>.
MintScope never asks for or holds keys.</p>

<h2>Disclaimer</h2>
<p class="disclaimer">MintScope is an analysis and risk-research tool. It is <b>not financial advice</b>. It does
<b>not</b> guarantee profit, price or resale value, and it does <b>not</b> guarantee that a project is or is not a scam.
Scores are hypothesis-weighted summaries of public data, which can be incomplete or wrong. Do your own research.</p>

<p class="note">API docs: <a href="/docs">/docs</a> &middot; Health: <a href="/health">/health</a></p>
</body></html>"""
