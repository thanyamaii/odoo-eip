# ==============================================================================
# models/telematics_report.py
#
# UC-07/08 — ดึงรายงานสรุปต่างๆ จาก Backend มาแสดงตรงๆ แทนที่จะคำนวณซ้ำ
# ในฝั่ง Odoo เอง (ป้องกันตัวเลขไม่ตรงกันระหว่าง 2 ระบบ)
#
# เป็น Wizard (TransientModel) กดปุ่มแล้วยิง GET ไป Backend ทันที แสดงผล
# เป็นตาราง HTML อ่านง่าย ไม่เก็บ state ถาวร เพราะอยากให้ข้อมูลอ้างอิงจาก
# Backend สดทุกครั้งที่เปิดดู ไม่ใช่ค่าที่ค้างจากการดึงครั้งก่อน
# ==============================================================================

import json
import logging

import requests

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class TelematicsFuelEfficiencyReport(models.TransientModel):
    """หน้าต่างดึงรายงาน Fuel Efficiency / Driver Score / Fuel Summary
    จาก Backend โดยตรง (ใช้ในเมนู Reports)"""
    _name = 'fleet.telematics.fuel.report.wizard'
    _description = 'Fuel Efficiency Report (จาก Backend โดยตรง)'

    driver_id = fields.Many2one('hr.employee', string='Driver (เว้นว่าง = ทั้งหมด)')
    result_html = fields.Html(string='ผลลัพธ์', readonly=True, sanitize=False)

    def _api(self):
        """ดึง API URL/Key ที่ใช้งานอยู่ตอนนี้ — error ทันทีถ้ายังไม่ตั้งค่า"""
        Config = self.env['fleet.telematics.config']
        api_url = Config.get_active_api_url()
        api_key = Config.get_active_api_key()
        if not api_url:
            raise UserError('กรุณาตั้งค่า API URL ของ Backend ใน Settings ก่อน')
        return api_url, api_key

    def action_fetch_fuel_efficiency(self):
        """ดึงรายงานประสิทธิภาพเชื้อเพลิงทั้งฟลีท (GET /reports/fuel-efficiency)"""
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
        """ดึงคะแนนของคนขับ 1 คนที่เลือกไว้ (GET /drivers/{id}/score)"""
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
        """ดึงสรุปการใช้เชื้อเพลิงของคนขับ 1 คน (GET /drivers/{id}/fuel-summary)"""
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
        """แปลง JSON response จาก Backend เป็นตาราง HTML อ่านง่าย
        (รองรับ dict/list ระดับเดียว ถ้าซ้อนลึกกว่านั้นแสดงเป็น JSON ดิบ)"""
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


class TelematicsBackendReports(models.TransientModel):
    """หน้าต่างรวมรายงานทั้งหมดจาก Backend ในที่เดียว — Fleet Summary,
    Driver Score (รายคน/ทุกคน), Fuel Efficiency, Maintenance Forecast,
    Harsh Events รายคน, Fuel Summary รายคน"""
    _name = 'fleet.telematics.backend.report'
    _description = 'Fleet Telematics Backend Reports (ดึงตรงจาก Backend)'

    driver_id    = fields.Many2one('hr.employee', string='Driver (เว้นว่าง = ทั้งหมด)')
    result_html  = fields.Html(string='ผลลัพธ์', readonly=True, sanitize=False)

    def _api(self):
        """ดึง API URL/Key ที่ใช้งานอยู่ตอนนี้ — error ทันทีถ้ายังไม่ตั้งค่า"""
        Config  = self.env['fleet.telematics.config']
        api_url = Config.get_active_api_url()
        api_key = Config.get_active_api_key()
        if not api_url:
            raise UserError('กรุณาตั้งค่า API URL ของ Backend ใน Settings ก่อน')
        return api_url, api_key

    def action_fetch_driver_events(self):
        """ประวัติ Harsh Events ของคนขับ 1 คน (GET /drivers/{id}/events)"""
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

    def action_fetch_all_driver_scores(self):
        """คะแนนของคนขับทุกคนในระบบรวมกัน (GET /reports/driver-score)"""
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

    def action_fetch_fleet_summary(self):
        """ภาพรวมทั้งฟลีทรายวัน (GET /reports/fleet-summary)"""
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

    def action_fetch_maintenance_forecast(self):
        """พยากรณ์ว่ารถคันไหนใกล้ถึงรอบซ่อมบำรุง (GET /reports/maintenance-forecast)"""
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

    def action_fetch_driver_score_single(self):
        """คะแนนของคนขับ 1 คนที่เลือก (GET /drivers/{id}/score) — แยกจาก
        action_fetch_all_driver_scores ที่ดึงทุกคนพร้อมกัน"""
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

    def action_fetch_fuel_summary(self):
        """สรุปการใช้เชื้อเพลิงของคนขับ 1 คน (GET /drivers/{id}/fuel-summary)"""
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

    def action_fetch_fuel_efficiency(self):
        """ประสิทธิภาพเชื้อเพลิงทั้งฟลีท (GET /reports/fuel-efficiency)"""
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

    def _reopen(self):
        """เปิดหน้าต่างเดิมค้างไว้ให้เห็นผลลัพธ์ที่เพิ่งดึงมา"""
        return {
            'type':      'ir.actions.act_window',
            'res_model': self._name,
            'res_id':    self.id,
            'view_mode': 'form',
            'target':    'current',
        }

    @staticmethod
    def _render_table(data, title):
        """แปลง JSON response จาก Backend เป็นตาราง HTML อ่านง่าย"""
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
