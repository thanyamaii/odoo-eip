# Known Risks / สมมติฐานที่ยังไม่ยืนยัน

รวบรวมจากการตรวจสอบโค้ดเทียบกับ FDD v1.4 (อัปเดตล่าสุด 2026-07-16) —
สถานะอัปเดตหลังผู้ใช้เริ่มทดสอบจริงบน Odoo 19 instance แล้วบางส่วน

## 0. ✅ แก้แล้ว — hr_contract ไม่มีในระบบผู้ใช้
ยืนยันแล้วว่า `hr_contract` ไม่มีอยู่จริง เพราะ **Odoo 19 รวมโมดูลนี้เข้า `hr`
core และเปลี่ยนชื่อโมเดลเป็น `hr.version`** (ยืนยันจาก Model Overview จริงของ
ผู้ใช้: มี field `employee_id`, `wage`, `is_current`) — แก้โค้ดใน
`_apply_backend_bonus()` ให้ query `hr.version` แทน พร้อม fallback หลายชั้น:
hr.version → hr.contract (เผื่อรันเวอร์ชันเก่ากว่า) → telematics_base_salary
บนโปรไฟล์พนักงาน → กรอกมือรายใบ

**บั๊กที่เจอเพิ่มระหว่างแก้จริง (ปิดแล้วทั้งหมด):**
- `is_current` เป็น compute field ไม่ stored → ใช้ใน `search()` ตรงๆ ไม่ได้
  ต้องดึงมาก่อนแล้วกรองด้วย `.filtered()` แทน
- `res.groups.users` ถูกถอดออกใน Odoo 19 — เจอ 2 จุด (`telematics_incentive.py`
  และ `telematics_log.py` ส่วนแจ้งเตือนซ่อมบำรุง) แก้ทั้งคู่ให้ query จาก
  `res.users` โดยเช็ค field `groups_id`/`group_ids` ก่อนใช้งาน
- `res.users.groups_id` ถูกเปลี่ยนชื่อเป็น `group_ids` — เจอจากการรัน
  automated test จริงครั้งแรก (พังทั้ง 11 คลาสที่จุดเดียวกัน) แก้ด้วย helper
  `_users_groups_field()` ตรวจหาชื่อ field แบบไดนามิกในไฟล์ test

## 1. ✅ แก้แล้ว — Portal Self-service (UC-11) ไม่ได้ใช้ work_contact_id
ตรวจโค้ดจริงใน `controllers/portal.py` (`_get_my_employee()`) แล้วยืนยันว่า
**ใช้ field `user_id` (Many2one มาตรฐานที่มั่นคงมาก) ไม่ได้ใช้
`work_contact_id` เลย** — ปิดความเสี่ยงนี้ได้

## 2. ✅ ไม่ใช่ความเสี่ยงจริง — เอกสารเก่าอ้างถึง JWT ที่โค้ดไม่เคยมี
เอกสารรุ่นก่อนหน้าเขียนไว้ว่า `models/telematics_config.py: _login_get_token()`
เดา schema ของ `POST /auth/login` (username/password → JWT token) — ตรวจโค้ด
จริงทั้งไฟล์แล้ว **ไม่มีเมธอดนี้และไม่มี JWT login flow อยู่ในโค้ดเลยแม้แต่
จุดเดียว** ระบบยืนยันตัวตนจริงที่ใช้อยู่ตลอดทั้งโมดูลคือ **APIKEY header
ธรรมดา** (`fields.Char` เก็บค่าไว้ที่ `fleet.telematics.config.api_key` แล้ว
แนบเป็น `headers={'APIKEY': api_key}` ทุก request ไปยัง Backend) — เป็น
ข้อความที่ตกค้างจากดีไซน์รุ่นก่อนที่เลิกใช้ไปแล้ว ไม่ใช่ความเสี่ยงที่มีจริง
ในโค้ดปัจจุบัน ปิดข้อนี้ได้

## 3. ✅ แก้แล้ว — xmlid fleet.module_category_fleet
`security/telematics_groups.xml` ไม่ได้อ้างอิง `category_id`/`privilege_id`
เลยแล้ว (ถอดออกทั้งหมด) ความเสี่ยงนี้ปิดแล้ว

## 4. ✅ แก้แล้ว — Odoo Version ในเอกสารไม่ตรงกับของจริง
FDD §4 เดิมเขียนว่า "Odoo 17 Enterprise" แต่ `__manifest__.py` เขียน
`19.0.1.0.0` จริง — **บริษัทอนุมัติให้ใช้ Odoo 19 อย่างเป็นทางการแล้ว**
ไม่ถือเป็นความเสี่ยงอีกต่อไป (เหลืองานเอกสาร: อัปเดตตัว FDD เป็น v2.0 ให้
ตรงกับของจริง แต่ไม่ใช่ blocker ของการใช้งาน)

## 5. 🔶 บางส่วน — เริ่มรันบน Odoo server จริงแล้ว พบ+แก้บั๊กจริงหลายจุด
รันคำสั่ง `--test-enable --test-tags /fleet_telematics_integration` ครั้งแรก
บน Odoo 19 instance จริงแล้ว พังทั้ง 11 คลาสจากบั๊ก `groups_id`/`group_ids`
(ดูข้อ 0) — แก้แล้ว รอผลการรันซ้ำรอบใหม่เพื่อยืนยันว่าผ่านครบทั้ง 107 test
(อัปเดตจำนวนล่าสุดหลังเพิ่ม test ของ Tier D/History/local fallback)

## 6. ⚠️ ยังไม่วัด — Test Coverage เป็นตัวเลขจริง
มี unit test รวม **107 เทส** ครอบคลุมทุก UC หลักแล้วเชิงคุณภาพ แต่ยังไม่มี
เครื่องมือรัน `coverage.py` คู่กับ Odoo server จริง จึงบอกได้แค่เชิงคุณภาพ
ไม่ใช่ตัวเลข % ตามเกณฑ์ FDD §14.2

## 7. ✅ แก้แล้ว — Data Retention (FDD §13) ไม่เคย implement เลย
เพิ่ม `_cron_purge_old_trips()` ใน `models/telematics_log.py` ทำงานทุกเดือน
ลบ Trip Log (+ Event cascade) ที่เก่ากว่า 3 ปี ปรับได้ผ่าน
`ir.config_parameter: fleet_telematics.trip_retention_years` **ไม่แตะ
Incentive เด็ดขาด** ("raw telemetry 90 วัน" เป็นหน้าที่ TimescaleDB ฝั่ง
Backend ไม่ใช่ Odoo)

## 8. ✅ แก้แล้ว — แถบเตือน Tier D ขึ้นผิดจังหวะ
เดิมแถบเตือน Tier D ขึ้นตั้งแต่สร้าง record ใหม่ก่อนเลือก Driver เลย (เพราะ
field มี default='D') แก้แล้วให้เช็คเพิ่มว่าต้องเลือก driver_id ก่อนด้วย
พร้อมเพิ่ม validation บังคับเลือก Driver ก่อนกด Refresh/Confirm

## 9. ✅ แก้แล้ว — 3 จุดที่ขาดจริงเทียบกับ FDD (พบจากการตรวจ field-by-field)
ไล่เทียบ FDD §12.3-12.5 กับโค้ดจริงทีละ field แล้วพบ 3 จุดที่ขาดจริง (ไม่ใช่
ตีความผิด) — แก้ครบแล้วทั้งหมด:
- **History section หายไป** (§12.5): เพิ่ม `created_date`, `last_used_date`,
  `total_trips_calculated` บน Scoring Config พร้อมฟังก์ชัน `_track_usage()`
  อัปเดตอัตโนมัติทุกครั้งที่ `_cron_sync_trips()` sync ทริปสำเร็จ
- **Tier D ไม่มี field ปรับได้** (§12.3): เพิ่ม `tier_d_min_score`,
  `tier_d_bonus_pct` เป็น field จริง (เดิม hardcode ไว้ในลอจิก) ล็อกเมื่อ
  Active=True เหมือนฟิลด์เกณฑ์อื่น พร้อมส่งไป Backend ใน
  `_build_config_payload()` ด้วย
- **ไม่แจ้ง HR ทุกครั้งที่มี draft ใหม่** (§12.4 ขั้นตอน 4): เดิมมีแค่
  `_notify_hr_tier_d()` แจ้งเฉพาะกรณี Tier D — เพิ่ม
  `_notify_hr_new_drafts_batch()` แจ้งสรุปทุกครั้งที่ cron รายเดือนสร้าง
  draft ใหม่ (ส่งเป็นสรุปเดียวต่อรอบ ไม่แยกอีเมลต่อพนักงาน)

**ยังไม่ตัดสินใจ:** Unique constraint ของ Incentive เปลี่ยนจาก
`driver_id + period_month + period_year` (ตามที่ FDD ระบุ) เป็น
`driver_id + date_from + date_to` (ตามบรีฟภายหลังที่รองรับรอบตัดวิกไม่ตรง
เดือนปฏิทิน) — เป็นการเบี่ยงจาก FDD โดยตั้งใจ ยังไม่ได้ยืนยันกับ Supervisor
ว่าจะใช้ตามบรีฟใหม่นี้ต่อไป หรือย้อนกลับให้ตรงเอกสารเดิม
