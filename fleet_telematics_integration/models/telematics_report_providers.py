# ==============================================================================
# models/telematics_report_providers.py
#
# ให้ QWeb PDF Report 2 ตัว (Energy Report, Monthly Score Report) ดึงข้อมูล
# สรุปสดจาก Backend API มาแปะเพิ่มในหน้ารายงาน แทนที่จะคำนวณจากข้อมูลใน
# Odoo เองอย่างเดียว — ใช้กลไกมาตรฐานของ Odoo (AbstractModel ชื่อ
# report.<module>.<report_template_id> + _get_report_values) ไม่ต้องแก้
# report action หรือโครงสร้าง template เดิม
#
# หมายเหตุ: endpoint สรุปฝั่ง Backend ไม่รองรับ filter ตามรถ/คนขับ/ช่วงเวลา
# ที่กำลังพิมพ์รายงาน จึงดึงแบบสรุปรวมทั้งฟลีท/ทุกคนมาแปะเป็นกล่อง "ข้อมูล
# อ้างอิงจาก Backend (สด)" แยกต่างหากจากตารางข้อมูลรายตัวเดิม
# ==============================================================================
import logging

import requests

from odoo import models

_logger = logging.getLogger(__name__)


def _fetch_backend_summary(env, path):
    """Helper ใช้ร่วมกัน — GET ไป Backend พร้อม APIKEY header คืน (data, error)"""
    Config = env['fleet.telematics.config']
    api_url = Config.get_active_api_url()
    if not api_url:
        return None, 'ยังไม่ได้ตั้งค่า Backend API URL'
    headers = {'APIKEY': Config.get_active_api_key()}
    try:
        resp = requests.get(f'{api_url}{path}', headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json(), None
    except requests.RequestException as e:
        return None, str(e)


class ReportEnergyDocument(models.AbstractModel):
    """Provider ของ Energy Report (reports/energy_report.xml)
    เพิ่ม backend_fuel_summary / backend_fuel_error เข้าไปใน values
    ดึงจาก GET /api/v1/reports/fuel-efficiency (FDD §11.3)"""
    _name = 'report.fleet_telematics_integration.report_energy_document'
    _description = 'Energy Report — Backend Summary Provider'

    def _get_report_values(self, docids, data=None):
        docs = self.env['fleet.telematics.log'].browse(docids)
        summary, err = _fetch_backend_summary(
            self.env, '/api/v1/reports/fuel-efficiency')
        return {
            'doc_ids':  docids,
            'doc_model': 'fleet.telematics.log',
            'docs':     docs,
            'backend_fuel_summary': summary,
            'backend_fuel_error':   err,
        }


class ReportDriverScoreDocument(models.AbstractModel):
    """Provider ของ Monthly Score Report (reports/driver_score_report.xml)
    เพิ่ม backend_driver_score / backend_score_error เข้าไปใน values
    ดึงจาก GET /api/v1/reports/driver-score (FDD §11.3)"""
    _name = 'report.fleet_telematics_integration.report_driver_score'
    _description = 'Monthly Score Report — Backend Summary Provider'

    def _get_report_values(self, docids, data=None):
        docs = self.env['fleet.telematics.incentive'].browse(docids)
        summary, err = _fetch_backend_summary(
            self.env, '/api/v1/reports/driver-score')
        return {
            'doc_ids':  docids,
            'doc_model': 'fleet.telematics.incentive',
            'docs':     docs,
            'backend_driver_score': summary,
            'backend_score_error':  err,
        }
