# ==============================================================================
# __init__.py — ไฟล์นำเข้าแพ็กเกจ Python ของโมดูล
# ==============================================================================
from . import models
from . import controllers


def _post_init_seed_telematics_config(env):
    """
    Post-install hook: เซ็ตค่าเชื่อมต่อ Backend จริงลง ir.config_parameter
    ทันทีตอนติดตั้ง/อัปเกรดโมดูล เพื่อให้ Cron Job (ทุก 5 นาที) ใช้งานได้
    โดยไม่ต้องเข้าไปกรอกผ่านหน้า Settings ก่อน

    ⚠️ คำเตือนด้านความปลอดภัย: API Key ด้านล่างถูกฝังไว้ใน source code ของ
    โมดูลตรง ๆ — ถ้า repo/zip นี้ถูกแชร์หรือเก็บใน version control ที่คนอื่น
    เข้าถึงได้ จะเห็น secret นี้ไปด้วย แนะนำพิจารณาในระยะยาว:
      - ย้ายไปตั้งค่าผ่าน Settings UI (ปุ่ม "บันทึกและทดสอบการเชื่อมต่อ")
        แทนการฝังในโค้ด แล้วลบ hook นี้ทิ้ง หรือ
      - อ่านค่าจาก environment variable ของเซิร์ฟเวอร์ตอน runtime แทน
    """
    ICP = env['ir.config_parameter'].sudo()

    base_url = 'http://192.168.1.43:8001'
    api_key  = 'ktc-fleet-2026-secret'

    ICP.set_param('fleet_telematics.api_url_input',     base_url)
    ICP.set_param('fleet_telematics.mtd_api_url',        base_url)  # compat เดิม
    ICP.set_param('fleet_telematics.last_confirmed_url', base_url)
    ICP.set_param('fleet_telematics.mtd_api_key',        api_key)
