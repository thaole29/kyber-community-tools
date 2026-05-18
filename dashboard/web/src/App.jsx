import { useState, useEffect, useRef } from "react";
import * as d3 from "d3";

// ============================================================
// Constants — colors mirror the prototype
// ============================================================

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
  const color = SENTIMENT_COLORS[sentiment] || SENTIMENT_COLORS.neutral;
  return (
    <span style={{
      fontSize: 10, padding: "3px 10px", borderRadius: 10,
      background: `${color}22`,
      color: color, fontWeight: 600, whiteSpace: "nowrap", ...style,
    }}>{sentiment}</span>
  );
}

function PriorityBadge({ priority }) {
  const colors = { high: "#ef4444", medium: "#f59e0b", low: "#22c55e" };
  return (
    <span style={{
      fontSize: 10, fontWeight: 700, textTransform: "uppercase",
      color: colors[priority] || "#94a3b8", flexShrink: 0, marginTop: 2, letterSpacing: "0.05em",
    }}>{priority}</span>
  );
}

function EmptyState({ message }) {
  return (
    <div style={{
      padding: "32px 16px", textAlign: "center",
      color: "#64748b", fontSize: 13,
    }}>
      {message || "No data available yet."}
    </div>
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
    const radiusScale = d3.scaleSqrt().domain([0, d3.max(filtered, d => d.count) || 1]).range([20, Math.min(width, height) * 0.12]);
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
      const c = d3.color(SENTIMENT_COLORS[d.sentiment] || SENTIMENT_COLORS.neutral);
      grad.append("stop").attr("offset", "0%").attr("stop-color", c.brighter(0.8));
      grad.append("stop").attr("offset", "100%").attr("stop-color", SENTIMENT_COLORS[d.sentiment] || SENTIMENT_COLORS.neutral);
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
  }, [filtered, dims, selectedTopic, filter, onSelect]);

  if (filtered.length === 0) {
    return <EmptyState message="No topics match the current filter." />;
  }
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
    if (!svgRef.current || !data || data.length === 0) return;
    const svg = d3.select(svgRef.current); svg.selectAll("*").remove();
    const margin = { top: 10, right: 15, bottom: 30, left: 30 };
    const w = width - margin.left - margin.right, ih = 140;
    const g = svg.append("g").attr("transform", `translate(${margin.left},${margin.top})`);
    const x = d3.scalePoint().domain(data.map(d => d.hour)).range([0, w]).padding(0.5);
    const maxVal = d3.max(data, d => Math.max(d.positive, d.neutral, d.negative)) || 1;
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

function DonutChart({ data, total }) {
  const svgRef = useRef(null);
  useEffect(() => {
    if (!svgRef.current || !data || data.length === 0) return;
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
      .attr("font-family", "'DM Sans', sans-serif").text(String(total ?? 0));
    g.append("text").attr("text-anchor", "middle").attr("dy", "1.4em")
      .attr("fill", "#64748b").attr("font-size", "11px")
      .attr("font-family", "'DM Sans', sans-serif").text("total tickets");
  }, [data, total]);
  return <svg ref={svgRef} width={220} height={220} style={{ display: "block", margin: "0 auto" }} />;
}

function AgentCard({ agent, actions }) {
  const complianceColor = agent.slaCompliance >= 95 ? "#22c55e" : agent.slaCompliance >= 80 ? "#f59e0b" : "#ef4444";
  const agentActions = actions?.items || [];
  const breaches = agent.breaches || [];
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
        <span>🏆 <strong style={{ color: "#22c55e" }}>{agent.fastest?.time ?? "-"}m</strong> ({agent.fastest?.ticket ?? "-"})</span>
        <span>🐢 <strong style={{ color: "#ef4444" }}>{agent.slowest?.time ?? "-"}m</strong> ({agent.slowest?.ticket ?? "-"})</span>
      </div>

      {breaches.length > 0 && (
        <div style={{ fontSize: 11, color: "#ef4444", marginBottom: 12, padding: "6px 10px", background: "rgba(239,68,68,0.08)", borderRadius: 6 }}>
          ⚠️ SLA breaches: {breaches.join(", ")}
        </div>
      )}

      {agentActions.length > 0 && (
        <div>
          <div style={{ fontSize: 11, fontWeight: 600, color: "#64748b", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.05em" }}>Action Items</div>
          {agentActions.map((item, i) => (
            <div key={i} style={{
              display: "flex", gap: 10, padding: "8px 12px", marginBottom: 6,
              background: "rgba(255,255,255,0.02)", borderRadius: 8,
              borderLeft: `3px solid ${{ high: "#ef4444", medium: "#f59e0b", low: "#22c55e" }[item.priority] || "#94a3b8"}`,
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
  const pct = maxTickets ? (product.tickets / maxTickets) * 100 : 0;
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{ width: 10, height: 10, borderRadius: 3, background: product.color }} />
          <span style={{ fontSize: 13, fontWeight: 600, color: "#e2e8f0" }}>{product.name}</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ fontSize: 12, color: "#94a3b8" }}>{product.tickets} tickets ({product.pct}%)</span>
          {/* Sentiment badge hidden in Support Health — current value is SLA
              health (not real user sentiment) and is misleading. Re-enable
              once we wire up LLM-based user_sentiment. */}
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
        {(product.issues || []).map((issue, i) => (
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

function CommunityPulseTab({ data }) {
  const [selectedTopic, setSelectedTopic] = useState(null);
  const [filter, setFilter] = useState("all");
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
      <div data-responsive-grid style={{ display: "grid", gridTemplateColumns: "1fr 320px", gap: 16 }}>
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
            {data.topics.length > 0 ? (
              <BubbleChart topics={data.topics} selectedTopic={selectedTopic} onSelect={setSelectedTopic} filter={filter} />
            ) : (
              <EmptyState message="No topics yet — run community_digest.py to populate." />
            )}
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
                <span>Channels: {(selected.channels || []).join(", ")}</span>
              </div>
            </div>
          )}

          <Card style={{ marginBottom: 16 }}>
            <CardTitle title="Sentiment Over Time (UTC)" subtitle="Estimated 2-hour activity curve (per-message timestamps not stored)" />
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
            {data.channelBreakdown.length === 0 && <EmptyState message="No channel activity captured." />}
            {data.channelBreakdown.map((ch, i) => {
              const maxMsgs = Math.max(...data.channelBreakdown.map(c => c.messages), 1);
              const pct = (ch.messages / maxMsgs) * 100;
              return (
                <div key={i} style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
                  <span style={{ width: 130, fontSize: 13, color: "#cbd5e1", fontFamily: "'JetBrains Mono', monospace", flexShrink: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{ch.name}</span>
                  <div style={{ flex: 1, height: 22, background: "rgba(255,255,255,0.04)", borderRadius: 4, overflow: "hidden", position: "relative" }}>
                    <div style={{ width: `${pct}%`, height: "100%", background: `linear-gradient(90deg, ${SENTIMENT_COLORS[ch.sentiment] || SENTIMENT_COLORS.neutral}44, ${SENTIMENT_COLORS[ch.sentiment] || SENTIMENT_COLORS.neutral}aa)`, borderRadius: 4 }} />
                    <span style={{ position: "absolute", right: 8, top: 3, fontSize: 11, color: "#94a3b8" }}>{ch.messages}</span>
                  </div>
                  <SentimentBadge sentiment={ch.sentiment} />
                </div>
              );
            })}
          </Card>

          <Card style={{ marginBottom: 16 }}>
            <CardTitle title="🔧 Action Items" subtitle="AI-generated from today's discussions" />
            {data.actionItems.length === 0 && <EmptyState message="No action items detected." />}
            {data.actionItems.map((item, i) => (
              <div key={i} style={{
                display: "flex", gap: 12, padding: "12px 16px", marginBottom: 8,
                background: "rgba(255,255,255,0.03)", borderRadius: 10,
                borderLeft: `3px solid ${{ high: "#ef4444", medium: "#f59e0b", low: "#22c55e" }[item.priority] || "#94a3b8"}`,
              }}>
                <PriorityBadge priority={item.priority} />
                <div>
                  <div style={{ fontSize: 13, color: "#e2e8f0", lineHeight: 1.5 }}>{item.text}</div>
                  <div style={{ fontSize: 11, color: "#64748b", marginTop: 4 }}>Channel: {item.topic}</div>
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
            <CardTitle title="📰 Market News — Most Discussed" subtitle="Themes from market/speculation channel — counts come from LLM digest" />
          </div>
          <span style={{ fontSize: 11, color: "#64748b", fontFamily: "'JetBrains Mono', monospace" }}>#degen-speculation</span>
        </div>
        {data.marketNews.length === 0 && <EmptyState message="No market news themes captured today." />}
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
                <span>{news.mentions} mentions</span><span>·</span><span>{news.timeAgo}</span>
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

function SupportHealthTab({ data }) {
  const maxTickets = Math.max(...data.productBreakdown.map(p => p.tickets), 1);

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
      <div data-responsive-grid style={{ display: "grid", gridTemplateColumns: "1fr 320px", gap: 16, marginBottom: 16 }}>
        <Card>
          <CardTitle title="📦 Ticket Volume by Product" subtitle="Auto-classified from first user message keywords" />
          {data.productBreakdown.length === 0 ? (
            <EmptyState message="No tickets in the last 24h." />
          ) : (
            data.productBreakdown.map((p, i) => <ProductRow key={i} product={p} maxTickets={maxTickets} />)
          )}
        </Card>

        <div>
          <Card style={{ marginBottom: 16 }}>
            <CardTitle title="Product Distribution" subtitle="Share of total tickets" />
            {data.productBreakdown.length > 0 ? (
              <>
                <DonutChart data={data.productBreakdown} total={data.totalTickets} />
                <div style={{ display: "flex", flexWrap: "wrap", gap: 8, justifyContent: "center", marginTop: 14 }}>
                  {data.productBreakdown.map((p, i) => (
                    <div key={i} style={{ display: "flex", alignItems: "center", gap: 5 }}>
                      <div style={{ width: 8, height: 8, borderRadius: 3, background: p.color }} />
                      <span style={{ fontSize: 11, color: "#94a3b8" }}>{p.name} ({p.pct}%)</span>
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <EmptyState message="No tickets to chart." />
            )}
          </Card>

          {/* Resolution Rate */}
          <Card>
            <CardTitle title="Resolution Rate" />
            <div style={{ position: "relative", height: 28, background: "rgba(255,255,255,0.04)", borderRadius: 8, overflow: "hidden", marginBottom: 8 }}>
              <div style={{
                width: `${data.totalTickets ? (data.resolved / data.totalTickets) * 100 : 0}%`, height: "100%",
                background: "linear-gradient(90deg, #22c55e88, #22c55ecc)", borderRadius: 8,
              }} />
              <span style={{ position: "absolute", left: "50%", top: 6, transform: "translateX(-50%)", fontSize: 12, fontWeight: 600, color: "#fff" }}>
                {data.resolved}/{data.totalTickets} ({data.totalTickets ? Math.round((data.resolved / data.totalTickets) * 100) : 0}%)
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
        <p style={{ fontSize: 12, color: "#64748b", margin: "0 0 16px" }}>Per-agent metrics, SLA compliance, and rule-based action items</p>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))", gap: 16 }}>
          {data.agents.map((agent, i) => (
            <AgentCard key={i} agent={agent} actions={data.agentActions.find(a => a.agent === agent.name)} />
          ))}
        </div>
      </div>

      {/* Open Tickets */}
      <Card>
        <CardTitle title="⏳ Open Tickets — Needs Action" subtitle={`${data.openTickets.length} tickets still unresolved (all-time)`} />
        {data.openTickets.length === 0 ? (
          <EmptyState message="🎉 No open tickets — backlog clear." />
        ) : (
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
        )}
      </Card>
    </>
  );
}

// ============================================================
// DATA SOURCE — dual-mode (live FastAPI or static snapshot)
// ============================================================
//
// Live mode (default): hits /api/community + /api/support on the same
//   origin as the page. Used when served by uvicorn or behind ngrok.
// Snapshot mode: hits /data/community_<range>.json + /data/support_<range>.json
//   instead. Used when deployed to GitHub Pages — the data files are
//   refreshed by scripts/generate_snapshot.py and committed daily.
//
// Auto-detect: if `/data/meta.json` exists, we are in snapshot mode.
// "Custom" date range is disabled in snapshot mode (no on-the-fly query).

function fmtUtcDate(d) {
  return d.toISOString().slice(0, 10);
}

function rangeToParams(range, custom) {
  const today = new Date();
  const todayUtc = fmtUtcDate(today);
  if (range === "24h") return null;
  if (range === "7d" || range === "30d") {
    const days = range === "7d" ? 7 : 30;
    const start = new Date(today.getTime() - (days - 1) * 86400000);
    return { start: fmtUtcDate(start), end: todayUtc };
  }
  if (range === "custom" && custom?.start && custom?.end) {
    return { start: custom.start, end: custom.end };
  }
  return null;
}

async function detectMode() {
  // Probe /data/meta.json. If it loads → snapshot mode.
  try {
    const r = await fetch("./data/meta.json", { cache: "no-store" });
    if (r.ok) {
      const meta = await r.json();
      return { mode: "snapshot", meta };
    }
  } catch (_e) { /* fall through */ }
  return { mode: "live", meta: null };
}

async function fetchData(mode, range, custom) {
  if (mode === "snapshot") {
    // Snapshot mode only knows the 3 preset ranges. Custom range falls
    // back to the longest available (30d) with a hint to the user.
    const key = ["24h", "7d", "30d"].includes(range) ? range : "30d";
    const [c, s] = await Promise.all([
      fetch(`./data/community_${key}.json`, { cache: "no-store" }).then(r => {
        if (!r.ok) throw new Error(`community_${key} ${r.status}`);
        return r.json();
      }),
      fetch(`./data/support_${key}.json`, { cache: "no-store" }).then(r => {
        if (!r.ok) throw new Error(`support_${key} ${r.status}`);
        return r.json();
      }),
    ]);
    return [c, s];
  }
  // live mode
  const params = rangeToParams(range, custom);
  const qs = params ? "?" + new URLSearchParams(params).toString() : "";
  const [c, s] = await Promise.all([
    fetch("/api/community" + qs).then(r => {
      if (!r.ok) throw new Error(`community ${r.status}`);
      return r.json();
    }),
    fetch("/api/support" + qs).then(r => {
      if (!r.ok) throw new Error(`support ${r.status}`);
      return r.json();
    }),
  ]);
  return [c, s];
}

function RangeFilter({ range, setRange, custom, setCustom, loading, mode }) {
  const presets = [
    { id: "24h", label: "24 hours" },
    { id: "7d",  label: "7 days" },
    { id: "30d", label: "30 days" },
    { id: "custom", label: "Custom" },
  ];
  const today = fmtUtcDate(new Date());
  const isSnapshot = mode === "snapshot";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 16 }}>
      <div style={{ display: "flex", gap: 4, background: "rgba(255,255,255,0.04)", borderRadius: 10, padding: 4 }}>
        {presets.map(p => {
          const disabled = isSnapshot && p.id === "custom";
          return (
            <button
              key={p.id}
              onClick={() => !disabled && setRange(p.id)}
              disabled={disabled}
              title={disabled ? "Custom range unavailable in snapshot mode" : undefined}
              style={{
                padding: "8px 14px", fontSize: 12, fontWeight: 600, border: "none",
                borderRadius: 8, cursor: disabled ? "not-allowed" : "pointer",
                transition: "all 0.15s",
                opacity: disabled ? 0.35 : 1,
                background: range === p.id ? "rgba(34,197,94,0.18)" : "transparent",
                color: range === p.id ? "#86efac" : "#64748b",
                boxShadow: range === p.id ? "0 0 0 1px rgba(34,197,94,0.3)" : "none",
              }}>{p.label}</button>
          );
        })}
      </div>
      {range === "custom" && (
        <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "#94a3b8" }}>
          <input
            type="date"
            value={custom.start}
            max={custom.end || today}
            onChange={(e) => setCustom({ ...custom, start: e.target.value })}
            style={{
              padding: "6px 10px", borderRadius: 8, border: "1px solid rgba(255,255,255,0.1)",
              background: "rgba(255,255,255,0.04)", color: "#e2e8f0", fontSize: 12,
              fontFamily: "'JetBrains Mono', monospace",
            }}
          />
          <span style={{ color: "#64748b" }}>→</span>
          <input
            type="date"
            value={custom.end}
            min={custom.start}
            max={today}
            onChange={(e) => setCustom({ ...custom, end: e.target.value })}
            style={{
              padding: "6px 10px", borderRadius: 8, border: "1px solid rgba(255,255,255,0.1)",
              background: "rgba(255,255,255,0.04)", color: "#e2e8f0", fontSize: 12,
              fontFamily: "'JetBrains Mono', monospace",
            }}
          />
        </div>
      )}
      {loading && (
        <span style={{ fontSize: 11, color: "#64748b", fontFamily: "'JetBrains Mono', monospace" }}>loading…</span>
      )}
    </div>
  );
}

// ============================================================
// MAIN DASHBOARD — fetches both endpoints; refetches when range changes
// ============================================================

export default function Dashboard() {
  const [tab, setTab] = useState("community");
  const [community, setCommunity] = useState(null);
  const [support, setSupport] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(true);
  const [mode, setMode] = useState(null); // 'live' or 'snapshot'
  const [snapshotMeta, setSnapshotMeta] = useState(null);

  const [range, setRange] = useState("24h");
  const today = fmtUtcDate(new Date());
  const sevenDaysAgo = fmtUtcDate(new Date(Date.now() - 6 * 86400000));
  const [custom, setCustom] = useState({ start: sevenDaysAgo, end: today });

  // Detect data source once on mount
  useEffect(() => {
    detectMode().then(({ mode, meta }) => {
      setMode(mode);
      setSnapshotMeta(meta);
    });
  }, []);

  useEffect(() => {
    if (mode === null) return; // wait until detectMode resolves
    if (range === "custom" && (!custom.start || !custom.end)) return;
    let cancelled = false;
    setLoading(true);
    setErr(null);
    fetchData(mode, range, custom)
      .then(([c, s]) => {
        if (cancelled) return;
        setCommunity(c);
        setSupport(s);
        setLoading(false);
      })
      .catch(e => {
        if (cancelled) return;
        setErr(String(e.message || e));
        setLoading(false);
      });
    return () => { cancelled = true; };
  }, [mode, range, custom.start, custom.end]);

  const tabs = [
    { id: "community", label: "🎯 Community Pulse" },
    { id: "support", label: "🛡️ Support Health" },
  ];

  const activeData = tab === "community" ? community : support;
  const periodLabel = activeData?.period || "Last 24 hours";
  const lastUpdated = activeData?.lastUpdated || "loading…";

  return (
    <div style={{
      minHeight: "100vh", background: "#0a0e1a",
      fontFamily: "'DM Sans', sans-serif", color: "#e2e8f0",
      padding: "24px 20px",
    }}>
      {/* Header */}
      <div style={{ maxWidth: 1100, margin: "0 auto 20px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 4 }}>
          <div style={{
            width: 10, height: 10, borderRadius: "50%",
            background: err ? "#ef4444" : "#22c55e",
            boxShadow: err ? "0 0 8px #ef444488" : "0 0 8px #22c55e88",
            animation: "pulse 2s infinite",
          }} />
          <span style={{ fontSize: 11, color: "#64748b", textTransform: "uppercase", letterSpacing: "0.1em" }}>
            {err ? "API error" : "Live Dashboard"}
          </span>
        </div>
        <h1 style={{ fontSize: 28, fontWeight: 700, margin: "4px 0 6px", color: "#f1f5f9", letterSpacing: "-0.02em" }}>
          Kyber<span style={{ color: "#22c55e" }}>Swap</span> — {tab === "community" ? "Community Pulse" : "Support Health"}
        </h1>
        <p style={{ fontSize: 13, color: "#64748b", margin: "0 0 16px" }}>
          {periodLabel} · Updated {lastUpdated}
        </p>

        {/* Tab Switcher */}
        <div style={{ display: "flex", gap: 4, background: "rgba(255,255,255,0.04)", borderRadius: 12, padding: 4, width: "fit-content", marginBottom: 14 }}>
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

        {/* Date-range filter */}
        <RangeFilter
          range={range}
          setRange={setRange}
          custom={custom}
          setCustom={setCustom}
          loading={loading}
          mode={mode}
        />

        {/* Data freshness indicator */}
        {mode === "snapshot" && snapshotMeta?.generated_at_utc && (
          <div style={{
            fontSize: 11, color: "#64748b",
            fontFamily: "'JetBrains Mono', monospace",
            marginBottom: 16,
          }}>
            📸 Snapshot mode · last refreshed {new Date(snapshotMeta.generated_at_utc).toLocaleString()}
            {" · "}data updates daily at 09:00 (UTC+7)
          </div>
        )}
      </div>

      {/* Tab Content */}
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        {err && (
          <Card style={{ borderColor: "rgba(239,68,68,0.3)", marginBottom: 16 }}>
            <div style={{ color: "#ef4444", fontSize: 14 }}>
              Failed to load dashboard data: <code style={{ fontFamily: "'JetBrains Mono', monospace" }}>{err}</code>
            </div>
            <div style={{ color: "#64748b", fontSize: 12, marginTop: 6 }}>
              Verify the FastAPI server is reachable at <code>/api/community</code> and <code>/api/support</code>.
            </div>
          </Card>
        )}
        {!err && !community && !support && (
          <Card><EmptyState message="Loading dashboard data…" /></Card>
        )}
        {!err && tab === "community" && community && <CommunityPulseTab data={community} />}
        {!err && tab === "support" && support && <SupportHealthTab data={support} />}
      </div>

      {/* Footer */}
      <div style={{ maxWidth: 1100, margin: "24px auto 0", textAlign: "center" }}>
        <p style={{ fontSize: 11, color: "#475569" }}>
          Powered by Gemini 2.5 Flash digests · SQLite-backed · Privacy-first: no usernames sent to LLM
        </p>
      </div>
    </div>
  );
}
