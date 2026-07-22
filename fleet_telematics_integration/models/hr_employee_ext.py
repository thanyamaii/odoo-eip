# ==============================================================================
# models/hr_employee_ext.py
#
# ต่อยอดโมเดล hr.employee (พนักงาน) เพิ่มช่องเก็บฐานเงินเดือนสำรอง
# สำหรับใช้คำนวณโบนัสของระบบ Fleet Telematics โดยเฉพาะ
#
# ใช้เมื่อไหร่: กรณีระบบไม่มีข้อมูลสัญญาจ้าง (hr.version ใน Odoo 19 หรือ
# hr.contract ในเวอร์ชันเก่ากว่า) ให้ผูกกับพนักงานคนนั้น ระบบจะดึงเงินเดือน
# จากช่องนี้แทน โดยกรอกครั้งเดียวที่นี่ ใช้ได้ทุกเดือนโดยไม่ต้องพิมพ์ซ้ำใน
# ใบโบนัสแต่ละใบ (ดูลำดับการดึงค่าเต็มๆ ใน models/telematics_incentive.py
# ฟังก์ชัน _apply_backend_bonus)
# ==============================================================================
from odoo import models, fields


class HrEmployeeTelematicsExt(models.Model):
    """ต่อยอด hr.employee เพิ่ม field สำหรับระบบ Fleet Telematics"""
    _inherit = 'hr.employee'

    telematics_base_salary = fields.Float(
        string='Base Salary (สำหรับคำนวณโบนัส Telematics)',
        digits=(10, 2),
        help='ฐานเงินเดือนสำรองสำหรับคำนวณ Incentive/Bonus ของระบบ Fleet '
             'Telematics เท่านั้น — ใช้เมื่อไม่มีข้อมูลสัญญาจ้าง (hr.version/'
             'hr.contract) ของพนักงานคนนี้ในระบบ ถ้ามีข้อมูลสัญญาจ้างจริง '
             'ระบบจะดึงจากตรงนั้นเป็นหลักก่อนเสมอ ฟิลด์นี้เป็นแค่ทางเลือก '
             'สำรองลำดับถัดไป'
    )
