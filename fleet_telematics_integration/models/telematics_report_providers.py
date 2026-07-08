# ==============================================================================
# models/telematics_report_providers.py  (เพิ่มใหม่ 2026-07-06)
#
# ให้ QWeb PDF Report 2 ตัวที่มีอยู่แล้วตาม FDD §12.6 ตาราง 43
# (Energy Report, Monthly Score Report) ดึงข้อมูลสรุปจาก Backend API มาแปะ
# เพิ่มในหน้ารายงาน แทนที่จะคำนวณจาก local data อย่างเดียว ตามที่ FDD
# ตั้งใจไว้ ("endpoint กลุ่มนี้มีไว้ให้ Odoo ดึงไปแสดงตรงๆ ได้เลย ไม่ต้อง
# คำนวณซ้ำ") — ใช้กลไกมาตรฐานของ Odoo (AbstractModel ชื่อ
# report.<module>.<report_template_id> + _get_report_values) ไม่ต้องแก้
# report action หรือ template structure เดิม
#
# ⚠️ หมายเหตุสมมติฐาน: endpoint /reports/fuel-efficiency และ
# /reports/driver-score ตาม FDD §11.3 ไม่ได้ระบุว่ารองรับ filter ตาม
# vehicle/driver/ช่วงเวลาที่กำลังพิมพ์รายงานหรือไม่ จึงเรียกแบบสรุปรวม
# ทั้งฟลีท/ทุกคนครั้งเดียว (ไม่ผูกกับ record ที่เลือกพิมพ์) แล้วแปะเป็น
# กล่อง "ข้อมูลอ้างอิงจาก Backend (สด)" แยกต่างหากจากตารางข้อมูลรายตัว
# เดิม — ถ้า Backend รองรับ filter ละเอียดกว่านี้ ควรแก้ให้ส่ง params ตาม
# doc ที่พิมพ์จริง
# ==============================================================================
import logging

import requests

from odoo import models

_logger = logging.getLogger(__name__)


def _fetch_backend_summary(env, path):
    """Helper ใช้ร่วมกัน — GET ไป Backend พร้อม JWT คืน (data, error)"""
    Config = env['fleet.telematics.config']
    api_url = Config.get_active_api_url()
    if not api_url:
        return None, 'ยังไม่ได้ตั้งค่า Backend API URL'
    try:
        headers = Config.get_auth_headers()
    except Exception as e:
        return None, f'Login ไม่สำเร็จ: {e}'
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
