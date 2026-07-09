# ==============================================================================
# __manifest__.py — Fleet Telematics Integration
# ลงทะเบียนโมดูล กำหนดข้อมูลผู้พัฒนา และ depends: fleet, hr
# ==============================================================================
{
    'name': 'Fleet Telematics Integration',
    'version': '19.0.1.0.0',
    'category': 'Fleet',
    'summary': 'Driver Behavior Monitoring, Scoring & Incentive via Telematics API',

    'author': 'Kotchasaan Technology Invention Co., Ltd.',

    'depends': [
        'fleet',
        'hr',
        'hr_contract',  # เพิ่ม 2026-07-09 — _apply_backend_bonus() ต้องใช้
                        # hr.contract.wage ดึง Base Salary แต่โมดูลนี้ไม่เคย
                        # อยู่ใน depends เลย ทำให้ self.env['hr.contract']
                        # หาโมเดลไม่เจอ (KeyError) ถ้ายังไม่เคยติดตั้งแยกไว้เอง
        'web',
        'mail',     # เพิ่ม 2026-07-06 — UC-10 Audit Log (mail.thread บน Incentive)
        'portal',   # เพิ่ม 2026-07-06 — UC-11 Self-service ผ่าน Odoo Portal
    ],

    # controllers/ ไม่ต้องระบุใน data — Odoo โหลดอัตโนมัติผ่าน __init__.py

    'data': [

        # 1. Security — เปิดสิทธิ์ Read/Write/Create ให้โมเดลที่สร้างใหม่
        'security/ir.model.access.csv',
        'security/telematics_security.xml',   # Record Rules — driver เห็นแค่ตัวเอง (เพิ่มใหม่)

        # 2. Cron Jobs — ตั้งเวลา Scheduled Action ดึง API ทุก 5 นาที
        'data/telematics_cron.xml',

        # 3. Views — หน้าจอ UI ทั้งหมด
        'views/telematics_config_views.xml',
        'views/fleet_vehicle_ext_views.xml',
        'views/telematics_device_views.xml',  # UC-01 Device Register (เพิ่มใหม่)
        'views/telematics_report_views.xml',  # UC-07/08 Backend Report Wizard (เดิม)
        'views/telematics_backend_report_views.xml',  # Backend Reports ครบทุก API
        'views/telematics_vehicle_trip_views.xml',  # Vehicle Trip History wizard

        # 3b. QWeb PDF Reports (FDD §12.6)
        'reports/energy_report.xml',        # Energy/Fuel Efficiency PDF
        'reports/driver_score_report.xml',  # Monthly Score & Bonus PDF
        'views/telematics_log_views.xml',
        'views/telematics_event_views.xml',
        'views/telematics_scoring_views.xml',
        'views/telematics_incentive_views.xml',
        'views/telematics_payload_views.xml',  # เปิดใช้งานแล้ว 2026-06-30 (เดิมเป็น dead code)

        # 4. Menu — แถบเมนูหลักและเมนูย่อย
        'views/telematics_menus.xml',

        # 5. Portal — พนักงานดูคะแนน/โบนัสตนเอง (UC-11, FDD §2.3 Self-service)
        'views/portal_templates.xml',
    ],

    'installable': True,
    'application': True,
    'license': 'LGPL-3',

    # Live Map widget (UC-06) — โหลด Leaflet ผ่าน CDN ตอน runtime ในไฟล์ JS เอง
    'assets': {
        'web.assets_backend': [
            'fleet_telematics_integration/static/src/js/fleet_live_map.js',
            'fleet_telematics_integration/static/src/xml/fleet_live_map.xml',
            'fleet_telematics_integration/static/src/js/trip_map_widget.js',
            'fleet_telematics_integration/static/src/xml/trip_map_widget.xml',
            'fleet_telematics_integration/static/src/js/driver_dashboard.js',
            'fleet_telematics_integration/static/src/xml/driver_dashboard.xml',
        ],
    },

    # Seed ค่า Base URL / API Key จริงลง ir.config_parameter ตอนติดตั้งโมดูล
    # ดูคำเตือนเรื่องความปลอดภัยของ API Key ที่ฝังในโค้ดได้ที่ __init__.py
    'post_init_hook': '_post_init_seed_telematics_config',
}