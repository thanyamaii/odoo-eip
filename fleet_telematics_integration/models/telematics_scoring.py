# ==============================================================================
# models/telematics_scoring.py
#
# เกณฑ์คะแนนพฤติกรรมการขับขี่ (Scoring Config) — Admin ปรับน้ำหนักคะแนน/
# threshold ต่างๆ ได้เองจากหน้าจอ ไม่ต้องแก้โค้ด แล้วกด Push ส่งเกณฑ์นี้ไป
# ให้ Backend ใช้คำนวณคะแนนจริงตอนประมวลผลทริป
#
# workflow: สร้าง (Active=False) → กรอกเกณฑ์ → Approve (ต้องเป็น Fleet
# Manager) → Push Config → เปิด Active (ล็อกฟอร์มถาวรจนกว่าจะปิด Active)
# ==============================================================================
import logging
import requests

from odoo import models, fields, api
from odoo.exceptions import ValidationError, UserError

_logger = logging.getLogger(__name__)


class TelematicsScoringConfig(models.Model):
    """เกณฑ์คะแนน 1 ชุด — อนุญาตให้ Active พร้อมกันได้แค่ชุดเดียวในระบบ"""
    _name        = 'fleet.telematics.scoring.config'
    _description = 'Fleet Telematics Scoring Configuration'
    _order       = 'effective_date desc'

    name           = fields.Char(string='Config Name', required=True)
    active         = fields.Boolean(string='Active', default=False,
        help='Active ได้เพียง 1 config เท่านั้น — ตอนสร้างใหม่ต้องปิดไว้ก่อน '
             'เพื่อให้กรอกข้อมูลและทดสอบ Push ได้ก่อนเปิดใช้งานจริง')
    effective_date = fields.Date(string='Effective Date', required=True)

    # ── คะแนนพื้นฐาน ────────────────────────────────────────────────────
    score_base          = fields.Float(string='Base Score (เต็ม)', default=100.0)
    max_deduct_per_trip = fields.Float(string='Max Deduct / Trip', default=50.0)

    # ── คะแนนที่หักต่อครั้งของแต่ละพฤติกรรม ─────────────────────────────
    harsh_brake_deduct  = fields.Float(string='Harsh Brake Deduct',  default=5.0)
    harsh_accel_deduct  = fields.Float(string='Harsh Accel Deduct',  default=3.0)
    harsh_corner_deduct = fields.Float(string='Harsh Corner Deduct', default=3.0)
    speeding_deduct     = fields.Float(string='Speeding Deduct',     default=10.0)
    idling_deduct       = fields.Float(string='Idling Deduct',       default=2.0)
    bump_deduct         = fields.Float(string='Bump Deduct',         default=4.0)

    # ── เกณฑ์ตัดสินว่าเป็นพฤติกรรมเสี่ยงหรือไม่ ──────────────────────────
    harsh_brake_g      = fields.Float(string='Brake G Threshold',         default=0.40)
    harsh_accel_g      = fields.Float(string='Accel G Threshold',         default=0.40)
    harsh_corner_g     = fields.Float(string='Corner G Threshold',        default=0.40)
    speeding_kmh_over  = fields.Float(string='Speeding (km/h เกินกำหนด)', default=20.0)
    idle_min_threshold = fields.Float(string='Idle Min Threshold (min)',   default=5.0)

    # ── ความเร็วจำกัดแยกโซน (กทม./นอกเมือง) ──────────────────────────────
    # ส่งไปพร้อม Push Config ให้ Backend ใช้ตัดสินว่า event ไหนนับเป็น
    # "speeding" ตามโซนที่รถวิ่งอยู่จริง — ทำงานคู่กับ zone_label/
    # speed_limit_kmh ที่คำนวณไว้บน fleet.telematics.event ฝั่ง Odoo เพื่อ
    # cross-check ย้อนหลังได้
    speed_limit_bkk = fields.Float(
        string='ความเร็วจำกัดในกรุงเทพฯ (km/h)', default=80.0,
        help='ใช้กับ event ที่พิกัดอยู่ในเขตกรุงเทพฯ')
    speed_limit_upcountry = fields.Float(
        string='ความเร็วจำกัดนอกเมือง (km/h)', default=90.0,
        help='ใช้กับ event ที่พิกัดอยู่นอกเขตกรุงเทพฯ')

    # ── เกณฑ์ Tier และ % โบนัส ────────────────────────────────────────────
    tier_a_min_score = fields.Float(string='Tier A — Min Score', default=90.0)
    tier_a_bonus_pct = fields.Float(string='Tier A — Bonus %',  default=10.0)
    tier_b_min_score = fields.Float(string='Tier B — Min Score', default=75.0)
    tier_b_bonus_pct = fields.Float(string='Tier B — Bonus %',  default=5.0)
    tier_c_min_score = fields.Float(string='Tier C — Min Score', default=60.0)
    tier_c_bonus_pct = fields.Float(string='Tier C — Bonus %',  default=0.0)
    # เพิ่มตาม FDD §12.3 — ตาราง Tier ระบุว่า Admin ปรับ Tier D ได้เหมือน
    # A/B/C ทุกประการ (เดิมโค้ด hardcode Tier D ไว้เป็น fallback ในลอจิก
    # ไม่ใช่ field ที่ปรับได้จาก UI)
    #
    # tier_d_min_score: ปกติควรเป็น 0 เสมอ (นิยาม: คะแนนต่ำกว่า Tier C
    # ทั้งหมดคือ D อยู่แล้ว) เก็บเป็น field ให้แก้ไขได้เพื่อความสอดคล้องกับ
    # สเปคเท่านั้น — ไม่มีจุดไหนในโค้ดเทียบค่านี้จริง
    #
    # tier_d_bonus_pct: มีผลจริงทั้ง 2 เส้นทาง — (1) ถูกส่งไป Backend ผ่าน
    # _build_config_payload() ให้ Backend ใช้คำนวณเมื่อเชื่อมต่อสำเร็จ และ
    # (2) ถูกอ่านโดย telematics_incentive.py: _local_tier_from_score()
    # เป็น fallback ตอนเรียก Backend ไม่สำเร็จด้วย (ปกติตั้งเป็น 0% ตาม FDD
    # แต่ Admin ปรับเป็นค่าอื่นได้ถ้าต้องการ)
    tier_d_min_score = fields.Float(string='Tier D — Min Score', default=0.0)
    tier_d_bonus_pct = fields.Float(string='Tier D — Bonus %',  default=0.0)

    # ── History (readonly) — ตาม FDD §12.5: track ว่า config นี้ถูกใช้กับ
    # กี่ trip แล้ว ต่างจาก last_push_at/last_push_status ที่ track แค่การ
    # ส่งไป Backend เท่านั้น ไม่ใช่การใช้งานจริง ──────────────────────────
    created_date = fields.Datetime(
        string='Created Date', readonly=True,
        default=lambda self: fields.Datetime.now())
    last_used_date = fields.Datetime(
        string='Last Used Date', readonly=True,
        help='วันเวลาล่าสุดที่มี Trip ใช้ config ชุดนี้คำนวณคะแนน')
    total_trips_calculated = fields.Integer(
        string='Total Trips Calculated', readonly=True, default=0,
        help='จำนวน Trip สะสมที่เคยใช้ config ชุดนี้คำนวณคะแนนแล้ว')

    last_push_at     = fields.Datetime(string='Last Pushed At', readonly=True)
    last_push_status = fields.Char(string='Push Status',        readonly=True)

    # ต้องอนุมัติก่อนถึงจะ Push Config ไป Backend ได้จริง (บังคับใน
    # action_push_to_backend) — อนุมัติได้เฉพาะกลุ่ม Fleet Manager
    approved_by_id = fields.Many2one(
        'res.users', string='ผู้อนุมัติ', readonly=True,
        help='ผู้มีอำนาจอนุมัติเกณฑ์คะแนนชุดนี้ก่อนนำไปใช้จริง')
    approved_at = fields.Datetime(string='วันที่อนุมัติ', readonly=True)

    is_locked = fields.Boolean(
        string='ล็อกการแก้ไข', compute='_compute_is_locked',
        help='True เมื่อ Active=True เท่านั้น — ฟิลด์เกณฑ์ทั้งหมดจะแก้ไขไม่ได้ '
             'จนกว่าจะปิด Active (ตอน Active=False แก้ไข/Push ซ้ำได้เรื่อยๆ)')

    @api.depends('active')
    def _compute_is_locked(self):
        for rec in self:
            rec.is_locked = bool(rec.active)

    # ── ตรวจสอบความสมเหตุสมผลของค่าที่กรอก ──────────────────────────────

    @api.constrains('active')
    def _check_single_active(self):
        """ห้ามมี Config ที่ Active=True พร้อมกันเกิน 1 ชุดในระบบ"""
        for rec in self:
            if rec.active:
                others = self.search([('active', '=', True), ('id', '!=', rec.id)])
                if others:
                    raise ValidationError(
                        f'มี Scoring Config ที่ Active อยู่แล้ว: "{others[0].name}"\n'
                        'กรุณา deactivate config นั้นก่อน'
                    )

    @api.constrains('tier_a_min_score', 'tier_b_min_score', 'tier_c_min_score')
    def _check_tier_order(self):
        """เกณฑ์คะแนนขั้นต่ำต้องเรียงจากมากไปน้อย A > B > C > 0 เสมอ
        (Tier D คือทุกคะแนนที่ต่ำกว่า C ลงไป — tier_d_min_score เป็นแค่
        field เก็บไว้ให้ Admin เห็น ไม่ได้ใช้เทียบในการจัด tier จริง)"""
        for rec in self:
            if not (rec.tier_a_min_score > rec.tier_b_min_score > rec.tier_c_min_score > 0):
                raise ValidationError('Tier min score ต้องเรียงจากมากไปน้อย: A > B > C > 0')

    @api.constrains('tier_d_bonus_pct')
    def _check_tier_d_bonus_not_negative(self):
        """% โบนัส Tier D ต้องไม่ติดลบ (ปกติ 0% ตาม FDD แต่ห้ามติดลบ)"""
        for rec in self:
            if rec.tier_d_bonus_pct < 0:
                raise ValidationError('Tier D — Bonus % ต้องไม่ติดลบ')

    @api.constrains(
        'harsh_brake_deduct', 'harsh_accel_deduct', 'harsh_corner_deduct',
        'speeding_deduct', 'idling_deduct', 'bump_deduct',
        'score_base', 'max_deduct_per_trip',
    )
    def _check_positive_deducts(self):
        """คะแนนหักทุกประเภทต้อง >= 0 และคะแนนเต็มต้อง > 0"""
        deduct_fields = [
            ('harsh_brake_deduct',  'Harsh Brake Deduct'),
            ('harsh_accel_deduct',  'Harsh Accel Deduct'),
            ('harsh_corner_deduct', 'Harsh Corner Deduct'),
            ('speeding_deduct',     'Speeding Deduct'),
            ('idling_deduct',       'Idling Deduct'),
            ('bump_deduct',         'Bump Deduct'),
            ('max_deduct_per_trip', 'Max Deduct / Trip'),
        ]
        for rec in self:
            if rec.score_base <= 0:
                raise ValidationError(f'Base Score ต้องมากกว่า 0 (ค่าที่กรอก: {rec.score_base})')
            for field_name, label in deduct_fields:
                if getattr(rec, field_name, 0) < 0:
                    raise ValidationError(f'{label} ต้องมีค่า >= 0 (ค่าที่กรอก: {getattr(rec, field_name)})')

    @api.constrains('harsh_brake_g', 'harsh_accel_g', 'harsh_corner_g',
                    'speeding_kmh_over', 'idle_min_threshold')
    def _check_positive_thresholds(self):
        """threshold ตรวจจับพฤติกรรมเสี่ยงทุกตัวต้องมากกว่า 0"""
        threshold_fields = [
            ('harsh_brake_g',      'Brake G Threshold'),
            ('harsh_accel_g',      'Accel G Threshold'),
            ('harsh_corner_g',     'Corner G Threshold'),
            ('speeding_kmh_over',  'Speeding km/h'),
            ('idle_min_threshold', 'Idle Min Threshold'),
        ]
        for rec in self:
            for field_name, label in threshold_fields:
                if getattr(rec, field_name, 0) <= 0:
                    raise ValidationError(f'{label} ต้องมากกว่า 0 (ค่าที่กรอก: {getattr(rec, field_name)})')

    @api.constrains('speed_limit_bkk', 'speed_limit_upcountry')
    def _check_speed_limit_zone(self):
        """ความเร็วจำกัดต้องมากกว่า 0 และในกรุงเทพฯ ต้องไม่สูงกว่านอกเมือง"""
        for rec in self:
            if rec.speed_limit_bkk <= 0 or rec.speed_limit_upcountry <= 0:
                raise ValidationError('ความเร็วจำกัดตามโซน (กรุงเทพฯ/นอกเมือง) ต้องมากกว่า 0')
            if rec.speed_limit_bkk > rec.speed_limit_upcountry:
                raise ValidationError(
                    f'ความเร็วจำกัดในกรุงเทพฯ ({rec.speed_limit_bkk}) ไม่ควรสูงกว่า '
                    f'นอกเมือง ({rec.speed_limit_upcountry}) — ตรวจค่าที่กรอกอีกครั้ง'
                )

    @api.constrains('score_base', 'max_deduct_per_trip')
    def _check_max_deduct_not_exceed_base(self):
        """หักคะแนนสูงสุดต่อทริปต้องไม่เกินคะแนนเต็ม"""
        for rec in self:
            if rec.max_deduct_per_trip > rec.score_base:
                raise ValidationError(
                    f'Max Deduct / Trip ({rec.max_deduct_per_trip}) ต้องไม่เกิน Base Score ({rec.score_base})'
                )

    # ── ล็อกฟิลด์เกณฑ์คะแนนทั้งหมดเมื่อ Active=True ──────────────────────
    # นี่คือชั้น Python (บังคับจริงแม้เรียกผ่าน API/RPC ตรงๆ) ส่วนชั้น XML
    # (readonly บนฟอร์ม) อยู่ที่ views/telematics_scoring_views.xml — ไม่ล็อก
    # field สถานะ (last_push_at, approved_by_id ฯลฯ) และไม่ล็อก 'active' เอง
    # เพื่อให้ผู้ใช้ปิด Active ปลดล็อกฟิลด์อื่นได้เสมอ
    _LOCKED_CONFIG_FIELDS = {
        'name', 'effective_date',
        'score_base', 'max_deduct_per_trip',
        'harsh_brake_deduct', 'harsh_accel_deduct', 'harsh_corner_deduct',
        'speeding_deduct', 'idling_deduct', 'bump_deduct',
        'harsh_brake_g', 'harsh_accel_g', 'harsh_corner_g',
        'speeding_kmh_over', 'idle_min_threshold',
        'speed_limit_bkk', 'speed_limit_upcountry',
        'tier_a_min_score', 'tier_a_bonus_pct',
        'tier_b_min_score', 'tier_b_bonus_pct',
        'tier_c_min_score', 'tier_c_bonus_pct',
        'tier_d_min_score', 'tier_d_bonus_pct',
    }

    def write(self, vals):
        touched = self._LOCKED_CONFIG_FIELDS.intersection(vals.keys())
        if touched:
            for rec in self:
                if rec.active:
                    raise UserError(
                        'Config นี้ Active อยู่ — แก้ไขเกณฑ์คะแนนไม่ได้ '
                        'เพื่อความโปร่งใสระหว่างรอบประเมิน\n\n'
                        'วิธีแก้ไข: ปิด Active ก่อน (หรือสร้าง Config เวอร์ชันใหม่แทน)'
                    )
        return super().write(vals)

    def action_approve(self):
        """อนุมัติเกณฑ์คะแนนชุดนี้ — เฉพาะกลุ่ม Fleet Manager กดได้"""
        self.ensure_one()
        if not self.env.user.has_group('fleet.fleet_group_manager'):
            raise UserError('เฉพาะ Fleet Manager เท่านั้นที่มีสิทธิ์อนุมัติ Scoring Config')
        self.write({
            'approved_by_id': self.env.user.id,
            'approved_at':    fields.Datetime.now(),
        })
        return {
            'type': 'ir.actions.client',
            'tag':  'display_notification',
            'params': {
                'title':   '✅ อนุมัติแล้ว',
                'message': f'{self.env.user.name} อนุมัติ Config "{self.name}" แล้ว',
                'type':    'success',
            },
        }

    def _get_base_url(self):
        """ดึง Base URL ของ Backend มาจาก config พร้อมตัด path ที่กรอกเกินมา
        (เช่นกรอก .../api/v1 มาด้วย) ให้เหลือแค่ scheme+host+port"""
        ICP     = self.env['ir.config_parameter'].sudo()
        api_url = ICP.get_param('fleet_telematics.mtd_api_url', '').rstrip('/')
        if not api_url:
            raise UserError(
                'ยังไม่ได้ตั้งค่า MTD API URL\n'
                'ไปที่ Fleet Telematics → Settings แล้วกรอก:\n'
                'http://192.168.1.43:8001'
            )
        for suffix in ['/api/v1', '/api']:
            if api_url.endswith(suffix):
                api_url = api_url[: -len(suffix)]
                break
        return api_url

    def _build_config_payload(self):
        """แปลงเกณฑ์คะแนนทั้งหมดในฟอร์มนี้ เป็น dict ตาม schema ที่ Backend
        ต้องการ สำหรับส่งไปตอนกด Push Config (POST /api/v1/config/scoring)"""
        return {
            'config_name':         self.name,
            'score_base':          self.score_base,
            'speeding_deduct':     self.speeding_deduct,
            'harsh_brake_deduct':  self.harsh_brake_deduct,
            'harsh_accel_deduct':  self.harsh_accel_deduct,
            'harsh_corner_deduct': self.harsh_corner_deduct,
            'idling_deduct':       self.idling_deduct,
            'bump_deduct':         self.bump_deduct,
            'harsh_brake_g':       self.harsh_brake_g,
            'harsh_accel_g':       self.harsh_accel_g,
            'harsh_corner_g':      self.harsh_corner_g,
            'speeding_kmh_over':   self.speeding_kmh_over,
            'idle_min_threshold':  self.idle_min_threshold,
            'speed_limit_bkk':        self.speed_limit_bkk,
            'speed_limit_upcountry':  self.speed_limit_upcountry,
            'tier_a_min_score':    self.tier_a_min_score,
            'tier_a_bonus_pct':    self.tier_a_bonus_pct,
            'tier_b_min_score':    self.tier_b_min_score,
            'tier_b_bonus_pct':    self.tier_b_bonus_pct,
            'tier_c_min_score':    self.tier_c_min_score,
            'tier_c_bonus_pct':    self.tier_c_bonus_pct,
            'tier_d_min_score':    self.tier_d_min_score,
            'tier_d_bonus_pct':    self.tier_d_bonus_pct,
            'max_deduct_per_trip': self.max_deduct_per_trip,
            'is_active':           self.active,
            'synced_from_odoo_at': (
                self.effective_date.isoformat() if self.effective_date else None
            ),
        }

    def action_push_to_backend(self):
        """ส่งเกณฑ์คะแนนทั้งหมดไปให้ Backend ใช้งานจริง (POST /api/v1/
        config/scoring) — ต้องผ่านการอนุมัติก่อนเท่านั้นถึงจะกดได้"""
        self.ensure_one()
        if not self.approved_by_id:
            raise UserError(
                'Config นี้ยังไม่ได้รับการอนุมัติ — กด "✅ Approve" ก่อน Push ไป Backend\n'
                '(เฉพาะ Fleet Manager เท่านั้นที่อนุมัติได้)'
            )
        base_url = self._get_base_url()
        endpoint = f'{base_url}/api/v1/config/scoring'
        payload  = self._build_config_payload()

        _logger.info('action_push_to_backend: POST %s | config_name=%s', endpoint, self.name)

        try:
            resp = requests.post(
                endpoint,
                headers={'Content-Type': 'application/json'},
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()

            try:
                resp_cfg     = resp.json().get('config', {})
                backend_name = resp_cfg.get('config_name', self.name)
                msg = f"Config '{backend_name}' activated บน Backend แล้ว"
            except Exception:
                msg = f'Backend ตอบกลับ {resp.status_code}'

            self.write({
                'last_push_at':     fields.Datetime.now(),
                'last_push_status': f'OK {resp.status_code}',
            })
            return {
                'type': 'ir.actions.client',
                'tag':  'display_notification',
                'params': {
                    'title':   '💾 Push Config สำเร็จ ✅',
                    'message': msg,
                    'type':    'success',
                    'sticky':  False,
                },
            }
        except requests.RequestException as e:
            self.write({'last_push_status': f'ERROR: {e}'})
            raise UserError(f'ส่งค่าไป Backend ไม่สำเร็จ:\n{e}')

    def action_test_connection(self):
        """ทดสอบว่าเชื่อมต่อ Backend ได้ไหม (GET / — ไม่มี /health แยก จึง
        ใช้หน้าแรกซึ่งตอบสถานะ running กลับมาแทน)"""
        self.ensure_one()
        base_url = self._get_base_url()
        url = f'{base_url}/'

        _logger.info('action_test_connection: GET %s', url)

        try:
            resp = requests.get(url, timeout=8)
        except requests.ConnectionError:
            raise UserError(
                f'เชื่อมต่อ Backend ไม่ได้: {url}\n\n'
                'เช็คว่า\n'
                '  • Backend รันอยู่หรือยัง\n'
                '  • IP/Port ถูกต้องไหม (ปัจจุบัน: 192.168.1.43:8001)'
            )
        except requests.RequestException as e:
            raise UserError(f'เชื่อมต่อ Backend ไม่สำเร็จ:\n{e}')

        if resp.status_code == 404:
            raise UserError(
                f'Backend ตอบ 404 — URL อาจผิด: {url}\n'
                'ตรวจ API URL ใน Settings ว่ากรอกแค่: http://192.168.1.43:8001'
            )

        try:
            info    = resp.json()
            project = info.get('project', '')
            version = info.get('version', '')
            msg     = f'Backend ตอบ {resp.status_code}'
            if project:
                msg += f' — {project}'
            if version:
                msg += f' v{version}'
        except Exception:
            msg = f'Backend ตอบกลับ {resp.status_code}'

        return {
            'type': 'ir.actions.client',
            'tag':  'display_notification',
            'params': {
                'title':   '⚡ เชื่อมต่อสำเร็จ',
                'message': msg,
                'type':    'success',
                'sticky':  False,
            },
        }

    def action_fetch_current_config(self):
        """ดึงเกณฑ์คะแนนที่ Backend ใช้งานอยู่จริงตอนนี้ (GET /api/v1/config/
        scoring/current) มาแสดงเทียบกับที่ตั้งไว้ใน Odoo ผ่าน popup"""
        self.ensure_one()
        base_url = self._get_base_url()
        url      = f'{base_url}/api/v1/config/scoring/current'

        _logger.info('action_fetch_current_config: GET %s', url)

        try:
            resp = requests.get(
                url,
                headers={'accept': 'application/json'},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.ConnectionError:
            raise UserError(
                f'เชื่อมต่อ Backend ไม่ได้: {url}\n'
                'เช็คว่า Backend รันอยู่และ IP/Port ถูกต้อง'
            )
        except requests.RequestException as e:
            raise UserError(f'ดึง Config จาก Backend ไม่สำเร็จ:\n{e}')

        config_name  = data.get('config_name',  'N/A')
        score_base   = data.get('score_base',   'N/A')
        is_active    = '✅ Active' if data.get('is_active') else '❌ Inactive'
        eff_date     = data.get('effective_date', 'N/A')

        lines = [
            f"Config: {config_name}  |  {is_active}  |  Effective: {eff_date}",
            f"Base Score: {score_base}  |  Max Deduct/Trip: {data.get('max_deduct_per_trip','N/A')}",
            "",
            "— Deduction Weights —",
            f"Harsh Brake: {data.get('harsh_brake_deduct','N/A')}  "
            f"Accel: {data.get('harsh_accel_deduct','N/A')}  "
            f"Corner: {data.get('harsh_corner_deduct','N/A')}",
            f"Speeding: {data.get('speeding_deduct','N/A')}  "
            f"Idling: {data.get('idling_deduct','N/A')}  "
            f"Bump: {data.get('bump_deduct','N/A')}",
            "",
            "— Thresholds —",
            f"Brake G: {data.get('harsh_brake_g','N/A')}  "
            f"Accel G: {data.get('harsh_accel_g','N/A')}  "
            f"Corner G: {data.get('harsh_corner_g','N/A')}",
            f"Speeding over: {data.get('speeding_kmh_over','N/A')} km/h  "
            f"Idle: {data.get('idle_min_threshold','N/A')} min",
            f"Speed Limit — กรุงเทพฯ: {data.get('speed_limit_bkk','N/A')} km/h  "
            f"นอกเมือง: {data.get('speed_limit_upcountry','N/A')} km/h",
        ]
        msg = '\n'.join(lines)

        return {
            'type': 'ir.actions.client',
            'tag':  'display_notification',
            'params': {
                'title':   f'🔄 Config บน Backend: {config_name}',
                'message': msg,
                'type':    'info',
                'sticky':  True,
            },
        }

    @api.model
    def _track_usage(self, count=1):
        """อัปเดต History fields (last_used_date, total_trips_calculated)
        ของ config ที่ Active อยู่ตอนนี้ — เรียกจาก models/telematics_log.py
        ทุกครั้งที่มี Trip ใหม่ sync เข้ามา (Backend คำนวณคะแนนด้วย config
        ที่ Active อยู่ ณ ขณะนั้นเสมอ ตาม FDD §12.5)"""
        active_cfg = self.search([('active', '=', True)], limit=1)
        if active_cfg:
            active_cfg.write({
                'last_used_date':          fields.Datetime.now(),
                'total_trips_calculated':  active_cfg.total_trips_calculated + count,
            })
