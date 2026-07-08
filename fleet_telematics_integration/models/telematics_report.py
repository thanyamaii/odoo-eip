# ==============================================================================
# models/telematics_report.py  (ไฟล์ใหม่)
#
# UC-07/08 — เดิม Energy Report และ Driver Scorecard คำนวณ aggregate เอง
# จาก trip log ทั้งหมดในฝั่ง Odoo ทำให้มีความเสี่ยงตัวเลขไม่ตรงกับ Backend
#
# ตามคำตอบยืนยันจาก Backend (2026-06-30): endpoint สำเร็จรูปต่อไปนี้
# มีไว้ให้ Odoo ดึงไปแสดงตรงๆ ได้เลย ไม่ต้องคำนวณซ้ำ:
#   - GET /api/v1/reports/fuel-efficiency
#   - GET /api/v1/drivers/{id}/score
#   - GET /api/v1/drivers/{id}/fuel-summary
#
# Implementation: Wizard (TransientModel) ที่กดปุ่มแล้วยิง GET ตรงๆ
# แล้วแสดงผลลัพธ์ดิบในรูปตาราง/ข้อความที่อ่านง่าย — ไม่เก็บ state ถาวร
# เพราะข้อมูลควรอ้างอิงจาก Backend สดทุกครั้งที่เปิดดู
# ==============================================================================

import json
import logging

import requests

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class TelematicsFuelEfficiencyReport(models.TransientModel):
    _name = 'fleet.telematics.fuel.report.wizard'
    _description = 'Fuel Efficiency Report (จาก Backend โดยตรง)'

    driver_id = fields.Many2one('hr.employee', string='Driver (เว้นว่าง = ทั้งหมด)')
    result_html = fields.Html(string='ผลลัพธ์', readonly=True, sanitize=False)

    def _api(self):
        Config = self.env['fleet.telematics.config']
        api_url = Config.get_active_api_url()
        api_key = Config.get_active_api_key()
        if not api_url:
            raise UserError('กรุณาตั้งค่า API URL ของ Backend ใน Settings ก่อน')
        return api_url, api_key

    def action_fetch_fuel_efficiency(self):
        self.ensure_one()
        api_url, api_key = self._api()
        try:
            resp = requests.get(
                f'{api_url}/api/v1/reports/fuel-efficiency',
                headers={'APIKEY': api_key},
                timeout=15,
            )
        except requests.RequestException as e:
            raise UserError(f'เชื่อมต่อ Backend ไม่สำเร็จ: {e}')

        if resp.status_code != 200:
            raise UserError(f'Backend ตอบกลับผิดพลาด (HTTP {resp.status_code}): {resp.text[:300]}')

        self.result_html = self._render_json_table(resp.json(), 'Fuel Efficiency Report')
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_fetch_driver_score(self):
        self.ensure_one()
        if not self.driver_id:
            raise UserError('กรุณาเลือก Driver ก่อนดึง Driver Score')
        api_url, api_key = self._api()
        try:
            resp = requests.get(
                f'{api_url}/api/v1/drivers/{self.driver_id.id}/score',
                headers={'APIKEY': api_key},
                timeout=15,
            )
        except requests.RequestException as e:
            raise UserError(f'เชื่อมต่อ Backend ไม่สำเร็จ: {e}')

        if resp.status_code != 200:
            raise UserError(f'Backend ตอบกลับผิดพลาด (HTTP {resp.status_code}): {resp.text[:300]}')

        self.result_html = self._render_json_table(
            resp.json(), f'Driver Score — {self.driver_id.name}')
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_fetch_fuel_summary(self):
        self.ensure_one()
        if not self.driver_id:
            raise UserError('กรุณาเลือก Driver ก่อนดึง Fuel Summary')
        api_url, api_key = self._api()
        try:
            resp = requests.get(
                f'{api_url}/api/v1/drivers/{self.driver_id.id}/fuel-summary',
                headers={'APIKEY': api_key},
                timeout=15,
            )
        except requests.RequestException as e:
            raise UserError(f'เชื่อมต่อ Backend ไม่สำเร็จ: {e}')

        if resp.status_code != 200:
            raise UserError(f'Backend ตอบกลับผิดพลาด (HTTP {resp.status_code}): {resp.text[:300]}')

        self.result_html = self._render_json_table(
            resp.json(), f'Fuel Summary — {self.driver_id.name}')
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    @staticmethod
    def _render_json_table(data, title):
        """แปลง JSON response เป็นตาราง HTML อ่านง่าย (รองรับ dict/list ระดับเดียว)"""
        rows = ''
        if isinstance(data, dict):
            items = data.items()
        elif isinstance(data, list):
            items = enumerate(data)
        else:
            items = [('value', data)]

        for k, v in items:
            if isinstance(v, (dict, list)):
                v_display = f'<pre>{json.dumps(v, ensure_ascii=False, indent=2)}</pre>'
            else:
                v_display = str(v)
            rows += f'<tr><td><b>{k}</b></td><td>{v_display}</td></tr>'

        return (
            f'<h4>{title}</h4>'
            f'<table class="table table-bordered table-sm">{rows}</table>'
        )


# ==============================================================================
# เพิ่ม 4 API endpoints ที่ยังไม่เชื่อม (2026-07-02)
# FDD §12.6 + API Spec:
#   - GET /api/v1/drivers/{id}/events      — ประวัติ harsh events รายคน
#   - GET /api/v1/reports/driver-score     — คะแนนรวมทุกคน
#   - GET /api/v1/reports/fleet-summary    — ภาพรวม fleet รายวัน
#   - GET /api/v1/reports/maintenance-forecast — พยากรณ์ซ่อมบำรุง
# ==============================================================================

class TelematicsBackendReports(models.TransientModel):
    _name = 'fleet.telematics.backend.report'
    _description = 'Fleet Telematics Backend Reports (ดึงตรงจาก Backend)'

    driver_id    = fields.Many2one('hr.employee', string='Driver (เว้นว่าง = ทั้งหมด)')
    result_html  = fields.Html(string='ผลลัพธ์', readonly=True, sanitize=False)

    def _api(self):
        Config  = self.env['fleet.telematics.config']
        api_url = Config.get_active_api_url()
        api_key = Config.get_active_api_key()
        if not api_url:
            raise UserError('กรุณาตั้งค่า API URL ของ Backend ใน Settings ก่อน')
        return api_url, api_key

    # ── 1) GET /drivers/{id}/events ────────────────────────────────────────────
    def action_fetch_driver_events(self):
        self.ensure_one()
        if not self.driver_id:
            raise UserError('กรุณาเลือก Driver ก่อนดึงประวัติ Harsh Events')
        api_url, api_key = self._api()
        try:
            resp = requests.get(
                f'{api_url}/api/v1/drivers/{self.driver_id.id}/events',
                headers={'APIKEY': api_key},
                timeout=15,
            )
        except requests.RequestException as e:
            raise UserError(f'เชื่อมต่อ Backend ไม่สำเร็จ: {e}')
        if resp.status_code != 200:
            raise UserError(f'Backend ตอบ (HTTP {resp.status_code}): {resp.text[:300]}')
        self.result_html = self._render_table(
            resp.json(), f'Harsh Events — {self.driver_id.name}')
        return self._reopen()

    # ── 2) GET /reports/driver-score (ทุกคน) ──────────────────────────────────
    def action_fetch_all_driver_scores(self):
        self.ensure_one()
        api_url, api_key = self._api()
        try:
            resp = requests.get(
                f'{api_url}/api/v1/reports/driver-score',
                headers={'APIKEY': api_key},
                timeout=15,
            )
        except requests.RequestException as e:
            raise UserError(f'เชื่อมต่อ Backend ไม่สำเร็จ: {e}')
        if resp.status_code != 200:
            raise UserError(f'Backend ตอบ (HTTP {resp.status_code}): {resp.text[:300]}')
        self.result_html = self._render_table(
            resp.json(), 'Driver Score Report (ทุกคน)')
        return self._reopen()

    # ── 3) GET /reports/fleet-summary ──────────────────────────────────────────
    def action_fetch_fleet_summary(self):
        self.ensure_one()
        api_url, api_key = self._api()
        try:
            resp = requests.get(
                f'{api_url}/api/v1/reports/fleet-summary',
                headers={'APIKEY': api_key},
                timeout=15,
            )
        except requests.RequestException as e:
            raise UserError(f'เชื่อมต่อ Backend ไม่สำเร็จ: {e}')
        if resp.status_code != 200:
            raise UserError(f'Backend ตอบ (HTTP {resp.status_code}): {resp.text[:300]}')
        self.result_html = self._render_table(
            resp.json(), 'Fleet Summary (ภาพรวม Fleet)')
        return self._reopen()

    # ── 4) GET /reports/maintenance-forecast ───────────────────────────────────
    def action_fetch_maintenance_forecast(self):
        self.ensure_one()
        api_url, api_key = self._api()
        try:
            resp = requests.get(
                f'{api_url}/api/v1/reports/maintenance-forecast',
                headers={'APIKEY': api_key},
                timeout=15,
            )
        except requests.RequestException as e:
            raise UserError(f'เชื่อมต่อ Backend ไม่สำเร็จ: {e}')
        if resp.status_code != 200:
            raise UserError(f'Backend ตอบ (HTTP {resp.status_code}): {resp.text[:300]}')
        self.result_html = self._render_table(
            resp.json(), 'Maintenance Forecast (พยากรณ์ซ่อมบำรุง)')
        return self._reopen()

    # ── 5) GET /drivers/{id}/score (รายคน) — แยกจาก /reports/driver-score ──────
    def action_fetch_driver_score_single(self):
        """ดึงคะแนนของคนขับรายคน — GET /drivers/{id}/score
        ตรงกับ Swagger: Get Driver Score (ต้องเลือก Driver)"""
        self.ensure_one()
        if not self.driver_id:
            raise UserError('กรุณาเลือก Driver ก่อนดึง Driver Score รายคน')
        api_url, api_key = self._api()
        try:
            resp = requests.get(
                f'{api_url}/api/v1/drivers/{self.driver_id.id}/score',
                headers={'APIKEY': api_key},
                timeout=15,
            )
        except requests.RequestException as e:
            raise UserError(f'เชื่อมต่อ Backend ไม่สำเร็จ: {e}')
        if resp.status_code != 200:
            raise UserError(f'Backend ตอบ (HTTP {resp.status_code}): {resp.text[:300]}')
        self.result_html = self._render_table(
            resp.json(), f'Driver Score — {self.driver_id.name}')
        return self._reopen()

    # ── 6) GET /drivers/{id}/fuel-summary ──────────────────────────────────────
    def action_fetch_fuel_summary(self):
        self.ensure_one()
        if not self.driver_id:
            raise UserError('กรุณาเลือก Driver ก่อนดึง Fuel Summary')
        api_url, api_key = self._api()
        try:
            resp = requests.get(
                f'{api_url}/api/v1/drivers/{self.driver_id.id}/fuel-summary',
                headers={'APIKEY': api_key},
                timeout=15,
            )
        except requests.RequestException as e:
            raise UserError(f'เชื่อมต่อ Backend ไม่สำเร็จ: {e}')
        if resp.status_code != 200:
            raise UserError(f'Backend ตอบ (HTTP {resp.status_code}): {resp.text[:300]}')
        self.result_html = self._render_table(
            resp.json(), f'Fuel Summary — {self.driver_id.name}')
        return self._reopen()

    # ── 6) GET /reports/fuel-efficiency ────────────────────────────────────────
    def action_fetch_fuel_efficiency(self):
        self.ensure_one()
        api_url, api_key = self._api()
        try:
            resp = requests.get(
                f'{api_url}/api/v1/reports/fuel-efficiency',
                headers={'APIKEY': api_key},
                timeout=15,
            )
        except requests.RequestException as e:
            raise UserError(f'เชื่อมต่อ Backend ไม่สำเร็จ: {e}')
        if resp.status_code != 200:
            raise UserError(f'Backend ตอบ (HTTP {resp.status_code}): {resp.text[:300]}')
        self.result_html = self._render_table(
            resp.json(), 'Fuel Efficiency Report (ทั้งฟลีท)')
        return self._reopen()

    # ── helper ──────────────────────────────────────────────────────────────────
    def _reopen(self):
        return {
            'type':      'ir.actions.act_window',
            'res_model': self._name,
            'res_id':    self.id,
            'view_mode': 'form',
            'target':    'current',
        }

    @staticmethod
    def _render_table(data, title):
        rows = ''
        if isinstance(data, dict):
            items = data.items()
        elif isinstance(data, list):
            items = enumerate(data)
        else:
            items = [('value', data)]
        for k, v in items:
            if isinstance(v, (dict, list)):
                v_display = f'<pre>{json.dumps(v, ensure_ascii=False, indent=2)}</pre>'
            else:
                v_display = str(v)
            rows += f'<tr><td><b>{k}</b></td><td>{v_display}</td></tr>'
        return (
            f'<h5 class="mt-2">{title}</h5>'
            f'<table class="table table-bordered table-sm">{rows}</table>'
        )
