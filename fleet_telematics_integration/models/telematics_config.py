# ==============================================================================
# models/telematics_config.py
#
# หน้าตั้งค่าการเชื่อมต่อ Backend (API URL + API Key) — เก็บเป็นทั้ง record
# (fleet.telematics.config) และ ir.config_parameter คู่กัน เพื่อให้โมเดลอื่น
# ในระบบดึงค่าไปใช้ได้ง่ายผ่าน get_active_api_url()/get_active_api_key()
# โดยไม่ต้องมาเปิดหา record เอง
#
# ระบบอนุญาตให้มี config ได้แค่ 1 record เท่านั้นในระบบ (ดู create() ด้านล่าง)
# ==============================================================================

import logging
import requests

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_PARAM_URL           = 'fleet_telematics.api_url_input'
_PARAM_CONFIRMED_URL = 'fleet_telematics.last_confirmed_url'
_PARAM_API_KEY       = 'fleet_telematics.mtd_api_key'
_PARAM_API_URL       = 'fleet_telematics.mtd_api_url'   # ชื่อเดิม เก็บไว้เพื่อความเข้ากันได้ย้อนหลัง


class TelematicsConfig(models.Model):
    """ตั้งค่าการเชื่อมต่อ Backend + Dashboard สถานะระบบ + เครื่องมือ
    ตรวจสอบ Device ตรงกันระหว่าง Odoo กับ Backend (Reconcile)"""
    _name = 'fleet.telematics.config'
    _description = 'Fleet Telematics Configuration'
    _rec_name = 'name'

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

    last_confirmed_url = fields.Char(
        string='API URL ล่าสุดที่ใช้งานได้',
        readonly=True,
        help='ระบบบันทึก URL นี้อัตโนมัติเมื่อทดสอบการเชื่อมต่อสำเร็จ'
    )

    connection_status = fields.Selection([
        ('untested', '⚪ Untested'),
        ('ready',    '🟢 Ready'),
        ('error',    '🔴 Error'),
    ],
        string='Connection Status',
        default='untested',
        readonly=True
    )

    # หมายเหตุ: fields.Datetime ของ Odoo เก็บใน DB เป็น UTC เสมอ ห้าม
    # localize เป็น timezone ผู้ใช้ก่อนบันทึกเด็ดขาด — ใช้ fields.Datetime.
    # now() เท่านั้นตอนเขียนค่า ส่วนการแสดงผลตาม timezone ผู้ใช้เป็นหน้าที่
    # ของ Odoo ตอน render ให้อัตโนมัติอยู่แล้ว
    last_test_at = fields.Datetime(string='Last Tested At', readonly=True)
    last_sync_at = fields.Datetime(string='Last Synced At', readonly=True)
    last_error   = fields.Text(string='Last Error',         readonly=True)

    # ── Device Reconciliation ────────────────────────────────────────────
    # ตรวจว่า Device ที่ผูกไว้ใน Odoo (fleet.vehicle.telematics_device_id)
    # ตรงกับที่ Backend บันทึกจริงไหม — ป้องกันกรณีมีคนไป register/แก้ไข
    # ตรงที่ Backend โดยตรง (ไม่ผ่าน Odoo) แล้วข้อมูล 2 ฝั่งไม่ตรงกันแบบ
    # ไม่มีใครรู้
    last_reconciled_at    = fields.Datetime(string='Last Device Reconcile At', readonly=True)
    device_mismatch_count = fields.Integer(string='Device Mismatch Found', readonly=True)
    device_mismatch_note  = fields.Text(string='Device Mismatch Detail', readonly=True)

    @staticmethod
    def _normalize_url(raw):
        """เติม http:// นำหน้าให้ถ้า input ที่กรอกมายังไม่มี scheme"""
        raw = (raw or '').strip()
        if not raw:
            return ''
        if raw.startswith('http://') or raw.startswith('https://'):
            return raw.rstrip('/')
        return f'http://{raw}'.rstrip('/')

    # ── จำกัดให้มี config ได้แค่ 1 record ในระบบ ────────────────────────────
    # ปุ่ม New บนหน้าจอ, RPC, import CSV/XLSX หรือช่องทางอื่นที่เผลอเรียก
    # create() ตรงๆ จะโดนบล็อกด้วย UserError ทันที อนุญาตเฉพาะตอนสร้าง
    # record แรกของระบบผ่าน context พิเศษ 'allow_telematics_config_create'
    # เท่านั้น (ส่งมาจาก server action ตอนยังไม่มี config เลย)
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

    def default_get(self, fields_list):
        """โหลดค่าที่เคยตั้งไว้ใน ir.config_parameter มาแสดงตอนเปิดฟอร์มใหม่"""
        res = super().default_get(fields_list)
        ICP = self.env['ir.config_parameter'].sudo()

        stored_url = ICP.get_param(_PARAM_URL, '') \
                     or ICP.get_param(_PARAM_API_URL, '')

        res.update({
            'api_url':            stored_url,
            'api_key':            ICP.get_param(_PARAM_API_KEY, ''),
            'last_confirmed_url': ICP.get_param(_PARAM_CONFIRMED_URL, ''),
        })
        return res

    def action_save_and_test(self):
        """บันทึกค่า API URL/Key ลง ir.config_parameter แล้วทดสอบเชื่อมต่อ
        ทันทีด้วยการยิง GET /api/v1/devices — เขียนทับ record เดิมเสมอ
        (write ไม่ใช่ create) ไม่มีทางสร้างแถวซ้ำได้"""
        self.ensure_one()

        raw_url = (self.api_url or '').strip()
        if not raw_url:
            raise UserError('กรุณาระบุ API URL ของ Backend ก่อน')

        api_url = self._normalize_url(raw_url)
        api_key = self.api_key or ''

        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param(_PARAM_URL,     raw_url)
        ICP.set_param(_PARAM_API_URL, api_url)
        ICP.set_param(_PARAM_API_KEY, api_key)

        try:
            resp = requests.get(
                f'{api_url}/api/v1/devices',
                headers={'APIKEY': api_key},
                timeout=10,
            )
            resp.raise_for_status()

            # Backend คืนเป็น dict {"total": N, "devices": [...]} ไม่ใช่
            # list ตรงๆ — รองรับทั้ง 2 รูปแบบเผื่อ Backend เปลี่ยน schema
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

            ICP.set_param(_PARAM_CONFIRMED_URL, raw_url)

            self.write({
                'connection_status':  'ready',
                'last_test_at':       fields.Datetime.now(),
                'last_confirmed_url': raw_url,
                'last_error':         False,
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

    @api.model
    def get_active_api_url(self):
        """คืน API URL ที่ใช้งานได้จริงตอนนี้ — เลือก last_confirmed_url
        (ค่าที่เคยทดสอบผ่านแล้ว) ก่อนเสมอ ถ้าไม่มีค่อย fallback ไปที่
        api_url ปัจจุบัน — โมเดลอื่นในระบบเรียกใช้ฟังก์ชันนี้แทนการเปิด
        record ของ config เอง"""
        ICP = self.env['ir.config_parameter'].sudo()
        confirmed = ICP.get_param(_PARAM_CONFIRMED_URL, '').strip()
        current   = ICP.get_param(_PARAM_URL, '').strip() \
                    or ICP.get_param(_PARAM_API_URL, '').strip()
        raw = confirmed or current
        return self._normalize_url(raw)

    @api.model
    def get_active_api_key(self):
        """คืน API Key ที่ใช้งานอยู่ตอนนี้"""
        ICP = self.env['ir.config_parameter'].sudo()
        return ICP.get_param(_PARAM_API_KEY, '')

    def action_reconcile_devices(self):
        """ดึงรายการ Device ทั้งหมดจาก Backend (GET /api/v1/devices) มา
        เทียบกับที่ Odoo บันทึกไว้ (fleet.vehicle.telematics_device_id)
        ทีละคัน ตรวจ 3 แบบ:
          1) Odoo ผูก device ไว้ แต่ Backend ไม่รู้จัก device นั้นเลย
          2) Odoo กับ Backend ผูก device ตัวเดียวกันไว้กับรถคนละคัน
          3) Backend มี device ที่ไม่มีรถคันไหนใน Odoo ผูกไว้เลย
        แค่รายงานผลให้ Fleet Manager ตัดสินใจเอง ไม่ auto-fix ให้ — เพราะ
        การแก้ข้อมูลรถ/device มีผลกระทบต่อ Trip/คะแนน จึงเสี่ยงเกินไปที่จะ
        ให้ระบบแก้เองแบบเงียบๆ"""
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

        # รหัส device ในข้อมูลที่ Backend ส่งมาใช้ key "id" (ไม่ใช่
        # "device_id") — เผื่อรองรับทั้งสองชื่อ key ในกรณี Backend เปลี่ยน
        # schema ในอนาคต
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
        """เรียกจาก ir.cron รายวัน — reconcile ให้ config record แรกของระบบ"""
        config = self.search([], limit=1, order='id asc')
        if config:
            try:
                config.action_reconcile_devices()
            except UserError as e:
                _logger.warning('_cron_reconcile_devices: %s', e)
