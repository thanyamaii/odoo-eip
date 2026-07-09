# ==============================================================================
# models/telematics_event.py
# โมเดลเก็บเหตุการณ์เสี่ยง (Event Logs) — พฤติกรรมอันตรายของคนขับ
#
# แก้ 2026-07-08 (ตามข้อสั่งจากผู้ตรวจงาน):
#   1) ล็อกข้อมูลทั้งหมดให้แก้ไข/ลบ/สร้างผ่าน UI ไม่ได้เด็ดขาด — เขียนได้
#      เฉพาะ path อัตโนมัติจาก Backend sync เท่านั้น (ผ่าน context flag พิเศษ
#      ที่ผู้ใช้ปกติไม่มีทางส่งมาได้จากหน้าจอ) เพื่อความโปร่งใส ป้องกันการ
#      แก้ไขข้อมูลหรือโกงคะแนนพนักงาน
#   2) เพิ่มการคำนวณ speed limit ตามโซนพื้นที่ (กรุงเทพฯ 80 กม./ชม.,
#      นอกเมือง 90 กม./ชม.) จากพิกัด lat/lon ของ event เพื่อใช้เป็นข้อมูล
#      ตั้งต้นให้ระบบคะแนน/โบนัสนำไปประมวลผลต่อ
#   3) เพิ่ม vehicle_id / driver_id (related + store) เพื่อให้ Group By ได้
#      ตรงบนโมเดลนี้เลยโดยไม่ต้องผ่าน trip_id
# ==============================================================================
from odoo import models, fields, api
from odoo.exceptions import UserError

# ── ข้อ 2: Geofence โซนกรุงเทพฯ (ประมาณการแบบ bounding box) ──────────────────
# ⚠️ นี่คือกรอบพิกัดสี่เหลี่ยมคร่าวๆ ครอบคลุมพื้นที่กรุงเทพฯ+ปริมณฑลชั้นใน
#    ไม่ใช่ขอบเขตการปกครองจริงตาม shapefile — ความแม่นยำระดับ "ในเมือง/
#    นอกเมือง" เท่านั้น ถ้าต้องการความแม่นยำระดับเขต/อำเภอ ต้องเปลี่ยนไปใช้
#    GeoJSON polygon จริงของกรมการปกครองแทน bounding box นี้
BANGKOK_BBOX = {
    'lat_min': 13.49, 'lat_max': 13.96,
    'lon_min': 100.32, 'lon_max': 100.93,
}
SPEED_LIMIT_BANGKOK_KMH   = 80.0
SPEED_LIMIT_OUTSIDE_KMH   = 90.0


class TelematicsEvent(models.Model):
    _name        = 'fleet.telematics.event'
    _description = 'Fleet Telematics Harsh Event'
    _order       = 'occurred_at desc'

    # ============================================================
    # [A] ผูก Event กับ Trip
    # ถ้าลบ trip → events ถูกลบทั้งหมดอัตโนมัติ (ondelete cascade)
    # ============================================================
    trip_id = fields.Many2one(
        'fleet.telematics.log',
        string='Trip Log',
        required=True,
        ondelete='cascade',
        readonly=True,
    )

    # ── ข้อ 3: related fields สำหรับ Group By โดยตรงบนหน้า Event Logs ──────────
    vehicle_id = fields.Many2one(
        'fleet.vehicle', string='Vehicle',
        related='trip_id.vehicle_id', store=True, readonly=True, index=True)
    driver_id = fields.Many2one(
        'hr.employee', string='Driver',
        related='trip_id.driver_id', store=True, readonly=True, index=True)

    # ============================================================
    # [B] ประเภทและรายละเอียดของเหตุการณ์
    # จำแนกชนิดพฤติกรรมอันตราย พร้อมเวลา พิกัด ความเร็ว และความรุนแรง
    # ============================================================
    event_type = fields.Selection([
        ('harsh_brake',  'Harsh Brake'),
        ('harsh_accel',  'Harsh Acceleration'),
        ('harsh_corner', 'Harsh Cornering'),
        ('speeding',     'Speeding'),
        ('idling',       'Idling'),
        ('bump',         'Bump'),
    ], string='Event Type', required=True, readonly=True)

    occurred_at    = fields.Datetime(string='Occurred At', required=True, readonly=True)
    lat            = fields.Float(string='Latitude',          digits=(10, 7), readonly=True)
    lon            = fields.Float(string='Longitude',         digits=(10, 7), readonly=True)
    severity       = fields.Float(string='Severity (0–100)', digits=(5, 2), readonly=True)
    speed_at_event = fields.Float(string='Speed (km/h)',      digits=(10, 2), readonly=True)
    description    = fields.Char(string='Description', readonly=True)

    # ============================================================
    # [C] ข้อ 2 — Zone-based Speed Limit
    # ใช้พิกัด lat/lon ของ event เช็คว่าอยู่ในกรอบกรุงเทพฯ หรือไม่ แล้วคืน
    # speed limit ตามโซน + flag ว่าเกินหรือไม่ (เทียบกับ speed_at_event)
    # ============================================================
    speed_limit_kmh = fields.Float(
        string='Speed Limit ตามโซน (km/h)',
        compute='_compute_speed_zone', store=True, digits=(10, 1),
        help='80 กม./ชม. ถ้าอยู่ในกรอบกรุงเทพฯ, 90 กม./ชม. ถ้านอกเมือง '
             '(คำนวณจาก lat/lon ของ event นี้)',
    )
    is_over_speed_limit = fields.Boolean(
        string='เกินความเร็วตามโซน',
        compute='_compute_speed_zone', store=True,
        help='True ถ้า speed_at_event > speed_limit_kmh ของโซนนั้น',
    )
    zone_label = fields.Selection([
        ('bangkok', 'ในเขตกรุงเทพฯ'),
        ('outside', 'นอกเขตกรุงเทพฯ'),
    ], string='โซน', compute='_compute_speed_zone', store=True)

    @api.depends('lat', 'lon', 'speed_at_event')
    def _compute_speed_zone(self):
        for rec in self:
            in_bkk = (
                BANGKOK_BBOX['lat_min'] <= rec.lat <= BANGKOK_BBOX['lat_max']
                and BANGKOK_BBOX['lon_min'] <= rec.lon <= BANGKOK_BBOX['lon_max']
            ) if rec.lat and rec.lon else False

            rec.zone_label      = 'bangkok' if in_bkk else 'outside'
            rec.speed_limit_kmh = (
                SPEED_LIMIT_BANGKOK_KMH if in_bkk else SPEED_LIMIT_OUTSIDE_KMH
            )
            rec.is_over_speed_limit = rec.speed_at_event > rec.speed_limit_kmh

    # ============================================================
    # [D] ข้อ 1 — ล็อกให้แก้ไข/ลบ/สร้างผ่านหน้าจอไม่ได้เด็ดขาด
    #
    # ACL (security/ir.model.access.csv) ตัด perm_write/create/unlink ออก
    # ให้ทุกกลุ่มผู้ใช้แล้วเป็นชั้นแรก แต่ ACL ป้องกันไม่ได้ถ้ามีคน sudo()
    # เขียนตรงๆ จากที่อื่นในโค้ด — เพิ่มชั้นที่สองนี้กันไว้อีกชั้น: create/
    # write/unlink จะสำเร็จ "เฉพาะ" ตอนมี context flag
    # 'fleet_telematics_allow_sync' เท่านั้น ซึ่งมีแค่โค้ด sync อัตโนมัติของ
    # โมดูลนี้เอง (models/telematics_log.py) ที่ตั้ง flag นี้ได้ — ผู้ใช้
    # ทั่วไปหรือแม้แต่ Admin ผ่านหน้าจอปกติจะไม่มีทางส่ง context นี้มาได้
    # ============================================================
    def _check_sync_context(self, action):
        if not self.env.context.get('fleet_telematics_allow_sync'):
            raise UserError(
                'Event Logs เป็นข้อมูลที่ดึงจากบอร์ด GPS อัตโนมัติเท่านั้น — '
                f'ไม่อนุญาตให้{action}ผ่านหน้าจอ เพื่อความโปร่งใสของคะแนน/โบนัส'
            )

    @api.model_create_multi
    def create(self, vals_list):
        self._check_sync_context('สร้าง')
        return super().create(vals_list)

    def write(self, vals):
        self._check_sync_context('แก้ไข')
        return super().write(vals)

    def unlink(self):
        self._check_sync_context('ลบ')
        return super().unlink()