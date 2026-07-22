# ==============================================================================
# models/telematics_payload.py
#
# กล่องจดหมายรับ API (Payload Inbox) — เก็บทุก request ดิบๆ ที่ Backend ยิง
# เข้ามา ไม่ว่าข้อมูลจะถูกหรือผิด format ก็ตาม เพื่อใช้เป็นหลักฐานตรวจสอบ
# ย้อนหลังกับทีม Backend ได้เสมอ ไม่ให้ข้อมูลที่ผิดพลาดหายไปเงียบๆ
# ==============================================================================
import json
import logging

from odoo import models, fields, api

_logger = logging.getLogger(__name__)


class TelematicsPayload(models.Model):
    """บันทึก request ดิบทุกครั้งที่มีคนยิงเข้ามาที่ webhook ของระบบ"""
    _name = 'fleet.telematics.payload'
    _description = 'Telematics Incoming Payload (API Inbox)'
    _order = 'received_at desc'
    _rec_name = 'display_ref'

    display_ref = fields.Char(
        string='Reference',
        compute='_compute_display_ref',
        store=True,
    )
    received_at = fields.Datetime(
        string='Received At',
        default=fields.Datetime.now,
        readonly=True,
        index=True,
    )

    # ── HTTP meta ────────────────────────────────────────────────
    endpoint     = fields.Char(string='Endpoint',     readonly=True)
    http_method  = fields.Char(string='HTTP Method',  readonly=True)
    remote_addr  = fields.Char(string='Remote IP',    readonly=True)
    content_type = fields.Char(string='Content-Type', readonly=True)
    http_headers = fields.Text(string='HTTP Headers', readonly=True)

    # ── Raw payload ──────────────────────────────────────────────
    raw_payload = fields.Text(string='Raw Payload', readonly=True)

    payload_pretty = fields.Text(
        string='Payload (formatted)',
        compute='_compute_payload_pretty',
        store=False,
    )
    payload_valid_json = fields.Boolean(
        string='Valid JSON',
        compute='_compute_payload_pretty',
        store=True,
    )

    # ── ผลการประมวลผล ────────────────────────────────────────────
    state = fields.Selection([
        ('new',       '🆕 New'),
        ('processed', '✅ Processed'),
        ('error',     '❌ Error'),
        ('ignored',   '⚪ Ignored'),
    ], default='new', string='State', index=True)

    notes = fields.Text(string='Notes / Error')

    trip_id = fields.Many2one(
        'fleet.telematics.log',
        string='Trip ที่สร้างจาก Payload นี้',
        ondelete='set null',
        readonly=True,
    )

    @api.depends('received_at', 'endpoint')
    def _compute_display_ref(self):
        """สร้างชื่ออ้างอิงอ่านง่าย เช่น PAYLOAD/20260717-093000/42"""
        for rec in self:
            ts = rec.received_at or fields.Datetime.now()
            rec.display_ref = f'PAYLOAD/{ts:%Y%m%d-%H%M%S}/{rec.id or "new"}'

    @api.depends('raw_payload')
    def _compute_payload_pretty(self):
        """ลองแปลง raw_payload เป็น JSON สวยงามให้อ่านง่ายในหน้าจอ ถ้าแปลง
        ไม่ได้ (ไม่ใช่ JSON ที่ถูกต้อง) ให้แสดงข้อความดิบไว้เฉยๆ พร้อม flag
        payload_valid_json = False ไว้เตือน"""
        for rec in self:
            if rec.raw_payload:
                try:
                    obj = json.loads(rec.raw_payload)
                    rec.payload_pretty = json.dumps(obj, ensure_ascii=False, indent=2)
                    rec.payload_valid_json = True
                except (json.JSONDecodeError, ValueError):
                    rec.payload_pretty = rec.raw_payload
                    rec.payload_valid_json = False
            else:
                rec.payload_pretty = ''
                rec.payload_valid_json = False

    # ── ปุ่มจัดการ state ─────────────────────────────────────────
    def action_mark_processed(self):
        self.write({'state': 'processed'})

    def action_mark_ignored(self):
        self.write({'state': 'ignored'})

    def action_mark_error(self):
        self.write({'state': 'error'})
