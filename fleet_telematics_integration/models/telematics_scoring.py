# ==============================================================================
# models/telematics_scoring.py
# ==============================================================================
import logging
import requests

from odoo import models, fields, api
from odoo.exceptions import ValidationError, UserError

_logger = logging.getLogger(__name__)


class TelematicsScoringConfig(models.Model):
    _name        = 'fleet.telematics.scoring.config'
    _description = 'Fleet Telematics Scoring Configuration'
    _order       = 'effective_date desc'

    # [A] ข้อมูลระบุ Config
    name           = fields.Char(string='Config Name', required=True)
    active         = fields.Boolean(string='Active', default=True,
        help='Active ได้เพียง 1 config เท่านั้น')
    effective_date = fields.Date(string='Effective Date', required=True)

    # [B] คะแนนพื้นฐาน
    score_base          = fields.Float(string='Base Score (เต็ม)', default=100.0)
    max_deduct_per_trip = fields.Float(string='Max Deduct / Trip', default=50.0)

    # [C] ค่าหักคะแนน
    harsh_brake_deduct  = fields.Float(string='Harsh Brake Deduct',  default=5.0)
    harsh_accel_deduct  = fields.Float(string='Harsh Accel Deduct',  default=3.0)
    harsh_corner_deduct = fields.Float(string='Harsh Corner Deduct', default=3.0)
    speeding_deduct     = fields.Float(string='Speeding Deduct',     default=10.0)
    idling_deduct       = fields.Float(string='Idling Deduct',       default=2.0)
    bump_deduct         = fields.Float(string='Bump Deduct',         default=4.0)

    # [D] Threshold
    harsh_brake_g      = fields.Float(string='Brake G Threshold',         default=0.40)
    harsh_accel_g      = fields.Float(string='Accel G Threshold',         default=0.40)
    harsh_corner_g     = fields.Float(string='Corner G Threshold',        default=0.40)
    speeding_kmh_over  = fields.Float(string='Speeding (km/h เกินกำหนด)', default=20.0)
    idle_min_threshold = fields.Float(string='Idle Min Threshold (min)',   default=5.0)

    # [D2] เพิ่ม 2026-07-08 — ตามบรีฟ: กฎจำกัดความเร็วแยกโซน (กทม./นอกเมือง)
    # ส่งค่าชุดนี้ไปพร้อม Push Config เพื่อให้ Event Processor ฝั่ง Backend
    # ใช้ตัดสินว่า event ไหน "speeding" ตามโซนที่รถวิ่งอยู่จริง — ทำงานคู่กับ
    # zone_label/speed_limit_kmh ที่คำนวณไว้แล้วบน fleet.telematics.event
    # (models/telematics_event.py) ฝั่ง Odoo เพื่อ cross-check/audit ย้อนหลัง
    speed_limit_bkk = fields.Float(
        string='ความเร็วจำกัดในกรุงเทพฯ (km/h)', default=80.0,
        help='ใช้กับ event ที่พิกัดอยู่ในเขตกรุงเทพฯ')
    speed_limit_upcountry = fields.Float(
        string='ความเร็วจำกัดนอกเมือง (km/h)', default=90.0,
        help='ใช้กับ event ที่พิกัดอยู่นอกเขตกรุงเทพฯ')

    # [E] Tier
    tier_a_min_score = fields.Float(string='Tier A — Min Score', default=90.0)
    tier_a_bonus_pct = fields.Float(string='Tier A — Bonus %',  default=10.0)
    tier_b_min_score = fields.Float(string='Tier B — Min Score', default=75.0)
    tier_b_bonus_pct = fields.Float(string='Tier B — Bonus %',  default=5.0)
    tier_c_min_score = fields.Float(string='Tier C — Min Score', default=60.0)
    tier_c_bonus_pct = fields.Float(string='Tier C — Bonus %',  default=0.0)

    # [F] สถานะ Push
    last_push_at     = fields.Datetime(string='Last Pushed At', readonly=True)
    last_push_status = fields.Char(string='Push Status',        readonly=True)

    # [F2] เพิ่ม 2026-07-08 — ผู้อนุมัติเกณฑ์คะแนน (ตามบรีฟข้อ "กำหนดผู้อนุมัติ")
    # ต้องอนุมัติก่อนถึงจะกด Push Config ไป Backend ได้จริง (บังคับใน
    # action_push_to_backend ด้านล่าง) — เขียนได้เฉพาะกลุ่ม Fleet Manager
    approved_by_id = fields.Many2one(
        'res.users', string='ผู้อนุมัติ', readonly=True,
        help='ผู้มีอำนาจอนุมัติเกณฑ์คะแนนชุดนี้ก่อนนำไปใช้จริง')
    approved_at = fields.Datetime(string='วันที่อนุมัติ', readonly=True)

    # [F3] ล็อกฟอร์มอัตโนมัติเมื่อ Active หรือเคย Push แล้ว (ตามบรีฟข้อ 3)
    is_locked = fields.Boolean(
        string='ล็อกการแก้ไข', compute='_compute_is_locked',
        help='True เมื่อ Active=True หรือเคย Push ไป Backend แล้ว — '
             'ฟิลด์เกณฑ์ทั้งหมดจะแก้ไขไม่ได้จนกว่าจะปิด Active')

    @api.depends('active', 'last_push_at')
    def _compute_is_locked(self):
        for rec in self:
            rec.is_locked = bool(rec.active or rec.last_push_at)

    # ============================================================
    # [G] Constraints
    # ============================================================
    @api.constrains('active')
    def _check_single_active(self):
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
        for rec in self:
            if not (rec.tier_a_min_score > rec.tier_b_min_score > rec.tier_c_min_score > 0):
                raise ValidationError('Tier min score ต้องเรียงจากมากไปน้อย: A > B > C > 0')

    @api.constrains(
        'harsh_brake_deduct', 'harsh_accel_deduct', 'harsh_corner_deduct',
        'speeding_deduct', 'idling_deduct', 'bump_deduct',
        'score_base', 'max_deduct_per_trip',
    )
    def _check_positive_deducts(self):
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
        for rec in self:
            if rec.max_deduct_per_trip > rec.score_base:
                raise ValidationError(
                    f'Max Deduct / Trip ({rec.max_deduct_per_trip}) ต้องไม่เกิน Base Score ({rec.score_base})'
                )

    # ============================================================
    # [G2] เพิ่ม 2026-07-08 — ล็อกฟิลด์เกณฑ์ทั้งหมดเมื่อ Active=True หรือ
    # เคย Push ไป Backend แล้ว (ตามบรีฟข้อ 3 "Read-only ชั้น Python & XML")
    #
    # นี่คือชั้น Python (บังคับจริงแม้เรียกผ่าน API/RPC ตรงๆ) ส่วนชั้น XML
    # (attrs readonly บนฟอร์ม) อยู่ที่ views/telematics_scoring_views.xml
    #
    # ไม่ล็อก field สถานะ (last_push_at, last_push_status, approved_by_id,
    # approved_at, is_locked) และไม่ล็อก 'active' เอง — ผู้ใช้ต้องปิด
    # Active ได้เพื่อปลดล็อกฟิลด์อื่น (deactivate ก่อนแล้วค่อยแก้ไข)
    # ============================================================
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
    }

    def write(self, vals):
        touched = self._LOCKED_CONFIG_FIELDS.intersection(vals.keys())
        if touched:
            for rec in self:
                if rec.active or rec.last_push_at:
                    raise UserError(
                        'Config นี้ Active อยู่ หรือเคย Push ไป Backend แล้ว — '
                        'แก้ไขเกณฑ์คะแนนไม่ได้ เพื่อความโปร่งใสระหว่างรอบประเมิน\n\n'
                        'วิธีแก้ไข: ปิด Active ก่อน (หรือสร้าง Config เวอร์ชันใหม่แทน)'
                    )
        return super().write(vals)

    # ============================================================
    # [G3] เพิ่ม 2026-07-08 — Approve Config (ตามบรีฟ "กำหนดผู้อนุมัติ")
    # เฉพาะ group_fleet_manager กดอนุมัติได้ — ต้องอนุมัติก่อนถึงจะ Push
    # ไป Backend ได้จริง (เช็คใน action_push_to_backend ด้านล่าง)
    # ============================================================
    def action_approve(self):
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

    # ============================================================
    # [H] Helper — ดึง Base URL ที่ถูกต้อง
    # รองรับทั้ง "http://192.168.1.43:8001"
    #          และ "http://192.168.1.43:8001/api/v1" (กรอก path เกินมา)
    # ============================================================
    def _get_base_url(self):
        ICP     = self.env['ir.config_parameter'].sudo()
        api_url = ICP.get_param('fleet_telematics.mtd_api_url', '').rstrip('/')
        if not api_url:
            raise UserError(
                'ยังไม่ได้ตั้งค่า MTD API URL\n'
                'ไปที่ Fleet Telematics → Settings แล้วกรอก:\n'
                'http://192.168.1.43:8001'
            )
        # ถ้ากรอก URL มี /api/v1 ต่อท้ายอยู่แล้ว → ตัดออก ป้องกัน path ซ้ำ
        for suffix in ['/api/v1', '/api']:
            if api_url.endswith(suffix):
                api_url = api_url[: -len(suffix)]
                break
        return api_url

    # ============================================================
    # [I] สร้าง Payload ตาม Backend spec
    # POST http://192.168.1.43:8001/api/v1/config/scoring
    #
    # [แก้บั๊ก] เดิมส่ง now_utc (เวลาที่กดปุ่ม sync) ไว้ใต้คีย์
    # 'synced_from_odoo_at' ทำให้ค่า effective_date ที่ผู้ใช้กรอกในฟอร์ม
    # ไม่ถูกส่งไป Backend เลย (นี่คือ "ตัวแปรวันที่ที่หายไป" ที่ผู้ควบคุม
    # แจ้งมา) — Backend มีฟิลด์รองรับค่านี้อยู่แล้วแค่ใช้ชื่อคีย์
    # 'synced_from_odoo_at' จึงแก้ให้ส่ง self.effective_date (วันที่ config
    # นี้มีผลบังคับใช้จริง) ไปใต้คีย์นี้แทน
    # ============================================================
    def _build_config_payload(self):
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
            # เพิ่ม 2026-07-08: กฎความเร็วแยกโซน กทม./นอกเมือง (บรีฟข้อ 2)
            'speed_limit_bkk':        self.speed_limit_bkk,
            'speed_limit_upcountry':  self.speed_limit_upcountry,
            'max_deduct_per_trip': self.max_deduct_per_trip,
            'is_active':           self.active,
            'synced_from_odoo_at': (
                self.effective_date.isoformat() if self.effective_date else None
            ),
        }

    # ============================================================
    # [J] ปุ่ม "💾 Push Config"
    # POST http://192.168.1.43:8001/api/v1/config/scoring
    # ============================================================
    def action_push_to_backend(self):
        self.ensure_one()
        # เพิ่ม 2026-07-08: บังคับอนุมัติก่อน Push จริง (บรีฟ "กำหนดผู้อนุมัติ")
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

    # ============================================================
    # [K] ปุ่ม "⚡ Test Connection"
    # ลอง POST /api/v1/config/scoring ด้วย dry_run=true
    # endpoint เดียวกับ Push Config — ไม่ต้องหา health path แยก
    # ============================================================
    def action_test_connection(self):
        self.ensure_one()
        base_url = self._get_base_url()
        # Backend ไม่มี /health — ใช้ GET / แทน (ตอบ {"status":"running",...})
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

    # ============================================================
    # [L] action_fetch_current_config — GET /api/v1/config/scoring/current
    # ดึง config ที่ Backend ใช้งานอยู่ปัจจุบัน มาแสดงใน Odoo
    # เรียกจากปุ่ม "🔄 ดึง Config ปัจจุบัน" บนหน้า Scoring Config
    # ============================================================
    def action_fetch_current_config(self):
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

        # แสดงข้อมูลที่ได้รับกลับมาใน popup
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
                'sticky':  True,   # ค้างไว้ให้อ่านได้ ต้องกด X ปิดเอง
            },
        }