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
from datetime import date

import requests

from odoo import models, fields, api

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
    _sql_constraints = [
        ('driver_period_unique',
         'UNIQUE(driver_id, period_month, period_year)',
         'พนักงานคนนี้มีรายการโบนัสของเดือน/ปีนี้อยู่แล้ว — ห้ามสร้างซ้ำ'),
    ]

    # ============================================================
    # [A] ระบุว่าคำนวณโบนัสของใคร รอบไหน
    # snapshot ของ scoring config ป้องกันผลกระทบเมื่อแก้ config ภายหลัง
    # ============================================================
    driver_id = fields.Many2one(
        'hr.employee', string='Driver', required=True)
    scoring_config_id = fields.Many2one(
        'fleet.telematics.scoring.config',
        string='Scoring Config (snapshot)',
        help='Snapshot ของ config ที่ใช้คำนวณรอบนี้')
    period_month = fields.Integer(string='Month')
    period_year  = fields.Integer(string='Year')
    period_label = fields.Char(
        string='Period',
        compute='_compute_period_label', store=True,
        help='แสดงผลเป็น MM/YYYY เช่น 05/2025')

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
    ], string='Tier', default='D')
    bonus_pct    = fields.Float(string='Bonus %',      digits=(5, 2),  default=0.0)
    base_salary  = fields.Float(string='Base Salary',  digits=(10, 2), default=0.0)
    bonus_amount = fields.Float(string='Bonus (THB)',  digits=(10, 2), default=0.0)
    bonus_source = fields.Selection([
        ('backend', 'Backend API'),
        ('local_fallback', 'Local Fallback (Backend ไม่พร้อมใช้งาน)'),
    ], string='Bonus Source', readonly=True,
        help='ระบุว่า bonus_pct ปัจจุบันมาจาก Backend จริง หรือคำนวณสำรองในเครื่อง')
    bonus_last_synced = fields.Datetime(string='Bonus Synced At', readonly=True)

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
    # [E] Computed — แสดง Period เป็น MM/YYYY
    # ============================================================
    @api.depends('period_month', 'period_year')
    def _compute_period_label(self):
        for rec in self:
            if rec.period_month and rec.period_year:
                rec.period_label = f'{rec.period_month:02d}/{rec.period_year}'
            else:
                rec.period_label = '-'

    # ============================================================
    # [F] คำนวณสถิติจาก Trip Logs เท่านั้น (avg_score, total_trips ฯลฯ)
    # ไม่รวม bonus_pct/tier แล้ว — ย้ายไป _apply_backend_bonus() ด้านล่าง
    # ============================================================
    @api.depends('driver_id', 'period_month', 'period_year')
    def _compute_incentive(self):
        TripLog = self.env['fleet.telematics.log'].sudo()
        for rec in self:
            if not (rec.driver_id and rec.period_month and rec.period_year):
                rec.avg_score = rec.min_score = 0.0
                rec.total_trips = rec.total_harsh_events = 0
                rec.total_distance_km = rec.total_idle_min = 0.0
                continue

            y, m = rec.period_year, rec.period_month
            date_from = date(y, m, 1)
            date_to   = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)

            logs = TripLog.search([
                ('driver_id',  '=', rec.driver_id.id),
                ('trip_start', '>=', str(date_from)),
                ('trip_start', '<',  str(date_to)),
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
        Config = self.env['fleet.telematics.config']
        api_url = Config.get_active_api_url()
        api_key = Config.get_active_api_key()

        for rec in self:
            # ดึง base_salary จาก hr.contract (สัญญาจ้างที่ active) เสมอ
            contract = self.env['hr.contract'].sudo().search([
                ('employee_id', '=', rec.driver_id.id),
                ('state', '=', 'open'),
            ], limit=1)
            base_salary = contract.wage if contract else 0.0

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
                    'bonus_amount':      round(base_salary * bonus_pct / 100, 2),
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
                    'bonus_amount':      round(base_salary * pct_fb / 100, 2),
                    'bonus_source':      'local_fallback',
                    'bonus_last_synced': fields.Datetime.now(),
                })

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

    def action_refresh_bonus_from_backend(self):
        """ปุ่มในฟอร์ม — ดึง bonus_pct ล่าสุดจาก Backend ใหม่ด้วยมือ"""
        self._apply_backend_bonus()

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
    # ============================================================
    @api.model
    def _cron_calculate_monthly_incentive(self):
        today = date.today()
        if today.month == 1:
            period_year, period_month = today.year - 1, 12
        else:
            period_year, period_month = today.year, today.month - 1

        cfg = self.env['fleet.telematics.scoring.config'].sudo().search(
            [('active', '=', True)], limit=1)

        TripLog   = self.env['fleet.telematics.log'].sudo()
        date_from = date(period_year, period_month, 1)
        date_to   = date(period_year + 1, 1, 1) if period_month == 12 \
                    else date(period_year, period_month + 1, 1)

        logs = TripLog.search([
            ('trip_start', '>=', str(date_from)),
            ('trip_start', '<',  str(date_to)),
            ('state', '=', 'synced'),
        ])

        created = 0
        for driver in logs.mapped('driver_id'):
            if self.search([
                ('driver_id',    '=', driver.id),
                ('period_month', '=', period_month),
                ('period_year',  '=', period_year),
            ], limit=1):
                continue  # dedup — driver แต่ละคนมีได้เพียง 1 record ต่อเดือน

            new_rec = self.create({
                'driver_id':         driver.id,
                'scoring_config_id': cfg.id if cfg else False,
                'period_month':      period_month,
                'period_year':       period_year,
                'state':             'draft',
            })
            new_rec._apply_backend_bonus()
            created += 1

        _logger.info(
            'cron_monthly_incentive: สร้าง %d records สำหรับ %02d/%d',
            created, period_month, period_year
        )
