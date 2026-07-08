# ==============================================================================
# wizards/sync_wizard.py
# ==============================================================================
# (เพิ่มใหม่ 2026-07) Manual Sync Wizard — ตาม FDD §12.1 Module Structure ที่
# ระบุไว้ตั้งแต่แรกว่าต้องมี "wizards/sync_wizard.py # Manual sync from API"
# แต่ไม่เคยถูกสร้างเลยในของเดิม — Admin ไม่มีทางสั่ง sync trip เองได้ ต้องรอ
# cron ทุก 5 นาทีอย่างเดียว (data/telematics_cron.xml)
#
# Wizard นี้เรียก fleet.telematics.log._cron_sync_trips() ตัวเดียวกับที่ Cron
# เรียก ไม่ใช่ logic คนละชุด — เพื่อไม่ให้พฤติกรรมต่างจาก cron โดยไม่ตั้งใจ
# ==============================================================================
import logging

from odoo import models, fields

_logger = logging.getLogger(__name__)


class FleetTelematicsSyncWizard(models.TransientModel):
    _name = 'fleet.telematics.sync.wizard'
    _description = 'Manual Sync from Backend API (UC-05 — สั่ง Sync เองได้ ไม่ต้องรอ Cron)'

    result_message = fields.Text(string='ผลลัพธ์', readonly=True)
    last_sync_at   = fields.Datetime(string='Synced At', readonly=True)

    def action_sync_now(self):
        """เรียก logic เดียวกับ Cron ตรงๆ (_cron_sync_trips) เพื่อให้ผลลัพธ์
        เหมือนกันเป๊ะไม่ว่าจะสั่งเองหรือรอ Cron — ป้องกัน logic 2 ชุดที่อาจ
        ทำงานไม่ตรงกันในอนาคต"""
        self.ensure_one()
        Log = self.env['fleet.telematics.log']

        ICP = self.env['ir.config_parameter'].sudo()
        before_ts = ICP.get_param('fleet_telematics.trip_last_poll_ts', '')
        count_before = Log.search_count([])

        try:
            Log._cron_sync_trips()
        except Exception as e:
            _logger.exception('Manual sync ล้มเหลว')
            self.write({
                'result_message': f'❌ Sync ล้มเหลว: {e}',
                'last_sync_at':   fields.Datetime.now(),
            })
            return self._reopen_wizard()

        after_ts = ICP.get_param('fleet_telematics.trip_last_poll_ts', '')
        count_after = Log.search_count([])
        new_trips = count_after - count_before

        self.write({
            'result_message': (
                f'✅ Sync สำเร็จ\n'
                f'Trip ใหม่ที่ถูกบันทึก: {new_trips} รายการ\n'
                f'Last Poll Timestamp: {after_ts or "-"}'
                + ('\n(ไม่เปลี่ยนจากเดิม — อาจไม่มีข้อมูลใหม่จาก Backend)'
                   if before_ts == after_ts else '')
            ),
            'last_sync_at': fields.Datetime.now(),
        })
        return self._reopen_wizard()

    def _reopen_wizard(self):
        """เปิด wizard เดิมค้างไว้ให้เห็นผลลัพธ์ แทนที่จะปิดหน้าต่างทันที"""
        return {
            'type':     'ir.actions.act_window',
            'res_model': self._name,
            'res_id':   self.id,
            'view_mode': 'form',
            'target':   'new',
        }
