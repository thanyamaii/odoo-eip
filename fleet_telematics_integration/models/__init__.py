# ==============================================================================
# models/__init__.py — นำเข้าไฟล์โมเดลทั้งหมดในโฟลเดอร์ models
#
# ⚠️ สำคัญ: ทุกไฟล์โมเดลต้องถูก import ที่นี่ ไม่งั้น Odoo จะไม่รู้จักโมเดล/
# field ในไฟล์นั้นเลยแม้โค้ดจะถูกต้อง 100% ก็ตาม (import คือจุดเดียวที่ทำให้
# Python โหลดไฟล์นั้นขึ้นมาจริง)
# ==============================================================================
from . import telematics_config
from . import telematics_device
from . import fleet_vehicle_ext
from . import hr_employee_ext
from . import telematics_log
from . import telematics_event
from . import telematics_scoring
from . import telematics_incentive
from . import telematics_payload
from . import telematics_report
from . import telematics_report_providers
from . import telematics_vehicle_trip
