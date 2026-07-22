# ==============================================================================
# models/telematics_incentive.py
#
# ใบโบนัสรายเดือนของพนักงานขับรถ — คำนวณ Tier/% โบนัสจากคะแนนพฤติกรรม
# การขับขี่สะสม แล้วคูณกับฐานเงินเดือนได้เป็นยอดเงินโบนัสสุทธิ
#
# Workflow: Draft (แก้ไขได้) → Confirmed → Approved → Paid (ล็อกถาวรตั้งแต่
# พ้น Draft ต้องกด Reset กลับ Draft ก่อนถึงจะแก้ไขได้อีก)
#
# Bonus % ดึงจาก Backend (GET /drivers/{id}/bonus) เป็นหลัก — ถ้าเรียกไม่ได้
# (Backend ล่ม/timeout) จะคำนวณสำรองในเครื่องจาก threshold ของ Scoring
# Config แทน (bonus_source = local_fallback) เพื่อไม่ให้ระบบหยุดทำงาน
# ==============================================================================
import logging
from datetime import date, timedelta

import requests
from markupsafe import Markup

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class TelematicsIncentive(models.Model):
    """ใบโบนัส 1 ใบ = พนักงาน 1 คน ในช่วงวันที่ 1 ช่วง (รองรับรอบตัดวิกที่
    ไม่ตรงเดือนปฏิทิน) — ผูก mail.thread ไว้เก็บ audit log ทุกครั้งที่
    สถานะเปลี่ยน เพราะเป็นข้อมูลการเงิน"""
    _name        = 'fleet.telematics.incentive'
    _description = 'Fleet Telematics Monthly Incentive'
    _order       = 'period_year desc, period_month desc'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    # ห้ามพนักงานคนเดียวกันมีใบโบนัสช่วงวันที่เดียวกันซ้ำ 2 ใบ
    _driver_period_unique = models.Constraint(
        'UNIQUE(driver_id, date_from, date_to)',
        'พนักงานคนนี้มีรายการโบนัสของช่วงวันที่นี้อยู่แล้ว — ห้ามสร้างซ้ำ',
    )

    driver_id = fields.Many2one(
        'hr.employee', string='Driver', required=True)
    # field แบบ related+store แบบ flat ระดับเดียว ใช้เฉพาะสำหรับ record rule
    # (security/telematics_security.xml) — Odoo ตรวจ domain แบบ dotted-path
    # หลายชั้นในบางกรณีมีปัญหา ใช้ field ตรงนี้ตัดปัญหาไปเลย ไม่ต้องแสดงในฟอร์ม
    driver_user_id = fields.Many2one(
        'res.users', string='Driver User (internal)',
        related='driver_id.user_id', store=True, readonly=True,
        help='ใช้ภายในสำหรับ record rule เท่านั้น — ไม่ต้องแสดงในฟอร์ม')
    scoring_config_id = fields.Many2one(
        'fleet.telematics.scoring.config',
        string='Scoring Config (snapshot)',
        help='Snapshot ของ config ที่ใช้คำนวณรอบนี้')

    # ใช้ช่วงวันที่จริงแทนการเลือกเดือน/ปีแยก เพื่อรองรับรอบตัดวิกที่ไม่ตรง
    # เดือนปฏิทิน (เช่น 26 ของเดือนก่อน ถึง 25 เดือนนี้)
    date_from = fields.Date(string='วันที่เริ่มต้น', required=True)
    date_to   = fields.Date(string='วันที่สิ้นสุด', required=True)

    # เดือน/ปีคำนวณอัตโนมัติจาก date_from เก็บไว้ให้ report/portal เดิมที่
    # อ้างอิง field นี้อยู่ยังทำงานได้ปกติ ผู้ใช้ไม่ต้องกรอกเอง
    period_month = fields.Integer(
        string='Month', compute='_compute_period_ints', store=True)
    period_year = fields.Integer(
        string='Year', compute='_compute_period_ints', store=True)
    period_label = fields.Char(
        string='Period',
        compute='_compute_period_label', store=True,
        help='แสดงผลเป็นช่วงวันที่ เช่น 01/06/2026 - 25/06/2026')

    # ── สถิติสรุปจาก Trip Logs ในช่วงวันที่นี้ ────────────────────────────
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

    # ── ผลลัพธ์ Tier และยอดโบนัส ────────────────────────────────────────
    # bonus_pct/incentive_tier ดึงจาก Backend ผ่าน _apply_backend_bonus()
    # (ตอนสร้างจาก cron หรือกดปุ่ม "Refresh from Backend" เอง) ไม่ใช่ compute
    # field อัตโนมัติเพราะต้องเรียก API ภายนอก
    incentive_tier = fields.Selection([
        ('A', 'A — Excellent'),
        ('B', 'B — Good'),
        ('C', 'C — Fair'),
        ('D', 'D — Needs Improvement'),
    ], string='Tier', default='D', readonly=True)
    bonus_pct    = fields.Float(string='Bonus %',      digits=(5, 2),  default=0.0, readonly=True)
    # readonly คุมที่ระดับ View (readonly="is_locked") ไม่ใช่ hardcode ใน
    # Python — แก้ไขได้เฉพาะตอน state=Draft เท่านั้น
    base_salary  = fields.Float(string='Base Salary',  digits=(10, 2), default=0.0)
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
    # กันแจ้งเตือน HR ซ้ำหลายรอบ ถ้ากด Refresh from Backend ซ้ำๆ ขณะที่ยัง
    # เป็น Tier D อยู่เหมือนเดิม (ดู _notify_hr_tier_d)
    tier_d_notified = fields.Boolean(string='แจ้งเตือน Tier D แล้ว', default=False, readonly=True)

    is_locked = fields.Boolean(
        string='ล็อกการแก้ไข', compute='_compute_is_locked',
        help='True เมื่อ state ไม่ใช่ Draft แล้ว — ฟิลด์ทั้งหมดแก้ไขไม่ได้ '
             'จนกว่าจะกด Reset กลับเป็น Draft')

    @api.depends('state')
    def _compute_is_locked(self):
        for rec in self:
            rec.is_locked = rec.state != 'draft'

    state = fields.Selection([
        ('draft',     'Draft'),
        ('confirmed', 'Confirmed'),
        ('approved',  'Approved'),
        ('paid',      'Paid'),
    ], default='draft', tracking=True)  # tracking=True → chatter บันทึก log
                                         # ทุกครั้งที่ state เปลี่ยน (audit log)
    approved_by = fields.Many2one('res.users', string='Approved By', readonly=True, tracking=True)
    note        = fields.Text(string='Notes')

    @api.depends('date_from')
    def _compute_period_ints(self):
        """แยกเดือน/ปีออกจาก date_from ให้อัตโนมัติ"""
        for rec in self:
            if rec.date_from:
                rec.period_month = rec.date_from.month
                rec.period_year  = rec.date_from.year
            else:
                rec.period_month = 0
                rec.period_year  = 0

    @api.depends('date_from', 'date_to')
    def _compute_period_label(self):
        """แสดงช่วงวันที่แบบอ่านง่าย เช่น 01/06/2026 - 25/06/2026"""
        for rec in self:
            if rec.date_from and rec.date_to:
                rec.period_label = (
                    f'{rec.date_from.strftime("%d/%m/%Y")} - '
                    f'{rec.date_to.strftime("%d/%m/%Y")}'
                )
            else:
                rec.period_label = '-'

    @api.depends('driver_id', 'date_from', 'date_to')
    def _compute_incentive(self):
        """ดึง Trip Log ของพนักงานคนนี้ในช่วงวันที่ที่กำหนด (date_to นับ
        รวมเป็นวันสุดท้ายด้วย) มาคำนวณสถิติสรุป — ไม่รวม bonus_pct/tier
        ตรงนี้ ย้ายไปคำนวณใน _apply_backend_bonus() แยกต่างหาก"""
        TripLog = self.env['fleet.telematics.log'].sudo()
        for rec in self:
            if not (rec.driver_id and rec.date_from and rec.date_to):
                rec.avg_score = rec.min_score = 0.0
                rec.total_trips = rec.total_harsh_events = 0
                rec.total_distance_km = rec.total_idle_min = 0.0
                continue

            date_from_excl = rec.date_to + timedelta(days=1)

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

    def _apply_backend_bonus(self):
        """คำนวณ/อัปเดต Base Salary + Tier + Bonus % ให้ใบโบนัสนี้ทั้งหมด
        เรียกตอนกดปุ่ม "Refresh from Backend" หรือตอน Confirm

        ลำดับการหาฐานเงินเดือน:
          1) hr.version (Odoo 19) ที่ is_current=True ของพนักงานคนนี้
          2) hr.contract (Odoo เวอร์ชันเก่ากว่า 19 ที่ยังใช้โมเดลนี้อยู่)
          3) telematics_base_salary ที่กรอกไว้บนโปรไฟล์พนักงาน (fallback)
          4) ค่าที่กรอกเองไว้ในใบนี้ (fallback สุดท้าย)

        ลำดับการหา Tier/Bonus %:
          1) ยิง GET /drivers/{id}/bonus ไปที่ Backend
          2) ถ้าเรียกไม่ได้ → คำนวณสำรองในเครื่องจาก Scoring Config
             (bonus_source = local_fallback)
        """
        no_driver = self.filtered(lambda r: not r.driver_id)
        if no_driver:
            raise UserError('กรุณาเลือก Driver ก่อน ถึงจะคำนวณโบนัสได้')

        Config = self.env['fleet.telematics.config']
        api_url = Config.get_active_api_url()
        api_key = Config.get_active_api_key()

        for rec in self:
            base_salary = 0.0
            found_from_contract = False

            if 'hr.version' in self.env:
                # is_current เป็น compute field ที่ไม่ได้ stored ในฐานข้อมูล
                # จึงใช้ใน search() domain ตรงๆ ไม่ได้ ต้องดึงทุก version
                # ของพนักงานคนนั้นมาก่อน แล้วกรองด้วย .filtered() ภายหลัง
                # (ตอนนั้น compute field จะคำนวณค่าให้ตามปกติ เพราะไม่ผ่าน SQL)
                all_versions = self.env['hr.version'].sudo().search([
                    ('employee_id', '=', rec.driver_id.id),
                ])
                version = all_versions.filtered(lambda v: v.is_current)[:1]
                if version:
                    base_salary = version.wage or 0.0
                    found_from_contract = True
            elif 'hr.contract' in self.env:
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
                        # ชื่อ key จริงจาก Backend คือ 'incentive_tier' ไม่ใช่ 'tier'
                        tier = data.get('incentive_tier')
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
                tier_fb, pct_fb = rec._local_tier_from_score(return_pct=True)
                rec.write({
                    'base_salary':       base_salary,
                    'bonus_pct':         pct_fb,
                    'incentive_tier':    tier_fb,
                    'bonus_source':      'local_fallback',
                    'bonus_last_synced': fields.Datetime.now(),
                })

            # Tier D ต้องแจ้งเตือน HR ด้วย ไม่ใช่แค่ตั้ง bonus_pct=0 เฉยๆ —
            # ครอบด้วย try/except เพราะเป็นฟีเจอร์เสริม ไม่ควรทำให้การคำนวณ
            # โบนัสหลัก (สำคัญกว่า) พังไปด้วยถ้าระบบแจ้งเตือนมีปัญหา
            if rec.incentive_tier == 'D':
                try:
                    rec._notify_hr_tier_d()
                except Exception:
                    _logger.exception(
                        '_notify_hr_tier_d ล้มเหลวสำหรับใบโบนัส id=%s — ข้ามไป '
                        'ไม่ให้กระทบการคำนวณโบนัสหลัก', rec.id,
                    )

    def _local_tier_from_score(self, return_pct=False):
        """คำนวณ Tier สำรองในเครื่องจาก threshold ของ Scoring Config —
        ใช้เฉพาะตอนเรียก Backend ไม่สำเร็จเท่านั้น"""
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
            # ใช้ tier_d_bonus_pct จาก config ถ้ามี (ให้ตรงกับที่ Admin ตั้งไว้
            # ในหน้าจอ) ไม่ hardcode 0.0 ตรงๆ — เผื่อ Admin ปรับ Tier D ให้
            # ได้โบนัสบางส่วนแทนที่จะเป็น 0% เสมอ
            tier, pct = 'D', (cfg.tier_d_bonus_pct if cfg else 0.0)
        return (tier, pct) if return_pct else tier

    def _notify_hr_tier_d(self):
        """แจ้งเตือน HR/Fleet Manager เมื่อพนักงานได้ Tier D (คะแนนต่ำกว่า
        เกณฑ์ ไม่ได้รับโบนัส) — แจ้ง 2 ช่องทาง: บันทึกลง chatter ของใบโบนัส
        เอง และส่งแจ้งเตือนตรงถึงผู้ใช้ในกลุ่ม Fleet Manager กันแจ้งซ้ำด้วย
        flag tier_d_notified"""
        self.ensure_one()
        if self.tier_d_notified:
            return

        # ใช้ Markup(...).format(...) แทนต่อ f-string ตรงๆ เพื่อให้ค่าที่
        # แทรกเข้าไป (ชื่อพนักงาน, period_label) ถูก escape อัตโนมัติ กัน
        # ปัญหาถ้าชื่อพนักงานมีอักขระพิเศษปนอยู่ ในขณะที่ tag HTML ของ
        # template เอง (<b>, <br/>) ยัง render ได้ปกติ
        body = Markup(
            '⚠️ <b>แจ้งเตือน Tier D — พนักงานคะแนนต่ำกว่าเกณฑ์</b><br/>'
            'พนักงาน: <b>{driver_name}</b><br/>'
            'รอบ: {period}<br/>'
            'คะแนนเฉลี่ย: {score} (ต่ำกว่าเกณฑ์ขั้นต่ำของ Tier C)<br/>'
            'ผลลัพธ์: ไม่ได้รับโบนัสในรอบนี้ (0%)<br/><br/>'
            'Tier D ต้องแจ้งเตือน HR/Fleet Manager เพื่อพิจารณาติดตาม'
            'พฤติกรรมการขับขี่ของพนักงานคนนี้'
        ).format(
            driver_name=self.driver_id.name,
            period=self.period_label,
            score=f'{self.avg_score:.2f}',
        )

        self.message_post(body=body)

        # หา user ในกลุ่ม Fleet Manager — query จาก res.users แทนการอ่าน
        # .users จากกลุ่มตรงๆ (field นั้นถูกถอดออกไปในบาง Odoo version แล้ว)
        # เช็ค field ที่มีอยู่จริงก่อนใช้งาน กันพังถ้าเปลี่ยนชื่ออีก
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

    def _notify_hr_new_drafts_batch(self, period_month, period_year):
        """แจ้งเตือน HR/Fleet Manager ว่ามีใบโบนัส Draft รอตรวจสอบ — ตาม
        FDD §12.4 ขั้นตอนที่ 4 ของ Incentive Workflow เรียกครั้งเดียวต่อรอบ
        cron (ไม่แยกอีเมลต่อพนักงาน) สรุปจำนวนทั้งหมดในข้อความเดียว —
        คนละฟังก์ชันกับ _notify_hr_tier_d() ที่แจ้งเฉพาะกรณี Tier D

        แจ้ง 2 ช่องทางเหมือน _notify_hr_tier_d(): (1) message_post() ลง
        chatter ของใบโบนัสแต่ละใบในชุดนี้ ให้เห็นประวัติแจ้งเตือนติดกับ
        เอกสาร และ (2) message_notify() ส่งตรงถึงผู้ใช้กลุ่ม Fleet Manager
        """
        if not self:
            return

        driver_names = ', '.join(self.mapped('driver_id.name')[:10])
        more = f' และอีก {len(self) - 10} คน' if len(self) > 10 else ''

        body = Markup(
            '📋 <b>มีใบโบนัสรอตรวจสอบ — รอบ {month:02d}/{year}</b><br/>'
            'สร้าง Draft อัตโนมัติแล้ว <b>{count}</b> ใบ<br/>'
            'พนักงาน: {names}{more}<br/><br/>'
            'กรุณาตรวจสอบและกด Confirm ที่เมนู Incentive / Bonus'
        ).format(
            month=period_month, year=period_year,
            count=len(self), names=driver_names, more=more,
        )

        # (1) บันทึกลง chatter ของทุกใบโบนัสในชุดนี้ — ทำให้ message_ids ของ
        # แต่ละ record เพิ่มขึ้นจริง (message_notify อย่างเดียวไม่พอ เพราะ
        # ไม่ผูกเข้ากับ thread ของ record แต่ละตัวในชุด)
        for rec in self:
            rec.message_post(body=body)

        managers_group = self.env.ref('fleet.fleet_group_manager', raise_if_not_found=False)
        manager_users = self.env['res.users']
        if managers_group:
            User = self.env['res.users']
            for fname in ('groups_id', 'group_ids'):
                if fname in User._fields:
                    manager_users = User.sudo().search([(fname, 'in', managers_group.ids)])
                    break

        if manager_users:
            # (2) ส่งแจ้งเตือนตรงถึงผู้ใช้กลุ่ม Fleet Manager เพิ่มอีกทาง —
            # ใช้ record แรกเป็นตัวส่ง (message_notify ต้องมี record เดียว
            # ไม่ใช่ recordset หลายตัว) พร้อมแนบสรุปทั้งหมดไว้ใน body
            self[0].message_notify(
                partner_ids=manager_users.partner_id.ids,
                body=body,
                subject=f'📋 Incentive Draft รอตรวจสอบ — {period_month:02d}/{period_year} ({len(self)} ใบ)',
            )
        else:
            _logger.warning(
                '_notify_hr_new_drafts_batch: ไม่พบผู้ใช้ในกลุ่ม Fleet Manager '
                'ที่จะแจ้งเตือน (%d draft ใหม่ รอบ %02d/%d)',
                len(self), period_month, period_year,
            )

    @api.depends('base_salary', 'bonus_pct')
    def _compute_bonus_amount(self):
        """สูตรตายตัว: Bonus (THB) = Base Salary × Bonus % — เป็น compute
        field จริง แก้ไขตรงๆ ไม่ได้เลย ป้องกันตัวเลขหลุดจากสูตร"""
        for rec in self:
            rec.bonus_amount = round(rec.base_salary * rec.bonus_pct / 100, 2)

    def action_refresh_bonus_from_backend(self):
        """ปุ่มในฟอร์ม — ดึง bonus_pct ล่าสุดจาก Backend ใหม่ด้วยมือ"""
        self._apply_backend_bonus()

    # ── ล็อกทุกฟิลด์ถาวรเมื่อพ้น Draft ───────────────────────────────────
    # แก้ไขได้ทางเดียวคือกด Reset กลับ Draft ก่อน ป้องกันไม่ให้มีใครแก้ตัวเลข
    # ยอดบาทกลางคันหลังยืนยัน/อนุมัติไปแล้ว
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

    def action_export_to_appraisal(self):
        """ส่งสรุปผลไปที่ประวัติพนักงาน (chatter ของ hr.employee — มีอยู่
        แล้วทุก Odoo ไม่ต้องพึ่งโมดูลเสริม) และถ้ามีโมดูล hr_appraisal
        ติดตั้งอยู่ด้วยจะโพสต์ซ้ำลง appraisal ล่าสุดของพนักงานคนนั้นให้
        อัตโนมัติ ต้อง Approve ก่อนเท่านั้นถึงจะกดได้"""
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

    def action_confirm(self):
        """Draft → Confirmed — ดึง/อัปเดต bonus_pct จาก Backend ครั้งสุดท้าย
        ก่อนล็อกตัวเลข"""
        for rec in self:
            if rec.state == 'draft':
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
        """Confirmed → Approved"""
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
        """Approved → Paid"""
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
        """Confirmed/Approved → กลับ Draft (ปลดล็อกให้แก้ไขได้อีก)"""
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

    @api.model
    def _cron_calculate_monthly_incentive(self):
        """สร้างใบโบนัส Draft ให้พนักงานที่มีทริปในเดือนก่อนหน้าอัตโนมัติ
        ทุกวันที่ 1 ของเดือน (ครอบคลุมเต็มเดือนปฏิทินก่อนหน้าเสมอ)"""
        today = date.today()
        if today.month == 1:
            period_year, period_month = today.year - 1, 12
        else:
            period_year, period_month = today.year, today.month - 1

        date_from = date(period_year, period_month, 1)
        date_to = (
            date(period_year + 1, 1, 1) if period_month == 12
            else date(period_year, period_month + 1, 1)
        ) - timedelta(days=1)  # date_to คือวันสุดท้ายที่ "รวม" อยู่ในช่วง

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
        created_records = self.browse()
        for driver in logs.mapped('driver_id'):
            if self.search([
                ('driver_id', '=', driver.id),
                ('date_from', '=', str(date_from)),
                ('date_to',   '=', str(date_to)),
            ], limit=1):
                continue  # พนักงานคนนี้มีใบของรอบนี้อยู่แล้ว ข้าม

            new_rec = self.create({
                'driver_id':         driver.id,
                'scoring_config_id': cfg.id if cfg else False,
                'date_from':         date_from,
                'date_to':           date_to,
                'state':             'draft',
            })
            new_rec._apply_backend_bonus()
            created_records |= new_rec
            created += 1

        # ตาม FDD §12.4 ขั้นตอนที่ 4 ของ Incentive Workflow: "ส่ง Email แจ้ง
        # HR Manager ว่ามี Incentive รอ review" ทุกครั้งที่ cron สร้าง draft
        # — ส่งเป็นสรุปเดียวต่อรอบ cron (ไม่ใช่แยกอีเมลต่อพนักงาน กันสแปม
        # ถ้ามีพนักงานจำนวนมาก) ครอบด้วย try/except กันไม่ให้การแจ้งเตือน
        # (ฟีเจอร์เสริม) ทำให้การสร้าง draft หลัก (สำคัญกว่า) ล้มเหลวไปด้วย
        if created_records:
            try:
                created_records._notify_hr_new_drafts_batch(period_month, period_year)
            except Exception:
                _logger.exception(
                    '_notify_hr_new_drafts_batch ล้มเหลว — ข้ามไป ไม่ให้กระทบ '
                    'การสร้าง draft หลัก'
                )

        _logger.info(
            'cron_monthly_incentive: สร้าง %d records สำหรับ %02d/%d',
            created, period_month, period_year
        )
