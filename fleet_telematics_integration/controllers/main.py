# ==============================================================================
# controllers/main.py
#
# Endpoint ฝั่ง Odoo สำหรับ:
#   1) Health check (GET /api/v1/devices)
#   2) Fleet Live Map — SSE Proxy (GET /fleet_telematics/live_proxy)
#   3) Fleet Live Map — Polling ทุก 30 วิ (POST /fleet_telematics/vehicles_location)
#   4) รายการรถทั้งหมด สำหรับ Backend เรียกเช็ค (GET /api/v1/vehicles)
#
# สถาปัตยกรรมการ sync ทริปเป็นแบบ "Odoo ดึงจาก Backend" (Cron polling ผ่าน
# GET /trips/unsynced ใน models/telematics_log.py) เท่านั้น — ไม่มี endpoint
# แบบ webhook-push ที่ Backend ยิงเข้ามาหา Odoo โดยตรง
# ==============================================================================

import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

_WEBHOOK_SECRET_PARAM = 'fleet_telematics.webhook_secret'


def _verify_secret(req):
    """ตรวจสอบ APIKEY header เทียบกับค่าที่ตั้งไว้ ถ้ายังไม่ตั้งค่าไว้เลย
    (ค่าว่าง) จะปล่อยผ่านทั้งหมด — ไว้ปิดกั้นการเรียก endpoint สาธารณะ"""
    ICP = request.env['ir.config_parameter'].sudo()
    expected = ICP.get_param(_WEBHOOK_SECRET_PARAM, '')

    if not expected:
        return True

    incoming = req.httprequest.headers.get('APIKEY', '')
    return incoming == expected


class TelematicsWebhookController(http.Controller):

    @http.route(
        '/api/v1/devices',
        type='http',
        auth='public',
        methods=['GET'],
        csrf=False,
    )
    def health_check(self, **kwargs):
        """เช็คว่า Odoo instance นี้ตอบสนองอยู่ไหม (คนละ endpoint กับ
        /config_device ของ Backend ที่ใช้ลงทะเบียน device)"""
        return request.make_json_response({
            'status': 'ok',
            'service': 'fleet-telematics-odoo',
            'version': '19.0.1.0.0',
        })

    @http.route(
        '/fleet_telematics/live_proxy',
        type='http',
        auth='user',
        methods=['GET'],
        csrf=False,
    )
    def fleet_live_proxy(self, **kwargs):
        """เปิด stream (Server-Sent Events) ต่อไปยัง Backend (GET /fleet/live)
        แล้วส่งต่อให้ browser แบบเรียลไทม์ทุก 5 วินาที

        ต้องผ่าน proxy นี้แทนให้ browser เชื่อมตรง เพราะ:
          1) EventSource ของ browser ใส่ custom header (APIKEY) เองไม่ได้
          2) ไม่อยากเปิดเผย API Key ไว้ใน JavaScript ฝั่ง client

        ระหว่างทางแทรกชื่อรถ/ชื่อคนขับเข้าไปในแต่ละ event ด้วย เพราะ SSE
        จาก Backend ส่งมาแค่ vehicle_id/device_id ไม่มีชื่อให้แสดงเลย"""
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

        # ดึงรถทุกคัน (ไม่กรองเฉพาะที่มี device) เพราะ SSE อาจส่ง vehicle_id
        # ที่ยังไม่มี device ผูกใน Odoo มาด้วยได้
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
                                pass  # parse ไม่ได้ ก็ส่ง raw ต่อไปเลย

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

    @http.route(
        '/fleet_telematics/vehicles_location',
        type='json',
        auth='user',
        methods=['POST'],
        csrf=False,
    )
    def vehicles_location(self, **kwargs):
        """OWL Widget ของ Fleet Live Map เรียกมาที่นี่ทุก 30 วินาที (โหมด
        Polling ตอน SSE ใช้ไม่ได้) — คืนตำแหน่ง/ความเร็ว/สถานะ ignition
        ของรถทุกคันที่มี Device ผูกอยู่และมีพิกัดล่าสุด

        ลองทางหลักก่อน: ยิง GET /api/v1/vehicles ครั้งเดียวได้ข้อมูลรถ
        ทุกคัน (เร็วกว่า) — รองรับหลายชื่อ key เท่าที่เป็นไปได้เพราะ Swagger
        ไม่ได้ระบุ schema response แบบละเอียด ถ้าทางหลักล้มเหลวหรือ parse
        พิกัดไม่ได้เลยสักคัน จะ fallback ไปวน GET /vehicles/{id}/location
        ทีละคันแทน (ช้ากว่าแต่ชัวร์กว่า) กันไม่ให้ Live Map พังถ้า schema
        จริงไม่ตรงกับที่คาดไว้"""
        import requests as _requests

        Config  = request.env['fleet.telematics.config'].sudo()
        api_url = Config.get_active_api_url()
        api_key = Config.get_active_api_key()

        if not api_url:
            return []

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

        # ── ทางหลัก: ดึงรถทุกคันในคำขอเดียว ──────────────────────────────
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
                        continue  # รถคันนี้ไม่มี device ผูกใน Odoo ข้าม

                    tel = (
                        item.get('location')
                        or item.get('telemetry')
                        or item.get('last_telemetry')
                        or item
                    )
                    lat = tel.get('lat') or tel.get('latitude')
                    lon = tel.get('lon') or tel.get('longitude')
                    if not lat or not lon:
                        continue  # ยังไม่มีพิกัด ข้าม

                    bulk_result.append(_build_entry(
                        v, lat, lon,
                        tel.get('speed'),
                        tel.get('ignition', False),
                        tel.get('ts') or tel.get('date_update_latest'),
                    ))

                if bulk_result or not vehicles:
                    return bulk_result
                # ได้ 200 แต่ parse พิกัดไม่ได้เลยสักคัน (schema อาจไม่ตรง
                # กับที่คาดไว้) ตกไปใช้ fallback ด้านล่างแทน
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

        # ── Fallback: วนดึงทีละคัน (ช้ากว่าแต่ชัวร์กว่า) ────────────────────
        result = []
        for v in vehicles:
            try:
                resp = _requests.get(
                    f'{api_url}/api/v1/vehicles/{v.id}/location',
                    headers={'APIKEY': api_key},
                    timeout=5,
                )
                if resp.status_code != 200:
                    continue  # Backend ยังไม่รู้จักรถคันนี้ ข้าม

                data = resp.json()
                lat  = data.get('lat') or data.get('latitude')
                lon  = data.get('lon') or data.get('longitude')

                if not lat or not lon:
                    continue

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
        """คืนรายชื่อรถทั้งหมดในระบบ Odoo — ให้ Backend เรียกเช็คหรือ debug
        ใช้ (ต้องมี APIKEY header ที่ตรงกันถ้าตั้งค่า webhook_secret ไว้)"""
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
