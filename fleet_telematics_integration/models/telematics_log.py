# ==============================================================================
# models/telematics_log.py
#
# ประวัติเที่ยววิ่ง (Trip Log) — หัวใจของระบบ ดึงทริปจาก Backend มาเก็บ
# อัตโนมัติทุก 5 นาที (UC-05) พร้อมเหตุการณ์เสี่ยงที่เกิดระหว่างทาง แล้ว
# อัปเดตสถิติสะสมของรถ + เช็คว่าถึงรอบซ่อมบำรุงหรือยัง
#
# Endpoint ที่ใช้:
#   1) POST  /api/v1/webhook/odoo-sync        → ส่ง last_sync_timestamp รับ trips[]
#   2) PATCH /api/v1/trips/batch/mark-synced  → บอก Backend ว่า sync สำเร็จทั้งชุด
#   3) PATCH /api/v1/trips/{id}/mark-synced   → mark รายตัว (ใช้ manual เท่านั้น)
#   4) GET   /api/v1/trips/{id}               → ดึงเหตุการณ์เสี่ยง + GPS track
#
# ใช้ timestamp-based sync (ไม่ใช่ cursor/last_id) — ต้องใช้ค่า
# last_sync_timestamp ที่ Backend ส่งกลับมาเท่านั้น ห้ามคำนวณเวลาเองใน Odoo
# เพื่อป้องกันปัญหานาฬิกาเครื่องไม่ตรงกัน (clock drift)
# ==============================================================================
import logging
import requests
from datetime import datetime, timezone
from markupsafe import Markup

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_PARAM_LAST_TS = 'fleet_telematics.trip_last_sync_timestamp'
_BATCH_FULL    = 200


class TelematicsLog(models.Model):
    """ทริป 1 รายการ = การเดินทาง 1 เที่ยวของรถ 1 คัน พร้อมคะแนนพฤติกรรม
    การขับขี่และสถิติต่างๆ ของทริปนั้น"""
    _name        = 'fleet.telematics.log'
    _description = 'Fleet Telematics Trip Log'
    _order       = 'trip_start desc'
    _rec_name    = 'display_name'

    _external_trip_id_unique = models.Constraint(
        'UNIQUE(external_trip_id)',
        'external_trip_id ต้องไม่ซ้ำกัน — ห้ามบันทึก Trip ซ้ำจาก Backend',
    )

    vehicle_id = fields.Many2one(
        'fleet.vehicle', string='Vehicle',
        required=True, ondelete='restrict')
    driver_id = fields.Many2one(
        'hr.employee', string='Driver',
        required=False,
        # optional เพราะบางทริป Backend อาจส่ง driver_id=null/0 มา (ยังไม่ได้
        # assign คนขับ) — ต้องบันทึกทริปได้ก่อน แล้วค่อยผูกคนขับทีหลังได้
        ondelete='set null')
    telematics_device_id = fields.Char(
        string='Device ID',
        help='รหัสกล่องพ่วง GPS เช่น KTC-001')

    trip_start   = fields.Datetime(string='Trip Start', required=True)
    trip_end     = fields.Datetime(string='Trip End')
    duration_min = fields.Float(
        string='Duration (min)',
        compute='_compute_duration', store=True,
        digits=(10, 2))

    distance_km   = fields.Float(string='Distance (km)',    digits=(10, 2))
    max_speed     = fields.Float(string='Max Speed (km/h)', digits=(10, 2))
    avg_speed     = fields.Float(string='Avg Speed (km/h)', digits=(10, 2))
    idle_min      = fields.Float(string='Idle Time (min)',  digits=(10, 2))
    fuel_used_est = fields.Float(string='Fuel Est. (L)',    digits=(10, 3))

    driver_score       = fields.Float(string='Driver Score',        digits=(5, 2))
    harsh_brake_count  = fields.Integer(string='Harsh Brakes')
    harsh_accel_count  = fields.Integer(string='Harsh Accelerations')
    harsh_corner_count = fields.Integer(string='Harsh Cornering')
    speeding_count     = fields.Integer(string='Speeding Events')

    gps_track_json   = fields.Text(string='GPS Track (JSON)',
        help='เก็บ GPS track ทั้งสาย เช่น [{"lat": 18.7883, "lon": 98.9853, "ts": "..."}]')
    external_trip_id = fields.Char(
        string='External Trip ID',
        index=True,
        help='Trip ID จาก MTD Backend สำหรับ sync และ dedup')

    state = fields.Selection([
        ('draft',     'Draft'),
        ('confirmed', 'Confirmed'),
        ('synced',    'Synced'),
        ('failed',    'Failed'),
    ], string='Sync Status', default='draft')

    # กันทริปเดิมถูกยิง GET /trips/{id} ซ้ำทุกรอบ cron เพื่อดึงเหตุการณ์เสี่ยง
    # — ดึงครั้งเดียวตอนสร้างทริปใหม่ก็พอ
    events_synced = fields.Boolean(
        string='Events Synced', default=False, readonly=True,
        help='True แล้วเมื่อดึง Harsh Events ของ trip นี้จาก Backend มาเก็บครบแล้ว')

    event_ids = fields.One2many(
        'fleet.telematics.event', 'trip_id', string='Harsh Events')

    display_name = fields.Char(
        compute='_compute_display_name', store=True)

    @api.depends('vehicle_id', 'trip_start')
    def _compute_display_name(self):
        for rec in self:
            v = rec.vehicle_id.name or '?'
            t = rec.trip_start.strftime('%d/%m/%y %H:%M') if rec.trip_start else '-'
            rec.display_name = f'{v} — {t}'

    @api.depends('trip_start', 'trip_end')
    def _compute_duration(self):
        for rec in self:
            if rec.trip_start and rec.trip_end:
                rec.duration_min = (rec.trip_end - rec.trip_start).total_seconds() / 60
            else:
                rec.duration_min = 0.0

    def action_confirm(self):
        for rec in self:
            if rec.state == 'draft':
                rec.state = 'confirmed'

    @api.model
    def _cron_sync_trips(self):
        """Cron หลัก รันทุก 5 นาที ดึงทริปใหม่จาก Backend มาเก็บลง Odoo

        ขั้นตอน:
          1. POST /webhook/odoo-sync ส่ง last_sync_timestamp เดิมไป (รอบแรก
             ไม่มีค่านี้ → Backend ส่งทริปที่ยังไม่ sync ทั้งหมดกลับมา)
          2. บันทึกแต่ละทริปลง Odoo (update ถ้ามีอยู่แล้ว / สร้างใหม่ถ้าไม่มี)
          3. PATCH /trips/batch/mark-synced บอก Backend ว่าอันไหนสำเร็จแล้ว
          4. ทริปที่บันทึกไม่สำเร็จ (failed) จะไม่ถูก mark-synced เด็ดขาด —
             ปล่อยให้รอบ cron ถัดไปดึงมาลองใหม่เองอัตโนมัติ (Backend ยังไม่รู้
             ว่า sync แล้ว จึงส่งซ้ำให้เอง เป็นกลไก safety net ที่ตั้งใจ
             ออกแบบไว้ ป้องกันข้อมูลหายถาวรแบบเงียบๆ)
          5. เก็บ last_sync_timestamp ใหม่ที่ได้จาก Backend เท่านั้น (ห้ามใช้
             เวลาของ Odoo เองเด็ดขาด กันปัญหานาฬิกาเครื่องไม่ตรงกัน)
          6. ถ้า batch เต็ม (total == 200) วนดึงต่อทันที เผื่อมีทริปเหลืออีก
        """
        cfg_model = self.env['fleet.telematics.config']
        api_url   = cfg_model.get_active_api_url()
        api_key   = cfg_model.get_active_api_key()

        if not api_url:
            _logger.warning('fleet_telematics: ยังไม่ได้ตั้งค่า API URL — ข้าม Cron')
            return

        ICP     = self.env['ir.config_parameter'].sudo()
        last_ts = ICP.get_param(_PARAM_LAST_TS, '') or None

        total_synced = 0
        loop_count   = 0

        while True:
            loop_count += 1

            try:
                trips, new_ts, total = self._fetch_trips_batch(api_url, api_key, last_ts)
            except requests.RequestException as e:
                _logger.error('_cron_sync_trips: POST /webhook/odoo-sync ล้มเหลว: %s', e)
                cfg = cfg_model.search([], limit=1)
                if cfg:
                    cfg.write({'last_error': str(e)})
                return

            if not trips:
                _logger.info('_cron_sync_trips: ไม่มี trip ใหม่ (last_ts=%s)', last_ts)
                break

            _logger.info(
                '_cron_sync_trips loop %d: %d trips (total=%d) last_ts=%s',
                loop_count, len(trips), total, last_ts,
            )

            synced_ids = []
            failed_ids = []
            for t in trips:
                ext_id = t.get('id')
                if not ext_id:
                    continue
                vals = self._build_trip_vals(t)
                if not vals:
                    continue
                try:
                    existing = self.search(
                        [('external_trip_id', '=', str(ext_id))], limit=1)
                    if existing:
                        existing.write(vals)
                        trip_rec = existing
                    else:
                        trip_rec = self.create(vals)
                    synced_ids.append(ext_id)

                    # ดึงเหตุการณ์เสี่ยงของทริปนี้มาเก็บอัตโนมัติ — ทำครั้ง
                    # เดียวต่อทริป (กันยิง API ซ้ำทุกรอบ cron) ห่อ try/except
                    # ไม่ให้พัง flow หลักถ้าดึงไม่สำเร็จ
                    if not trip_rec.events_synced:
                        try:
                            self._sync_trip_events(trip_rec, api_url, api_key)
                        except Exception as e:
                            _logger.warning(
                                '_cron_sync_trips: ดึง events ของ trip %s ไม่สำเร็จ: %s',
                                ext_id, e)

                    # อัปเดต odometer + engine hours + สถิติสะสมของรถ แล้ว
                    # เช็คว่าถึงรอบซ่อมบำรุงหรือยัง
                    if vals.get('vehicle_id') and vals.get('distance_km'):
                        self._update_odometer_and_check_maintenance(
                            vals['vehicle_id'], vals['distance_km'],
                            duration_min=trip_rec.duration_min or 0.0,
                            driver_score=vals.get('driver_score'))
                except Exception as e:
                    _logger.warning(
                        '_cron_sync_trips: บันทึก trip %s ล้มเหลว: %s', ext_id, e)
                    failed_ids.append(ext_id)

            if synced_ids:
                try:
                    self._mark_trips_synced(api_url, api_key, synced_ids)
                    total_synced += len(synced_ids)
                    # อัปเดต History fields ของ Scoring Config ที่ Active อยู่
                    # ตอนนี้ (FDD §12.5: track ว่า config ถูกใช้กับกี่ trip แล้ว)
                    self.env['fleet.telematics.scoring.config']._track_usage(
                        count=len(synced_ids))
                except requests.RequestException as e:
                    _logger.error(
                        '_cron_sync_trips: batch mark-synced ล้มเหลว: %s '
                        '— ไม่อัปเดต last_ts รอบหน้าดึงซ้ำ idempotent', e)
                    cfg = cfg_model.search([], limit=1)
                    if cfg:
                        cfg.write({'last_error': str(e)})
                    return

            if failed_ids:
                _logger.error(
                    '_cron_sync_trips: บันทึก %d trip ลง Odoo ไม่สำเร็จ '
                    '(external_trip_id=%s) — Backend ยังไม่ถูกแจ้งว่า synced '
                    'จะดึงมาลองใหม่อัตโนมัติในรอบ cron ถัดไป',
                    len(failed_ids), failed_ids,
                )
                cfg = cfg_model.search([], limit=1)
                if cfg:
                    cfg.write({
                        'last_error': (
                            f'{len(failed_ids)} trip บันทึกไม่สำเร็จ (ids: {failed_ids}) '
                            f'— รอ retry อัตโนมัติรอบหน้า ดู log ระดับ error สำหรับสาเหตุ'
                        ),
                    })

            # อัปเดตค่า last_sync_timestamp ใหม่ — ป้องกัน loop ไม่สิ้นสุด
            # ถ้า Backend ไม่ส่ง new_ts มา หรือส่งค่าเดิมซ้ำ ให้หยุดทันที
            if new_ts and new_ts != last_ts:
                ICP.set_param(_PARAM_LAST_TS, new_ts)
                last_ts = new_ts
            elif not new_ts:
                _logger.warning(
                    '_cron_sync_trips: Backend ไม่ส่ง last_sync_timestamp กลับมา '
                    '(loop %d) — หยุด loop ป้องกันวนซ้ำไม่สิ้นสุด', loop_count)
                break
            elif new_ts == last_ts:
                _logger.warning(
                    '_cron_sync_trips: last_sync_timestamp ไม่เปลี่ยน (=%s, loop %d) '
                    '— หยุด loop ป้องกันวนซ้ำ', new_ts, loop_count)
                break

            if total < _BATCH_FULL:
                break
            _logger.warning(
                '_cron_sync_trips: total=%d batch เต็ม → loop ต่อรอบ %d',
                _BATCH_FULL, loop_count + 1,
            )

        cfg = cfg_model.search([], limit=1)
        if cfg:
            cfg.write({'last_sync_at': fields.Datetime.now(), 'last_error': False})
        _logger.info(
            '_cron_sync_trips: เสร็จ %d trips ใน %d loop',
            total_synced, loop_count,
        )

    @api.model
    def _fetch_trips_batch(self, api_url, api_key, last_ts):
        """ยิง POST /webhook/odoo-sync ครั้งเดียว คืนทริปที่ยังไม่ sync
        กลับมาพร้อม timestamp ล่าสุดที่ต้องใช้อ้างอิงรอบถัดไป — ถ้า last_ts
        เป็น None (รอบแรก) จะไม่ส่ง field นี้ไปเลย Backend จะส่งทริปทั้งหมด
        ที่ยังไม่เคย sync กลับมา"""
        url  = f'{api_url}/api/v1/webhook/odoo-sync'
        body = {}
        if last_ts:
            body['last_sync_timestamp'] = last_ts

        _logger.info('_fetch_trips_batch: POST %s body=%s', url, body)
        resp = requests.post(
            url,
            json=body,
            headers={'APIKEY': api_key} if api_key else {},
            timeout=30,
        )
        resp.raise_for_status()
        data   = resp.json()
        trips  = data.get('trips') or []
        new_ts = data.get('last_sync_timestamp')
        total  = int(data.get('total', len(trips)))
        return trips, new_ts, total

    @api.model
    def _retry_single_trip(self, api_url, api_key, trip_id):
        """PATCH /trips/{id}/mark-synced รายตัว — เป็นเครื่องมือสำรองสำหรับ
        Admin ใช้ด้วยมือเท่านั้น (ไม่ได้ถูกเรียกจาก _cron_sync_trips อัตโนมัติ
        อีกต่อไป เพื่อป้องกันการ mark ทริปที่ยังไม่ได้บันทึกลง Odoo จริงว่า
        sync สำเร็จแล้ว) ใช้ได้เฉพาะกรณีตรวจสอบด้วยตาแล้วว่า Odoo มี record
        ของทริปนี้ถูกต้องสมบูรณ์จริง แต่ Backend ยังไม่รู้ (เช่น batch
        mark-synced ครั้งก่อนล้มเหลวไปบางส่วน)"""
        url = f'{api_url}/api/v1/trips/{trip_id}/mark-synced'
        _logger.info('_retry_single_trip: PATCH %s', url)

        from datetime import datetime as _dt, timezone as _tz
        synced_at = _dt.now(_tz.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

        resp = requests.patch(
            url,
            json={'synced_at': synced_at},
            headers={'APIKEY': api_key} if api_key else {},
            timeout=15,
        )
        resp.raise_for_status()

    @api.model
    def _mark_trips_synced(self, api_url, api_key, trip_ids):
        """PATCH /trips/batch/mark-synced บอก Backend ว่าทริปชุดนี้บันทึก
        ลง Odoo สำเร็จแล้ว — เป็น all-or-nothing (ถ้าตัวใดตัวหนึ่งล้มเหลว
        ทั้ง batch จะ rollback) และ idempotent (ทริปที่ mark ไปแล้วจะถูก
        ข้ามเงียบๆ ไม่ error) ปล่อยให้ exception ลอยขึ้นไปให้ caller จัดการ
        เพราะถ้า PATCH ล้มเหลว caller จะไม่อัปเดต last_sync_timestamp ทำให้
        รอบหน้า Backend ส่งทริปชุดนี้มาให้ลองใหม่อีกครั้งอย่างปลอดภัย"""
        url = f'{api_url}/api/v1/trips/batch/mark-synced'

        _logger.info('_mark_trips_synced: PATCH %s trip_ids=%s', url, trip_ids)

        resp = requests.patch(
            url,
            headers={'APIKEY': api_key} if api_key else {},
            json={'trip_ids': trip_ids},
            timeout=30,
        )
        resp.raise_for_status()

    @api.model
    def _parse_trip_dt(self, value):
        """แปลงสตริงเวลาจาก Backend (ISO 8601 พร้อม timezone offset เช่น
        "2026-06-15T08:00:00+07:00") เป็น UTC naive datetime ที่ Odoo
        Datetime field รับได้ (Odoo เก็บเวลาเป็น UTC เสมอ ไม่มี timezone
        info ติดไปด้วย)"""
        if not value:
            return False
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except (ValueError, TypeError):
            _logger.warning('_parse_trip_dt: parse ไม่ได้ value=%s', value)
            return False

    @api.model
    def _build_trip_vals(self, t):
        """แปลง dict ทริป 1 รายการที่ได้จาก Backend (POST /webhook/
        odoo-sync) เป็น vals dict สำหรับ create()/write() — คืน {} ถ้าหารถ
        ที่เกี่ยวข้องไม่เจอเลย (caller จะข้ามทริปนั้นไป)

        การหารถ: ลองใช้ vehicle_id (Odoo record ID ที่ Backend อ้างอิงตรงๆ)
        ก่อน ถ้าไม่มีค่อย fallback ไปหาจาก device_id แทน
        การหาคนขับ: ใช้ driver_id (Odoo record ID ของ hr.employee) — อาจเป็น
        null ได้ถ้าทริปนั้นยังไม่ได้ assign คนขับ (ปลอดภัย เพราะ driver_id
        เป็น optional field)"""
        ext_id = t.get('id')
        if not ext_id:
            return {}

        vehicle = self.env['fleet.vehicle']
        raw_vehicle_id = t.get('vehicle_id')
        if raw_vehicle_id:
            vehicle = self.env['fleet.vehicle'].sudo().browse(int(raw_vehicle_id))
            if not vehicle.exists():
                vehicle = self.env['fleet.vehicle']

        device_id_str = t.get('device_id', '')
        if not vehicle and device_id_str:
            vehicle = self.env['fleet.vehicle'].sudo().search(
                [('telematics_device_id', '=', device_id_str)], limit=1)

        if not vehicle:
            _logger.warning(
                '_build_trip_vals: ไม่พบรถ (vehicle_id=%s, device_id=%s) — ข้าม trip id=%s',
                raw_vehicle_id, device_id_str, ext_id,
            )
            return {}

        driver = self.env['hr.employee']
        raw_driver_id = t.get('driver_id')
        if raw_driver_id:
            driver = self.env['hr.employee'].sudo().browse(int(raw_driver_id))
            if not driver.exists():
                _logger.warning(
                    '_build_trip_vals: ไม่พบ driver_id=%s ใน Odoo (trip id=%s)',
                    raw_driver_id, ext_id,
                )
                driver = self.env['hr.employee']

        trip_start = self._parse_trip_dt(t.get('trip_start'))
        if not trip_start:
            _logger.warning(
                '_build_trip_vals: trip_start parse ไม่ได้ (ค่าเดิม=%s) — ข้าม trip id=%s',
                t.get('trip_start'), ext_id,
            )
            return {}

        return {
            'external_trip_id':     str(ext_id),
            'vehicle_id':            vehicle.id,
            'driver_id':             driver.id if driver else False,
            'telematics_device_id':  device_id_str or vehicle.telematics_device_id,
            'trip_start':            trip_start,
            'trip_end':              self._parse_trip_dt(t.get('trip_end')),
            'distance_km':           float(t.get('distance_km',    0) or 0),
            'avg_speed':             float(t.get('avg_speed',      0) or 0),
            'max_speed':             float(t.get('max_speed',      0) or 0),
            'idle_min':              float(t.get('idle_min',       0) or 0),
            'fuel_used_est':         float(t.get('fuel_used',      0) or 0),  # Backend ใช้ชื่อ 'fuel_used'
            'driver_score':          float(t.get('driver_score',   0) or 0),
            'harsh_brake_count':     int(t.get('harsh_brake_count',  0) or 0),
            'harsh_accel_count':     int(t.get('harsh_accel_count',  0) or 0),
            'harsh_corner_count':    int(t.get('harsh_corner_count', 0) or 0),
            'speeding_count':        int(t.get('speeding_count',     0) or 0),
            'gps_track_json':        t.get('gps_track_json', ''),
            'state':                 'synced',
        }

    def _sync_trip_events(self, trip_rec, api_url, api_key):
        """ดึงเหตุการณ์เสี่ยงของทริปนี้จาก Backend (GET /trips/{trip_id})
        มาเก็บใน fleet.telematics.event อัตโนมัติ — เขียนแบบ defensive
        รองรับหลายชื่อ key ที่เป็นไปได้ (schema ของ event ในนี้ยังไม่ได้
        ยืนยันตายตัว 100% กับทีม Backend) กันสร้างซ้ำด้วยการเทียบ trip_id +
        event_type + occurred_at ก่อนสร้างทุกครั้ง"""
        if not trip_rec.external_trip_id:
            return

        resp = requests.get(
            f'{api_url}/api/v1/trips/{trip_rec.external_trip_id}',
            headers={'APIKEY': api_key} if api_key else {},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        raw_events = (
            data.get('events') or data.get('event_list')
            or data.get('harsh_events') or []
        )
        if not raw_events:
            trip_rec.write({'events_synced': True})
            return

        EventModel = self.env['fleet.telematics.event']
        vals_list = []
        for ev in raw_events:
            occurred_at = self._parse_trip_dt(
                ev.get('occurred_at') or ev.get('ts') or ev.get('timestamp'))
            event_type = ev.get('event_type') or ev.get('event') or ev.get('type')
            if not occurred_at or not event_type:
                _logger.warning(
                    '_sync_trip_events: ข้าม event ที่ไม่มี occurred_at/event_type '
                    '(trip=%s, raw=%s)', trip_rec.external_trip_id, ev)
                continue

            dup = EventModel.sudo().search([
                ('trip_id', '=', trip_rec.id),
                ('event_type', '=', event_type),
                ('occurred_at', '=', occurred_at),
            ], limit=1)
            if dup:
                continue

            severity = ev.get('severity')
            if severity is None:
                severity = ev.get('event_severity')
            # ถ้า Backend ส่งเป็นสัดส่วน 0-1 (เช่น 0.82) แปลงเป็นสเกล 0-100
            if isinstance(severity, (int, float)) and 0 <= severity <= 1:
                severity = severity * 100

            vals_list.append({
                'trip_id':        trip_rec.id,
                'event_type':     event_type,
                'occurred_at':    occurred_at,
                'lat':            ev.get('lat', 0.0) or 0.0,
                'lon':            ev.get('lon', 0.0) or 0.0,
                'severity':       severity or 0.0,
                'speed_at_event': ev.get('speed', ev.get('speed_at_event', 0.0)) or 0.0,
                'description':    ev.get('description') or '',
            })

        if vals_list:
            # context flag พิเศษนี้เป็นทางเดียวที่ผ่าน create() override ของ
            # fleet.telematics.event ได้ (ดู models/telematics_event.py)
            EventModel.sudo().with_context(
                fleet_telematics_allow_sync=True
            ).create(vals_list)

        trip_rec.write({'events_synced': True})

    def action_load_trip_detail(self):
        """ปุ่มดึง GPS Track เต็มเส้นทางของทริปนี้จาก Backend (GET /trips/
        {trip_id}) มาเก็บไว้แสดงบนแผนที่ในแท็บ GPS Track"""
        self.ensure_one()
        if not self.external_trip_id:
            raise UserError('Trip นี้ยังไม่มี External Trip ID — ไม่สามารถดึงจาก Backend ได้')

        cfg_model = self.env['fleet.telematics.config']
        api_url   = cfg_model.get_active_api_url()
        api_key   = cfg_model.get_active_api_key()
        if not api_url:
            raise UserError('กรุณาตั้งค่า API URL ใน Settings ก่อน')

        try:
            resp = requests.get(
                f'{api_url}/api/v1/trips/{self.external_trip_id}',
                headers={'APIKEY': api_key},
                timeout=15,
            )
        except requests.RequestException as e:
            raise UserError(f'เชื่อมต่อ Backend ไม่สำเร็จ: {e}')

        if resp.status_code != 200:
            raise UserError(f'Backend ตอบ (HTTP {resp.status_code}): {resp.text[:300]}')

        data      = resp.json()
        gps_track = data.get('gps_track') or data.get('gps_points') or []

        import json as _json
        self.write({'gps_track_json': _json.dumps(gps_track, ensure_ascii=False)})

        return {
            'type':    'ir.actions.client',
            'tag':     'display_notification',
            'params': {
                'title':   'โหลด GPS Track สำเร็จ',
                'message': f'ได้รับ {len(gps_track)} จุด GPS จาก Backend',
                'type':    'success',
            },
        }

    @api.model
    def _update_odometer_and_check_maintenance(self, vehicle_id, distance_km,
                                                duration_min=0.0, driver_score=None):
        """อัปเดตเลขไมล์สะสม + ชั่วโมงเดินเครื่องสะสม + สถิติสะสมของรถ
        (จำนวนทริป/ระยะทาง/คะแนนเฉลี่ย) ทุกครั้งที่มีทริปใหม่เข้ามา แล้ว
        เช็คว่าถึงรอบซ่อมบำรุงหรือยัง — มี 3 Trigger:
          1) ระยะทางสะสมถึง threshold (default 10,000 km)
          2) ชั่วโมงเดินเครื่องสะสมถึง threshold (default 250 ชม.)
          3) ผ่านมานานถึง threshold นับจาก service ล่าสุด (default 90 วัน)
        threshold ทั้ง 3 ปรับได้ผ่าน ir.config_parameter ให้ Admin ตั้งเอง"""
        Vehicle = self.env['fleet.vehicle'].sudo().browse(vehicle_id)
        if not Vehicle.exists():
            return

        current_odometer = Vehicle.odometer
        new_odometer     = current_odometer + distance_km
        new_engine_hours = (Vehicle.telematics_engine_hours or 0.0) + (duration_min / 60.0)

        new_total_trips = (Vehicle.total_trips or 0) + 1
        new_total_km    = (Vehicle.total_distance_km or 0.0) + distance_km
        vals = {
            'odometer':                new_odometer,
            'telematics_engine_hours': new_engine_hours,
            'total_trips':             new_total_trips,
            'total_distance_km':       round(new_total_km, 2),
        }
        if driver_score is not None:
            # ค่าเฉลี่ยสะสมแบบ running average — ไม่ต้อง query ทริปทั้งหมดซ้ำ
            old_avg = Vehicle.avg_driver_score or 0.0
            old_n   = (Vehicle.total_trips or 0)
            vals['avg_driver_score'] = round(
                (old_avg * old_n + driver_score) / new_total_trips, 2)

        Vehicle.write(vals)

        ICP = self.env['ir.config_parameter'].sudo()
        km_threshold    = float(ICP.get_param('fleet_telematics.maintenance_km',    10000))
        hour_threshold  = float(ICP.get_param('fleet_telematics.maintenance_hours',  250))
        day_threshold   = int(  ICP.get_param('fleet_telematics.maintenance_days',    90))

        Service = self.env['fleet.vehicle.log.services'].sudo()

        last_service = Service.search([
            ('vehicle_id', '=', vehicle_id),
        ], order='date desc', limit=1)

        should_create = False
        reason        = ''

        if last_service:
            km_since = new_odometer - (last_service.odometer or 0)
            if km_since >= km_threshold:
                should_create = True
                reason = f'ระยะทางสะสม {km_since:,.0f} km (threshold {km_threshold:,.0f} km)'

            hours_since = new_engine_hours - (last_service.engine_hours_at_service or 0.0)
            if hours_since >= hour_threshold:
                should_create = True
                reason = (reason + ' / ' if reason else '') + \
                    f'ชั่วโมงเดินเครื่องสะสม {hours_since:,.1f} ชม. (threshold {hour_threshold:,.0f} ชม.)'

            if last_service.date:
                from datetime import date as _date
                days_since = (_date.today() - last_service.date).days
                if days_since >= day_threshold:
                    should_create = True
                    reason = (reason + ' / ' if reason else '') + \
                        f'ผ่านมา {days_since} วัน (threshold {day_threshold} วัน)'
        else:
            # ยังไม่เคย service เลย — เช็คทั้งระยะทางและชั่วโมงเครื่องยนต์
            # เทียบกับ threshold แรกทั้งคู่
            if new_odometer >= km_threshold:
                should_create = True
                reason = f'odometer {new_odometer:,.0f} km ถึง threshold แรก {km_threshold:,.0f} km'
            if new_engine_hours >= hour_threshold:
                should_create = True
                reason = (reason + ' / ' if reason else '') + \
                    f'ชั่วโมงเดินเครื่อง {new_engine_hours:,.1f} ชม. ถึง threshold แรก {hour_threshold:,.0f} ชม.'

        if should_create:
            service_rec = Service.create({
                'vehicle_id':             vehicle_id,
                'date':                   fields.Date.today(),
                'odometer':               new_odometer,
                'engine_hours_at_service': new_engine_hours,
                'description':            f'[Auto] แจ้งเตือนซ่อมบำรุง: {reason}',
                'state':                  'new',
            })
            _logger.info(
                '_check_maintenance: สร้าง service alert รถ %s (id=%s): %s',
                Vehicle.name, vehicle_id, reason,
            )

            # แจ้งเตือนผู้ใช้ในกลุ่ม Fleet Manager — query จาก res.users แทน
            # การอ่าน .users จากกลุ่มตรงๆ (field นั้นถูกถอดออกในบาง Odoo
            # version) wrap body ด้วย Markup() ให้ HTML tag render ถูกต้อง
            managers_group = self.env.ref('fleet.fleet_group_manager', raise_if_not_found=False)
            manager_users = self.env['res.users']
            if managers_group:
                User = self.env['res.users']
                for fname in ('groups_id', 'group_ids'):
                    if fname in User._fields:
                        manager_users = User.sudo().search([(fname, 'in', managers_group.ids)])
                        break

            if manager_users:
                body = Markup(
                    '🔧 <b>แจ้งเตือนซ่อมบำรุงอัตโนมัติ</b><br/>'
                    'รถ: <b>{vehicle}</b><br/>'
                    'เหตุผล: {reason}<br/>'
                    'Odometer ปัจจุบัน: {odo} km'
                ).format(vehicle=Vehicle.name, reason=reason, odo=f'{new_odometer:,.0f}')
                service_rec.sudo().message_post(
                    body=body,
                    partner_ids=manager_users.partner_id.ids,
                    message_type='notification',
                    subtype_xmlid='mail.mt_note',
                )
            else:
                _logger.warning(
                    '_check_maintenance: ไม่พบผู้ใช้ในกลุ่ม Fleet Manager ที่จะแจ้งเตือน '
                    '(vehicle_id=%s)', vehicle_id,
                )

    @api.model
    def _cron_purge_old_trips(self):
        """Data Retention — ลบ Trip Log (พร้อม Event ที่ผูกอยู่ผ่าน cascade)
        ที่เก่ากว่า 3 ปี (ปรับได้ผ่าน ir.config_parameter) ทุกเดือน ไม่แตะ
        Incentive เด็ดขาดเพราะต้องเก็บไว้ตลอดชีพ

        หมายเหตุ: "raw telemetry" (ข้อมูลดิบจาก MQTT ทุก 5 วิ) เก็บอยู่ใน
        TimescaleDB ฝั่ง Backend เท่านั้น ไม่เคยถูกเก็บใน Odoo เลย การลบ
        ข้อมูลส่วนนั้นจึงเป็นหน้าที่ของทีม Backend ไม่ใช่โมดูลนี้"""
        ICP = self.env['ir.config_parameter'].sudo()
        retention_years = int(ICP.get_param('fleet_telematics.trip_retention_years', 3))

        from dateutil.relativedelta import relativedelta
        cutoff = fields.Datetime.now() - relativedelta(years=retention_years)

        old_trips = self.sudo().search([('trip_start', '<', cutoff)])
        count = len(old_trips)

        if count:
            _logger.info(
                '_cron_purge_old_trips: กำลังลบ Trip Log ที่เก่ากว่า %s ปี '
                '(ก่อน %s) จำนวน %s รายการ (Event ที่ผูกอยู่จะถูกลบตามด้วย '
                'ผ่าน cascade)', retention_years, cutoff, count,
            )
            old_trips.with_context(fleet_telematics_allow_purge=True).unlink()
        else:
            _logger.info(
                '_cron_purge_old_trips: ไม่พบ Trip Log ที่เก่ากว่า %s ปี — '
                'ไม่มีอะไรต้องลบ', retention_years,
            )
