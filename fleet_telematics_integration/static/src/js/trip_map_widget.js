/** @odoo-module **/
// static/src/js/trip_map_widget.js
// Trip Detail Map Widget — FDD §12.6
// แสดง GPS track เส้นทางวิ่งของทริปบนแผนที่ Leaflet
// + markers จุด harsh events ระบุสี/ไอคอนตามประเภท

import { Component, onMounted, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const LEAFLET_JS  = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js";
const LEAFLET_CSS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css";

// สีตาม event type
const EVENT_COLORS = {
    harsh_brake:   "#ef4444",   // แดง
    harsh_accel:   "#f97316",   // ส้ม
    harsh_corner:  "#eab308",   // เหลือง
    speeding:      "#8b5cf6",   // ม่วง
};

function loadLeaflet() {
    if (window.L) return Promise.resolve(window.L);
    return new Promise((resolve, reject) => {
        const link = document.createElement("link");
        link.rel = "stylesheet"; link.href = LEAFLET_CSS;
        document.head.appendChild(link);
        const script = document.createElement("script");
        script.src = LEAFLET_JS;
        script.onload = () => resolve(window.L);
        script.onerror = reject;
        document.head.appendChild(script);
    });
}

export class TripMapWidget extends Component {
    static template = "fleet_telematics_integration.TripMapWidget";

    setup() {
        this.mapRef = useRef("tripMapContainer");
        this.state  = useState({ error: null, pointCount: 0 });
        this.map    = null;

        onMounted(async () => {
            try {
                const L = await loadLeaflet();
                this.map = L.map(this.mapRef.el).setView([13.7563, 100.5018], 11);
                L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
                    attribution: "&copy; OpenStreetMap contributors",
                }).addTo(this.map);
                this._drawTrack(L);
            } catch (e) {
                this.state.error = "โหลดแผนที่ไม่สำเร็จ: " + e;
            }
        });
    }

    _drawTrack(L) {
        // อ่าน GPS track จาก field gps_track_json
        const raw = this.props.gpsTrackJson || "[]";
        let points = [];
        try { points = JSON.parse(raw); } catch { return; }
        if (!points.length) return;

        // วาดเส้นทาง
        const latlngs = points.map(p => [p.lat, p.lon]);
        const polyline = L.polyline(latlngs, {
            color: "#3b82f6", weight: 3, opacity: 0.8,
        }).addTo(this.map);

        // จุดเริ่ม
        L.marker(latlngs[0], {
            icon: L.divIcon({
                className: "",
                html: `<div style="background:#22c55e;width:12px;height:12px;border-radius:50%;border:2px solid white;box-shadow:0 1px 3px rgba(0,0,0,.4)"></div>`,
                iconSize: [12, 12], iconAnchor: [6, 6],
            }),
        }).addTo(this.map).bindPopup("จุดเริ่มต้น");

        // จุดสุดท้าย
        L.marker(latlngs[latlngs.length - 1], {
            icon: L.divIcon({
                className: "",
                html: `<div style="background:#ef4444;width:12px;height:12px;border-radius:50%;border:2px solid white;box-shadow:0 1px 3px rgba(0,0,0,.4)"></div>`,
                iconSize: [12, 12], iconAnchor: [6, 6],
            }),
        }).addTo(this.map).bindPopup("จุดสิ้นสุด");

        // วาด harsh event markers
        const events = this.props.events || [];
        for (const ev of events) {
            if (!ev.lat || !ev.lon) continue;
            const color = EVENT_COLORS[ev.event_type] || "#6b7280";
            L.circleMarker([ev.lat, ev.lon], {
                radius: 6, color, fillColor: color,
                fillOpacity: 0.9, weight: 2,
            }).addTo(this.map).bindPopup(
                `<b>${ev.event_type}</b><br/>Severity: ${ev.severity}<br/>Speed: ${ev.speed_at_event} km/h`
            );
        }

        // Fit map
        this.map.fitBounds(polyline.getBounds(), { padding: [20, 20] });
        this.state.pointCount = points.length;
    }
}

registry.category("view_widgets").add("trip_map", TripMapWidget);
