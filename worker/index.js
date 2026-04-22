/**
 * HCF Retention Chat — Cloudflare Worker
 *
 * POST /chat   { message, history, document? }
 *   → { reply, plan, fund }
 *
 * document (optional): { mediaType: "image/jpeg"|"application/pdf", data: "<base64>" }
 *
 * Secrets (set via: wrangler secret put ANTHROPIC_API_KEY):
 *   ANTHROPIC_API_KEY
 */

const ANTHROPIC_API = "https://api.anthropic.com/v1/messages";
const MODEL = "claude-haiku-4-5-20251001";
const NIB_API = "https://api-gateway.nib.com.au/pricing-api-lambda/v1/australian-resident";

const GOVT_REBATE_RATE = 0.24608; // base tier, age <65, 2025-26 FY

// ── Plan data ────────────────────────────────────────────────────────────────

const NIB_PRODUCTS = {
  2785: { name: "Basic Care Hospital Plus",       tier: "Basic"        },
  2786: { name: "Bronze Protect Hospital Plus",   tier: "Bronze"       },
  2787: { name: "Silver Secure Hospital Plus",    tier: "Silver"       },
  53:   { name: "Mid Hospital - Silver Plus",     tier: "Silver Plus"  },
  729:  { name: "Silver Select Hospital Plus",    tier: "Silver Plus"  },
};

const BUPA_PLANS = [
  { name: "Basic Accident Only Hospital",   tier: "Basic",       price_weekly: 25.47, price_weekly_rebated: 19.20, price_monthly: 110.37, excess: "750", factsheet_url: "https://www.bupa.com.au/health-insurance/cover/basic-accident-only-hospital" },
  { name: "Basic Plus Starter Hospital",    tier: "Basic Plus",  price_weekly: 27.63, price_weekly_rebated: 20.83, price_monthly: 119.73, excess: "750", factsheet_url: "https://www.bupa.com.au/health-insurance/cover/basic-plus-starter-hospital" },
  { name: "Bronze Hospital",                tier: "Bronze",      price_weekly: 29.03, price_weekly_rebated: 21.89, price_monthly: 125.80, excess: "750", factsheet_url: "https://www.bupa.com.au/health-insurance/cover/bronze-hospital" },
  { name: "Bronze Plus Select Hospital",    tier: "Bronze Plus", price_weekly: 30.65, price_weekly_rebated: 23.11, price_monthly: 132.82, excess: "750", factsheet_url: "https://www.bupa.com.au/health-insurance/cover/bronze-plus-select-hospital" },
  { name: "Bronze Plus Advantage Hospital", tier: "Bronze Plus", price_weekly: 33.32, price_weekly_rebated: 25.12, price_monthly: 144.39, excess: "750", factsheet_url: "https://www.bupa.com.au/health-insurance/cover/bronze-plus-advantage-hospital" },
  { name: "Silver Plus Classic Hospital",   tier: "Silver Plus", price_weekly: 37.14, price_weekly_rebated: 28.00, price_monthly: 160.94, excess: "750", factsheet_url: "https://www.bupa.com.au/health-insurance/cover/silver-plus-classic-hospital" },
  { name: "Silver Plus Advanced Hospital",  tier: "Silver Plus", price_weekly: 58.73, price_weekly_rebated: 44.28, price_monthly: 254.50, excess: "750", factsheet_url: "https://www.bupa.com.au/health-insurance/cover/silver-plus-advanced-hospital" },
  { name: "Gold Comprehensive Hospital",    tier: "Gold",        price_weekly: 96.27, price_weekly_rebated: 72.58, price_monthly: 417.17, excess: "750", factsheet_url: "https://www.bupa.com.au/health-insurance/cover/gold-comprehensive-hospital" },
];

// ── nib live pricing ─────────────────────────────────────────────────────────

async function getNibPlans(excess = "750", state = "NSW") {
  const params = new URLSearchParams({
    excess, previousCover: "true", partnerPreviousCover: "true",
    rebateTier: "0", applyRebate: "false", effectiveDate: "2026-04-18",
    rate: "0", paymentFrequency: "Weekly", dob: "1990-01-01", state, scale: "Single",
  });
  Object.keys(NIB_PRODUCTS).forEach((pid, i) => {
    params.append(`products[${i}][hospitalProduct]`, pid);
  });

  const resp = await fetch(`${NIB_API}?${params}`, {
    headers: { Accept: "application/json", Origin: "https://www.nib.com.au", Referer: "https://www.nib.com.au/" },
  });
  if (!resp.ok) throw new Error(`nib API ${resp.status}`);

  const data = await resp.json();
  const apiData = {};
  for (const item of data.data || []) {
    if (item.hospital) apiData[item.hospital.id] = item.hospital.baseRate;
  }

  return Object.entries(NIB_PRODUCTS).map(([pid, info]) => {
    const weekly = parseFloat(apiData[parseInt(pid)] || 0);
    if (!weekly) return null;
    return { name: info.name, tier: info.tier, price_weekly: weekly,
             price_monthly: Math.round(weekly * 52 / 12 * 100) / 100,
             excess, factsheet_url: `https://my.nib.com.au/product-collateral/${pid}` };
  }).filter(Boolean).sort((a, b) => a.price_weekly - b.price_weekly);
}

// ── Plan matching ────────────────────────────────────────────────────────────

function tierScore(planTier, keyword) {
  if (!keyword) return 0;
  const kw = keyword.toLowerCase();
  const pt = planTier.toLowerCase();
  if (pt === kw) return 10;
  if (pt.includes(kw) || kw.includes(pt)) return 6;
  // partial matches
  const words = kw.split(/\s+/);
  const hits = words.filter(w => pt.includes(w) || w.includes(pt.split(/\s+/)[0]));
  return hits.length * 2;
}

function matchPlanByPrice(plans, target, tolerance = 1.5) {
  // For Bupa: try both base price and rebated-to-base conversion
  const baseEquiv = target / (1 - GOVT_REBATE_RATE);
  let bestBase = null, bestBaseDist = Infinity;
  let bestRebated = null, bestRebatedDist = Infinity;
  for (const p of plans) {
    const dBase    = Math.abs(p.price_weekly - target);
    const dRebated = Math.abs(p.price_weekly - baseEquiv);
    if (dBase    < bestBaseDist)    { bestBaseDist    = dBase;    bestBase    = p; }
    if (dRebated < bestRebatedDist) { bestRebatedDist = dRebated; bestRebated = p; }
  }
  if (bestBaseDist <= tolerance && bestBaseDist <= bestRebatedDist) return bestBase;
  if (bestRebatedDist <= tolerance) return bestRebated;
  return null;
}

function findBestPlan(plans, { weekly_price, tier_keywords }) {
  let candidates = plans;

  // Filter by tier if keywords given
  if (tier_keywords) {
    const scored = candidates.map(p => ({ p, score: tierScore(p.tier, tier_keywords) }));
    const maxScore = Math.max(...scored.map(x => x.score));
    if (maxScore >= 2) candidates = scored.filter(x => x.score === maxScore).map(x => x.p);
  }

  // Match by price within candidates
  if (weekly_price) {
    const match = matchPlanByPrice(candidates, weekly_price);
    if (match) return match;
    // fallback: match across all plans if candidates didn't match
    if (candidates.length < plans.length) {
      const fallback = matchPlanByPrice(plans, weekly_price);
      if (fallback) return fallback;
    }
    return null;
  }

  // No price — return lowest-diff by tier or first candidate
  return candidates[0] || null;
}

// ── Claude helpers ───────────────────────────────────────────────────────────

const SYSTEM = `You are an AI assistant for HCF health insurance retention agents during live member calls.

You have two jobs:
1. IDENTIFY a competitor plan from whatever the agent gives you (price, fund + tier, plan name fragment, document).
2. SAVE rate data to products.json when the agent uploads a rate sheet and asks to update or add the data. Funds supported for saving: hcf, nib, bupa, medibank, ahm, australian_unity.

Tools:
- lookup_plan: identify a nib or Bupa plan by price and/or tier keywords.
- extract_rates: when a document is attached AND the user's message indicates SAVE intent (e.g. "save this", "update HCF", "add this plan", "this is the new …"). Returns structured premiums + a diff preview; never commits — the user confirms in the UI.

When intent is unclear with a document attached, default to IDENTIFY (no tool call needed — just read the doc and answer). Only call extract_rates if the agent explicitly asks to save/update/add.

Response style: 1-2 short sentences. The agent is on a live call — keep it fast.`;

const TOOLS = [
  {
    name: "lookup_plan",
    description: "Look up a competitor hospital plan by fund, price, and/or tier keywords. Use whenever fund + price or tier is known.",
    input_schema: {
      type: "object",
      properties: {
        fund:          { type: "string", description: "Fund name: 'nib' or 'bupa'" },
        weekly_price:  { type: "number", description: "Weekly price in dollars (e.g. 28.00)" },
        tier_keywords: { type: "string", description: "Tier or name keywords: 'gold', 'silver plus', 'bronze', 'basic', etc." },
        excess:        { type: "string", enum: ["500", "750"], description: "Excess amount, default 750" },
        state:         { type: "string", description: "State code, default NSW" },
      },
      required: ["fund"],
    },
  },
  {
    name: "extract_rates",
    description: "Extract structured rate data from an attached document so it can be saved to products.json. Only call when the agent's intent is to SAVE/UPDATE/ADD data, not to identify a plan. Document must already be attached in the conversation.",
    input_schema: {
      type: "object",
      properties: {
        fund_key:     { type: "string", enum: ["hcf", "nib", "bupa", "medibank", "ahm", "australian_unity"], description: "Which fund's product this is." },
        product_name: { type: "string", description: "Exact product name from the document (e.g. 'Hospital Optimal Gold', 'Top Extras')." },
        kind:         { type: "string", enum: ["hospital", "extras"], description: "Hospital or extras product." },
        tier:         { type: "string", description: "Tier for hospital products: Basic, Basic Plus, Bronze, Bronze Plus, Silver, Silver Plus, Gold. Omit for extras." },
        excess:       { type: "string", description: "Excess amount for hospital, e.g. '500' or '750'. Omit for extras." },
        premiums: {
          type: "object",
          description: "Monthly premiums BEFORE government rebate. Use null for values not found in the document.",
          properties: {
            single:               { type: ["number", "null"] },
            couple:               { type: ["number", "null"] },
            family:               { type: ["number", "null"] },
            single_parent_family: { type: ["number", "null"] },
          },
          required: ["single", "couple", "family", "single_parent_family"],
        },
        source_note:  { type: "string", description: "Brief label for where this data came from, e.g. 'HCF rate sheet 2026-04-23'." },
      },
      required: ["fund_key", "product_name", "kind", "premiums"],
    },
  },
];

async function callClaude(apiKey, messages, tools, betaHeader) {
  const headers = {
    "x-api-key": apiKey,
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
  };
  if (betaHeader) headers["anthropic-beta"] = betaHeader;

  const resp = await fetch(ANTHROPIC_API, {
    method: "POST",
    headers,
    body: JSON.stringify({ model: MODEL, max_tokens: 600, system: SYSTEM, tools, messages }),
  });
  if (!resp.ok) throw new Error(`Anthropic ${resp.status}: ${await resp.text()}`);
  return resp.json();
}

// ── Tool execution ────────────────────────────────────────────────────────────

async function executeLookupPlan({ fund, weekly_price, tier_keywords, excess = "750", state = "NSW" }) {
  const fundKey = (fund || "").toLowerCase().trim();

  if (fundKey === "nib") {
    const plans = await getNibPlans(excess, state);
    const plan = findBestPlan(plans, { weekly_price, tier_keywords });
    if (!plan) {
      return {
        text: `No nib plan found. Available: ${plans.map(p => `${p.name} $${p.price_weekly}/wk`).join(", ")}`,
        plan: null,
        fund: "nib",
      };
    }
    return { text: JSON.stringify(plan), plan, fund: "nib" };
  }

  if (fundKey === "bupa") {
    const plan = findBestPlan(BUPA_PLANS, { weekly_price, tier_keywords });
    if (!plan) {
      return {
        text: `No Bupa plan found. Available: ${BUPA_PLANS.map(p => `${p.name} base=$${p.price_weekly}/wk (~$${p.price_weekly_rebated} rebated)`).join(", ")}`,
        plan: null,
        fund: "bupa",
      };
    }
    return { text: JSON.stringify(plan), plan, fund: "bupa" };
  }

  // Other fund — no pricing data, return what we know from tier keywords
  return {
    text: `No pricing data for "${fund}" — I can note the tier as "${tier_keywords || "unknown"}". The agent should enter the price manually.`,
    plan: null,
    fund: fundKey,
  };
}

// ── GitHub Contents API ───────────────────────────────────────────────────────

const GH_OWNER = "nikfeao";
const GH_REPO  = "hcf-retention-tool";
const GH_PATH  = "data/products.json";
const GH_API   = `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/contents/${GH_PATH}`;

async function ghReadProducts(token) {
  const resp = await fetch(GH_API, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "hcf-retention-chat-worker",
    },
  });
  if (!resp.ok) throw new Error(`GitHub read ${resp.status}: ${await resp.text()}`);
  const meta = await resp.json();
  const json = JSON.parse(atob(meta.content.replace(/\n/g, "")));
  return { json, sha: meta.sha };
}

async function ghWriteProducts(token, json, sha, message) {
  const content = btoa(unescape(encodeURIComponent(JSON.stringify(json, null, 2) + "\n")));
  const resp = await fetch(GH_API, {
    method: "PUT",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      "User-Agent": "hcf-retention-chat-worker",
    },
    body: JSON.stringify({ message, content, sha, branch: "main" }),
  });
  if (!resp.ok) throw new Error(`GitHub write ${resp.status}: ${await resp.text()}`);
  return resp.json(); // { content, commit: { html_url, ... } }
}

// ── Upsert logic ──────────────────────────────────────────────────────────────

function slugifyId(fundKey, productName, excess) {
  const slug = productName.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  return excess ? `${fundKey}-${slug}-${excess}` : `${fundKey}-${slug}`;
}

function findProduct(fund, productName, kind) {
  const list = fund[kind] || [];
  const norm = s => s.toLowerCase().replace(/\s+/g, " ").trim();
  const target = norm(productName);
  return list.find(p => norm(p.name) === target)
      || list.find(p => norm(p.name).includes(target) || target.includes(norm(p.name)))
      || null;
}

function buildPreview(productsJson, payload) {
  const { fund_key, product_name, kind, tier, excess, premiums, source_note } = payload;
  const fund = productsJson.funds?.[fund_key];
  if (!fund) throw new Error(`Unknown fund '${fund_key}'. Add the fund skeleton to products.json first.`);

  const existing = findProduct(fund, product_name, kind);
  const today = new Date().toISOString().split("T")[0];
  const normNum = v => (v == null ? null : Math.round(Number(v) * 100) / 100);
  const pNew = {
    single:               normNum(premiums.single),
    couple:               normNum(premiums.couple),
    family:               normNum(premiums.family),
    single_parent_family: normNum(premiums.single_parent_family),
  };

  // Community-rating derivation when values missing
  if (pNew.single && !pNew.couple) pNew.couple = Math.round(pNew.single * 2 * 100) / 100;
  if (pNew.single && !pNew.family) pNew.family = pNew.couple;
  if (pNew.single && !pNew.single_parent_family) pNew.single_parent_family = pNew.single;

  let action, existingPremiums = null;

  if (kind === "hospital") {
    if (!existing) {
      action = "create_product";
    } else if (!excess || !existing.premiums?.[excess]) {
      action = "create_excess";
      existingPremiums = null;
    } else {
      action = "update_excess";
      existingPremiums = existing.premiums[excess];
    }
  } else {
    if (!existing) {
      action = "create_product";
    } else {
      action = "update_excess"; // reuse key — extras has flat premiums
      existingPremiums = existing.premiums;
    }
  }

  const labelMap = {
    single: "Single",
    couple: "Couple",
    family: "Family",
    single_parent_family: "Single parent family",
  };
  const changes = ["single", "couple", "family", "single_parent_family"]
    .filter(k => pNew[k] != null)
    .map(k => ({
      label: labelMap[k],
      old: existingPremiums ? `$${existingPremiums[k] || 0}` : null,
      new: `$${pNew[k]}`,
    }));

  return {
    action,
    fund_key,
    fund_name: fund.name || fund_key,
    product_name: existing ? existing.name : product_name,
    product_id: existing ? existing.id : slugifyId(fund_key, product_name, kind === "hospital" ? excess : null),
    kind,
    tier: tier || existing?.tier || null,
    excess: kind === "hospital" ? (excess || null) : null,
    premiums: pNew,
    source_note: source_note || `chat upload ${today}`,
    changes,
  };
}

function applyPreviewToJson(productsJson, preview) {
  const { fund_key, product_id, product_name, kind, tier, excess, premiums, source_note, action } = preview;
  const fund = productsJson.funds[fund_key];
  const list = fund[kind] || (fund[kind] = []);
  const today = new Date().toISOString().split("T")[0];

  let product = list.find(p => p.id === product_id) || list.find(p => p.name === product_name);

  if (!product) {
    product = kind === "hospital"
      ? {
          id: product_id,
          name: product_name,
          tier: tier || "",
          status: "current",
          excess_options: excess ? [Number(excess)] : [],
          premiums: {},
        }
      : {
          id: product_id,
          name: product_name,
          status: "current",
          premiums: { single: 0, couple: 0, family: 0, single_parent_family: 0 },
        };
    list.push(product);
  }

  if (kind === "hospital") {
    if (excess) {
      const ek = String(excess);
      if (!product.premiums[ek]) product.premiums[ek] = { single: 0, couple: 0, family: 0, single_parent_family: 0 };
      for (const k of Object.keys(premiums)) if (premiums[k] != null) product.premiums[ek][k] = premiums[k];
      if (!product.excess_options?.includes(Number(excess))) {
        product.excess_options = [...(product.excess_options || []), Number(excess)].sort((a, b) => a - b);
      }
    }
  } else {
    for (const k of Object.keys(premiums)) if (premiums[k] != null) product.premiums[k] = premiums[k];
  }

  product.source        = `chat upload: ${source_note}`;
  product.last_verified = today;
  product.last_uploaded = today;
  product.upload_source = source_note;

  // Meta log
  if (!productsJson.meta) productsJson.meta = {};
  // meta lives in a separate file; skip here — process_upload.py handles meta for the PDF workflow.
  return productsJson;
}

// ── Request handler ───────────────────────────────────────────────────────────

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

function jsonResponse(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...CORS, "content-type": "application/json" },
  });
}

async function handleChat(body, env) {
  const { message, history = [], document: doc } = body;
  if (!message && !doc) return jsonResponse({ error: "message required" }, 400);

  let identifiedPlan = null;
  let identifiedFund = null;
  let preview = null;
  let betaHeader = null;

  // Build the user message content — include document if provided. Cache the doc
  // so follow-up turns on the same doc are cheap.
  let userContent;
  if (doc) {
    const isPdf = doc.mediaType === "application/pdf";
    if (isPdf) betaHeader = "pdfs-2024-09-25";
    userContent = [
      {
        ...(isPdf
          ? { type: "document", source: { type: "base64", media_type: doc.mediaType, data: doc.data } }
          : { type: "image",    source: { type: "base64", media_type: doc.mediaType, data: doc.data } }),
        cache_control: { type: "ephemeral" },
      },
      { type: "text", text: message || "Please analyze this document and identify the health insurance plan details." },
    ];
  } else {
    userContent = message;
  }

  const messages = [...history, { role: "user", content: userContent }];
  let claudeData = await callClaude(env.ANTHROPIC_API_KEY, messages, TOOLS, betaHeader);
  let running = messages;

  // Tool loop (max 3 rounds — extract_rates may follow a think step)
  for (let round = 0; round < 3; round++) {
    if (claudeData.stop_reason !== "tool_use") break;

    const toolUse = claudeData.content.find(b => b.type === "tool_use");
    if (!toolUse) break;

    let toolResultText = "";

    if (toolUse.name === "lookup_plan") {
      const result = await executeLookupPlan(toolUse.input);
      toolResultText = result.text;
      if (result.plan) { identifiedPlan = result.plan; identifiedFund = result.fund; }
      if (!identifiedFund && result.fund) identifiedFund = result.fund;
    } else if (toolUse.name === "extract_rates") {
      if (!env.GITHUB_TOKEN) {
        toolResultText = "GITHUB_TOKEN is not configured on the Worker — cannot read products.json to build a preview.";
      } else {
        try {
          const { json: productsJson } = await ghReadProducts(env.GITHUB_TOKEN);
          preview = buildPreview(productsJson, toolUse.input);
          toolResultText = `Preview built: ${preview.action} for ${preview.fund_name} · ${preview.product_name}${preview.excess ? ` ($${preview.excess} excess)` : ""}. Tell the user what will change and ask them to confirm — the UI will show a preview card with a Commit button.`;
        } catch (e) {
          toolResultText = `Could not build preview: ${e.message}`;
        }
      }
    }

    running = [
      ...running,
      { role: "assistant", content: claudeData.content },
      { role: "user", content: [{ type: "tool_result", tool_use_id: toolUse.id, content: toolResultText }] },
    ];
    claudeData = await callClaude(env.ANTHROPIC_API_KEY, running, TOOLS, betaHeader);
  }

  const replyText = claudeData.content?.find(b => b.type === "text")?.text || "I couldn't process that. Try again.";

  return jsonResponse({ reply: replyText, plan: identifiedPlan, fund: identifiedFund, preview });
}

async function handleCommit(body, env) {
  if (!body.admin) return jsonResponse({ error: "admin flag required" }, 403);
  if (!env.GITHUB_TOKEN) return jsonResponse({ error: "GITHUB_TOKEN not configured on Worker" }, 500);

  const preview = body.payload;
  if (!preview || !preview.fund_key || !preview.product_name) {
    return jsonResponse({ error: "payload missing fund_key / product_name" }, 400);
  }

  // Read → apply → write, with one retry on 409 (concurrent commit).
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const { json: productsJson, sha } = await ghReadProducts(env.GITHUB_TOKEN);
      applyPreviewToJson(productsJson, preview);
      const msg = `Chat upload: ${preview.action} ${preview.fund_name} · ${preview.product_name}${preview.excess ? ` ($${preview.excess})` : ""}`;
      const result = await ghWriteProducts(env.GITHUB_TOKEN, productsJson, sha, msg);
      return jsonResponse({ ok: true, commit_url: result?.commit?.html_url || null });
    } catch (e) {
      if (attempt === 0 && /409/.test(e.message)) continue;
      console.error(e);
      return jsonResponse({ error: e.message }, 500);
    }
  }
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    if (request.method !== "POST") return new Response("Method Not Allowed", { status: 405, headers: CORS });

    const url = new URL(request.url);
    let body;
    try { body = await request.json(); } catch { return jsonResponse({ error: "Invalid JSON" }, 400); }

    try {
      if (url.pathname === "/commit") return await handleCommit(body, env);
      return await handleChat(body, env);   // default / "/chat"
    } catch (e) {
      console.error(e);
      return jsonResponse({ error: e.message }, 500);
    }
  },
};
