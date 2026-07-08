# ==============================================================================
# models/hr_employee_ext.py
# ==============================================================================
# (เพิ่มใหม่ 2026-07) แก้ปัญหาจริงที่พบ: ผู้ใช้ลอง upgrade โมดูลแล้วเจอ error
# "คุณพยายามอัปเกรดโมดูล fleet_telematics_integration ที่ขึ้นอยู่กับโมดูล:
# hr_contract แต่โมดูลนี้ไม่พร้อมใช้งานในระบบของคุณ"
#
# เดิม _apply_backend_bonus() ใน telematics_incentive.py พึ่งพา hr.contract
# เพียงอย่างเดียวเพื่อดึง wage — ตอนนี้ถอด hr_contract ออกจาก hard depends
# แล้ว (ดู __manifest__.py) และเพิ่ม field สำรองตรงนี้แทน เพื่อให้ระบบยัง
# คำนวณโบนัสได้แม้ hr_contract ไม่ถูกติดตั้งในระบบเลย
# ==============================================================================
from odoo import models, fields


class HrEmployeeTelematicsExt(models.Model):
    _inherit = 'hr.employee'

    telematics_base_salary = fields.Float(
        string='Base Salary (สำหรับคำนวณโบนัส Telematics)',
        digits=(10, 2),
        help='กรอกเงินเดือนฐานสำหรับใช้คำนวณ Incentive/Bonus ของระบบ '
             'Fleet Telematics เท่านั้น — ใช้ในกรณีที่ระบบไม่มีโมดูล '
             'hr_contract ติดตั้งอยู่ (ถ้ามี hr_contract และมีสัญญาจ้างที่ '
             'active อยู่ ระบบจะใช้ค่าจาก hr.contract.wage เป็นหลักก่อนเสมอ '
             'ฟิลด์นี้เป็นแค่ fallback)'
    )
