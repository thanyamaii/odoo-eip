# ==============================================================================
# models/telematics_incentive.py
# โมเดลคำนวณโบนัสประจำเดือน เชื่อมโยงกับ hr.contract
# ระบบ Incentive / Bonus HR
#
# แก้ไข 2026-06-30 (ตามคำตอบยืนยันจาก Backend):
#   GET /api/v1/drivers/{driver_id}/bonus คืน bonus_pct (และ tier) มาให้แล้ว
#   Backend ไม่รู้ hr.contract.wage จึงให้ Odoo เป็นฝ่ายคูณเงินเดือนเอง
#   → ตัด logic คำนวณ tier จาก scoring_config thresholds ออกจาก flow หลัก
#     เหลือไว้แค่เป็น fallback เผื่อเรียก Backend ไม่สำเร็จ (เช่น offline)
# ==============================================================================
import logging
from datetime import date, timedelta

import requests
from markupsafe import Markup

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class TelematicsIncentive(models.Model):
    _name        = 'fleet.telematics.incentive'
    _description = 'Fleet Telematics Monthly Incentive'
    _order       = 'period_year desc, period_month desc'
    # เพิ่ม 2026-07-06 (UC-10 — Audit Log): FDD §13 ระบุว่า "audit log ทุก
    # state change" แต่โมเดลนี้ไม่เคยมี mail.thread เลยสักครั้ง ทำให้ไม่มี
    # ประวัติว่าใคร/เมื่อไหร่เปลี่ยน state (draft→confirmed→approved→paid)
    # ซึ่งเป็นข้อมูลการเงิน (โบนัส) จึงต้องมี audit trail ที่แก้ไขเองไม่ได้
    _inherit = ['mail.thread', 'mail.activity.mixin']

    # กันสร้างโบนัสซ้ำที่ระดับ DB จริง (เดิมเช็คแค่ใน cron แบบ soft-check
    # ถ้าสร้างผ่านฟอร์มมือมีโอกาสสร้างซ้ำเดือนเดียวกันได้ — กระทบเงินจริง)
    # แก้ 2026-07-09: เดิม unique(driver_id, period_month, period_year) เปลี่ยนมา
    # unique ด้วย date_from/date_to ตรงๆ ตามที่บรีฟต้องการรองรับ "รอบตัดวิก"
    # ที่ไม่ตรงเดือนปฏิทิน (เช่น 26 มิ.ย. – 25 ก.ค.)
    _sql_constraints = [
        ('driver_period_unique',
         'UNIQUE(driver_id, date_from, date_to)',
         'พนักงานคนนี้มีรายการโบนัสของช่วงวันที่นี้อยู่แล้ว — ห้ามสร้างซ้ำ'),
    ]

    # ============================================================
    # [A] ระบุว่าคำนวณโบนัสของใคร รอบไหน
    # snapshot ของ scoring config ป้องกันผลกระทบเมื่อแก้ config ภายหลัง
    # ============================================================
    driver_id = fields.Many2one(
        'hr.employee', string='Driver', required=True)
    # เพิ่ม 2026-07-08 — flatten field สำหรับ ir.rule (security/telematics_security.xml)
    # เดิม rule ใช้ domain [('driver_id.user_id', '=', user.id)] แบบ dotted-path
    # ตรงๆ แล้ว upgrade module พัง (ParseError ตอนโหลด ir.rule ของโมเดลนี้
    # โดยเฉพาะ — โมเดลอื่นที่ domain รูปแบบเดียวกันไม่พัง) เปลี่ยนมาใช้ field
    # related+store แบบ flat ระดับเดียวแทน ตัดปัญหาเรื่อง multi-hop path
    # ในการ validate domain ของ ir.rule ไปเลย ปลอดภัยกว่าแน่นอน
    driver_user_id = fields.Many2one(
        'res.users', string='Driver User (internal)',
        related='driver_id.user_id', store=True, readonly=True,
        help='ใช้ภายในสำหรับ record rule เท่านั้น — ไม่ต้องแสดงในฟอร์ม')
    scoring_config_id = fields.Many2one(
        'fleet.telematics.scoring.config',
        string='Scoring Config (snapshot)',
        help='Snapshot ของ config ที่ใช้คำนวณรอบนี้')

    # แก้ 2026-07-09 (ตามบรีฟข้อ 1): เดิมเลือกแยก "เดือน"/"ปี" ไม่ยืดหยุ่นกับ
    # รอบตัดวิกที่ไม่ตรงเดือนปฏิทิน (เช่น 26 ของเดือนก่อน ถึง 25 เดือนนี้)
    # เปลี่ยนเป็นช่วงวันที่จริงแทน — เป็น field หลักที่ผู้ใช้กรอก/เลือกเอง
    date_from = fields.Date(string='วันที่เริ่มต้น', required=True)
    date_to   = fields.Date(string='วันที่สิ้นสุด', required=True)

    # period_month/period_year: เปลี่ยนจาก field กรอกเองเป็น compute+store
    # (ดึงมาจาก date_from อัตโนมัติ) เก็บไว้เพื่อไม่ให้กระทบของเดิมที่อ้างอิง
    # อยู่ (controllers/portal.py ใช้ order, reports/driver_score_report.xml
    # ใช้แสดงผล) — ผู้ใช้ไม่ต้องกรอกเองแล้ว
    period_month = fields.Integer(
        string='Month', compute='_compute_period_ints', store=True)
    period_year = fields.Integer(
        string='Year', compute='_compute_period_ints', store=True)
    period_label = fields.Char(
        string='Period',
        compute='_compute_period_label', store=True,
        help='แสดงผลเป็นช่วงวันที่ เช่น 01/06/2026 - 25/06/2026')

    # ============================================================
    # [B] สถิติสรุปจาก Trip Logs ของเดือนนั้น
    # ============================================================
    avg_score = fields.Float(
        string='Avg Score', digits=(5, 2),
        compute='_compute_incentive', store=True)
    min_score = fields.Float(
        string='Min Score', digits=(5, 2),
        compute='_compute_incentive', store=True)
    total_trips = fields.Integer(
        string='Total Trips',
        compute='_compute_incentive', store=True)
    total_distance_km = fields.Float(
        string='Total Distance (km)', digits=(10, 2),
        compute='_compute_incentive', store=True)
    total_harsh_events = fields.Integer(
        string='Total Harsh Events',
        compute='_compute_incentive', store=True)
    total_idle_min = fields.Float(
        string='Total Idle (min)', digits=(10, 2),
        compute='_compute_incentive', store=True)

    # ============================================================
    # [C] ผลลัพธ์ Tier และจำนวนโบนัสที่ได้รับ
    # ดึง bonus_pct/tier จาก Backend (GET /drivers/{id}/bonus) แล้วคูณ
    # base_salary (จาก hr.contract) เอง — ไม่ใช่ field compute อัตโนมัติ
    # อีกต่อไป เพราะต้องเรียก API ภายนอก ตั้งค่าผ่าน _apply_backend_bonus()
    # ที่ถูกเรียกตอนสร้างใน cron หรือกดปุ่ม "Refresh from Backend" เอง
    # ============================================================
    incentive_tier = fields.Selection([
        ('A', 'A — Excellent'),
        ('B', 'B — Good'),
        ('C', 'C — Fair'),
        ('D', 'D — Needs Improvement'),
    ], string='Tier', default='D', readonly=True)
    bonus_pct    = fields.Float(string='Bonus %',      digits=(5, 2),  default=0.0, readonly=True)
    # แก้ 2026-07-09: เอา readonly=True ระดับ Python ออก — ตัวนี้เคยบังคับ
    # ให้พิมพ์ไม่ได้ตลอดไม่ว่า state จะเป็นอะไร (override ทับ
    # readonly="is_locked" ที่ตั้งไว้ในฝั่ง View จนไม่มีผลเลย) ปล่อยให้ View
    # เป็นคนคุม readonly ตาม is_locked แทน (แก้ไขได้ตอน Draft เท่านั้น)
    base_salary  = fields.Float(string='Base Salary',  digits=(10, 2), default=0.0)
    # แก้ 2026-07-09 (บรีฟข้อ 3): เดิม bonus_amount เป็น field ธรรมดาที่ตั้งค่า
    # ผ่าน write() ใน _apply_backend_bonus() เท่านั้น แต่ยังเป็นช่องที่ผู้ใช้
    # พิมพ์แก้ตรงๆ ได้เองจากหน้าฟอร์ม (ช่องโหว่โกงเงินโบนัส เพราะไม่บังคับว่า
    # ต้อง = Base Salary × Bonus % เสมอ) เปลี่ยนเป็น compute field จริง ผูกสูตร
    # ตายตัว ป้องกันไม่ให้ตัวเลขหลุดจากสูตรได้เลย
    bonus_amount = fields.Float(
        string='Bonus (THB)', digits=(10, 2),
        compute='_compute_bonus_amount', store=True, readonly=True,
        help='คำนวณอัตโนมัติ = Base Salary × Bonus % — แก้ไขตรงๆ ไม่ได้')
    bonus_source = fields.Selection([
        ('backend', 'Backend API'),
        ('local_fallback', 'Local Fallback (Backend ไม่พร้อมใช้งาน)'),
    ], string='Bonus Source', readonly=True,
        help='ระบุว่า bonus_pct ปัจจุบันมาจาก Backend จริง หรือคำนวณสำรองในเครื่อง')
    bonus_last_synced = fields.Datetime(string='Bonus Synced At', readonly=True)
    # เพิ่ม 2026-07-16: กันแจ้งเตือน HR ซ้ำหลายรอบ ถ้ากด Refresh from
    # Backend ซ้ำๆ ขณะที่ยังเป็น Tier D อยู่เหมือนเดิม (ดู _notify_hr_tier_d)
    tier_d_notified = fields.Boolean(string='แจ้งเตือน Tier D แล้ว', default=False, readonly=True)

    # ============================================================
    # [D0] เพิ่ม 2026-07-09 (บรีฟข้อ 5): ล็อกทั้งฟอร์มถาวรเมื่อพ้น Draft
    # ============================================================
    is_locked = fields.Boolean(
        string='ล็อกการแก้ไข', compute='_compute_is_locked',
        help='True เมื่อ state ไม่ใช่ Draft แล้ว — ฟิลด์ทั้งหมดแก้ไขไม่ได้ '
             'จนกว่าจะกด Reset กลับเป็น Draft')

    @api.depends('state')
    def _compute_is_locked(self):
        for rec in self:
            rec.is_locked = rec.state != 'draft'

    # ============================================================
    # [D] Workflow State ของใบโบนัส
    # draft → confirmed → approved → paid
    # ============================================================
    state = fields.Selection([
        ('draft',     'Draft'),
        ('confirmed', 'Confirmed'),
        ('approved',  'Approved'),
        ('paid',      'Paid'),
    ], default='draft', tracking=True)  # tracking=True → chatter บันทึก log อัตโนมัติ
                                         # ทุกครั้งที่ state เปลี่ยน (UC-10 Audit Log)
    approved_by = fields.Many2one('res.users', string='Approved By', readonly=True, tracking=True)
    note        = fields.Text(string='Notes')

    # ============================================================
    # [E0] เพิ่ม 2026-07-09 — derive period_month/period_year จาก date_from
    # (เก็บไว้ให้ report/portal เดิมที่อ้างอิง field นี้อยู่ยังทำงานได้ปกติ)
    # ============================================================
    @api.depends('date_from')
    def _compute_period_ints(self):
        for rec in self:
            if rec.date_from:
                rec.period_month = rec.date_from.month
                rec.period_year  = rec.date_from.year
            else:
                rec.period_month = 0
                rec.period_year  = 0

    # ============================================================
    # [E] Computed — แสดง Period เป็นช่วงวันที่ (แก้ 2026-07-09 จาก MM/YYYY เดิม)
    # ============================================================
    @api.depends('date_from', 'date_to')
    def _compute_period_label(self):
        for rec in self:
            if rec.date_from and rec.date_to:
                rec.period_label = (
                    f'{rec.date_from.strftime("%d/%m/%Y")} - '
                    f'{rec.date_to.strftime("%d/%m/%Y")}'
                )
            else:
                rec.period_label = '-'

    # ============================================================
    # [F] คำนวณสถิติจาก Trip Logs เท่านั้น (avg_score, total_trips ฯลฯ)
    # แก้ 2026-07-09: ใช้ date_from/date_to ตรงๆ แทนการต่อจาก
    # period_month/period_year (รองรับรอบตัดวิกที่ไม่ตรงเดือนปฏิทิน)
    # date_to ถือเป็นวันสุดท้าย "รวม" อยู่ในช่วง (inclusive)
    # ไม่รวม bonus_pct/tier แล้ว — ย้ายไป _apply_backend_bonus() ด้านล่าง
    # ============================================================
    @api.depends('driver_id', 'date_from', 'date_to')
    def _compute_incentive(self):
        TripLog = self.env['fleet.telematics.log'].sudo()
        for rec in self:
            if not (rec.driver_id and rec.date_from and rec.date_to):
                rec.avg_score = rec.min_score = 0.0
                rec.total_trips = rec.total_harsh_events = 0
                rec.total_distance_km = rec.total_idle_min = 0.0
                continue

            date_from_excl = rec.date_to + timedelta(days=1)  # date_to รวมอยู่ในช่วง

            logs = TripLog.search([
                ('driver_id',  '=', rec.driver_id.id),
                ('trip_start', '>=', str(rec.date_from)),
                ('trip_start', '<',  str(date_from_excl)),
                ('state',      '=',  'synced'),
            ])

            scores = [l.driver_score for l in logs if l.driver_score]
            rec.avg_score          = round(sum(scores) / len(scores), 2) if scores else 0.0
            rec.min_score          = round(min(scores), 2) if scores else 0.0
            rec.total_trips        = len(logs)
            rec.total_distance_km  = round(sum(logs.mapped('distance_km')), 2)
            rec.total_idle_min     = round(sum(logs.mapped('idle_min')), 2)
            rec.total_harsh_events = sum(
                l.harsh_brake_count + l.harsh_accel_count + l.harsh_corner_count
                for l in logs
            )

    # ============================================================
    # [F2] ดึง bonus_pct จาก Backend (GET /drivers/{id}/bonus) แล้วคูณ
    # base_salary เอง — ตามคำตอบยืนยันจากทีม Backend (2026-06-30):
    # "Backend ไม่รู้ hr.contract.wage ดังนั้น /bonus คืนแค่ bonus_pct (%)
    #  Odoo ต้องคูณกับเงินเดือนจริงเอง"
    #
    # ถ้าเรียก Backend ไม่สำเร็จ (offline/timeout) ใช้ fallback คำนวณ
    # tier จาก threshold ใน Scoring Config เพื่อไม่ให้ระบบหยุดทำงาน
    # แต่จะ mark bonus_source = 'local_fallback' ให้รู้ว่าตัวเลขยังไม่ยืนยัน
    # จาก Backend ควรกด "Refresh from Backend" ซ้ำก่อน Approve จริง
    # ============================================================
    def _apply_backend_bonus(self):
        # เพิ่ม 2026-07-16: เดิมไม่มีการเช็คเลยว่าเลือก Driver ไว้หรือยัง
        # ก่อนรัน — กด Refresh from Backend/Confirm ทั้งที่ยังไม่เลือกใครเลย
        # จะเงียบๆ ไปคำนวณ avg_score จาก Trip Log ที่ driver_id ว่างเปล่า
        # (ได้ 0 เสมอ) แทนที่จะเตือนให้เลือกก่อน เพิ่ม guard ให้ชัดเจน
        no_driver = self.filtered(lambda r: not r.driver_id)
        if no_driver:
            raise UserError('กรุณาเลือก Driver ก่อน ถึงจะคำนวณโบนัสได้')

        Config = self.env['fleet.telematics.config']
        api_url = Config.get_active_api_url()
        api_key = Config.get_active_api_key()

        for rec in self:
            # ดึง base_salary จากสัญญาจ้างพนักงาน
            # แก้ 2026-07-16: ยืนยันโครงสร้างจริงแล้วจาก Model Overview ของ
            # ผู้ใช้ (Settings > Technical > Models > hr.version) — Odoo 19
            # ใช้โมเดล hr.version แทน hr.contract เดิม ยืนยัน field จริง:
            #   employee_id (many2one → hr.employee), wage (monetary),
            #   is_current (boolean — true = สัญญาปัจจุบันที่ใช้งานอยู่ ใช้แทน
            #   state == 'open' แบบเก่าของ hr.contract)
            base_salary = 0.0
            found_from_contract = False

            if 'hr.version' in self.env:
                # แก้ 2026-07-16: is_current เป็น compute field ที่ไม่ได้
                # stored ในฐานข้อมูล (ยืนยันจาก error จริง: "Cannot convert
                # hr.version.is_current to SQL because it is not stored")
                # จึงเอาไปใส่ในเงื่อนไข search() ตรงๆ ไม่ได้ ต้องดึงทุก version
                # ของพนักงานคนนั้นมาก่อน แล้วค่อยกรองด้วย Python ภายหลัง
                # (ตอนนั้น compute field จะคำนวณค่าให้ตามปกติ เพราะไม่ผ่าน SQL)
                all_versions = self.env['hr.version'].sudo().search([
                    ('employee_id', '=', rec.driver_id.id),
                ])
                version = all_versions.filtered(lambda v: v.is_current)[:1]
                if version:
                    base_salary = version.wage or 0.0
                    found_from_contract = True
            elif 'hr.contract' in self.env:
                # fallback: เผื่อรันบน Odoo เวอร์ชันเก่ากว่า 19 ที่ยังมี
                # hr.contract แบบเดิมอยู่ (ก่อนเปลี่ยนเป็น hr.version)
                contract = self.env['hr.contract'].sudo().search([
                    ('employee_id', '=', rec.driver_id.id),
                    ('state', '=', 'open'),
                ], limit=1)
                base_salary = contract.wage if contract else 0.0
                found_from_contract = bool(contract)

            if not found_from_contract:
                if rec.driver_id.telematics_base_salary:
                    _logger.info(
                        '_apply_backend_bonus: ไม่พบ hr.version ที่ is_current=True '
                        'สำหรับพนักงาน %s — ใช้ telematics_base_salary ที่กรอกไว้บน '
                        'โปรไฟล์พนักงานแทน', rec.driver_id.name,
                    )
                    base_salary = rec.driver_id.telematics_base_salary
                else:
                    _logger.info(
                        '_apply_backend_bonus: ไม่พบเงินเดือนจากทั้ง hr.version และ '
                        'โปรไฟล์พนักงาน — คงค่า Base Salary ที่กรอกเองไว้ในใบนี้ '
                        '(แก้ไขได้ตอน state=Draft เท่านั้น)'
                    )
                    base_salary = rec.base_salary

            bonus_pct = None
            tier = None

            if api_url:
                try:
                    resp = requests.get(
                        f'{api_url}/api/v1/drivers/{rec.driver_id.id}/bonus',
                        headers={'APIKEY': api_key},
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        bonus_pct = float(data.get('bonus_pct', 0) or 0)
                        tier = data.get('incentive_tier')  # แก้บั๊ก: ชื่อจริงจาก Backend คือ
                                                            # 'incentive_tier' ไม่ใช่ 'tier'
                                                            # (ยืนยันจาก JSON ตัวอย่างจริง 2026-06-30)
                    else:
                        _logger.warning(
                            'Bonus API HTTP %s สำหรับ driver_id=%s — ใช้ fallback',
                            resp.status_code, rec.driver_id.id,
                        )
                except requests.RequestException as e:
                    _logger.warning(
                        'เรียก /drivers/%s/bonus ไม่สำเร็จ (%s) — ใช้ fallback',
                        rec.driver_id.id, e,
                    )

            if bonus_pct is not None:
                rec.write({
                    'base_salary':       base_salary,
                    'bonus_pct':         bonus_pct,
                    'incentive_tier':    tier or rec._local_tier_from_score(),
                    'bonus_source':      'backend',
                    'bonus_last_synced': fields.Datetime.now(),
                })
            else:
                # Fallback: คำนวณ tier เองจาก scoring config thresholds
                tier_fb, pct_fb = rec._local_tier_from_score(return_pct=True)
                rec.write({
                    'base_salary':       base_salary,
                    'bonus_pct':         pct_fb,
                    'incentive_tier':    tier_fb,
                    'bonus_source':      'local_fallback',
                    'bonus_last_synced': fields.Datetime.now(),
                })

            # เพิ่ม 2026-07-16: ตาม FDD §12.4 (ตาราง Tier) ระบุไว้ชัดเจนว่า
            # Tier D ต้องมี "0% + แจ้งเตือน HR" ไม่ใช่แค่ตั้ง bonus_pct=0
            # เฉยๆ — เดิมโค้ดไม่เคยมีการแจ้งเตือนส่วนนี้เลย เพิ่มให้ครบตามสเปค
            # ครอบด้วย try/except เพราะเป็นฟีเจอร์เสริม (นoti) ไม่ควรทำให้
            # ปุ่ม Confirm/คำนวณโบนัสหลัก (ธุรกรรมสำคัญกว่า) พังไปด้วยถ้า
            # ระบบแจ้งเตือนมีปัญหา (เช่น field ชื่อเปลี่ยนไปอีกใน Odoo เวอร์ชันถัดไป)
            if rec.incentive_tier == 'D':
                try:
                    rec._notify_hr_tier_d()
                except Exception:
                    _logger.exception(
                        '_notify_hr_tier_d ล้มเหลวสำหรับใบโบนัส id=%s — ข้ามไป '
                        'ไม่ให้กระทบการคำนวณโบนัสหลัก', rec.id,
                    )

    def _local_tier_from_score(self, return_pct=False):
        """Fallback เท่านั้น — ใช้เมื่อเรียก Backend ไม่สำเร็จ"""
        self.ensure_one()
        cfg = self.scoring_config_id or self.env['fleet.telematics.scoring.config'].search(
            [('active', '=', True)], limit=1)
        if cfg and self.avg_score >= cfg.tier_a_min_score:
            tier, pct = 'A', cfg.tier_a_bonus_pct
        elif cfg and self.avg_score >= cfg.tier_b_min_score:
            tier, pct = 'B', cfg.tier_b_bonus_pct
        elif cfg and self.avg_score >= cfg.tier_c_min_score:
            tier, pct = 'C', cfg.tier_c_bonus_pct
        else:
            tier, pct = 'D', 0.0
        return (tier, pct) if return_pct else tier

    # ============================================================
    # [F1b] เพิ่ม 2026-07-16 — ตาม FDD §12.4 ตาราง Tier ระบุไว้ชัดเจนว่า:
    #   "D | 0 | ต้องปรับปรุง | แดง #dc3545 | 0% + แจ้งเตือน HR"
    # เดิมระบบมีแค่แถบเตือนสีแดงในหน้าจอ (UI) แต่ไม่เคยแจ้งเตือน HR จริง
    # ทั้งทาง chatter และอีเมล — เพิ่มให้ครบทั้ง 2 ช่องทางตามสเปค:
    #   1) message_post บนตัว record เอง (ติดตามผ่าน chatter/inbox ได้)
    #   2) ส่งอีเมลตรงถึงผู้ใช้ในกลุ่ม Fleet Manager (ทำหน้าที่เป็น HR/ผู้อนุมัติ
    #      ตามที่ Security requirement ของ FDD กำหนดไว้ว่า
    #      "เฉพาะ group_fleet_manager approve Incentive ได้")
    # กันไม่ให้แจ้งซ้ำหลายรอบถ้ากด Refresh from Backend ซ้ำๆ ด้วย flag
    # tier_d_notified
    # ============================================================
    def _notify_hr_tier_d(self):
        self.ensure_one()
        if self.tier_d_notified:
            return  # กันแจ้งซ้ำถ้ากด Refresh from Backend หลายรอบ

        # ใช้ Markup(...).format(...) แทนการต่อ f-string ตรงๆ เพื่อให้ค่าที่
        # แทรกเข้าไป (ชื่อพนักงาน, period_label) ถูก escape อัตโนมัติ ป้องกัน
        # ปัญหาถ้าชื่อพนักงานมีอักขระพิเศษปนอยู่ ในขณะที่ tag HTML ของ template
        # เองยัง render ปกติ (Markup.format ฉลาดพอจะไม่ escape ซ้ำสิ่งที่เป็น
        # Markup อยู่แล้ว แต่จะ escape เฉพาะค่าธรรมดาที่ส่งเข้ามา)
        body = Markup(
            '⚠️ <b>แจ้งเตือน Tier D — พนักงานคะแนนต่ำกว่าเกณฑ์</b><br/>'
            'พนักงาน: <b>{driver_name}</b><br/>'
            'รอบ: {period}<br/>'
            'คะแนนเฉลี่ย: {score} (ต่ำกว่าเกณฑ์ขั้นต่ำของ Tier C)<br/>'
            'ผลลัพธ์: ไม่ได้รับโบนัสในรอบนี้ (0%)<br/><br/>'
            'ตาม FDD §12.4 — Tier D ต้องแจ้งเตือน HR/Fleet Manager เพื่อพิจารณา'
            'ติดตามพฤติกรรมการขับขี่ของพนักงานคนนี้'
        ).format(
            driver_name=self.driver_id.name,
            period=self.period_label,
            score=f'{self.avg_score:.2f}',
        )

        # 1) บันทึกไว้ใน chatter ของ Incentive record เอง
        self.message_post(body=body)

        # 2) ส่งอีเมลตรงถึงกลุ่ม Fleet Manager (ทำหน้าที่ HR/ผู้อนุมัติ)
        # แก้ 2026-07-16: เดิมใช้ managers.users ตรงๆ แต่ Odoo 19 เปลี่ยนชื่อ
        # field ฝั่ง res.groups ไปแล้ว (error จริง: "'res.groups' object has
        # no attribute 'users'") จึงเลี่ยงไปหาแบบ query จาก res.users แทน
        # โดยเช็ค field ที่มีอยู่จริงก่อนใช้งาน (กันพังซ้ำถ้าเปลี่ยนชื่ออีก)
        managers_group = self.env.ref('fleet.fleet_group_manager', raise_if_not_found=False)
        manager_users = self.env['res.users']
        if managers_group:
            User = self.env['res.users']
            group_field_candidates = ['groups_id', 'group_ids']
            for fname in group_field_candidates:
                if fname in User._fields:
                    manager_users = User.sudo().search([(fname, 'in', managers_group.ids)])
                    break
            else:
                _logger.warning(
                    '_notify_hr_tier_d: หา field เชื่อมกลุ่มบน res.users ไม่เจอ '
                    '(ลองแล้ว: %s) — ข้ามการส่งอีเมล แจ้งได้แค่ chatter เท่านั้น',
                    group_field_candidates,
                )

        if manager_users:
            self.message_notify(
                partner_ids=manager_users.partner_id.ids,
                body=body,
                subject=f'⚠️ Tier D — {self.driver_id.name} ({self.period_label})',
            )
        else:
            _logger.warning(
                '_notify_hr_tier_d: ไม่พบผู้ใช้ในกลุ่ม Fleet Manager ที่จะแจ้งเตือน '
                '— แจ้งเตือนได้แค่ทาง chatter ของ record นี้เท่านั้น (ใบโบนัส %s)',
                self.id,
            )

        self.tier_d_notified = True

    # ============================================================
    # [F3] เพิ่ม 2026-07-09 (บรีฟข้อ 3): Bonus (THB) = Base Salary × Bonus %
    # เป็น compute field จริง ตายตัวตามสูตร ป้องกันตัวเลขหลุดจากสูตร
    # ============================================================
    @api.depends('base_salary', 'bonus_pct')
    def _compute_bonus_amount(self):
        for rec in self:
            rec.bonus_amount = round(rec.base_salary * rec.bonus_pct / 100, 2)

    def action_refresh_bonus_from_backend(self):
        """ปุ่มในฟอร์ม — ดึง bonus_pct ล่าสุดจาก Backend ใหม่ด้วยมือ"""
        self._apply_backend_bonus()

    # ============================================================
    # [F4] เพิ่ม 2026-07-09 (บรีฟข้อ 5): ล็อกทุกฟิลด์ถาวรเมื่อพ้น Draft
    # (Confirmed/Approved/Paid) — แก้ไขได้ทางเดียวคือกด Reset กลับ Draft
    # ก่อน ป้องกันไม่ให้มีใครดึงข้อมูลซ้ำเพื่อเปลี่ยนตัวเลขยอดบาทกลางคัน
    # ============================================================
    _LOCKED_INCENTIVE_FIELDS = {
        'driver_id', 'date_from', 'date_to', 'scoring_config_id', 'note',
        'total_trips', 'total_distance_km', 'avg_score', 'min_score',
        'total_harsh_events', 'total_idle_min',
        'incentive_tier', 'bonus_pct', 'base_salary', 'bonus_amount',
        'bonus_source', 'bonus_last_synced',
    }

    def write(self, vals):
        touched = self._LOCKED_INCENTIVE_FIELDS.intersection(vals.keys())
        if touched:
            for rec in self:
                if rec.state != 'draft':
                    raise UserError(
                        'ใบโบนัสนี้ผ่านสถานะ Draft ไปแล้ว (Confirmed ขึ้นไป) — '
                        'แก้ไขข้อมูลผลงาน/โบนัสไม่ได้อีก เพื่อความโปร่งใส\n\n'
                        'ถ้าต้องการแก้ไข: กด "Reset" กลับเป็น Draft ก่อน'
                    )
        return super().write(vals)

    # ============================================================
    # [F5] เพิ่ม 2026-07-09 (บรีฟข้อ 4): ส่งสรุปผลไประบบประเมินผล (Appraisal)
    # ไม่ผูก hard-dependency กับโมดูล hr_appraisal (อาจไม่ได้ติดตั้ง) —
    # เขียนสรุปลง chatter ของพนักงานเสมอ (ใช้ mail.thread ของ hr.employee
    # ที่มีอยู่แล้วในทุก Odoo) และถ้ามีโมดูล hr_appraisal ติดตั้งอยู่ด้วย
    # จะโพสต์ซ้ำลง appraisal ล่าสุดของพนักงานคนนั้นให้อัตโนมัติ
    # ============================================================
    def action_export_to_appraisal(self):
        self.ensure_one()
        if self.state not in ('approved', 'paid'):
            raise UserError(
                'ต้อง Approve ใบโบนัสนี้ก่อน ถึงจะส่งออกไปยังระบบประเมินผลได้'
            )
        summary = (
            f'📊 สรุปผลโบนัส Fleet Telematics — {self.period_label}\n'
            f'Avg Score: {self.avg_score:.2f} | Min Score: {self.min_score:.2f} | '
            f'Total Trips: {self.total_trips}\n'
            f'Tier: {self.incentive_tier} | Bonus: {self.bonus_pct:.2f}% '
            f'= {self.bonus_amount:,.2f} THB'
        )
        self.driver_id.message_post(body=summary)

        # ถ้ามีโมดูล hr_appraisal ติดตั้งอยู่ (optional) ผูกเข้า appraisal
        # ล่าสุดของพนักงานคนนี้ด้วย — ถ้าไม่มีโมดูลนี้ก็แค่ข้ามไปเงียบๆ
        Appraisal = self.env.get('hr.appraisal')
        appraisal_linked = False
        if Appraisal is not None:
            appraisal = Appraisal.sudo().search(
                [('employee_id', '=', self.driver_id.id)],
                order='create_date desc', limit=1,
            )
            if appraisal:
                appraisal.message_post(body=summary)
                appraisal_linked = True

        self.message_post(body=f'📤 ส่งออกสรุปผลไปยังประวัติพนักงานแล้ว โดย {self.env.user.name}')
        return {
            'type': 'ir.actions.client',
            'tag':  'display_notification',
            'params': {
                'title': '📤 ส่งออกสำเร็จ',
                'message': (
                    'บันทึกสรุปผลไปที่ประวัติพนักงานแล้ว'
                    + (' และผูกเข้า Appraisal ล่าสุดแล้ว' if appraisal_linked else '')
                ),
                'type': 'success',
            },
        }

    # ============================================================
    # [G] ปุ่มเปลี่ยนสถานะตาม Workflow
    # Confirm → Approve → Mark as Paid / Reset
    # ============================================================
    def action_confirm(self):
        for rec in self:
            if rec.state == 'draft':
                # ดึง/อัปเดต bonus_pct จาก Backend ครั้งสุดท้ายก่อน lock ตัวเลข
                rec._apply_backend_bonus()
                rec.state = 'confirmed'
                rec.message_post(
                    body=(
                        f'✅ Confirmed โดย {self.env.user.name} — '
                        f'Tier {rec.incentive_tier}, Bonus {rec.bonus_pct}% '
                        f'= {rec.bonus_amount:,.2f} THB (Source: {rec.bonus_source or "-"})'
                    )
                )

    def action_approve(self):
        for rec in self:
            if rec.state == 'confirmed':
                rec.state       = 'approved'
                rec.approved_by = self.env.user
                rec.message_post(
                    body=(
                        f'👍 Approved โดย {self.env.user.name} — '
                        f'ยอดโบนัสที่อนุมัติ: {rec.bonus_amount:,.2f} THB '
                        f'({rec.driver_id.name}, {rec.period_label})'
                    )
                )

    def action_mark_paid(self):
        for rec in self:
            if rec.state == 'approved':
                rec.state = 'paid'
                rec.message_post(
                    body=(
                        f'💰 Marked as Paid โดย {self.env.user.name} — '
                        f'{rec.bonus_amount:,.2f} THB ({rec.driver_id.name}, {rec.period_label})'
                    )
                )

    def action_reset(self):
        for rec in self:
            if rec.state in ('confirmed', 'approved'):
                old_state        = rec.state
                rec.state       = 'draft'
                rec.approved_by = False
                rec.message_post(
                    body=(
                        f'↩️ Reset กลับเป็น Draft โดย {self.env.user.name} '
                        f'(เดิม: {old_state}) — {rec.driver_id.name}, {rec.period_label}'
                    )
                )

    # ============================================================
    # [H] Cron — สร้างใบโบนัส Draft อัตโนมัติทุกวันที่ 1 ของเดือน
    # แก้ 2026-07-09: สร้างด้วย date_from/date_to ตรงๆ (ครอบคลุมเต็มเดือน
    # ปฏิทินก่อนหน้า) แทนการกรอก period_month/period_year ที่ตัดไปแล้ว
    # ============================================================
    @api.model
    def _cron_calculate_monthly_incentive(self):
        today = date.today()
        if today.month == 1:
            period_year, period_month = today.year - 1, 12
        else:
            period_year, period_month = today.year, today.month - 1

        date_from = date(period_year, period_month, 1)
        date_to = (
            date(period_year + 1, 1, 1) if period_month == 12
            else date(period_year, period_month + 1, 1)
        ) - timedelta(days=1)  # date_to เป็น "วันสุดท้ายที่รวมอยู่ในช่วง"

        cfg = self.env['fleet.telematics.scoring.config'].sudo().search(
            [('active', '=', True)], limit=1)

        TripLog     = self.env['fleet.telematics.log'].sudo()
        date_to_excl = date_to + timedelta(days=1)

        logs = TripLog.search([
            ('trip_start', '>=', str(date_from)),
            ('trip_start', '<',  str(date_to_excl)),
            ('state', '=', 'synced'),
        ])

        created = 0
        for driver in logs.mapped('driver_id'):
            if self.search([
                ('driver_id', '=', driver.id),
                ('date_from', '=', str(date_from)),
                ('date_to',   '=', str(date_to)),
            ], limit=1):
                continue  # dedup — driver แต่ละคนมีได้เพียง 1 record ต่อรอบ

            new_rec = self.create({
                'driver_id':         driver.id,
                'scoring_config_id': cfg.id if cfg else False,
                'date_from':         date_from,
                'date_to':           date_to,
                'state':             'draft',
            })
            new_rec._apply_backend_bonus()
            created += 1

        _logger.info(
            'cron_monthly_incentive: สร้าง %d records สำหรับ %02d/%d',
            created, period_month, period_year
        )