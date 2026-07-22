# ==============================================================================
# wizards/sync_wizard.py
#
# UC-05 — หน้าต่างให้ Admin สั่ง Sync ทริปจาก Backend เองได้ทันที
# ไม่ต้องรอตัวจับเวลาอัตโนมัติ (Cron) ที่รันทุก 5 นาที
#
# เรียกฟังก์ชันเดียวกับที่ Cron ใช้ (fleet.telematics.log._cron_sync_trips)
# ตรงๆ เพื่อให้ผลลัพธ์เหมือนกันเป๊ะไม่ว่าจะสั่งเองหรือรอ Cron — ไม่มี logic
# แยกชุดที่อาจทำงานไม่ตรงกัน
# ==============================================================================
import logging

from odoo import models, fields

_logger = logging.getLogger(__name__)


class FleetTelematicsSyncWizard(models.TransientModel):
    """หน้าต่างสั่ง Sync ทริปจาก Backend ด้วยมือ แสดงผลว่าได้ทริปใหม่กี่รายการ"""
    _name = 'fleet.telematics.sync.wizard'
    _description = 'Manual Sync from Backend API (UC-05 — สั่ง Sync เองได้ ไม่ต้องรอ Cron)'

    result_message = fields.Text(string='ผลลัพธ์', readonly=True)
    last_sync_at   = fields.Datetime(string='Synced At', readonly=True)

    def action_sync_now(self):
        """กดปุ่มแล้วเรียก _cron_sync_trips() ทันที นับจำนวนทริปที่เพิ่มขึ้น
        มาก่อน-หลัง แล้วสรุปผลให้ Admin เห็นบนหน้าจอ"""
        self.ensure_one()
        Log = self.env['fleet.telematics.log']
        ICP = self.env['ir.config_parameter'].sudo()

        before_ts = ICP.get_param('fleet_telematics.trip_last_sync_timestamp', '')
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

        after_ts = ICP.get_param('fleet_telematics.trip_last_sync_timestamp', '')
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
        """เปิดหน้าต่างเดิมค้างไว้ให้เห็นผลลัพธ์ แทนที่จะปิดทันทีหลังกดปุ่ม"""
        return {
            'type':     'ir.actions.act_window',
            'res_model': self._name,
            'res_id':   self.id,
            'view_mode': 'form',
            'target':   'new',
        }
