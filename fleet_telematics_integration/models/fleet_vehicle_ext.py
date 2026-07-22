# ==============================================================================
# models/fleet_vehicle_ext.py
#
# ต่อยอด fleet.vehicle (รถ) เพิ่มฟิลด์และปุ่มทั้งหมดที่เกี่ยวกับ Telematics:
# ผูก/ลงทะเบียน GPS Device, เช็คสถานะ/พิกัดสด, ตรวจสอบ Device ตรงกันไหม,
# ส่งข้อมูลรถ+บอร์ดไป Backend, สถิติสะสม (ทริป/ระยะทาง/คะแนน/ชั่วโมงเครื่องยนต์)
# ==============================================================================
import logging
import requests
from datetime import datetime, timezone

from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class FleetVehicleExt(models.Model):
    """ต่อยอด fleet.vehicle เพิ่ม field/ปุ่มสำหรับระบบ Fleet Telematics"""
    _inherit = 'fleet.vehicle'

    telematics_device_id = fields.Char(
        string='GPS Device ID',
        help='รหัสกล่อง GPS เช่น KTC-001 — ต้องตรงกับ device_id ใน Backend'
    )

    telematics_device_name = fields.Char(
        string='Device Name',
        help='ชื่อเรียก Device สำหรับแสดงผล (ส่งไป Backend ตอนลงทะเบียนครั้งแรก)'
    )
    telematics_register_status = fields.Selection(
        [('draft', 'ยังไม่ลงทะเบียน'),
         ('registered', 'ลงทะเบียนแล้ว'),
         ('error', 'ลงทะเบียนไม่สำเร็จ')],
        string='สถานะการลงทะเบียน Device',
        default='draft', readonly=True,
    )
    telematics_registered_at = fields.Datetime(
        string='Registered At (Backend)', readonly=True,
    )
    telematics_register_error = fields.Text(string='Register Error', readonly=True)

    # เลข ID ของคนขับแบบตัวเลขชัดๆ เทียบกับที่ Backend ใช้เป็น driver_id ใน
    # รายงานต่างๆ (เช่น JSON ของ /drivers/{id}/bonus คืน "driver_id": "12"
    # — เลขนี้คือ id ของ hr.employee ใน Odoo ตรงๆ)
    driver_backend_id = fields.Integer(
        string='Driver ID (สำหรับเทียบกับ Backend)',
        compute='_compute_driver_backend_id',
        help='เลข ID ของพนักงานคนขับใน Odoo — ตรงกับค่า driver_id ที่ Backend '
             'ใช้อ้างอิงในรายงานต่างๆ (Driver Score, Bonus, Fuel Summary)'
    )

    @api.depends('driver_id')
    def _compute_driver_backend_id(self):
        for rec in self:
            rec.driver_backend_id = rec.driver_id.id if rec.driver_id else 0

    # จดจำบอร์ดเดิมอัตโนมัติผ่าน write() hook ด้านล่าง ใช้เป็น old_device_id
    # ตอนยิง PUT /api/v1/config/vehicle เวลาย้ายบอร์ดไปผูกรถคันอื่น
    previous_device_id = fields.Char(
        string='Previous Device ID',
        readonly=True,
        help='รหัสบอร์ดก่อนการเปลี่ยนครั้งล่าสุด — ระบบบันทึกอัตโนมัติ'
    )

    device_verified_at = fields.Datetime(
        string='Device Verified At',
        readonly=True,
        help='เวลาที่ตรวจสอบข้อมูล Device กับ Backend ล่าสุด (GET /vehicles/{id}/device)'
    )
    device_verify_mismatch = fields.Boolean(
        string='Device Mismatch',
        readonly=True,
        help='True ถ้า device_id ที่ Backend บันทึกไว้ไม่ตรงกับ Odoo',
    )
    device_verify_note = fields.Text(
        string='Device Verify Note',
        readonly=True,
    )

    last_lat      = fields.Float(string='Last Latitude',        digits=(10, 7))
    last_lon      = fields.Float(string='Last Longitude',       digits=(10, 7))
    last_seen     = fields.Datetime(string='Last GPS Update')
    current_speed = fields.Float(string='Current Speed (km/h)', digits=(10, 1))
    ignition      = fields.Boolean(string='Ignition On',         default=False)

    online_status = fields.Selection([
        ('online',  '🟢 Online'),
        ('offline', '🔴 Offline'),
        ('unknown', '⚪ Unknown'),
    ], string='Online Status', default='unknown', readonly=True)

    sync_status = fields.Selection([
        ('idle',    'กำลังทำงาน'),
        ('syncing', 'กำลังรอ'),
        ('synced',  'อัปเดตสำเร็จ'),
    ], string='Sync Status', default='idle', readonly=True,
       help='แสดงสถานะการส่งข้อมูลไป Backend')

    # ── สถิติสะสม ────────────────────────────────────────────────────────
    total_trips        = fields.Integer(string='Total Trips',        default=0)
    total_distance_km  = fields.Float(string='Total Distance (km)',  digits=(10, 2), default=0.0)
    avg_driver_score   = fields.Float(string='Avg Driver Score',     digits=(5,  2), default=0.0)
    # ชั่วโมงเดินเครื่องสะสม — เป็น Trigger ที่ 2 ของการแจ้งเตือนซ่อมบำรุง
    # (อีก 2 แบบคือระยะทางสะสมและช่วงเวลา รวมเป็น 3 รูปแบบตาม FDD §2.2)
    telematics_engine_hours = fields.Float(
        string='Engine Hours (สะสม)', digits=(10, 2), default=0.0,
        help='ชั่วโมงเดินเครื่องสะสม รวมจาก duration_min ของทุกทริปที่ sync แล้ว '
             '— ใช้เป็น Trigger ที่ 2 ของการแจ้งเตือนซ่อมบำรุง (FDD §2.2)')
    telematics_log_ids = fields.One2many(
        'fleet.telematics.log', 'vehicle_id', string='Trip Logs'
    )

    def _get_api_credentials(self):
        """ดึง API URL/Key ที่ใช้งานอยู่ตอนนี้ — error ทันทีถ้ายังไม่ตั้งค่า"""
        Config = self.env['fleet.telematics.config']
        api_url = Config.get_active_api_url()
        api_key = Config.get_active_api_key()
        if not api_url:
            raise UserError(
                'ยังไม่ได้ตั้งค่า Backend API URL\n'
                'ไปที่ Fleet Telematics → Settings แล้วกรอก API URL'
            )
        return api_url, api_key

    def action_register_device(self):
        """ลงทะเบียนผูก GPS Device เข้ากับรถคันนี้เป็นครั้งแรก (POST
        /config_device/register) — ใช้ครั้งแรกที่ Device ยังไม่เคยลงทะเบียน
        เลย ถ้าจะ "ย้าย" Device ไปผูกรถคันอื่นในภายหลัง ให้ใช้
        action_sync_to_backend() (ปุ่ม Push to Backend) แทน"""
        self.ensure_one()
        if not self.telematics_device_id:
            raise UserError('กรุณากรอก GPS Device ID ก่อน (รูปแบบ KTC-XXX)')
        if not self.telematics_device_name:
            raise UserError('กรุณากรอก Device Name ก่อน')

        api_url, api_key = self._get_api_credentials()

        payload = {
            'device_id': self.telematics_device_id.upper(),
            'device_name': self.telematics_device_name,
            'vehicle_id': self.id,
        }

        try:
            resp = requests.post(
                f'{api_url}/api/v1/config_device/register',
                json=payload,
                headers={'APIKEY': api_key},
                timeout=15,
            )
        except requests.RequestException as e:
            self.write({
                'telematics_register_status': 'error',
                'telematics_register_error': str(e),
            })
            raise UserError(f'เชื่อมต่อ Backend ไม่สำเร็จ: {e}')

        if resp.status_code == 201:
            data = resp.json()
            self.write({
                'telematics_register_status': 'registered',
                'telematics_registered_at': data.get('registered_at') and
                    data['registered_at'].replace('T', ' ')[:19],
                'telematics_register_error': False,
                'previous_device_id': self.telematics_device_id,
            })
            return True

        if resp.status_code == 409:
            try:
                msg = resp.json().get('message', 'Device/Vehicle ถูกผูกไว้แล้ว')
            except ValueError:
                msg = 'Device/Vehicle ถูกผูกไว้แล้ว'
            self.write({
                'telematics_register_status': 'error',
                'telematics_register_error': msg,
            })
            raise UserError(
                f'ไม่สามารถลงทะเบียนได้ (409): {msg}\n'
                'ถ้า Device นี้เคยลงทะเบียนกับรถคันอื่นมาก่อน ให้ใช้ปุ่ม '
                '"Push to Backend" แทน (จะยิง PUT /config/vehicle เพื่อย้ายการผูกแทน)'
            )

        self.write({
            'telematics_register_status': 'error',
            'telematics_register_error': resp.text[:500],
        })
        raise UserError(f'Backend ตอบกลับผิดพลาด (HTTP {resp.status_code}): {resp.text[:300]}')

    @api.constrains('license_plate', 'telematics_device_id')
    def _check_duplicate_vehicle(self):
        """ห้ามทะเบียนรถหรือ Device ID ซ้ำกับรถคันอื่นในระบบ"""
        for rec in self:
            if rec.license_plate:
                dup = self.search([
                    ('license_plate', '=', rec.license_plate),
                    ('id', '!=', rec.id),
                ], limit=1)
                if dup:
                    raise ValidationError(
                        f'🚗 รถคันนี้มีอยู่ในระบบแล้ว!\n'
                        f'ทะเบียน "{rec.license_plate}" ถูกใช้โดยรถ: {dup.name}'
                    )
            if rec.telematics_device_id:
                dup_dev = self.search([
                    ('telematics_device_id', '=', rec.telematics_device_id),
                    ('id', '!=', rec.id),
                ], limit=1)
                if dup_dev:
                    raise ValidationError(
                        f'📡 บอร์ด GPS นี้มีอยู่ในระบบแล้ว!\n'
                        f'Device ID "{rec.telematics_device_id}" ถูกใช้โดยรถ: {dup_dev.name}'
                    )

    def write(self, vals):
        """ก่อนเปลี่ยน telematics_device_id ให้จำค่าเดิมไว้ที่
        previous_device_id ในการเขียนครั้งเดียวกัน (ไม่ write ซ้อน ไม่ trigger
        constrains สองรอบ ไม่มี race condition) เพื่อใช้เป็น old_device_id
        ตอนยิง PUT ไป Backend ภายหลัง"""
        if 'telematics_device_id' in vals:
            new_val = vals.get('telematics_device_id') or ''
            for rec in self:
                old_val = rec.telematics_device_id or ''
                if old_val and old_val != new_val:
                    super(FleetVehicleExt, rec).write(
                        dict(vals, previous_device_id=old_val)
                    )
            remaining = self.filtered(
                lambda r: not (r.telematics_device_id and
                               r.telematics_device_id != new_val)
            )
            if remaining:
                return super(FleetVehicleExt, remaining).write(vals)
            return True
        return super().write(vals)

    def action_sync_to_backend(self):
        """ส่งข้อมูลรถ+บอร์ด+คนขับปัจจุบันไปอัปเดตที่ Backend (PUT /api/v1/
        config/vehicle) — ใช้ตอนย้ายบอร์ดไปผูกรถอื่น หรือเปลี่ยนคนขับ
        หลังส่งสำเร็จจะอัปเดต previous_device_id ให้พร้อมสำหรับการเปลี่ยน
        บอร์ดครั้งถัดไปทันที"""
        self.ensure_one()

        if not self.telematics_device_id:
            raise UserError('กรุณาระบุ GPS Device ID ในแท็บ Telematics ก่อน')

        api_url, api_key = self._get_api_credentials()

        new_device = self.telematics_device_id or ''
        old_device = self.previous_device_id or None

        payload = {
            'vehicle_id':    int(self.id),
            'new_device_id': new_device,
            'old_device_id': old_device,
            'driver_id':     self.driver_id.id if self.driver_id else 0,
        }

        _logger.info(
            'action_sync_to_backend: vehicle_id=%s new_device=%s old_device=%s payload=%s',
            self.id, new_device, old_device, payload
        )

        super(FleetVehicleExt, self).write({'sync_status': 'syncing'})

        try:
            resp = requests.put(
                f'{api_url}/api/v1/config/vehicle',
                headers={'APIKEY': api_key, 'Content-Type': 'application/json'},
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()

            # ใช้ super() ตรงๆ เพื่อข้าม write() hook ด้านบน (ไม่ต้องการ
            # บันทึก previous_device_id ซ้ำอีกรอบตรงนี้)
            super(FleetVehicleExt, self).write({
                'sync_status':       'synced',
                'previous_device_id': new_device,
            })

            _logger.info(
                'action_sync_to_backend: success vehicle_id=%s → HTTP %s',
                self.id, resp.status_code
            )

            old_label = f'เปลี่ยนจาก {old_device} → ' if old_device else 'บอร์ดใหม่: '
            return {
                'type': 'ir.actions.client',
                'tag':  'display_notification',
                'params': {
                    'title':   '⬆️ ส่งข้อมูลสำเร็จ',
                    'message': (
                        f'รถ {self.name}  (Vehicle ID: {self.id})\n'
                        f'📡 {old_label}{new_device}\n'
                        f'Backend อัปเดตเรียบร้อยแล้ว'
                    ),
                    'type':   'success',
                    'sticky': False,
                },
            }

        except requests.RequestException as e:
            super(FleetVehicleExt, self).write({'sync_status': 'idle'})
            raise UserError(f'ส่งข้อมูลไป Backend ไม่สำเร็จ:\n{e}')

    def action_check_vehicle_status(self):
        """ดึงพิกัด/ความเร็ว/สถานะ ignition สดของรถคันนี้ (GET /vehicles/
        {id}/location) มาอัปเดตหน้าจอทันที — ถือว่า Online ถ้า ignition
        เปิดอยู่หรือความเร็ว > 0 นอกจากนี้ยังอัปเดต telematics_device_id
        ตามที่ Backend ตอบกลับมาด้วย เผื่อกรณี Backend สลับบอร์ดไปแล้ว"""
        self.ensure_one()

        api_url, api_key = self._get_api_credentials()

        super(FleetVehicleExt, self).write({'sync_status': 'syncing'})

        try:
            resp = requests.get(
                f'{api_url}/api/v1/vehicles/{self.id}/location',
                headers={'APIKEY': api_key},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

        except requests.RequestException as e:
            super(FleetVehicleExt, self).write({'sync_status': 'idle'})
            raise UserError(f'เรียก Backend API ไม่สำเร็จ:\n{e}')

        lat      = data.get('lat',      self.last_lat)
        lon      = data.get('lon',      self.last_lon)
        speed    = float(data.get('speed',    0) or 0)
        ignition = bool(data.get('ignition', False))
        ts_raw   = data.get('ts')

        backend_device_id = data.get('device_id') or self.telematics_device_id or '-'

        if ts_raw:
            try:
                dt = datetime.fromisoformat(ts_raw)
                last_seen = dt.astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                last_seen = fields.Datetime.now()
        else:
            last_seen = fields.Datetime.now()

        is_online = ignition or (speed > 0)

        write_vals = {
            'last_lat':      lat,
            'last_lon':      lon,
            'last_seen':     last_seen,
            'current_speed': speed,
            'ignition':      ignition,
            'online_status': 'online' if is_online else 'offline',
            'sync_status':   'synced',
        }
        if backend_device_id and backend_device_id != '-':
            write_vals['telematics_device_id'] = backend_device_id

        self.write(write_vals)

        _logger.info(
            'action_check_vehicle_status: vehicle_id=%s device=%s online=%s speed=%s lat=%s lon=%s',
            self.id, backend_device_id, is_online, speed, lat, lon
        )

        device_line = (
            f'📡 รหัสบอร์ดปัจจุบัน: {backend_device_id} (เชื่อมต่อแล้ว)'
            if backend_device_id and backend_device_id != '-'
            else '📡 สถานะบอร์ด: ยังไม่ได้เชื่อมต่อบอร์ด'
        )
        lat_fmt = f'{float(lat):.6f}' if lat else '-'
        lon_fmt = f'{float(lon):.6f}' if lon else '-'

        return {
            'type': 'ir.actions.client',
            'tag':  'display_notification',
            'params': {
                'title':   f'{"🟢 Online" if is_online else "🔴 Offline"} — {self.name}',
                'message': '\n'.join([
                    f'🚗 Vehicle ID: {self.id}  ({self.name})',
                    device_line,
                    f'📍 พิกัดล่าสุด (Real-time): {lat_fmt}, {lon_fmt}',
                    f'🔑 Ignition: {"เปิด ✅" if ignition else "ปิด 🔴"}',
                    f'💨 Speed: {speed} km/h',
                ]),
                'type':   'success' if is_online else 'warning',
                'sticky': True,
            },
        }

    def action_verify_device(self):
        """ตรวจว่า Device ID ที่ Backend ผูกกับรถคันนี้จริง ตรงกับ
        telematics_device_id ที่บันทึกไว้ใน Odoo หรือไม่ (GET /vehicles/
        {id}/device) — ต่างจาก action_check_vehicle_status ตรงที่ตัวนี้
        เช็คเฉพาะ "ความถูกต้องของการผูก device" ไม่ใช่พิกัด/ความเร็วสด

        กรณีเชื่อมต่อ Backend ไม่ได้ หรือ Backend ตอบ 404 (ไม่รู้จักรถคันนี้
        เลย) ถือเป็น mismatch เสมอไม่มีเงื่อนไข เพราะทั้งสองกรณีคือสัญญาณ
        ว่าข้อมูลไม่ตรงกันแน่นอน ไม่ว่า Odoo จะมีค่า telematics_device_id
        อยู่หรือไม่ก็ตาม

        หมายเหตุการทดสอบ: ถ้าเทสของฟังก์ชันนี้ครอบด้วย self.assertRaises()
        ของ Odoo (ไม่ใช่ try/except ของ Python เฉยๆ) ค่าที่ write() ไว้ตรงนี้
        จะถูก rollback ทิ้งไปเสมอทันทีที่จับ UserError ได้ — เพราะ
        self.assertRaises() ของ Odoo (TransactionCase) ตั้ง savepoint ก่อน
        เข้า block แล้ว rollback กลับไปที่นั่นทันทีที่จับ exception ที่คาดไว้
        ได้ (กันโค้ดทดสอบ error ทิ้งข้อมูลเพี้ยนไว้) เทสของฟังก์ชันนี้จึงต้อง
        ใช้ try/except ธรรมดาแทน ดู tests/test_fleet_integration.py:
        TestUC12VerifyDevice.test_03/test_04"""
        self.ensure_one()

        api_url, api_key = self._get_api_credentials()

        try:
            resp = requests.get(
                f'{api_url}/api/v1/vehicles/{self.id}/device',
                headers={'APIKEY': api_key},
                timeout=10,
            )
        except requests.RequestException as e:
            self.write({
                'device_verified_at':     fields.Datetime.now(),
                'device_verify_mismatch': True,
                'device_verify_note':     f'เรียก Backend ไม่สำเร็จ: {e}',
            })
            raise UserError(f'ตรวจสอบ Device ไม่สำเร็จ — เรียก Backend ไม่ได้:\n{e}')

        if resp.status_code == 404:
            mismatch = True
            note = (
                'Backend ไม่มีข้อมูล Device ผูกกับรถคันนี้ '
                f'(Odoo บันทึกไว้ว่า: {self.telematics_device_id or "-"})'
            )
            self.write({
                'device_verified_at':     fields.Datetime.now(),
                'device_verify_mismatch': mismatch,
                'device_verify_note':     note,
            })
            raise UserError(f'⚠️ {note}')

        resp.raise_for_status()
        data = resp.json()

        backend_device_id = (data.get('device_id') or '').strip()
        odoo_device_id     = (self.telematics_device_id or '').strip()
        mismatch = backend_device_id.upper() != odoo_device_id.upper()

        last_update = data.get('date_update_latest') or data.get('registered_at')

        note = (
            f'Odoo: {odoo_device_id or "-"}  |  Backend: {backend_device_id or "-"}'
            + (f'  |  อัปเดตล่าสุด (Backend): {last_update}' if last_update else '')
        )

        self.write({
            'device_verified_at':     fields.Datetime.now(),
            'device_verify_mismatch': mismatch,
            'device_verify_note':     note,
        })

        _logger.info(
            'action_verify_device: vehicle_id=%s odoo=%s backend=%s mismatch=%s',
            self.id, odoo_device_id, backend_device_id, mismatch,
        )

        return {
            'type': 'ir.actions.client',
            'tag':  'display_notification',
            'params': {
                'title':   '⚠️ Device ไม่ตรงกัน!' if mismatch else '✅ Device ตรงกัน',
                'message': note,
                'type':    'danger' if mismatch else 'success',
                'sticky':  mismatch,
            },
        }

    def get_trip_history(self, page=1, limit=20,
                         date_from=None, date_to=None, synced_only=None):
        """ดึงประวัติทริปของรถคันนี้จาก Backend (GET /vehicles/{vehicle_id}/
        trips) — vehicle_id ในที่นี้คือ Odoo record ID (int) ไม่ใช่ device_id
        รองรับแบ่งหน้า + กรองตามช่วงวันที่ + เฉพาะที่ sync แล้ว"""
        self.ensure_one()

        api_url, api_key = self._get_api_credentials()

        url = f'{api_url}/api/v1/vehicles/{self.id}/trips'

        params = {'page': page, 'limit': min(limit, 200)}
        if date_from:
            params['date_from'] = date_from
        if date_to:
            params['date_to'] = date_to
        if synced_only is not None:
            params['synced_only'] = 'true' if synced_only else 'false'

        _logger.info('get_trip_history: GET %s params=%s', url, params)

        resp = requests.get(
            url,
            headers={'APIKEY': api_key} if api_key else {},
            params=params,
            timeout=30,
        )
        resp.raise_for_status()

        data = resp.json()
        if isinstance(data, list):
            return {'trips': data, 'total': len(data)}
        return data

    def action_view_vehicle_trips(self):
        """ปุ่มดู Trip History จาก Backend ในแท็บ Telematics Settings —
        ดึงมาแค่สรุปตัวเลขแสดงเป็น notification บอกให้ไปดูรายละเอียดเต็มที่
        เมนู Trip Logs แทน"""
        self.ensure_one()
        try:
            result = self.get_trip_history(limit=20, synced_only=False)
            trips  = result.get('trips', []) if isinstance(result, dict) else result
            total  = result.get('total', len(trips)) if isinstance(result, dict) else len(trips)
            pages  = result.get('total_pages', 1) if isinstance(result, dict) else 1
            return {
                'type':   'ir.actions.client',
                'tag':    'display_notification',
                'params': {
                    'title':   f'Trip History (Backend) — {self.name}',
                    'message': (
                        f'Backend มี {total} ทริป ({pages} หน้า)\n'
                        f'แสดง {len(trips)} รายการแรก\n'
                        f'ดูรายละเอียดครบที่เมนู Trip Logs'
                    ),
                    'type': 'info',
                },
            }
        except Exception as e:
            raise UserError(f'ดึงประวัติ trip ไม่สำเร็จ: {e}')


class FleetVehicleLogServicesExt(models.Model):
    """ต่อยอด fleet.vehicle.log.services (ประวัติการซ่อมบำรุงมาตรฐานของ
    Odoo Fleet) เพิ่ม snapshot ชั่วโมงเดินเครื่องสะสม ณ ตอนที่ทำ service
    แต่ละครั้ง — ใช้เป็นจุดอ้างอิงคำนวณ Trigger ชั่วโมงเดินเครื่องของรอบ
    ซ่อมบำรุงถัดไป (ดู models/telematics_log.py:
    _update_odometer_and_check_maintenance)"""
    _inherit = 'fleet.vehicle.log.services'

    engine_hours_at_service = fields.Float(
        string='Engine Hours (ตอน Service)', digits=(10, 2),
        help='ชั่วโมงเดินเครื่องสะสมของรถคันนี้ ณ ตอนที่ทำ service ครั้งนี้ '
             '— ใช้เทียบ Trigger ชั่วโมงเดินเครื่องของรอบซ่อมบำรุงถัดไป')
