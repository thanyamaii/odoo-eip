# ==============================================================================
# controllers/portal.py
#
# UC-11 — หน้าเว็บให้พนักงานขับรถล็อกอินดูคะแนน/โบนัสของตัวเองได้
# (Odoo Portal Self-service ตาม FDD §2.3)
#
# ใช้งานได้กับทั้งผู้ใช้ภายในองค์กร (internal user) และผู้ใช้ Portal ภายนอก
# ที่ผูกบัญชีไว้กับ hr.employee — ดึงข้อมูลด้วย sudo() แล้วกรอง driver_id
# ด้วยมือในนี้เอง (ไม่เปิดสิทธิ์เข้าถึงโมเดลตรงๆ ให้กลุ่ม Portal ผ่าน
# ir.model.access) เพื่อกันไม่ให้เข้าถึงข้อมูลคนอื่นผ่านช่องทางอื่น เช่น RPC
# ==============================================================================

from odoo import http
from odoo.http import request
from odoo.addons.portal.controllers.portal import CustomerPortal


class FleetTelematicsPortal(CustomerPortal):
    """เพิ่มหน้าเว็บ /my/telematics ให้พนักงานดูคะแนนตัวเอง"""

    def _get_my_employee(self):
        """หาว่าผู้ใช้ที่ล็อกอินอยู่ตอนนี้ ผูกกับพนักงานคนไหนในระบบ HR
        ใช้ field user_id (Many2one มาตรฐาน) จับคู่ผู้ใช้ Odoo กับ hr.employee"""
        return request.env['hr.employee'].sudo().search(
            [('user_id', '=', request.env.user.id)], limit=1
        )

    @http.route(
        ['/my/telematics', '/my/telematics/score'],
        type='http', auth='user', website=True, sitemap=False,
    )
    def portal_my_telematics_score(self, **kwargs):
        """หน้าสรุปคะแนน — แสดงประวัติโบนัส 12 รอบล่าสุด + ทริป 15 รายการ
        ล่าสุด + คะแนนเฉลี่ยของทริปล่าสุด กรองด้วย driver_id ของตัวเองเสมอ
        เพื่อไม่ให้เห็นข้อมูลของพนักงานคนอื่น"""
        employee = self._get_my_employee()
        if not employee:
            # ผู้ใช้คนนี้ไม่ได้ผูกกับพนักงานคนไหนเลยในระบบ HR
            return request.render(
                'fleet_telematics_integration.portal_telematics_no_employee', {}
            )

        Incentive = request.env['fleet.telematics.incentive'].sudo()
        TripLog   = request.env['fleet.telematics.log'].sudo()

        incentives = Incentive.search(
            [('driver_id', '=', employee.id)],
            order='period_year desc, period_month desc',
            limit=12,
        )
        recent_trips = TripLog.search(
            [('driver_id', '=', employee.id), ('state', '=', 'synced')],
            order='trip_start desc',
            limit=15,
        )

        latest_score = recent_trips[:1].driver_score if recent_trips else 0.0
        avg_score_recent = (
            round(sum(recent_trips.mapped('driver_score')) / len(recent_trips), 2)
            if recent_trips else 0.0
        )

        values = {
            'employee':          employee,
            'incentives':        incentives,
            'recent_trips':      recent_trips,
            'latest_score':      latest_score,
            'avg_score_recent':  avg_score_recent,
            'page_name':         'telematics_score',
        }
        return request.render(
            'fleet_telematics_integration.portal_my_telematics_score', values
        )
