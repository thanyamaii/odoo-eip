# ==============================================================================
# models/telematics_event.py
#
# เก็บเหตุการณ์เสี่ยง (Event Logs) — พฤติกรรมการขับขี่อันตรายของคนขับ
# เช่น เบรกกะทันหัน เร่งกะทันหัน เข้าโค้งแรง ขับเร็วเกิน จอดติดเครื่องนาน
#
# กฎสำคัญของโมเดลนี้: ข้อมูลทั้งหมดมาจากบอร์ด GPS ผ่านการ sync อัตโนมัติ
# เท่านั้น ห้ามสร้าง/แก้ไข/ลบผ่านหน้าจอเด็ดขาด (ดูฟังก์ชัน _check_sync_context
# ด้านล่าง) เพื่อความโปร่งใสของระบบให้คะแนน/โบนัส ป้องกันไม่ให้ใครมาแก้ไข
# หลักฐานย้อนหลังได้
# ==============================================================================
from odoo import models, fields, api
from odoo.exceptions import UserError

# กรอบพิกัดสี่เหลี่ยมคร่าวๆ ครอบคลุมพื้นที่กรุงเทพฯ+ปริมณฑลชั้นใน ใช้แยกว่า
# เหตุการณ์นี้เกิด "ในเมือง" หรือ "นอกเมือง" เพื่อใช้ความเร็วจำกัดคนละค่ากัน
# ⚠️ เป็นกรอบพิกัดโดยประมาณเท่านั้น ไม่ใช่ขอบเขตการปกครองจริง ถ้าต้องการ
# ความแม่นยำระดับเขต/อำเภอ ต้องเปลี่ยนไปใช้ GeoJSON polygon จริงแทน
BANGKOK_BBOX = {
    'lat_min': 13.49, 'lat_max': 13.96,
    'lon_min': 100.32, 'lon_max': 100.93,
}
SPEED_LIMIT_BANGKOK_KMH = 80.0
SPEED_LIMIT_OUTSIDE_KMH = 90.0


class TelematicsEvent(models.Model):
    """เหตุการณ์เสี่ยง 1 รายการ = พฤติกรรมอันตราย 1 ครั้งที่เกิดขึ้นระหว่างทริป
    ผูกกับ Trip Log เสมอ ลบทริปทิ้ง เหตุการณ์ในทริปนั้นจะถูกลบตามไปด้วย"""
    _name        = 'fleet.telematics.event'
    _description = 'Fleet Telematics Harsh Event'
    _order       = 'occurred_at desc'

    trip_id = fields.Many2one(
        'fleet.telematics.log',
        string='Trip Log',
        required=True,
        ondelete='cascade',
        readonly=True,
    )

    # ดึงมาจากทริปแม่โดยตรง (related+store) เพื่อให้กรอง/จัดกลุ่มตามรถหรือ
    # คนขับได้ทันทีในหน้า Event Logs โดยไม่ต้องเปิดผ่าน trip_id ทุกครั้ง
    vehicle_id = fields.Many2one(
        'fleet.vehicle', string='Vehicle',
        related='trip_id.vehicle_id', store=True, readonly=True, index=True)
    driver_id = fields.Many2one(
        'hr.employee', string='Driver',
        related='trip_id.driver_id', store=True, readonly=True, index=True)

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

    # ── ความเร็วจำกัดตามโซนพื้นที่ ──────────────────────────────────────────
    # คำนวณจากพิกัด lat/lon ของเหตุการณ์นี้เอง: อยู่ในกรอบกรุงเทพฯ → 80 km/h
    # นอกกรอบ → 90 km/h แล้วเทียบกับความเร็วจริงตอนเกิดเหตุว่าเกินไหม
    speed_limit_kmh = fields.Float(
        string='Speed Limit ตามโซน (km/h)',
        compute='_compute_speed_zone', store=True, digits=(10, 1),
        help='80 กม./ชม. ถ้าอยู่ในกรอบกรุงเทพฯ, 90 กม./ชม. ถ้านอกเมือง',
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
        """เช็คพิกัดว่าอยู่ในกรอบกรุงเทพฯ ไหม แล้วตั้งค่าความเร็วจำกัด +
        flag เกินความเร็วให้ครบทั้ง 3 field พร้อมกัน"""
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

    # ── ล็อกไม่ให้แก้ไขผ่านหน้าจอ ───────────────────────────────────────────
    # ป้องกัน 2 ชั้น: (1) security/ir.model.access.csv ตัดสิทธิ์เขียน/สร้าง/
    # ลบออกจากทุกกลุ่มผู้ใช้แล้วเป็นชั้นแรก (2) เผื่อมีโค้ดที่ไหน sudo()
    # เขียนตรงๆ ข้าม ACL ได้ จึงเช็คซ้ำอีกชั้นที่นี่: ยอมให้ทำได้ก็ต่อเมื่อมี
    # context flag พิเศษ 'fleet_telematics_allow_sync' ซึ่งมีแค่ตัว sync
    # อัตโนมัติในโมดูลนี้เอง (models/telematics_log.py) เท่านั้นที่ส่งได้
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
