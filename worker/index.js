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

Your job: identify which competitor health fund plan the member is considering. Work with whatever the agent gives you — a price, a plan name fragment, a tier name, or a document.

Tools at your disposal:
- lookup_plan: use when you know the fund AND have at least a price OR a tier keyword. Supported funds: "nib", "bupa". For other funds use tier_keywords only (no pricing data available).
- analyze_document: use when a document has been uploaded and you need to extract plan details from it.

Strategy:
1. Extract fund name from the agent's message (nib, Bupa, Medibank, ahm, etc.)
2. Extract price if mentioned ($X/week or $X/month)
3. Extract tier keywords (gold, silver, silver plus, bronze, basic, etc.)
4. Call lookup_plan with what you have. If fund is nib or bupa and you have a price or tier, call it.
5. If a document is present, call analyze_document first to extract the details.

Response style: 1-2 short sentences. Confirm the plan if found. If unsure, name the candidates. The agent is on a live call — keep it fast.`;

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
    name: "analyze_document",
    description: "Extract health insurance plan details (fund, plan name, price, excess) from an uploaded document or image.",
    input_schema: {
      type: "object",
      properties: {
        instruction: { type: "string", description: "What to look for in the document" },
      },
      required: [],
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

// ── Request handler ───────────────────────────────────────────────────────────

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    if (request.method !== "POST") return new Response("Method Not Allowed", { status: 405, headers: CORS });

    let body;
    try { body = await request.json(); }
    catch { return new Response(JSON.stringify({ error: "Invalid JSON" }), { status: 400, headers: { ...CORS, "content-type": "application/json" } }); }

    const { message, history = [], document: doc } = body;
    if (!message && !doc) {
      return new Response(JSON.stringify({ error: "message required" }), { status: 400, headers: { ...CORS, "content-type": "application/json" } });
    }

    let identifiedPlan = null;
    let identifiedFund = null;
    let betaHeader = null;

    try {
      // Build the user message content — include document if provided
      let userContent;
      if (doc) {
        const isPdf = doc.mediaType === "application/pdf";
        if (isPdf) betaHeader = "pdfs-2024-09-25";
        userContent = [
          isPdf
            ? { type: "document", source: { type: "base64", media_type: doc.mediaType, data: doc.data } }
            : { type: "image",    source: { type: "base64", media_type: doc.mediaType, data: doc.data } },
          { type: "text", text: message || "Please analyze this document and identify the health insurance plan details." },
        ];
      } else {
        userContent = message;
      }

      const messages = [...history, { role: "user", content: userContent }];
      let claudeData = await callClaude(env.ANTHROPIC_API_KEY, messages, TOOLS, betaHeader);

      // Handle tool use loop (max 2 rounds)
      for (let round = 0; round < 2; round++) {
        if (claudeData.stop_reason !== "tool_use") break;

        const toolUse = claudeData.content.find(b => b.type === "tool_use");
        if (!toolUse) break;

        let toolResultText = "";

        if (toolUse.name === "lookup_plan") {
          const result = await executeLookupPlan(toolUse.input);
          toolResultText = result.text;
          if (result.plan) { identifiedPlan = result.plan; identifiedFund = result.fund; }
          if (!identifiedFund && result.fund) identifiedFund = result.fund;
        } else if (toolUse.name === "analyze_document") {
          toolResultText = "Document analysis: please extract fund name, plan name, weekly/monthly premium, and excess amount from the document content already provided in this conversation.";
        }

        const messagesWithTool = [
          ...messages,
          { role: "assistant", content: claudeData.content },
          { role: "user", content: [{ type: "tool_result", tool_use_id: toolUse.id, content: toolResultText }] },
        ];
        claudeData = await callClaude(env.ANTHROPIC_API_KEY, messagesWithTool, TOOLS, betaHeader);
      }

      const replyText = claudeData.content?.find(b => b.type === "text")?.text || "I couldn't process that. Try again.";

      return new Response(
        JSON.stringify({ reply: replyText, plan: identifiedPlan, fund: identifiedFund }),
        { headers: { ...CORS, "content-type": "application/json" } }
      );

    } catch (e) {
      console.error(e);
      return new Response(
        JSON.stringify({ error: e.message }),
        { status: 500, headers: { ...CORS, "content-type": "application/json" } }
      );
    }
  },
};
