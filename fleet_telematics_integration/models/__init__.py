# ==============================================================================
# models/__init__.py — ไฟล์นำเข้าไฟล์ Python ทั้งหมดในโฟลเดอร์ models
# ==============================================================================
from . import telematics_config
from . import telematics_device      # เพิ่ม 2026-06-30 — เดิมไม่เคยถูก import เลย
                                      # ทำให้โมเดล fleet.telematics.device ไม่มีตัวตนจริง
from . import fleet_vehicle_ext
from . import telematics_log
from . import telematics_event
from . import telematics_scoring
from . import telematics_incentive
from . import telematics_payload      # เพิ่ม 2026-06-30 — เดิมไม่เคยถูก import เลย (dead code)
from . import telematics_report      # เพิ่ม 2026-06-30 — wizard ดึงรายงานจาก Backend
from . import telematics_vehicle_trip  # Vehicle Trip History wizard
