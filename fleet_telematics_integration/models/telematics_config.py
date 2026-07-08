# ==============================================================================
# models/telematics_config.py  [MODIFIED v2]
# แก้บั๊ก:
#   1. action_save_and_test() → write() ทับเรคคอร์ดเดิมเสมอ ไม่ create ซ้ำ
#   2. get_active_api_url() / _api_url — แก้ bug ตัวแปร ip/url_input ปนกัน
# ==============================================================================

import logging
import requests

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_PARAM_URL           = 'fleet_telematics.api_url_input'
_PARAM_CONFIRMED_URL = 'fleet_telematics.last_confirmed_url'
_PARAM_API_KEY       = 'fleet_telematics.mtd_api_key'
_PARAM_API_URL       = 'fleet_telematics.mtd_api_url'   # compat เดิม


class TelematicsConfig(models.Model):
    _name = 'fleet.telematics.config'
    _description = 'Fleet Telematics Configuration'
    _rec_name = 'name'

    # ============================================================
    # [A] ฟิลด์ตั้งค่า — api_url + api_key
    # ============================================================

    name = fields.Char(
        string='Config Name',
        default='Fleet Telematics Settings'
    )

    api_url = fields.Char(
        string='API URL ของ Backend',
        help='API URL ของ Backend เช่น http://192.168.1.43:8001'
    )

    api_key = fields.Char(
        string='API Key',
        help='Bearer token / APIKEY สำหรับยืนยันตัวตน'
    )

    # ============================================================
    # [B] API URL ล่าสุดที่ใช้งานได้ (จดจำอัตโนมัติเมื่อ Test ผ่าน)
    # ============================================================

    last_confirmed_url = fields.Char(
        string='API URL ล่าสุดที่ใช้งานได้',
        readonly=True,
        help='ระบบบันทึก URL นี้อัตโนมัติเมื่อทดสอบการเชื่อมต่อสำเร็จ'
    )

    # ============================================================
    # [C] System Health Dashboard
    # ============================================================

    connection_status = fields.Selection([
        ('untested', '⚪ Untested'),
        ('ready',    '🟢 Ready'),
        ('error',    '🔴 Error'),
    ],
        string='Connection Status',
        default='untested',
        readonly=True
    )

    # ⚠️ หมายเหตุเรื่อง Datetime: fields.Datetime ของ Odoo เก็บค่าใน DB เป็น
    # UTC เสมอ (ห้าม localize เป็น timezone ผู้ใช้ก่อนบันทึกเด็ดขาด) — ใช้
    # fields.Datetime.now() / datetime.utcnow() เท่านั้นเวลาเขียนค่าลงฟิลด์นี้
    # ส่วนการแปลงไปแสดงตาม timezone ของผู้ใช้ (res.users.tz) เป็นหน้าที่ของ
    # Odoo ตอน render UI ให้อัตโนมัติอยู่แล้ว ไม่ต้องแปลงเองในโค้ด
    last_test_at = fields.Datetime(string='Last Tested At', readonly=True)
    last_sync_at = fields.Datetime(string='Last Synced At', readonly=True)
    last_error   = fields.Text(string='Last Error',         readonly=True)

    # ============================================================
    # [C2] เพิ่มใหม่ — Device Reconciliation (GET /api/v1/config_device)
    #   เดิม endpoint นี้มีอยู่ใน Swagger Backend แต่ไม่เคยถูกเรียกใช้เลย
    #   ทำให้ Odoo ไม่มีทางรู้เลยว่า device ที่ผูกไว้ในระบบ (fleet.vehicle
    #   .telematics_device_id / fleet.telematics.device) ตรงกับที่ Backend
    #   บันทึกจริงหรือไม่ — ถ้ามีคนไป register/แก้ตรงที่ Backend โดยตรง
    #   (ไม่ผ่าน Odoo) ข้อมูลจะเงียบๆ ไม่ตรงกันแบบไม่มีใครรู้
    # ============================================================
    last_reconciled_at   = fields.Datetime(string='Last Device Reconcile At', readonly=True)
    device_mismatch_count = fields.Integer(string='Device Mismatch Found', readonly=True)
    device_mismatch_note  = fields.Text(string='Device Mismatch Detail', readonly=True)

    # ============================================================
    # [D] Helper: แปลง input → URL เต็ม
    # ============================================================

    @staticmethod
    def _normalize_url(raw):
        """เติม http:// ถ้า input ยังไม่มี scheme"""
        raw = (raw or '').strip()
        if not raw:
            return ''
        if raw.startswith('http://') or raw.startswith('https://'):
            return raw.rstrip('/')
        return f'http://{raw}'.rstrip('/')

    # ============================================================
    # [D2] Database Lockdown — บล็อกการสร้างเรคคอร์ดใหม่จากทุกช่องทาง
    #   - ปุ่ม New บน List/Form, RPC/External API, import CSV/XLSX,
    #     หรือโมดูลอื่นที่เผลอเรียก .create() ตรง ๆ → โดน UserError ทันที
    #   - อนุญาตเฉพาะ "การสร้างเรคคอร์ดแรกของระบบ" ที่มาจาก server action
    #     action_open_telematics_config (กรณียังไม่มี config เลย) โดยต้องส่ง
    #     context key 'allow_telematics_config_create=True' มาด้วยเท่านั้น
    #   ⚠️ หมายเหตุความปลอดภัยของข้อมูล: เพราะมีเงื่อนไข [D3]/Server Action
    #     ที่ unlink() เรคคอร์ดส่วนเกินอัตโนมัติ การบล็อก create() ที่นี่จึง
    #     สำคัญมาก — ถ้าไม่บล็อก จะมีคนสร้างเรคคอร์ดใหม่ซ้ำได้เรื่อย ๆ แล้วถูก
    #     ระบบ cleanup ลบทิ้งแบบไม่มีการแจ้งเตือนล่วงหน้า
    # ============================================================

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get('allow_telematics_config_create'):
            raise UserError(
                'ไม่อนุญาตให้สร้างเรคคอร์ด Fleet Telematics Config เพิ่ม '
                '(Database Lockdown)\n'
                'ระบบนี้อนุญาตให้มีค่าตั้งค่าได้เพียง 1 รายการในระบบเท่านั้น '
                'กรุณาเปิดเมนู "Fleet Telematics Settings" แล้วแก้ไข (Edit) '
                'เรคคอร์ดที่มีอยู่แทนการสร้างใหม่'
            )
        return super().create(vals_list)

    # ============================================================
    # [E] โหลดค่าจาก ir.config_parameter เมื่อเปิดฟอร์มใหม่ (New)
    # ============================================================

    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        ICP = self.env['ir.config_parameter'].sudo()

        stored_url = ICP.get_param(_PARAM_URL, '') \
                     or ICP.get_param(_PARAM_API_URL, '')  # fallback compat

        res.update({
            'api_url':           stored_url,
            'api_key':           ICP.get_param(_PARAM_API_KEY, ''),
            'last_confirmed_url': ICP.get_param(_PARAM_CONFIRMED_URL, ''),
        })
        return res

    # ============================================================
    # [F] action_save_and_test  ← แก้บั๊กหลัก #1
    #   - ใช้ write() ทับเรคคอร์ดปัจจุบันเสมอ → ไม่สร้างแถวซ้ำ
    #   - เรคคอร์ด self มาจาก Form View ที่เปิดอยู่โดยตรง
    # ============================================================

    def action_save_and_test(self):
        self.ensure_one()  # ป้องกันการเรียกพร้อมกันหลายรายการ

        raw_url = (self.api_url or '').strip()
        if not raw_url:
            raise UserError('กรุณาระบุ API URL ของ Backend ก่อน')

        api_url = self._normalize_url(raw_url)
        api_key = self.api_key or ''

        # ── บันทึกค่าลง ir.config_parameter ──
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param(_PARAM_URL,    raw_url)
        ICP.set_param(_PARAM_API_URL, api_url)   # compat เดิม
        ICP.set_param(_PARAM_API_KEY, api_key)

        # ── ทดสอบการเชื่อมต่อ ──
        try:
            resp = requests.get(
                f'{api_url}/api/v1/devices',
                headers={'APIKEY': api_key},
                timeout=10,
            )
            resp.raise_for_status()

            # Backend GET /api/v1/devices คืนเป็น dict {"total": N, "devices": [...]}
            # ไม่ใช่ list ตรงๆ (ยืนยันจาก Swagger จริง 2026-07-06) — เดิมเช็ค
            # isinstance(payload, list) จึงไม่เคย True เลย ทำให้จำนวน device
            # ที่แสดงเป็น "-" เสมอแม้เชื่อมต่อสำเร็จ
            try:
                payload = resp.json()
                if isinstance(payload, dict):
                    device_count = payload.get('total')
                    if device_count is None:
                        devices_list = payload.get('devices')
                        device_count = len(devices_list) if isinstance(devices_list, list) else '-'
                elif isinstance(payload, list):
                    device_count = len(payload)
                else:
                    device_count = '-'
            except Exception:
                device_count = '-'

            # เชื่อมต่อสำเร็จ → fix URL นี้เป็น last_confirmed_url
            ICP.set_param(_PARAM_CONFIRMED_URL, raw_url)

            # write() ทับเรคคอร์ดเดิม — ไม่ create แถวใหม่
            self.write({
                'connection_status': 'ready',
                'last_test_at':      fields.Datetime.now(),
                'last_confirmed_url': raw_url,
                'last_error':        False,
            })

            _logger.info(
                'action_save_and_test: connected to %s | devices=%s',
                api_url, device_count
            )

            return {
                'type': 'ir.actions.client',
                'tag':  'display_notification',
                'params': {
                    'title':   '✅ บันทึกและเชื่อมต่อสำเร็จ',
                    'message': (
                        f'Backend ตอบกลับ {resp.status_code} | พบ {device_count} devices\n'
                        f'ระบบจดจำ API URL: {raw_url} เรียบร้อยแล้ว'
                    ),
                    'type':   'success',
                    'sticky': True,
                },
            }

        except requests.RequestException as e:
            confirmed = ICP.get_param(_PARAM_CONFIRMED_URL, '')

            # write() ทับเรคคอร์ดเดิม — ไม่ create แถวใหม่
            self.write({
                'connection_status': 'error',
                'last_test_at':      fields.Datetime.now(),
                'last_error':        str(e),
            })

            fallback_msg = (
                f'\n\n⚠️ ระบบจะใช้ API URL ล่าสุดที่เคยใช้ได้: {confirmed}'
                if confirmed else ''
            )

            raise UserError(
                f'เชื่อมต่อ Backend ไม่สำเร็จ:\n{e}{fallback_msg}'
            )

    # ============================================================
    # [G] Helper: ดึง API URL ที่ใช้งานได้จริง (เรียกจากโมเดลอื่น)
    #   ลำดับ: 1) last_confirmed_url  2) api_url ปัจจุบัน
    # ============================================================

    @api.model
    def get_active_api_url(self):
        ICP = self.env['ir.config_parameter'].sudo()
        confirmed = ICP.get_param(_PARAM_CONFIRMED_URL, '').strip()
        current   = ICP.get_param(_PARAM_URL, '').strip() \
                    or ICP.get_param(_PARAM_API_URL, '').strip()
        raw = confirmed or current
        return self._normalize_url(raw)

    @api.model
    def get_active_api_key(self):
        ICP = self.env['ir.config_parameter'].sudo()
        return ICP.get_param(_PARAM_API_KEY, '')

    # ============================================================
    # [H] action_reconcile_devices — GET /api/v1/devices
    #   [แก้บั๊ก 2026-07-06] เดิมเรียก GET /api/v1/config_device ซึ่งจาก
    #   Swagger จริงของ Backend endpoint นี้ต้องการ query param `device_id`
    #   เป็น required และคืนค่าสถานะของ device แค่ตัวเดียว (ไม่ใช่ลิสต์)
    #   — เรียกแบบไม่ส่ง device_id จะได้ 422 Validation Error ทุกครั้ง
    #   endpoint ที่คืนรายการ device ทั้งหมดจริงๆ คือ GET /api/v1/devices
    #   (ตัวเดียวกับที่ action_save_and_test ใช้ทดสอบการเชื่อมต่ออยู่แล้ว)
    #   response จริง: {"total": N, "devices": [{"id": "KTC-001",
    #   "vehicle_id": 101, "active": true, "registered_at": "..."}]}
    #   — field รหัส device ในนี้ชื่อ "id" ไม่ใช่ "device_id"
    #
    #   ดึงรายการ device+vehicle mapping ทั้งหมดจาก Backend มาเทียบกับ
    #   fleet.vehicle.telematics_device_id ใน Odoo ทีละคัน
    #   ผลลัพธ์ที่ตรวจพบ:
    #     1) Backend ผูก device กับ vehicle_id ที่ไม่ตรงกับ Odoo
    #     2) Backend มี device ที่ Odoo ไม่มีเลย (สร้างตรงที่ Backend)
    #     3) Odoo มี device ที่ Backend ไม่รู้จัก (ยังไม่ได้ register จริง)
    #   ไม่ auto-fix ให้ — แค่รายงานเพื่อให้ Fleet Manager ตัดสินใจเอง
    #   (การ auto-fix ข้อมูลรถ/device มีผลกับ Trip/Score จึงเสี่ยงเกินไป
    #    ที่จะให้ cron แก้เองแบบเงียบๆ)
    # ============================================================
    def action_reconcile_devices(self):
        self.ensure_one()
        api_url = self.get_active_api_url()
        api_key = self.get_active_api_key()
        if not api_url:
            raise UserError('กรุณาตั้งค่า API URL ของ Backend ก่อน')

        try:
            resp = requests.get(
                f'{api_url}/api/v1/devices',
                headers={'APIKEY': api_key},
                timeout=20,
            )
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, dict):
                backend_devices = payload.get('devices', [])
            elif isinstance(payload, list):
                backend_devices = payload
            else:
                backend_devices = []
        except requests.RequestException as e:
            self.write({
                'last_reconciled_at': fields.Datetime.now(),
                'last_error':         f'Reconcile devices ล้มเหลว: {e}',
            })
            raise UserError(f'ดึงรายการ Device จาก Backend ไม่สำเร็จ:\n{e}')

        # index Backend devices ด้วย device_id (upper-case)
        # หมายเหตุ: response ของ GET /api/v1/devices ใช้ key "id" สำหรับรหัส
        # device (ไม่ใช่ "device_id") — รองรับทั้งสองชื่อ key เผื่อ Backend
        # เปลี่ยน schema ในอนาคต
        backend_by_id = {
            (d.get('id') or d.get('device_id') or '').upper(): d
            for d in backend_devices
            if d.get('id') or d.get('device_id')
        }

        Vehicle = self.env['fleet.vehicle'].sudo()
        odoo_vehicles = Vehicle.search([('telematics_device_id', '!=', False)])
        odoo_by_device = {
            (v.telematics_device_id or '').upper(): v for v in odoo_vehicles
        }

        mismatches = []

        # 1+2) เทียบฝั่ง Odoo → Backend
        for dev_id, vehicle in odoo_by_device.items():
            b = backend_by_id.get(dev_id)
            if not b:
                mismatches.append(
                    f'⚠️ {vehicle.name}: Odoo ผูก device {dev_id} แต่ Backend '
                    f'ไม่มี device นี้เลย (ยังไม่ได้ register จริง)'
                )
                continue
            backend_vehicle_id = b.get('vehicle_id')
            if backend_vehicle_id and int(backend_vehicle_id) != vehicle.id:
                mismatches.append(
                    f'⚠️ {vehicle.name}: Odoo ผูก device {dev_id} กับรถ id={vehicle.id} '
                    f'แต่ Backend ผูก device นี้กับ vehicle_id={backend_vehicle_id} แทน'
                )

        # 3) เทียบฝั่ง Backend → Odoo (device ที่ Backend มีแต่ Odoo ไม่รู้จัก)
        for dev_id, b in backend_by_id.items():
            if dev_id not in odoo_by_device:
                mismatches.append(
                    f'⚠️ Backend มี device {dev_id} (vehicle_id={b.get("vehicle_id")}) '
                    f'แต่ไม่มีรถคันไหนใน Odoo ผูกกับ device นี้เลย'
                )

        note = '\n'.join(mismatches) if mismatches else 'ไม่พบความไม่ตรงกัน — ข้อมูลตรงกันทั้งหมด ✅'

        self.write({
            'last_reconciled_at':    fields.Datetime.now(),
            'device_mismatch_count': len(mismatches),
            'device_mismatch_note':  note,
        })

        _logger.info(
            'action_reconcile_devices: ตรวจ %d devices (Backend) เทียบกับ %d รถ (Odoo) → พบ %d mismatch',
            len(backend_by_id), len(odoo_by_device), len(mismatches),
        )

        return {
            'type': 'ir.actions.client',
            'tag':  'display_notification',
            'params': {
                'title':   f'พบ {len(mismatches)} รายการไม่ตรงกัน' if mismatches else '✅ Device ตรงกันทั้งหมด',
                'message': note[:500],
                'type':    'warning' if mismatches else 'success',
                'sticky':  bool(mismatches),
            },
        }

    @api.model
    def _cron_reconcile_devices(self):
        """เรียกจาก ir.cron รายวัน — reconcile ให้เรคคอร์ด config แรกของระบบ"""
        config = self.search([], limit=1, order='id asc')
        if config:
            try:
                config.action_reconcile_devices()
            except UserError as e:
                _logger.warning('_cron_reconcile_devices: %s', e)
