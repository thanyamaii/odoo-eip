# ==============================================================================
# controllers/portal.py  (เพิ่มใหม่ 2026-07-06)
#
# UC-11 — พนักงานดูคะแนนตนเอง (Self-service) ผ่าน Odoo Portal
# (FDD §2.3: "พนักงานสามารถดูคะแนนตนเองแบบ Self-service ผ่าน Odoo Portal ได้")
#
# เดิม UC-11 ไม่มีการ implement เลยแม้แต่น้อย — ไม่มี controller เว็บ,
# ไม่มี template, และ ir.model.access.csv ยัง block perm_read=0 อยู่ด้วย
# (แก้ไปแล้วใน security/ir.model.access.csv สำหรับ internal user;
#  ไฟล์นี้เพิ่มเส้นทางที่สองสำหรับ "Odoo Portal" ตัวจริง — ใช้ได้ทั้ง
#  internal user และ portal user ภายนอกที่ผูกกับ hr.employee)
#
# ออกแบบให้ query ด้วย sudo() แล้วกรอง driver_id ด้วยมือใน controller เอง
# (ไม่เปิด ir.model.access ให้กลุ่ม portal โดยตรง) เพื่อไม่ให้ portal user
# มีสิทธิ์เข้าถึงโมเดลกว้างเกินจำเป็นผ่านช่องทางอื่น เช่น RPC
# ==============================================================================

from odoo import http
from odoo.http import request
from odoo.addons.portal.controllers.portal import CustomerPortal


class FleetTelematicsPortal(CustomerPortal):

    def _get_my_employee(self):
        """หา hr.employee ที่ผูกกับผู้ใช้ที่ login อยู่ (internal หรือ portal ก็ได้)"""
        return request.env['hr.employee'].sudo().search(
            [('user_id', '=', request.env.user.id)], limit=1
        )

    @http.route(
        ['/my/telematics', '/my/telematics/score'],
        type='http', auth='user', website=True, sitemap=False,
    )
    def portal_my_telematics_score(self, **kwargs):
        employee = self._get_my_employee()
        if not employee:
            return request.render(
                'fleet_telematics_integration.portal_telematics_no_employee', {}
            )

        Incentive = request.env['fleet.telematics.incentive'].sudo()
        TripLog   = request.env['fleet.telematics.log'].sudo()

        # กรองด้วย driver_id = employee.id เสมอ — พนักงานเห็นแค่ของตัวเองเท่านั้น
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
