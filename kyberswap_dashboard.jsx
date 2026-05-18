import { useState, useEffect, useRef } from "react";
import * as d3 from "d3";

// ============================================================
// DATA
// ============================================================

const COMMUNITY_DATA = {
  lastUpdated: "May 13, 2026 — 00:05 UTC",
  period: "Last 24 hours",
  totalMessages: 347,
  activeUsers: 89,
  channels: 8,
  topics: [
    { id: 1, label: "Smart Exit\nConfusion", count: 48, sentiment: "negative", category: "product", channels: ["#help", "#general"] },
    { id: 2, label: "FairFlow\nLP Rewards", count: 41, sentiment: "positive", category: "product", channels: ["#general", "#degen"] },
    { id: 3, label: "Gas Fee\nComplaints", count: 35, sentiment: "negative", category: "infra", channels: ["#help", "#feedback"] },
    { id: 4, label: "Arbitrum\nMigration", count: 32, sentiment: "mixed", category: "infra", channels: ["#general", "#announcements"] },
    { id: 5, label: "Cross-Chain\nSwap Issues", count: 28, sentiment: "mixed", category: "product", channels: ["#help", "#general"] },
    { id: 6, label: "LP Position\nRewards", count: 24, sentiment: "positive", category: "product", channels: ["#general", "#help"] },
    { id: 7, label: "ZAP Failed\nTransactions", count: 22, sentiment: "negative", category: "bug", channels: ["#help", "#feedback"] },
    { id: 8, label: "Mobile UI\nIssues", count: 18, sentiment: "negative", category: "bug", channels: ["#feedback"] },
    { id: 9, label: "New Pool\nRequests", count: 15, sentiment: "positive", category: "feature", channels: ["#feature-requests"] },
    { id: 10, label: "Kyber Earn\nStrategies", count: 14, sentiment: "positive", category: "product", channels: ["#general", "#degen"] },
    { id: 11, label: "Token Price\nDiscussion", count: 12, sentiment: "mixed", category: "community", channels: ["#degen"] },
    { id: 12, label: "Bridge\nSpeed", count: 10, sentiment: "negative", category: "infra", channels: ["#help"] },
    { id: 13, label: "Multi-chain\nSupport", count: 9, sentiment: "positive", category: "feature", channels: ["#feature-requests", "#general"] },
    { id: 14, label: "Referral\nProgram", count: 7, sentiment: "positive", category: "community", channels: ["#general"] },
    { id: 15, label: "API\nDocs", count: 6, sentiment: "neutral", category: "feature", channels: ["#dev"] },
  ],
  actionItems: [
    { priority: "high", text: "Update Smart Exit FAQ — 48 mentions, mostly confused users asking how to open positions", topic: "Smart Exit Confusion" },
    { priority: "high", text: "Investigate ZAP transaction failures on Arbitrum — 22 reports today, up 3x from yesterday", topic: "ZAP Failed Transactions" },
    { priority: "medium", text: "Mobile UI performance audit — 18 complaints about slow loading and unresponsive buttons", topic: "Mobile UI Issues" },
    { priority: "medium", text: "Gas fee optimization communication — users comparing unfavorably to competitors", topic: "Gas Fee Complaints" },
    { priority: "low", text: "Consider adding requested pools: SOL/USDC, ARB/ETH mentioned by multiple users", topic: "New Pool Requests" },
  ],
  sentimentTimeline: [
    { hour: "00:00", positive: 3, neutral: 5, negative: 2 },
    { hour: "02:00", positive: 1, neutral: 3, negative: 1 },
    { hour: "04:00", positive: 2, neutral: 2, negative: 0 },
    { hour: "06:00", positive: 4, neutral: 6, negative: 3 },
    { hour: "08:00", positive: 8, neutral: 10, negative: 5 },
    { hour: "10:00", positive: 12, neutral: 15, negative: 8 },
    { hour: "12:00", positive: 15, neutral: 12, negative: 11 },
    { hour: "14:00", positive: 10, neutral: 18, negative: 14 },
    { hour: "16:00", positive: 8, neutral: 14, negative: 10 },
    { hour: "18:00", positive: 11, neutral: 16, negative: 7 },
    { hour: "20:00", positive: 9, neutral: 12, negative: 6 },
    { hour: "22:00", positive: 5, neutral: 8, negative: 4 },
  ],
  channelBreakdown: [
    { name: "#general", messages: 94, sentiment: "mixed" },
    { name: "#help", messages: 78, sentiment: "negative" },
    { name: "#degen", messages: 52, sentiment: "positive" },
    { name: "#feedback", messages: 41, sentiment: "negative" },
    { name: "#feature-requests", messages: 28, sentiment: "positive" },
    { name: "#degen-speculation", messages: 22, sentiment: "mixed" },
    { name: "#dev", messages: 18, sentiment: "neutral" },
    { name: "#announcements", messages: 14, sentiment: "positive" },
  ],
  marketNews: [
    { headline: "ETH breaks $4,200 — highest since Jan 2022", reactions: 84, sentiment: "positive", emoji: "🚀", timeAgo: "3h ago", mentions: 31 },
    { headline: "SEC delays spot Solana ETF decision to Q3", reactions: 61, sentiment: "negative", emoji: "⚖️", timeAgo: "5h ago", mentions: 22 },
    { headline: "Whale moved 12,000 BTC from Coinbase to unknown wallet", reactions: 47, sentiment: "mixed", emoji: "🐋", timeAgo: "7h ago", mentions: 16 },
    { headline: "Base TVL surpasses Arbitrum for the first time", reactions: 39, sentiment: "positive", emoji: "📊", timeAgo: "9h ago", mentions: 12 },
    { headline: "Bybit resumes withdrawals after 4-hour maintenance", reactions: 28, sentiment: "neutral", emoji: "🔧", timeAgo: "11h ago", mentions: 8 },
  ],
};

const SUPPORT_DATA = {
  lastUpdated: "May 13, 2026 — 00:00 UTC",
  period: "Last 24 hours",
  totalTickets: 38,
  resolved: 31,
  open: 7,
  avgFRT: 14.7,
  medianFRT: 11.2,
  slaCompliance: 89.5,
  productBreakdown: [
    { name: "Swap", tickets: 23, pct: 60.5, resolved: 20, avgFRT: 12.1, sentiment: "mixed", color: "#6366f1", issues: ["Slippage too high on large orders", "Swap failed on Polygon", "Price impact warning unclear"] },
    { name: "Limit Order", tickets: 5, pct: 13.2, resolved: 5, avgFRT: 15.3, sentiment: "neutral", color: "#06b6d4", issues: ["Order not executing at target price", "Cancel button unresponsive"] },
    { name: "Kyber Earn & ZAP", tickets: 4, pct: 10.5, resolved: 3, avgFRT: 18.7, sentiment: "negative", color: "#22c55e", issues: ["ZAP failed mid-transaction", "Unclear APY calculation", "Earn deposit stuck pending"] },
    { name: "Integration", tickets: 3, pct: 7.9, resolved: 2, avgFRT: 22.4, sentiment: "neutral", color: "#f59e0b", issues: ["API rate limit hit", "Widget not loading on partner site"] },
    { name: "Community & Rewards", tickets: 3, pct: 7.9, resolved: 1, avgFRT: 19.8, sentiment: "positive", color: "#ec4899", issues: ["Referral not tracked", "Mee6 leaderboard glitch", "Reward distribution delay"] },
  ],
  agents: [
    {
      name: "TerrorMichael", shift: "17:00–02:00 UTC", shiftLabel: "C",
      onShift: 14, responded: 13, missed: 1, crossHelp: 2,
      avgFRT: 11.2, medianFRT: 8.5, fastest: { ticket: "#1892", time: 2.3 }, slowest: { ticket: "#1901", time: 34.1 },
      slaCompliance: 92.9, breaches: ["#1901"],
      topProducts: [{ name: "Swap", count: 9 }, { name: "Limit Order", count: 3 }, { name: "ZAP", count: 2 }],
    },
    {
      name: "Mikaelson", shift: "09:00–17:00 UTC", shiftLabel: "B",
      onShift: 16, responded: 16, missed: 0, crossHelp: 1,
      avgFRT: 13.8, medianFRT: 12.1, fastest: { ticket: "#1888", time: 3.1 }, slowest: { ticket: "#1895", time: 28.7 },
      slaCompliance: 100, breaches: [],
      topProducts: [{ name: "Swap", count: 10 }, { name: "Kyber Earn", count: 3 }, { name: "Integration", count: 2 }],
    },
    {
      name: "Dablendo", shift: "02:00–09:00 UTC", shiftLabel: "A",
      onShift: 8, responded: 6, missed: 2, crossHelp: 0,
      avgFRT: 21.3, medianFRT: 18.5, fastest: { ticket: "#1885", time: 5.7 }, slowest: { ticket: "#1887", time: 47.1 },
      slaCompliance: 75, breaches: ["#1887", "#1890"],
      topProducts: [{ name: "Swap", count: 4 }, { name: "Community", count: 2 }, { name: "Limit Order", count: 2 }],
    },
  ],
  agentActions: [
    { agent: "TerrorMichael", items: [
      { priority: "medium", text: "Review ticket #1901 (34 min FRT) — swap slippage issue during high volatility. Consider canned response for peak-hour swap failures." },
      { priority: "low", text: "Strong cross-shift coverage (helped Dablendo 2x). Recognize in weekly standup." },
    ]},
    { agent: "Mikaelson", items: [
      { priority: "low", text: "100% SLA compliance — zero misses. Highest volume shift (16 tickets) handled flawlessly." },
      { priority: "medium", text: "Kyber Earn tickets took longer (avg 18.7 min). Consider adding Earn-specific quick replies to speed up." },
    ]},
    { agent: "Dablendo", items: [
      { priority: "high", text: "2 SLA breaches (#1887 at 47 min, #1890 at 33 min). Both during 04:00–06:00 UTC — investigate if availability gap exists." },
      { priority: "high", text: "2 tickets missed (covered by TerrorMichael). Review shift handoff process at 02:00 UTC." },
      { priority: "medium", text: "Community & Rewards tickets unresolved — referral tracking issue needs escalation to product team." },
    ]},
  ],
  openTickets: [
    { id: "#1903", age: "3h 12m", product: "Swap", status: "No response", onDuty: "Dablendo", severity: "high" },
    { id: "#1905", age: "1h 45m", product: "Kyber Earn & ZAP", status: "Awaiting user reply", onDuty: "TerrorMichael", severity: "low" },
    { id: "#1906", age: "52m", product: "Swap", status: "No response", onDuty: "TerrorMichael", severity: "medium" },
    { id: "#1907", age: "38m", product: "Community & Rewards", status: "In progress", onDuty: "TerrorMichael", severity: "low" },
    { id: "#1908", age: "25m", product: "Swap", status: "No response", onDuty: "TerrorMichael", severity: "medium" },
    { id: "#1909", age: "14m", product: "Limit Order", status: "No response", onDuty: "TerrorMichael", severity: "low" },
    { id: "#1910", age: "6m", product: "Integration", status: "New", onDuty: "TerrorMichael", severity: "low" },
  ],
};

const SENTIMENT_COLORS = {
  positive: "#22c55e",
  mixed: "#f59e0b",
  negative: "#ef4444",
  neutral: "#94a3b8",
};

// ============================================================
// SHARED COMPONENTS
// ============================================================

function Card({ children, style }) {
  return (
    <div style={{
      background: "rgba(255,255,255,0.03)", borderRadius: 16,
      border: "1px solid rgba(255,255,255,0.06)",
      padding: 20, ...style,
    }}>{children}</div>
  );
}

function CardTitle({ title, subtitle }) {
  return (
    <>
      <h2 style={{ fontSize: 15, fontWeight: 600, margin: "0 0 4px", color: "#f1f5f9" }}>{title}</h2>
      {subtitle && <p style={{ fontSize: 12, color: "#64748b", margin: "0 0 14px" }}>{subtitle}</p>}
    </>
  );
}

function SentimentBadge({ sentiment, style }) {
  return (
    <span style={{
      fontSize: 10, padding: "3px 10px", borderRadius: 10,
      background: `${SENTIMENT_COLORS[sentiment]}22`,
      color: SENTIMENT_COLORS[sentiment], fontWeight: 600, whiteSpace: "nowrap", ...style,
    }}>{sentiment}</span>
  );
}

function PriorityBadge({ priority }) {
  const colors = { high: "#ef4444", medium: "#f59e0b", low: "#22c55e" };
  return (
    <span style={{
      fontSize: 10, fontWeight: 700, textTransform: "uppercase",
      color: colors[priority], flexShrink: 0, marginTop: 2, letterSpacing: "0.05em",
    }}>{priority}</span>
  );
}

// ============================================================
// COMMUNITY PULSE TAB COMPONENTS
// ============================================================

function BubbleChart({ topics, selectedTopic, onSelect, filter }) {
  const svgRef = useRef(null);
  const containerRef = useRef(null);
  const [dims, setDims] = useState({ width: 700, height: 500 });
  const filtered = filter === "all" ? topics : topics.filter(t => t.sentiment === filter);

  useEffect(() => {
    const ro = new ResizeObserver(entries => {
      for (const entry of entries) {
        const w = entry.contentRect.width;
        setDims({ width: Math.max(w, 300), height: Math.max(w * 0.65, 350) });
      }
    });
    if (containerRef.current) ro.observe(containerRef.current);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    if (!svgRef.current || filtered.length === 0) return;
    const svg = d3.select(svgRef.current);
    svg.selectAll("*").remove();
    const { width, height } = dims;
    const radiusScale = d3.scaleSqrt().domain([0, d3.max(filtered, d => d.count)]).range([20, Math.min(width, height) * 0.12]);
    const nodes = filtered.map(d => ({ ...d, r: radiusScale(d.count) }));
    const simulation = d3.forceSimulation(nodes)
      .force("charge", d3.forceManyBody().strength(5))
      .force("center", d3.forceCenter(width / 2, height / 2))
      .force("collision", d3.forceCollide().radius(d => d.r + 4).strength(0.9))
      .force("x", d3.forceX(width / 2).strength(0.05))
      .force("y", d3.forceY(height / 2).strength(0.05))
      .stop();
    for (let i = 0; i < 300; i++) simulation.tick();

    const defs = svg.append("defs");
    nodes.forEach((d, i) => {
      const grad = defs.append("radialGradient").attr("id", `bg-${i}`).attr("cx", "35%").attr("cy", "35%");
      const c = d3.color(SENTIMENT_COLORS[d.sentiment]);
      grad.append("stop").attr("offset", "0%").attr("stop-color", c.brighter(0.8));
      grad.append("stop").attr("offset", "100%").attr("stop-color", SENTIMENT_COLORS[d.sentiment]);
    });

    const groups = svg.selectAll("g.bubble").data(nodes, d => d.id).enter().append("g")
      .attr("class", "bubble").attr("transform", d => `translate(${d.x},${d.y})`)
      .style("cursor", "pointer").on("click", (e, d) => onSelect(d.id === selectedTopic ? null : d.id));

    groups.append("circle").attr("r", 0)
      .attr("fill", (d, i) => `url(#bg-${i})`)
      .attr("stroke", d => d.id === selectedTopic ? "#fff" : "rgba(255,255,255,0.15)")
      .attr("stroke-width", d => d.id === selectedTopic ? 3 : 1)
      .attr("opacity", d => selectedTopic && d.id !== selectedTopic ? 0.3 : 0.9)
      .transition().duration(600).delay((d, i) => i * 40).attr("r", d => d.r);

    groups.each(function(d) {
      const g = d3.select(this);
      const lines = d.label.split("\n");
      const fs = Math.max(9, Math.min(d.r * 0.32, 14));
      lines.forEach((line, i) => {
        g.append("text").attr("text-anchor", "middle").attr("dy", `${(i - (lines.length - 1) / 2) * 1.15}em`)
          .attr("y", -fs * 0.3).attr("fill", "#fff").attr("font-size", `${fs}px`).attr("font-weight", "600")
          .attr("font-family", "'DM Sans', sans-serif").attr("pointer-events", "none")
          .attr("opacity", 0).text(line).transition().duration(400).delay(600 + i * 50).attr("opacity", d.r > 25 ? 1 : 0);
      });
      g.append("text").attr("text-anchor", "middle").attr("y", fs * 0.8 + (lines.length - 1) * fs * 0.4)
        .attr("fill", "rgba(255,255,255,0.7)").attr("font-size", `${Math.max(8, fs - 2)}px`)
        .attr("font-family", "'DM Sans', sans-serif").attr("pointer-events", "none")
        .attr("opacity", 0).text(`${d.count} msgs`).transition().duration(400).delay(800).attr("opacity", d.r > 30 ? 1 : 0);
    });
  }, [filtered, dims, selectedTopic, filter]);

  return (
    <div ref={containerRef} style={{ width: "100%", minHeight: 350 }}>
      <svg ref={svgRef} width={dims.width} height={dims.height} style={{ display: "block" }} />
    </div>
  );
}

function SentimentTimeline({ data }) {
  const svgRef = useRef(null);
  const containerRef = useRef(null);
  const [width, setWidth] = useState(600);
  useEffect(() => {
    const ro = new ResizeObserver(entries => { for (const e of entries) setWidth(Math.max(e.contentRect.width, 300)); });
    if (containerRef.current) ro.observe(containerRef.current);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    if (!svgRef.current) return;
    const svg = d3.select(svgRef.current); svg.selectAll("*").remove();
    const margin = { top: 10, right: 15, bottom: 30, left: 30 };
    const w = width - margin.left - margin.right, ih = 140;
    const g = svg.append("g").attr("transform", `translate(${margin.left},${margin.top})`);
    const x = d3.scalePoint().domain(data.map(d => d.hour)).range([0, w]).padding(0.5);
    const maxVal = d3.max(data, d => Math.max(d.positive, d.neutral, d.negative));
    const y = d3.scaleLinear().domain([0, maxVal + 2]).range([ih, 0]);
    g.append("g").attr("transform", `translate(0,${ih})`).call(d3.axisBottom(x).tickValues(data.filter((_, i) => i % 2 === 0).map(d => d.hour)))
      .selectAll("text").attr("fill", "#64748b").attr("font-size", "10px");
    g.selectAll(".domain, .tick line").attr("stroke", "rgba(100,116,139,0.2)");
    const lineGen = (key) => d3.line().x(d => x(d.hour)).y(d => y(d[key])).curve(d3.curveCatmullRom);
    [{ key: "positive", color: "#22c55e" }, { key: "neutral", color: "#94a3b8" }, { key: "negative", color: "#ef4444" }].forEach(({ key, color }) => {
      g.append("path").datum(data).attr("d", lineGen(key)).attr("fill", "none").attr("stroke", color).attr("stroke-width", 2).attr("opacity", 0.85);
      g.selectAll(`.dot-${key}`).data(data).enter().append("circle").attr("cx", d => x(d.hour)).attr("cy", d => y(d[key])).attr("r", 3).attr("fill", color).attr("opacity", 0.9);
    });
  }, [data, width]);

  return <div ref={containerRef} style={{ width: "100%" }}><svg ref={svgRef} width={width} height={180} /></div>;
}

// ============================================================
// SUPPORT HEALTH TAB COMPONENTS
// ============================================================

function DonutChart({ data }) {
  const svgRef = useRef(null);
  useEffect(() => {
    if (!svgRef.current) return;
    const svg = d3.select(svgRef.current); svg.selectAll("*").remove();
    const size = 220, radius = size / 2;
    const g = svg.append("g").attr("transform", `translate(${radius},${radius})`);
    const arc = d3.arc().innerRadius(radius * 0.55).outerRadius(radius * 0.88);
    const pie = d3.pie().value(d => d.tickets).sort(null).padAngle(0.03);
    g.selectAll("path").data(pie(data)).enter().append("path")
      .attr("d", arc).attr("fill", d => d.data.color).attr("opacity", 0.85)
      .attr("stroke", "rgba(10,14,26,0.5)").attr("stroke-width", 2);
    g.append("text").attr("text-anchor", "middle").attr("dy", "-0.1em")
      .attr("fill", "#f1f5f9").attr("font-size", "28px").attr("font-weight", "700")
      .attr("font-family", "'DM Sans', sans-serif").text("38");
    g.append("text").attr("text-anchor", "middle").attr("dy", "1.4em")
      .attr("fill", "#64748b").attr("font-size", "11px")
      .attr("font-family", "'DM Sans', sans-serif").text("total tickets");
  }, [data]);
  return <svg ref={svgRef} width={220} height={220} style={{ display: "block", margin: "0 auto" }} />;
}

function AgentCard({ agent, actions }) {
  const complianceColor = agent.slaCompliance >= 95 ? "#22c55e" : agent.slaCompliance >= 80 ? "#f59e0b" : "#ef4444";
  const agentActions = actions?.items || [];
  return (
    <Card style={{ marginBottom: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 14 }}>
        <div>
          <h3 style={{ fontSize: 16, fontWeight: 700, margin: 0, color: "#f1f5f9" }}>{agent.name}</h3>
          <span style={{ fontSize: 12, color: "#64748b" }}>Shift {agent.shiftLabel}: {agent.shift}</span>
        </div>
        <div style={{
          padding: "6px 14px", borderRadius: 10,
          background: `${complianceColor}18`, border: `1px solid ${complianceColor}44`,
          textAlign: "center",
        }}>
          <div style={{ fontSize: 20, fontWeight: 700, color: complianceColor }}>{agent.slaCompliance}%</div>
          <div style={{ fontSize: 9, color: "#64748b", textTransform: "uppercase", letterSpacing: "0.05em" }}>SLA</div>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 8, marginBottom: 14 }}>
        {[
          { label: "On-Shift", value: agent.onShift, color: "#6366f1" },
          { label: "Responded", value: agent.responded, color: "#22c55e" },
          { label: "Missed", value: agent.missed, color: agent.missed > 0 ? "#ef4444" : "#22c55e" },
          { label: "Cross-Help", value: agent.crossHelp, color: "#06b6d4" },
        ].map((s, i) => (
          <div key={i} style={{ textAlign: "center", padding: "8px 4px", background: "rgba(255,255,255,0.03)", borderRadius: 8 }}>
            <div style={{ fontSize: 18, fontWeight: 700, color: s.color }}>{s.value}</div>
            <div style={{ fontSize: 9, color: "#64748b", marginTop: 2 }}>{s.label}</div>
          </div>
        ))}
      </div>

      <div style={{ display: "flex", gap: 16, marginBottom: 14, fontSize: 12, color: "#94a3b8", flexWrap: "wrap" }}>
        <span>Avg FRT: <strong style={{ color: "#e2e8f0" }}>{agent.avgFRT}m</strong></span>
        <span>Median: <strong style={{ color: "#e2e8f0" }}>{agent.medianFRT}m</strong></span>
        <span>🏆 <strong style={{ color: "#22c55e" }}>{agent.fastest.time}m</strong> ({agent.fastest.ticket})</span>
        <span>🐢 <strong style={{ color: "#ef4444" }}>{agent.slowest.time}m</strong> ({agent.slowest.ticket})</span>
      </div>

      {agent.breaches.length > 0 && (
        <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 12, padding: "6px 10px", background: "rgba(239,68,68,0.08)", borderRadius: 6 }}>
          ⚠️ SLA breaches: {agent.breaches.join(", ")}
        </div>
      )}

      {agentActions.length > 0 && (
        <div>
          <div style={{ fontSize: 11, fontWeight: 600, color: "#64748b", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.05em" }}>Action Items</div>
          {agentActions.map((item, i) => (
            <div key={i} style={{
              display: "flex", gap: 10, padding: "8px 12px", marginBottom: 6,
              background: "rgba(255,255,255,0.02)", borderRadius: 8,
              borderLeft: `3px solid ${{ high: "#ef4444", medium: "#f59e0b", low: "#22c55e" }[item.priority]}`,
            }}>
              <PriorityBadge priority={item.priority} />
              <span style={{ fontSize: 12, color: "#cbd5e1", lineHeight: 1.5 }}>{item.text}</span>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function ProductRow({ product, maxTickets }) {
  const pct = (product.tickets / maxTickets) * 100;
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{ width: 10, height: 10, borderRadius: 3, background: product.color }} />
          <span style={{ fontSize: 13, fontWeight: 600, color: "#e2e8f0" }}>{product.name}</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 12, color: "#94a3b8" }}>{product.tickets} tickets ({product.pct}%)</span>
          <SentimentBadge sentiment={product.sentiment} />
        </div>
      </div>
      <div style={{ height: 24, background: "rgba(255,255,255,0.04)", borderRadius: 6, overflow: "hidden", position: "relative" }}>
        <div style={{
          width: `${pct}%`, height: "100%",
          background: `linear-gradient(90deg, ${product.color}66, ${product.color}cc)`,
          borderRadius: 6, transition: "width 0.8s ease",
        }} />
        <span style={{ position: "absolute", left: 10, top: 4, fontSize: 11, color: "#fff", fontWeight: 600 }}>
          {product.resolved}/{product.tickets} resolved · Avg FRT: {product.avgFRT}m
        </span>
      </div>
      <div style={{ display: "flex", gap: 6, marginTop: 6, flexWrap: "wrap" }}>
        {product.issues.map((issue, i) => (
          <span key={i} style={{
            fontSize: 10, padding: "3px 8px", borderRadius: 6,
            background: "rgba(255,255,255,0.05)", color: "#94a3b8",
          }}>• {issue}</span>
        ))}
      </div>
    </div>
  );
}

// ============================================================
// TAB: COMMUNITY PULSE
// ============================================================

function CommunityPulseTab() {
  const [selectedTopic, setSelectedTopic] = useState(null);
  const [filter, setFilter] = useState("all");
  const data = COMMUNITY_DATA;
  const selected = data.topics.find(t => t.id === selectedTopic);
  const sentimentCounts = {
    positive: data.topics.filter(t => t.sentiment === "positive").length,
    mixed: data.topics.filter(t => t.sentiment === "mixed").length,
    negative: data.topics.filter(t => t.sentiment === "negative").length,
    neutral: data.topics.filter(t => t.sentiment === "neutral").length,
  };

  return (
    <>
      {/* Stats Row */}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 20 }}>
        {[
          { label: "Total Messages", value: data.totalMessages, color: "#6366f1" },
          { label: "Active Users", value: data.activeUsers, color: "#06b6d4" },
          { label: "Topics Detected", value: data.topics.length, color: "#f59e0b" },
          { label: "Action Items", value: data.actionItems.length, color: "#ef4444" },
        ].map((s, i) => (
          <div key={i} style={{
            flex: "1 1 140px", padding: "16px 18px",
            background: "rgba(255,255,255,0.03)", borderRadius: 12,
            border: "1px solid rgba(255,255,255,0.06)",
          }}>
            <div style={{ fontSize: 11, color: "#64748b", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.05em" }}>{s.label}</div>
            <div style={{ fontSize: 26, fontWeight: 700, color: s.color }}>{s.value}</div>
          </div>
        ))}
      </div>

      {/* Main Grid */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 320px", gap: 16 }}>
        <div>
          <Card style={{ marginBottom: 16 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12, flexWrap: "wrap", gap: 8 }}>
              <div>
                <CardTitle title="What the Community is Talking About" subtitle="Bubble size = message volume · Color = sentiment · Click to inspect" />
              </div>
              <div style={{ display: "flex", gap: 6 }}>
                {["all", "positive", "mixed", "negative"].map(f => (
                  <button key={f} onClick={() => { setFilter(f); setSelectedTopic(null); }} style={{
                    padding: "5px 12px", fontSize: 11, fontWeight: 600, border: "1px solid",
                    borderColor: filter === f ? (f === "all" ? "#6366f1" : SENTIMENT_COLORS[f]) : "rgba(255,255,255,0.1)",
                    background: filter === f ? (f === "all" ? "#6366f144" : `${SENTIMENT_COLORS[f]}22`) : "transparent",
                    color: filter === f ? (f === "all" ? "#a5b4fc" : SENTIMENT_COLORS[f]) : "#64748b",
                    borderRadius: 8, cursor: "pointer", textTransform: "capitalize",
                  }}>{f}</button>
                ))}
              </div>
            </div>
            <BubbleChart topics={data.topics} selectedTopic={selectedTopic} onSelect={setSelectedTopic} filter={filter} />
            <div style={{ display: "flex", gap: 16, justifyContent: "center", padding: "8px 0 4px", flexWrap: "wrap" }}>
              {Object.entries(SENTIMENT_COLORS).filter(([k]) => k !== "neutral").map(([key, color]) => (
                <div key={key} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <div style={{ width: 10, height: 10, borderRadius: "50%", background: color }} />
                  <span style={{ fontSize: 11, color: "#94a3b8", textTransform: "capitalize" }}>{key}</span>
                </div>
              ))}
            </div>
          </Card>

          {selected && (
            <div style={{
              background: `${SENTIMENT_COLORS[selected.sentiment]}11`,
              border: `1px solid ${SENTIMENT_COLORS[selected.sentiment]}33`,
              borderRadius: 12, padding: 18, marginBottom: 16,
            }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                <h3 style={{ fontSize: 15, fontWeight: 600, margin: 0, color: "#f1f5f9" }}>{selected.label.replace("\n", " ")}</h3>
                <SentimentBadge sentiment={selected.sentiment} />
              </div>
              <div style={{ display: "flex", gap: 20, fontSize: 12, color: "#94a3b8" }}>
                <span><strong style={{ color: "#cbd5e1" }}>{selected.count}</strong> messages</span>
                <span>Channels: {selected.channels.join(", ")}</span>
              </div>
            </div>
          )}

          <Card style={{ marginBottom: 16 }}>
            <CardTitle title="Sentiment Over Time (UTC)" subtitle="Message volume by sentiment per 2-hour window" />
            <SentimentTimeline data={data.sentimentTimeline} />
            <div style={{ display: "flex", gap: 16, justifyContent: "center", marginTop: 8 }}>
              {[{ label: "Positive", color: "#22c55e" }, { label: "Neutral", color: "#94a3b8" }, { label: "Negative", color: "#ef4444" }].map(l => (
                <div key={l.label} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <div style={{ width: 16, height: 3, borderRadius: 2, background: l.color }} />
                  <span style={{ fontSize: 11, color: "#94a3b8" }}>{l.label}</span>
                </div>
              ))}
            </div>
          </Card>
        </div>

        <div>
          <Card style={{ marginBottom: 16 }}>
            <CardTitle title="Channel Activity" />
            {data.channelBreakdown.map((ch, i) => {
              const pct = (ch.messages / Math.max(...data.channelBreakdown.map(c => c.messages))) * 100;
              return (
                <div key={i} style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
                  <span style={{ width: 130, fontSize: 13, color: "#cbd5e1", fontFamily: "'JetBrains Mono', monospace", flexShrink: 0 }}>{ch.name}</span>
                  <div style={{ flex: 1, height: 22, background: "rgba(255,255,255,0.04)", borderRadius: 4, overflow: "hidden", position: "relative" }}>
                    <div style={{ width: `${pct}%`, height: "100%", background: `linear-gradient(90deg, ${SENTIMENT_COLORS[ch.sentiment]}44, ${SENTIMENT_COLORS[ch.sentiment]}aa)`, borderRadius: 4 }} />
                    <span style={{ position: "absolute", right: 8, top: 3, fontSize: 11, color: "#94a3b8" }}>{ch.messages}</span>
                  </div>
                  <SentimentBadge sentiment={ch.sentiment} />
                </div>
              );
            })}
          </Card>

          <Card style={{ marginBottom: 16 }}>
            <CardTitle title="🔧 Action Items" subtitle="AI-generated from today's discussions" />
            {data.actionItems.map((item, i) => (
              <div key={i} style={{
                display: "flex", gap: 12, padding: "12px 16px", marginBottom: 8,
                background: "rgba(255,255,255,0.03)", borderRadius: 10,
                borderLeft: `3px solid ${{ high: "#ef4444", medium: "#f59e0b", low: "#22c55e" }[item.priority]}`,
              }}>
                <PriorityBadge priority={item.priority} />
                <div>
                  <div style={{ fontSize: 13, color: "#e2e8f0", lineHeight: 1.5 }}>{item.text}</div>
                  <div style={{ fontSize: 11, color: "#64748b", marginTop: 4 }}>Related: {item.topic}</div>
                </div>
              </div>
            ))}
          </Card>

          <Card>
            <CardTitle title="Topic Sentiment Split" />
            <div style={{ display: "flex", gap: 4, height: 32, borderRadius: 8, overflow: "hidden", marginBottom: 12 }}>
              {Object.entries(sentimentCounts).filter(([_, v]) => v > 0).map(([key, val]) => (
                <div key={key} style={{
                  flex: val, background: SENTIMENT_COLORS[key], opacity: 0.8,
                  display: "flex", alignItems: "center", justifyContent: "center",
                  fontSize: 11, fontWeight: 600, color: "#fff", minWidth: 30,
                }}>{val}</div>
              ))}
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
              {Object.entries(sentimentCounts).filter(([_, v]) => v > 0).map(([key, val]) => (
                <div key={key} style={{ display: "flex", alignItems: "center", gap: 5 }}>
                  <div style={{ width: 8, height: 8, borderRadius: "50%", background: SENTIMENT_COLORS[key] }} />
                  <span style={{ fontSize: 11, color: "#94a3b8", textTransform: "capitalize" }}>{key}: {val}</span>
                </div>
              ))}
            </div>
          </Card>
        </div>
      </div>

      {/* Market News */}
      <Card style={{ marginTop: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
          <div>
            <CardTitle title="📰 Market News — Most Discussed" subtitle="Topics that sparked the most community reactions in #degen-speculation" />
          </div>
          <span style={{ fontSize: 11, color: "#64748b", fontFamily: "'JetBrains Mono', monospace" }}>#degen-speculation</span>
        </div>
        {data.marketNews.map((news, i) => (
          <div key={i} style={{
            display: "flex", alignItems: "center", gap: 14, padding: "12px 16px", marginBottom: 8,
            background: i === 0 ? "rgba(99,102,241,0.08)" : "rgba(255,255,255,0.02)",
            borderRadius: 10, border: i === 0 ? "1px solid rgba(99,102,241,0.2)" : "1px solid rgba(255,255,255,0.04)",
          }}>
            <span style={{ fontSize: 22, flexShrink: 0 }}>{news.emoji}</span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 13, color: "#e2e8f0", fontWeight: 500, lineHeight: 1.4 }}>{news.headline}</div>
              <div style={{ display: "flex", gap: 12, marginTop: 4, fontSize: 11, color: "#64748b" }}>
                <span>{news.mentions} users mentioned this</span><span>·</span><span>{news.timeAgo}</span>
              </div>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 16, flexShrink: 0 }}>
              <div style={{ textAlign: "center" }}>
                <div style={{ fontSize: 18, fontWeight: 700, color: "#f1f5f9" }}>{news.reactions}</div>
                <div style={{ fontSize: 10, color: "#64748b" }}>reactions</div>
              </div>
              <SentimentBadge sentiment={news.sentiment} />
            </div>
          </div>
        ))}
      </Card>
    </>
  );
}

// ============================================================
// TAB: SUPPORT HEALTH
// ============================================================

function SupportHealthTab() {
  const data = SUPPORT_DATA;
  const maxTickets = Math.max(...data.productBreakdown.map(p => p.tickets));

  return (
    <>
      {/* Stats Row */}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 20 }}>
        {[
          { label: "Total Tickets", value: data.totalTickets, color: "#6366f1" },
          { label: "Resolved", value: data.resolved, color: "#22c55e" },
          { label: "Still Open", value: data.open, color: "#f59e0b" },
          { label: "Avg FRT", value: `${data.avgFRT}m`, color: "#06b6d4" },
          { label: "SLA Compliance", value: `${data.slaCompliance}%`, color: data.slaCompliance >= 90 ? "#22c55e" : "#f59e0b" },
        ].map((s, i) => (
          <div key={i} style={{
            flex: "1 1 120px", padding: "16px 18px",
            background: "rgba(255,255,255,0.03)", borderRadius: 12,
            border: "1px solid rgba(255,255,255,0.06)",
          }}>
            <div style={{ fontSize: 11, color: "#64748b", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.05em" }}>{s.label}</div>
            <div style={{ fontSize: 26, fontWeight: 700, color: s.color }}>{s.value}</div>
          </div>
        ))}
      </div>

      {/* Product Breakdown + Donut */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 320px", gap: 16, marginBottom: 16 }}>
        <Card>
          <CardTitle title="📦 Ticket Volume by Product" subtitle="What users need help with — distribution across product areas" />
          {data.productBreakdown.map((p, i) => <ProductRow key={i} product={p} maxTickets={maxTickets} />)}
        </Card>

        <div>
          <Card style={{ marginBottom: 16 }}>
            <CardTitle title="Product Distribution" subtitle="Share of total tickets" />
            <DonutChart data={data.productBreakdown} />
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8, justifyContent: "center", marginTop: 14 }}>
              {data.productBreakdown.map((p, i) => (
                <div key={i} style={{ display: "flex", alignItems: "center", gap: 5 }}>
                  <div style={{ width: 8, height: 8, borderRadius: 3, background: p.color }} />
                  <span style={{ fontSize: 11, color: "#94a3b8" }}>{p.name} ({p.pct}%)</span>
                </div>
              ))}
            </div>
          </Card>

          {/* Resolution Rate */}
          <Card>
            <CardTitle title="Resolution Rate" />
            <div style={{ position: "relative", height: 28, background: "rgba(255,255,255,0.04)", borderRadius: 8, overflow: "hidden", marginBottom: 8 }}>
              <div style={{
                width: `${(data.resolved / data.totalTickets) * 100}%`, height: "100%",
                background: "linear-gradient(90deg, #22c55e88, #22c55ecc)", borderRadius: 8,
              }} />
              <span style={{ position: "absolute", left: "50%", top: 6, transform: "translateX(-50%)", fontSize: 12, fontWeight: 600, color: "#fff" }}>
                {data.resolved}/{data.totalTickets} ({Math.round((data.resolved / data.totalTickets) * 100)}%)
              </span>
            </div>
            <div style={{ fontSize: 11, color: "#64748b", textAlign: "center" }}>
              Median FRT: <strong style={{ color: "#e2e8f0" }}>{data.medianFRT}m</strong> · Target: ≤15m
            </div>
          </Card>
        </div>
      </div>

      {/* Agent Performance */}
      <div style={{ marginBottom: 16 }}>
        <h2 style={{ fontSize: 18, fontWeight: 700, color: "#f1f5f9", margin: "0 0 4px" }}>👤 Agent Performance</h2>
        <p style={{ fontSize: 12, color: "#64748b", margin: "0 0 16px" }}>Per-agent metrics, SLA compliance, and personalized action items</p>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))", gap: 16 }}>
          {data.agents.map((agent, i) => (
            <AgentCard key={i} agent={agent} actions={data.agentActions.find(a => a.agent === agent.name)} />
          ))}
        </div>
      </div>

      {/* Open Tickets */}
      <Card>
        <CardTitle title="⏳ Open Tickets — Needs Action" subtitle={`${data.openTickets.length} tickets still unresolved`} />
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ borderBottom: "1px solid rgba(255,255,255,0.08)" }}>
                {["Ticket", "Age", "Product", "Status", "On-Duty", ""].map((h, i) => (
                  <th key={i} style={{ textAlign: "left", padding: "8px 12px", color: "#64748b", fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.05em" }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.openTickets.map((t, i) => {
                const sevColor = { high: "#ef4444", medium: "#f59e0b", low: "#22c55e" }[t.severity];
                return (
                  <tr key={i} style={{ borderBottom: "1px solid rgba(255,255,255,0.04)" }}>
                    <td style={{ padding: "10px 12px", color: "#e2e8f0", fontWeight: 600, fontFamily: "'JetBrains Mono', monospace" }}>{t.id}</td>
                    <td style={{ padding: "10px 12px", color: "#e2e8f0" }}>{t.age}</td>
                    <td style={{ padding: "10px 12px", color: "#94a3b8" }}>{t.product}</td>
                    <td style={{ padding: "10px 12px", color: t.status === "No response" ? "#ef4444" : "#f59e0b" }}>{t.status}</td>
                    <td style={{ padding: "10px 12px", color: "#94a3b8" }}>{t.onDuty}</td>
                    <td style={{ padding: "10px 12px" }}>
                      <div style={{ width: 8, height: 8, borderRadius: "50%", background: sevColor }} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>
    </>
  );
}

// ============================================================
// MAIN DASHBOARD
// ============================================================

export default function Dashboard() {
  const [tab, setTab] = useState("community");
  const tabs = [
    { id: "community", label: "🎯 Community Pulse", icon: "📡" },
    { id: "support", label: "🛡️ Support Health", icon: "📊" },
  ];

  return (
    <div style={{
      minHeight: "100vh", background: "#0a0e1a",
      fontFamily: "'DM Sans', sans-serif", color: "#e2e8f0",
      padding: "24px 20px",
    }}>
      <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />

      {/* Header */}
      <div style={{ maxWidth: 1100, margin: "0 auto 20px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 4 }}>
          <div style={{
            width: 10, height: 10, borderRadius: "50%",
            background: "#22c55e", boxShadow: "0 0 8px #22c55e88",
            animation: "pulse 2s infinite",
          }} />
          <span style={{ fontSize: 11, color: "#64748b", textTransform: "uppercase", letterSpacing: "0.1em" }}>Live Dashboard</span>
        </div>
        <h1 style={{ fontSize: 28, fontWeight: 700, margin: "4px 0 6px", color: "#f1f5f9", letterSpacing: "-0.02em" }}>
          Kyber<span style={{ color: "#22c55e" }}>Swap</span> — {tab === "community" ? "Community Pulse" : "Support Health"}
        </h1>
        <p style={{ fontSize: 13, color: "#64748b", margin: "0 0 16px" }}>
          {tab === "community" ? COMMUNITY_DATA.period : SUPPORT_DATA.period} · Updated {tab === "community" ? COMMUNITY_DATA.lastUpdated : SUPPORT_DATA.lastUpdated}
        </p>

        {/* Tab Switcher */}
        <div style={{ display: "flex", gap: 4, background: "rgba(255,255,255,0.04)", borderRadius: 12, padding: 4, width: "fit-content" }}>
          {tabs.map(t => (
            <button key={t.id} onClick={() => setTab(t.id)} style={{
              padding: "10px 20px", fontSize: 13, fontWeight: 600, border: "none",
              borderRadius: 10, cursor: "pointer", transition: "all 0.2s",
              background: tab === t.id ? "rgba(99,102,241,0.2)" : "transparent",
              color: tab === t.id ? "#a5b4fc" : "#64748b",
              boxShadow: tab === t.id ? "0 0 0 1px rgba(99,102,241,0.3)" : "none",
            }}>
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {/* Tab Content */}
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        {tab === "community" ? <CommunityPulseTab /> : <SupportHealthTab />}
      </div>

      {/* Footer */}
      <div style={{ maxWidth: 1100, margin: "24px auto 0", textAlign: "center" }}>
        <p style={{ fontSize: 11, color: "#475569" }}>
          Powered by AI summarization (Gemini 2.5 Flash) · Privacy-first: no usernames sent to LLM · Built with Claude Code
        </p>
      </div>

      <style>{`
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
        * { box-sizing: border-box; }
        @media (max-width: 800px) {
          div[style*="grid-template-columns"] { grid-template-columns: 1fr !important; }
        }
      `}</style>
    </div>
  );
}
