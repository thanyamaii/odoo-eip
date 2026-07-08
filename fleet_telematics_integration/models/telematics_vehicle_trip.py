# ==============================================================================
# models/telematics_vehicle_trip.py
#
# Wizard: Vehicle Trip History — ดึงประวัติการเดินทางของรถรายคัน
# เรียก GET /api/v1/vehicles/{vehicle_id}/trips จาก Backend โดยตรง
#
# ต่างจาก Trip Logs (fleet.telematics.log) ที่มีอยู่แล้วตรงที่:
# - ดึงข้อมูลสดจาก Backend ทุก trip รวมถึงที่ยังไม่ได้ sync เข้า Odoo
# - กรองตาม date_from/date_to และ synced_only ได้
# - แสดงพฤติกรรมการขับขี่รายทริป (driver_score, harsh events, speeding)
# ==============================================================================
import json
import logging
import requests

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class TelematicsVehicleTripHistory(models.TransientModel):
    _name        = 'fleet.telematics.vehicle.trip.history'
    _description = 'Vehicle Trip History (ดึงจาก Backend โดยตรง)'

    # ── Filter fields ──────────────────────────────────────────────────────────
    vehicle_id = fields.Integer(
        string='Vehicle ID', required=True,
        help='พิมพ์รหัสรถตรงๆ เช่น 1, 43')
    vehicle_name = fields.Char(
        string='ชื่อรถ', readonly=True,
        compute='_compute_vehicle_name', store=False)

    @api.depends('vehicle_id')
    def _compute_vehicle_name(self):
        for rec in self:
            if rec.vehicle_id:
                v = self.env['fleet.vehicle'].sudo().browse(rec.vehicle_id)
                rec.vehicle_name = v.display_name if v.exists() else f'Vehicle {rec.vehicle_id}'
            else:
                rec.vehicle_name = ''
    date_from = fields.Date(string='ตั้งแต่วันที่')
    date_to   = fields.Date(string='ถึงวันที่')
    synced_only = fields.Boolean(
        string='เฉพาะที่ sync เข้า Odoo แล้ว', default=False)
    page  = fields.Integer(string='หน้า', default=1)
    limit = fields.Integer(string='จำนวนต่อหน้า', default=20)

    # ── Result fields ──────────────────────────────────────────────────────────
    result_html  = fields.Html(string='ผลลัพธ์', readonly=True, sanitize=False)
    total_trips  = fields.Integer(string='Trip ทั้งหมด', readonly=True)
    total_pages  = fields.Integer(string='จำนวนหน้า', readonly=True)
    has_result   = fields.Boolean(default=False)

    def _api(self):
        Config  = self.env['fleet.telematics.config']
        api_url = Config.get_active_api_url()
        api_key = Config.get_active_api_key()
        if not api_url:
            raise UserError('กรุณาตั้งค่า API URL ของ Backend ใน Settings ก่อน')
        return api_url, api_key

    def action_fetch(self):
        self.ensure_one()
        if not self.vehicle_id:
            raise UserError('กรุณากรอก Vehicle ID ก่อน')

        # ถ้ายังไม่มี id ให้ save ก่อน
        if not self.id:
            self = self.create({
                'vehicle_id':  self.vehicle_id,
                'date_from':   self.date_from,
                'date_to':     self.date_to,
                'synced_only': self.synced_only,
                'page':        self.page or 1,
                'limit':       self.limit or 20,
            })

        api_url, api_key = self._api()

        params = {
            'page':  self.page,
            'limit': min(self.limit, 200),
        }
        if self.date_from:
            params['date_from'] = self.date_from.strftime('%Y-%m-%dT00:00:00')
        if self.date_to:
            params['date_to'] = self.date_to.strftime('%Y-%m-%dT23:59:59')
        if self.synced_only:
            params['synced_only'] = 'true'

        try:
            resp = requests.get(
                f'{api_url}/api/v1/vehicles/{self.vehicle_id}/trips',
                headers={'APIKEY': api_key},
                params=params,
                timeout=15,
            )
        except requests.RequestException as e:
            raise UserError(f'เชื่อมต่อ Backend ไม่สำเร็จ: {e}')

        if resp.status_code != 200:
            raise UserError(f'Backend ตอบ (HTTP {resp.status_code}): {resp.text[:300]}')

        data        = resp.json()
        trips       = data.get('trips', []) if isinstance(data, dict) else data
        total       = data.get('total', len(trips)) if isinstance(data, dict) else len(trips)
        total_pages = data.get('total_pages', 1) if isinstance(data, dict) else 1

        self.write({
            'total_trips':  total,
            'total_pages':  total_pages,
            'has_result':   True,
            'result_html':  self._render_trips(trips, total, total_pages),
        })

        # reload หน้าเดิม (current) ไม่เปิด popup ใหม่
        return {
            'type':      'ir.actions.act_window',
            'res_model': self._name,
            'res_id':    self.id,
            'view_mode': 'form',
            'target':    'current',
        }

        api_url, api_key = self._api()

        params = {
            'page':  self.page,
            'limit': min(self.limit, 200),
        }
        if self.date_from:
            params['date_from'] = self.date_from.strftime('%Y-%m-%dT00:00:00')
        if self.date_to:
            params['date_to'] = self.date_to.strftime('%Y-%m-%dT23:59:59')
        if self.synced_only:
            params['synced_only'] = 'true'

        try:
            resp = requests.get(
                f'{api_url}/api/v1/vehicles/{self.vehicle_id}/trips',
                headers={'APIKEY': api_key},
                params=params,
                timeout=15,
            )
        except requests.RequestException as e:
            raise UserError(f'เชื่อมต่อ Backend ไม่สำเร็จ: {e}')

        if resp.status_code != 200:
            raise UserError(f'Backend ตอบ (HTTP {resp.status_code}): {resp.text[:300]}')

        data        = resp.json()
        trips       = data.get('trips', []) if isinstance(data, dict) else data
        total       = data.get('total', len(trips)) if isinstance(data, dict) else len(trips)
        total_pages = data.get('total_pages', 1) if isinstance(data, dict) else 1

        self.write({
            'total_trips':  total,
            'total_pages':  total_pages,
            'has_result':   True,
            'result_html':  self._render_trips(trips, total, total_pages),
        })
        return {
            'type':      'ir.actions.act_window',
            'res_model': self._name,
            'res_id':    self.id,
            'view_mode': 'form',
            'target':    'new',
        }

    def action_prev_page(self):
        self.ensure_one()
        if self.page > 1:
            self.page -= 1
        return self.action_fetch()

    def action_next_page(self):
        self.ensure_one()
        if self.page < self.total_pages:
            self.page += 1
        return self.action_fetch()

    def action_export_excel(self):
        """Export trip data to Excel — ดาวน์โหลดรายงานพฤติกรรมการขับขี่"""
        self.ensure_one()
        if not self.vehicle_id:
            raise UserError('กรุณากรอก Vehicle ID ก่อน')

        api_url, api_key = self._api()

        # ดึงข้อมูลทั้งหมด (limit 200 สูงสุดต่อ request)
        params = {'page': 1, 'limit': 200}
        if self.date_from:
            params['date_from'] = self.date_from.strftime('%Y-%m-%dT00:00:00')
        if self.date_to:
            params['date_to'] = self.date_to.strftime('%Y-%m-%dT23:59:59')
        if self.synced_only:
            params['synced_only'] = 'true'

        try:
            resp = requests.get(
                f'{api_url}/api/v1/vehicles/{self.vehicle_id}/trips',
                headers={'APIKEY': api_key},
                params=params,
                timeout=15,
            )
        except requests.RequestException as e:
            raise UserError(f'เชื่อมต่อ Backend ไม่สำเร็จ: {e}')

        if resp.status_code != 200:
            raise UserError(f'Backend ตอบ (HTTP {resp.status_code}): {resp.text[:300]}')

        data  = resp.json()
        trips = data.get('trips', []) if isinstance(data, dict) else data

        if not trips:
            raise UserError('ไม่มีข้อมูล Trip ให้ Export')

        import io
        import base64

        # ใช้ xlsxwriter ซึ่งมาพร้อม Odoo ทุกเวอร์ชัน
        try:
            import xlsxwriter
            buf = io.BytesIO()
            wb  = xlsxwriter.Workbook(buf, {'in_memory': True})
            ws  = wb.add_worksheet('Vehicle Trip History')

            # Formats
            title_fmt = wb.add_format({'bold': True, 'font_size': 14,
                                        'font_color': '#1F2D3D', 'align': 'center',
                                        'font_name': 'Arial'})
            sub_fmt   = wb.add_format({'font_size': 11, 'font_color': '#595959',
                                        'align': 'center', 'font_name': 'Arial'})
            hdr_fmt   = wb.add_format({'bold': True, 'bg_color': '#1F2D3D',
                                        'font_color': '#FFFFFF', 'align': 'center',
                                        'border': 1, 'font_name': 'Arial'})
            cell_fmt  = wb.add_format({'align': 'center', 'border': 1,
                                        'font_name': 'Arial', 'font_size': 10})
            green_fmt = wb.add_format({'align': 'center', 'border': 1, 'bold': True,
                                        'font_color': '#15803D', 'font_name': 'Arial'})
            blue_fmt  = wb.add_format({'align': 'center', 'border': 1, 'bold': True,
                                        'font_color': '#1D4ED8', 'font_name': 'Arial'})
            amber_fmt = wb.add_format({'align': 'center', 'border': 1, 'bold': True,
                                        'font_color': '#D97706', 'font_name': 'Arial'})
            red_fmt   = wb.add_format({'align': 'center', 'border': 1, 'bold': True,
                                        'font_color': '#B91C1C', 'font_name': 'Arial'})
            sum_fmt   = wb.add_format({'bold': True, 'bg_color': '#E6F1FB',
                                        'border': 1, 'align': 'center',
                                        'num_format': '#,##0.00', 'font_name': 'Arial'})

            # Title
            ws.merge_range('A1:J1',
                f'รายงานประวัติการเดินทาง — {self.vehicle_name}', title_fmt)
            period = ''
            if self.date_from:
                period += f'ตั้งแต่ {self.date_from} '
            if self.date_to:
                period += f'ถึง {self.date_to}'
            ws.merge_range('A2:J2', period or 'ทุกช่วงเวลา', sub_fmt)

            # Header
            headers = ['Trip ID', 'วันเริ่มต้น', 'วันสิ้นสุด', 'Driver ID',
                       'ระยะทาง (km)', 'คะแนน', 'เบรคกระชาก', 'เร่งกะทันหัน',
                       'ขับเร็วเกิน', 'Sync Odoo']
            col_widths = [10, 20, 20, 12, 15, 10, 14, 16, 14, 12]
            for i, (h, w) in enumerate(zip(headers, col_widths)):
                ws.write(3, i, h, hdr_fmt)
                ws.set_column(i, i, w)

            # Data rows
            for r, t in enumerate(trips, 4):
                score  = t.get('driver_score')
                synced = 'ใช่' if t.get('synced_to_odoo') else 'ยังไม่ sync'
                row    = [
                    t.get('id', ''),
                    (t.get('trip_start') or '')[:16].replace('T', ' '),
                    (t.get('trip_end')   or '')[:16].replace('T', ' '),
                    t.get('driver_id', ''),
                    round(t.get('distance_km', 0), 2),
                    round(score, 2) if isinstance(score, (int, float)) else '',
                    t.get('harsh_brake_count', 0),
                    t.get('harsh_accel_count', 0),
                    t.get('speeding_count', 0),
                    synced,
                ]
                for c, val in enumerate(row):
                    if c == 5 and isinstance(val, (int, float)):
                        fmt = (green_fmt if val >= 90 else blue_fmt if val >= 75
                               else amber_fmt if val >= 60 else red_fmt)
                        ws.write(r, c, val, fmt)
                    else:
                        ws.write(r, c, val, cell_fmt)

            # Summary
            last = len(trips) + 4
            ws.write(last, 0, 'รวม / เฉลี่ย', sum_fmt)
            ws.write_formula(last, 4, f'=SUM(E5:E{last})', sum_fmt)
            ws.write_formula(last, 5, f'=AVERAGE(F5:F{last})', sum_fmt)
            ws.write_formula(last, 6, f'=SUM(G5:G{last})', sum_fmt)
            ws.write_formula(last, 7, f'=SUM(H5:H{last})', sum_fmt)
            ws.write_formula(last, 8, f'=SUM(I5:I{last})', sum_fmt)

            wb.close()
            buf.seek(0)
            xlsx_data = base64.b64encode(buf.read()).decode()
            ext = 'xlsx'
            mimetype = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

        except ImportError:
            # Fallback: CSV ถ้าไม่มี xlsxwriter
            import csv
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(['Trip ID', 'วันเริ่มต้น', 'วันสิ้นสุด', 'Driver ID',
                             'ระยะทาง (km)', 'คะแนน', 'เบรคกระชาก',
                             'เร่งกะทันหัน', 'ขับเร็วเกิน', 'Sync'])
            for t in trips:
                writer.writerow([
                    t.get('id', ''),
                    (t.get('trip_start') or '')[:16].replace('T', ' '),
                    (t.get('trip_end')   or '')[:16].replace('T', ' '),
                    t.get('driver_id', ''),
                    round(t.get('distance_km', 0), 2),
                    t.get('driver_score', ''),
                    t.get('harsh_brake_count', 0),
                    t.get('harsh_accel_count', 0),
                    t.get('speeding_count', 0),
                    'ใช่' if t.get('synced_to_odoo') else 'ยังไม่ sync',
                ])
            xlsx_data = base64.b64encode(buf.getvalue().encode('utf-8-sig')).decode()
            ext = 'csv'
            mimetype = 'text/csv'

        # สร้าง ir.attachment แล้ว return download
        filename = (
            f"vehicle_trip_{self.vehicle_id}"
            f"{'_' + str(self.date_from) if self.date_from else ''}"
            f"{'_' + str(self.date_to) if self.date_to else ''}.{ext}"
        )
        attach = self.env['ir.attachment'].create({
            'name':     filename,
            'type':     'binary',
            'datas':    xlsx_data,
            'mimetype': mimetype,
        })
        return {
            'type':   'ir.actions.act_url',
            'url':    f'/web/content/{attach.id}?download=true',
            'target': 'new',
        }

    def _render_trips(self, trips, total, total_pages):
        if not trips:
            return '<p class="text-muted text-center py-4">ไม่พบข้อมูล Trip ในช่วงวันที่ที่เลือก</p>'

        rows = ''
        for t in trips:
            score = t.get('driver_score', '-')
            score_color = (
                '#15803d' if isinstance(score, (int, float)) and score >= 90 else
                '#1d4ed8' if isinstance(score, (int, float)) and score >= 75 else
                '#d97706' if isinstance(score, (int, float)) and score >= 60 else
                '#b91c1c'
            )
            synced = '✅' if t.get('synced_to_odoo') else '⏳'
            rows += f'''<tr>
                <td>{t.get("id", "-")}</td>
                <td>{t.get("trip_start", "-")[:16].replace("T", " ") if t.get("trip_start") else "-"}</td>
                <td>{t.get("trip_end", "-")[:16].replace("T", " ") if t.get("trip_end") else "-"}</td>
                <td>{t.get("driver_id", "-")}</td>
                <td>{round(t.get("distance_km", 0), 2)} km</td>
                <td style="color:{score_color};font-weight:bold">{score}</td>
                <td style="color:#ef4444">{t.get("harsh_brake_count", 0)}</td>
                <td style="color:#f97316">{t.get("harsh_accel_count", 0)}</td>
                <td style="color:#8b5cf6">{t.get("speeding_count", 0)}</td>
                <td>{synced}</td>
            </tr>'''

        return f'''
        <div>
            <p class="text-muted small mb-2">
                พบ <b>{total}</b> ทริป | หน้า {self.page}/{total_pages}
                | รถ: <b>{self.vehicle_name}</b>
            </p>
            <table class="table table-bordered table-sm table-hover" style="font-size:13px">
                <thead class="table-dark">
                    <tr>
                        <th>Trip ID</th>
                        <th>เริ่ม</th>
                        <th>สิ้นสุด</th>
                        <th>Driver ID</th>
                        <th>ระยะทาง</th>
                        <th>คะแนน</th>
                        <th>เบรคกระชาก</th>
                        <th>เร่งกะทันหัน</th>
                        <th>ขับเร็วเกิน</th>
                        <th>Sync</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
        </div>'''
