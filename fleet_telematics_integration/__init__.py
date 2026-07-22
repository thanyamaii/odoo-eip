# ==============================================================================
# __init__.py — จุดเริ่มต้นของโมดูล นำเข้าโฟลเดอร์ models/controllers/wizards
# ==============================================================================
from . import models
from . import controllers
from . import wizards


def _post_init_seed_telematics_config(env):
    """Post-install hook: ตั้งค่าเชื่อมต่อ Backend เริ่มต้นลง ir.config_parameter
    ทันทีตอนติดตั้ง/อัปเกรดโมดูล เพื่อให้ Cron sync ทำงานได้เลยโดยไม่ต้องเข้า
    ไปกรอกที่หน้า Settings ก่อน

    ⚠️ คำเตือนความปลอดภัย: ค่า API Key ด้านล่างฝังอยู่ใน source code ตรงๆ
    ถ้า repo/zip นี้ถูกแชร์ออกไป จะเห็น secret นี้ไปด้วย แนะนำให้พิจารณา:
      - ย้ายไปตั้งค่าผ่านหน้า Settings เอง (ปุ่ม "บันทึกและทดสอบการเชื่อมต่อ")
        แทนการฝังในโค้ด แล้วลบ hook นี้ทิ้ง หรือ
      - อ่านค่าจาก environment variable ของเซิร์ฟเวอร์ตอน runtime แทน
    """
    ICP = env['ir.config_parameter'].sudo()

    base_url = 'http://192.168.1.43:8001'
    api_key  = 'ktc-fleet-2026-secret'

    ICP.set_param('fleet_telematics.api_url_input',     base_url)
    ICP.set_param('fleet_telematics.mtd_api_url',        base_url)
    ICP.set_param('fleet_telematics.last_confirmed_url', base_url)
    ICP.set_param('fleet_telematics.mtd_api_key',        api_key)
