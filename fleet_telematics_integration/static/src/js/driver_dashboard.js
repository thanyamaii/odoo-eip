/** @odoo-module **/
// static/src/js/driver_dashboard.js
// Driver Dashboard OWL Component — FDD §12.6
//
// แสดง:
//   1) Scorecard รายพนักงาน (avatar + tier badge + คะแนนเฉลี่ย)
//   2) Trend กราฟคะแนนรายเดือน (Chart.js)
//   3) Energy KPI (น้ำมัน, idle time, ระยะทาง, harsh events)
//   4) กดชื่อคนขับ → ดู Trip Log ของคนนั้น

import { Component, useState, onMounted, useRef } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const TIER_CONFIG = {
    A: { label: "A — Excellent",         color: "#15803d", bg: "#dcfce7" },
    B: { label: "B — Good",              color: "#1d4ed8", bg: "#dbeafe" },
    C: { label: "C — Fair",              color: "#d97706", bg: "#fef3c7" },
    D: { label: "D — Needs Improvement", color: "#b91c1c", bg: "#fee2e2" },
};

function getTier(score, cfg) {
    if (!cfg) return "D";
    if (score >= (cfg.tier_a_min_score || 90)) return "A";
    if (score >= (cfg.tier_b_min_score || 75)) return "B";
    if (score >= (cfg.tier_c_min_score || 60)) return "C";
    return "D";
}

async function odooRpc(route, params = {}) {
    const res = await fetch(route, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        },
        body: JSON.stringify({ jsonrpc: "2.0", method: "call", params }),
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error.data?.message || data.error.message);
    return data.result;
}

export class DriverDashboard extends Component {
    static template = "fleet_telematics_integration.DriverDashboard";

    setup() {
        this.chartRef      = useRef("trendChart");
        this.actionService = useService("action");
        this.state = useState({
            loading:        true,
            error:          null,
            drivers:        [],         // [{driver_id, name, avg_score, tier, trips, distance, fuel, idle, harsh}]
            selectedDriver: null,       // driver_id ที่เลือกดู trend
            trendData:      [],         // [{month, avg_score}]
            energyKPI:      null,       // {total_distance, total_fuel, total_idle, total_harsh}
            scoringConfig:  null,
        });
        this.chart = null;

        onMounted(async () => {
            await this._loadDashboard();
        });
    }

    async _loadDashboard() {
        this.state.loading = true;
        this.state.error   = null;
        try {
            // 1) ดึง Scoring Config สำหรับ tier thresholds
            const cfgResult = await odooRpc("/web/dataset/call_kw", {
                model:  "fleet.telematics.scoring.config",
                method: "search_read",
                args:   [[["active", "=", true]]],
                kwargs: {
                    fields:  ["tier_a_min_score", "tier_b_min_score", "tier_c_min_score"],
                    limit:   1,
                },
            });
            this.state.scoringConfig = cfgResult?.[0] || null;

            // 2) ดึง aggregate ต่อคนขับ
            const logs = await odooRpc("/web/dataset/call_kw", {
                model:  "fleet.telematics.log",
                method: "read_group",
                args:   [[["state", "=", "synced"]], ["driver_id", "driver_score:avg", "distance_km:sum", "fuel_used_est:sum", "idle_min:sum", "harsh_brake_count:sum", "harsh_accel_count:sum", "harsh_corner_count:sum"], ["driver_id"]],
                kwargs: { orderby: "driver_score desc" },
            });

            this.state.drivers = (logs || [])
                .filter(r => r.driver_id)
                .map(r => ({
                    driver_id:   r.driver_id[0],
                    name:        r.driver_id[1],
                    avg_score:   Math.round((r.driver_score || 0) * 100) / 100,
                    trips:       r.driver_id_count || 0,
                    distance:    Math.round(r.distance_km || 0),
                    fuel:        Math.round((r.fuel_used_est || 0) * 10) / 10,
                    idle:        Math.round(r.idle_min || 0),
                    harsh:       (r.harsh_brake_count || 0) + (r.harsh_accel_count || 0) + (r.harsh_corner_count || 0),
                    tier:        getTier(r.driver_score || 0, this.state.scoringConfig),
                }));

            // 3) Energy KPI รวมทั้งฟลีท
            const total = this.state.drivers.reduce((acc, d) => ({
                distance: acc.distance + d.distance,
                fuel:     acc.fuel     + d.fuel,
                idle:     acc.idle     + d.idle,
                harsh:    acc.harsh    + d.harsh,
            }), { distance: 0, fuel: 0, idle: 0, harsh: 0 });
            this.state.energyKPI = total;

            // 4) โหลด trend ของคนแรก
            if (this.state.drivers.length > 0) {
                await this._loadTrend(this.state.drivers[0].driver_id);
            }

        } catch (e) {
            this.state.error = "โหลดข้อมูลไม่สำเร็จ: " + (e.message || e);
        } finally {
            this.state.loading = false;
        }
    }

    async _loadTrend(driverId) {
        this.state.selectedDriver = driverId;
        try {
            const trend = await odooRpc("/web/dataset/call_kw", {
                model:  "fleet.telematics.log",
                method: "read_group",
                args:   [
                    [["driver_id", "=", driverId], ["state", "=", "synced"]],
                    ["driver_score:avg", "trip_start"],
                    ["trip_start:month"],
                ],
                kwargs: { orderby: "trip_start asc", limit: 12 },
            });
            this.state.trendData = (trend || []).map(r => ({
                month: r.trip_start,
                score: Math.round((r.driver_score || 0) * 10) / 10,
            }));
            this._renderChart();
        } catch (e) {
            this.state.trendData = [];
        }
    }

    _renderChart() {
        if (!this.chartRef.el) return;
        const ctx = this.chartRef.el.getContext("2d");
        if (!ctx) return;

        if (this.chart) { this.chart.destroy(); this.chart = null; }

        // ใช้ Chart.js ที่โหลดผ่าน CDN
        if (!window.Chart) {
            const s = document.createElement("script");
            s.src = "https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js";
            s.onload = () => this._renderChart();
            document.head.appendChild(s);
            return;
        }

        const labels = this.state.trendData.map(d => d.month);
        const scores = this.state.trendData.map(d => d.score);

        this.chart = new window.Chart(ctx, {
            type: "line",
            data: {
                labels,
                datasets: [{
                    label:           "Driver Score",
                    data:            scores,
                    borderColor:     "#3b82f6",
                    backgroundColor: "rgba(59,130,246,0.08)",
                    tension:         0.3,
                    fill:            true,
                    pointRadius:     4,
                    pointBackgroundColor: "#3b82f6",
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    y: { min: 0, max: 100, title: { display: true, text: "Score" } },
                    x: { title: { display: true, text: "เดือน" } },
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: ctx => `Score: ${ctx.parsed.y}`
                        },
                    },
                },
            },
        });
    }

    onDriverClick(driverId) {
        this._loadTrend(driverId);
    }

    onViewTrips(driverId, ev) {
        ev.stopPropagation();
        this.actionService.doAction({
            type:      "ir.actions.act_window",
            name:      "Trip Logs",
            res_model: "fleet.telematics.log",
            views:     [[false, "list"], [false, "form"]],
            domain:    [["driver_id", "=", driverId]],
            context:   { search_default_driver_id: driverId },
        });
    }

    getTierConfig(tier) {
        return TIER_CONFIG[tier] || TIER_CONFIG["D"];
    }
}

registry.category("actions").add("fleet_telematics_driver_dashboard", DriverDashboard);
