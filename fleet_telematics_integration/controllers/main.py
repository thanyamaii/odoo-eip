# ==============================================================================
# controllers/main.py
# ==============================================================================
#
# หมายเหตุ (แก้ไข 2026-06-30):
# เดิมไฟล์นี้มี route POST /api/v1/vehicles ไว้รับ trip+event จาก Backend
# แบบ webhook-push แต่ตรวจสอบกับ Swagger ของ Backend จริง (ยืนยัน 2 รอบ)
# แล้วไม่มี endpoint ฝั่ง Backend ที่ยิง POST เข้ามาที่ Odoo เลย —
# Backend ใช้สถาปัตยกรรมแบบ Cron ดึง (GET /trips/unsynced) เป็นทางการเท่านั้น
# (ดู models/telematics_log.py: _cron_sync_trips)
#
# จึงตัดส่วน POST ออกทั้งหมด เหลือไว้แค่ GET /api/v1/vehicles สำหรับ
# debug/เช็คสถานะรถจาก Odoo เท่านั้น เพื่อลดความเสี่ยงข้อมูล trip ซ้ำซ้อน
# ==============================================================================

import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

_WEBHOOK_SECRET_PARAM = 'fleet_telematics.webhook_secret'


def _verify_secret(req):
    """ตรวจสอบ APIKEY header"""
    ICP = request.env['ir.config_parameter'].sudo()
    expected = ICP.get_param(_WEBHOOK_SECRET_PARAM, '')

    if not expected:
        return True

    incoming = req.httprequest.headers.get('APIKEY', '')
    return incoming == expected


class TelematicsWebhookController(http.Controller):

    # ==========================================================================
    # GET /api/v1/devices — health check ของฝั่ง Odoo เอง
    # (คนละ endpoint กับ /config_device ของ Backend ที่ใช้ลงทะเบียน device)
    # ==========================================================================
    @http.route(
        '/api/v1/devices',
        type='http',
        auth='public',
        methods=['GET'],
        csrf=False,
    )
    def health_check(self, **kwargs):

        return request.make_json_response({
            'status': 'ok',
            'service': 'fleet-telematics-odoo',
            'version': '19.0.1.0.0',
        })

    # ==========================================================================
    # GET /fleet_telematics/live_proxy  (UC-06 — SSE Real-time ตาม FDD spec)
    #
    # เปิด EventSource ไปที่ GET /api/v1/fleet/live ของ Backend
    # แล้ว forward stream มาให้ browser ทุก 5 วินาที
    #
    # เหตุผลที่ต้องผ่าน proxy นี้:
    #   1) native EventSource ของ browser ใส่ custom header (APIKEY) ไม่ได้
    #   2) ไม่ต้องการ expose API Key ไว้ใน JS ฝั่ง client
    #
    # เพิ่ม: enrich vehicle_name และ driver_name จาก Odoo database
    # เพราะ Backend SSE ส่งมาแค่ vehicle_id/device_id ไม่มีชื่อ
    #
    # อ้างอิง nginx config: docs/nginx_fleet_telematics_sse.conf
    # ==========================================================================
    @http.route(
        '/fleet_telematics/live_proxy',
        type='http',
        auth='user',
        methods=['GET'],
        csrf=False,
    )
    def fleet_live_proxy(self, **kwargs):
        import json as _json
        from odoo.http import Response

        Config  = request.env['fleet.telematics.config'].sudo()
        api_url = Config.get_active_api_url()
        api_key = Config.get_active_api_key()

        if not api_url:
            return Response(
                'data: {"error": "API URL ยังไม่ได้ตั้งค่าใน Settings"}\n\n',
                mimetype='text/event-stream',
            )

        # lookup table: vehicle_id → {name, driver_name}
        # ดึง ALL vehicles ไม่กรองแค่ที่มี device
        # เพราะ SSE อาจส่ง vehicle_id ที่ยังไม่มี device ใน Odoo มาด้วย
        vehicles = request.env['fleet.vehicle'].sudo().search([])
        vehicle_info = {
            v.id: {
                'vehicle_name': v.display_name or v.name,
                'driver_name':  v.driver_id.name if v.driver_id else '-',
            }
            for v in vehicles
        }

        def generate():
            import requests as _req
            try:
                with _req.get(
                    f'{api_url}/api/v1/fleet/live',
                    headers={
                        'APIKEY':  api_key,
                        'Accept':  'text/event-stream',
                    },
                    stream=True,
                    timeout=120,
                ) as r:
                    for line in r.iter_lines(decode_unicode=True):
                        if not line:
                            yield b'\n'
                            continue

                        # เพิ่ม vehicle_name / driver_name ก่อนส่งต่อ browser
                        if line.startswith('data:'):
                            raw = line[5:].strip()
                            try:
                                arr = _json.loads(raw)
                                if isinstance(arr, list):
                                    for item in arr:
                                        vid  = item.get('vehicle_id')
                                        info = vehicle_info.get(vid, {})
                                        item['vehicle_name'] = info.get('vehicle_name', f'Vehicle {vid}')
                                        item['driver_name']  = info.get('driver_name', '-')
                                    line = 'data: ' + _json.dumps(arr, ensure_ascii=False)
                            except Exception:
                                pass  # ถ้า parse ไม่ได้ส่ง raw ไปเลย

                        yield (line + '\n').encode('utf-8')

            except _req.RequestException as e:
                _logger.warning('fleet_live_proxy: %s', e)
                yield (
                    'data: {"error": "%s"}\n\n' % str(e).replace('"', "'")
                ).encode('utf-8')

        return Response(
            generate(),
            mimetype='text/event-stream',
            direct_passthrough=True,
            headers=[
                ('Cache-Control', 'no-cache'),
                ('X-Accel-Buffering', 'no'),
                ('Connection', 'keep-alive'),
            ],
        )
    # ==========================================================================
    # POST /fleet_telematics/vehicles_location  (เพิ่มใหม่ 2026-07-01 — UC-06)
    # OWL Widget เรียก RPC มาที่นี่ทุก 30 วินาที (Polling ตาม FDD §7.3)
    #
    # [แก้ไข 2026-07-06] FDD Table "Vehicles & Live" ระบุ endpoint
    # GET /api/v1/vehicles ("ดึงรายการยานพาหนะทั้งหมด") ไว้เป็นทางการ แต่โค้ด
    # เดิมไม่เคยเรียกเลย — วนยิง GET /vehicles/{id}/location ทีละคันแทน
    # (ทำงานได้ แต่ยิง N ครั้งต่อรอบ polling แทนที่จะยิงครั้งเดียว)
    #
    # แก้ให้เรียก GET /api/v1/vehicles (bulk) เป็นตัวหลักตาม FDD ก่อน:
    #   - ลองอ่านพิกัด/สถานะจากผลลัพธ์ bulk โดยรองรับหลายชื่อ key เท่าที่
    #     เป็นไปได้ (lat/latitude, lon/longitude, ...) เพราะ Swagger ที่มี
    #     ไม่ได้ระบุ schema ของ response แบบละเอียด (เห็นแค่ตัวอย่าง "string")
    #   - ถ้า bulk call ล้มเหลว (network error / ไม่ใช่ 200 / parse ไม่ได้)
    #     หรือได้ response แต่ไม่พบพิกัดของรถคันไหนเลยทั้งที่มีรถที่ต้องดึง
    #     จะ fallback ไปใช้วิธีเดิม (วน GET /vehicles/{id}/location ทีละคัน)
    #     เพื่อไม่ให้ Live Map พังถ้า schema จริงไม่ตรงกับที่สมมติไว้
    #
    # คืน array ของรถทุกคันที่มี Device + มีพิกัด GPS (รูปแบบผลลัพธ์เดิม
    # ไม่เปลี่ยน — widget ฝั่ง frontend ไม่ต้องแก้)
    # ==========================================================================
    @http.route(
        '/fleet_telematics/vehicles_location',
        type='json',
        auth='user',
        methods=['POST'],
        csrf=False,
    )
    def vehicles_location(self, **kwargs):
        import requests as _requests

        Config  = request.env['fleet.telematics.config'].sudo()
        api_url = Config.get_active_api_url()
        api_key = Config.get_active_api_key()

        if not api_url:
            return []

        # ดึงเฉพาะรถที่มี Device ผูกอยู่แล้ว (Register Device แล้ว)
        vehicles = request.env['fleet.vehicle'].sudo().search([
            ('telematics_device_id', '!=', False),
        ])
        vehicles_by_id = {v.id: v for v in vehicles}

        def _build_entry(v, lat, lon, speed, ignition, ts):
            return {
                'vehicle_id':   v.id,
                'vehicle_name': v.display_name or v.name,
                'device_id':    v.telematics_device_id,
                'driver_name':  v.driver_id.name if v.driver_id else '-',
                'lat':          float(lat),
                'lon':          float(lon),
                'speed':        speed or 0,
                'ignition':     bool(ignition),
                'ts':           ts or '',
            }

        # ── 1) ลองทางหลักตาม FDD: GET /api/v1/vehicles (bulk, ครั้งเดียว) ──
        try:
            resp = _requests.get(
                f'{api_url}/api/v1/vehicles',
                headers={'APIKEY': api_key},
                timeout=8,
            )
            if resp.status_code == 200:
                payload = resp.json()
                if isinstance(payload, dict):
                    items = (
                        payload.get('vehicles')
                        or payload.get('data')
                        or payload.get('items')
                        or []
                    )
                elif isinstance(payload, list):
                    items = payload
                else:
                    items = []

                bulk_result = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    vid = (
                        item.get('vehicle_id')
                        or item.get('id')
                        or item.get('odoo_vehicle_id')
                    )
                    try:
                        vid = int(vid)
                    except (TypeError, ValueError):
                        continue
                    v = vehicles_by_id.get(vid)
                    if not v:
                        continue  # รถคันนี้ไม่มี device ผูกใน Odoo (หรือไม่รู้จัก) ข้าม

                    # telemetry อาจซ้อนอยู่ใต้ key เช่น 'location' / 'telemetry'
                    tel = (
                        item.get('location')
                        or item.get('telemetry')
                        or item.get('last_telemetry')
                        or item
                    )
                    lat = tel.get('lat') or tel.get('latitude')
                    lon = tel.get('lon') or tel.get('longitude')
                    if not lat or not lon:
                        continue  # ยังไม่มีพิกัด ข้าม (เหมือนพฤติกรรมเดิม)

                    bulk_result.append(_build_entry(
                        v, lat, lon,
                        tel.get('speed'),
                        tel.get('ignition', False),
                        tel.get('ts') or tel.get('date_update_latest'),
                    ))

                if bulk_result or not vehicles:
                    # ได้ผลลัพธ์ใช้ได้จริง (หรือไม่มีรถให้ดึงตั้งแต่แรก) ใช้ทางนี้เลย
                    return bulk_result
                # bulk เรียกสำเร็จแต่ parse ไม่ได้พิกัดของรถคันไหนเลย ทั้งที่มีรถ
                # ต้องดึง → เดา schema ผิด, ตกไป fallback ด้านล่าง
                _logger.warning(
                    'vehicles_location: GET /api/v1/vehicles คืน 200 แต่ไม่พบพิกัด '
                    'ที่ parse ได้เลย (schema อาจไม่ตรงตามที่คาด) → fallback เป็น per-vehicle'
                )
            else:
                _logger.warning(
                    'vehicles_location: GET /api/v1/vehicles ตอบ HTTP %s → fallback เป็น per-vehicle',
                    resp.status_code,
                )
        except Exception as e:
            _logger.warning(
                'vehicles_location: GET /api/v1/vehicles ล้มเหลว (%s) → fallback เป็น per-vehicle', e)

        # ── 2) Fallback: วิธีเดิม — วน GET /vehicles/{id}/location ทีละคัน ──
        result = []
        for v in vehicles:
            try:
                resp = _requests.get(
                    f'{api_url}/api/v1/vehicles/{v.id}/location',
                    headers={'APIKEY': api_key},
                    timeout=5,
                )
                if resp.status_code != 200:
                    continue  # Backend ไม่รู้จักรถคันนี้ยัง ข้ามไป

                data = resp.json()
                lat  = data.get('lat') or data.get('latitude')
                lon  = data.get('lon') or data.get('longitude')

                if not lat or not lon:
                    continue  # ยังไม่มีพิกัด ข้ามไป

                result.append(_build_entry(
                    v, lat, lon,
                    data.get('speed'),
                    data.get('ignition', False),
                    data.get('ts'),
                ))
            except Exception as e:
                _logger.warning(
                    'vehicles_location: รถ %s (id=%s) ดึงไม่ได้: %s',
                    v.name, v.id, e)

        return result

    @http.route(
        '/api/v1/vehicles',
        type='json',
        auth='public',
        methods=['GET'],
        csrf=False,
    )
    def vehicles(self, **kwargs):

        if not _verify_secret(request):
            return {'status': 'error', 'message': 'Unauthorized - invalid APIKEY'}

        vehicles = request.env['fleet.vehicle'].sudo().search([])
        return {
            'status': 'ok',
            'count': len(vehicles),
            'vehicles': [
                {
                    'id':           v.id,
                    'name':         v.name,
                    'license_plate': v.license_plate,
                    'device_id':    v.telematics_device_id or None,
                    'vehicle_id':   v.id,
                    'active':       v.active,
                    'available':    not bool(v.driver_id),
                    'date_update_latest': (
                        v.last_seen.strftime('%Y-%m-%dT%H:%M:%SZ')
                        if hasattr(v, 'last_seen') and v.last_seen else None
                    ),
                }
                for v in vehicles
            ],
        }
