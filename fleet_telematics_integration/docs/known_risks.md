# Known Risks / สมมติฐานที่ยังไม่ยืนยัน

รวบรวมจากการตรวจสอบโค้ดเทียบกับ FDD v1.1 (2026-07) — สิ่งเหล่านี้ต้องตรวจสอบ
กับ Odoo instance จริงและทีม Backend ก่อน deploy production

## 0. ✅ ยืนยันแล้วจริง — hr_contract ไม่มีในระบบผู้ใช้ (แก้แล้ว)
ผู้ใช้ทดลอง upgrade จริงแล้วเจอ error "โมดูลนี้ไม่พร้อมใช้งานในระบบของคุณ"
ยืนยันว่า `hr_contract` ไม่มีอยู่จริงในระบบเป้าหมาย — **แก้แล้ว**: ถอด
`hr_contract` ออกจาก hard dependency, เพิ่ม field
`hr.employee.telematics_base_salary` เป็น fallback แทน (ดู
`models/hr_employee_ext.py`) ระบบจะลองใช้ `hr.contract` ก่อนถ้ามีติดตั้งอยู่
จริง (เช็คแบบ runtime) ถ้าไม่มีจะ fallback มาที่ field นี้แทนโดยไม่ crash

## 1. Portal Self-service (UC-11) — ความเสี่ยงสูง
`controllers/portal.py` ใช้ `hr.employee.work_contact_id` เป็น fallback
ในการจับคู่ portal user กับพนักงาน มีรายงานจากชุมชน Odoo ว่า field นี้ถูกลบ
ออกตั้งแต่ v17 — ถ้าไม่มีจริงใน Odoo 19 จะเกิด error ทันทีตอนเรียก
`/my/driving-score` (ไม่ใช่แค่ fallback เฉยๆ)

**วิธีตรวจสอบ:** Developer Mode > Technical > Database Structure > Models >
ค้นหา `hr.employee` > เช็ค field list

**ถ้าไม่มีจริง:** ต้องแก้ `_get_portal_employee()` ให้ใช้ field อื่นแทน (เช่น
เทียบ email ตรงๆ ระหว่าง `res.users.login` กับ `hr.employee.work_email`)

## 2. JWT Authentication schema — ความเสี่ยงสูง
`models/telematics_config.py: _login_get_token()` เดา schema ของ
`POST /auth/login` ดังนี้:
- Request: `{"username": ..., "password": ...}`
- Response: `access_token` หรือ `token` หรือ `jwt`, พร้อม `expires_in`/`expires_in_min`

FDD §13 ระบุแค่ "JWT token สำหรับ API" ไม่มีรายละเอียด schema จริง —
**ต้องทดสอบยิง endpoint จริงก่อนใช้งาน production**

## 3. xmlid ที่ยังไม่ยืนยัน — ความเสี่ยงกลาง
`security/telematics_groups.xml` อ้างอิง `fleet.module_category_fleet` —
ถ้า xmlid นี้ไม่มีจริงในเวอร์ชัน Odoo ที่ใช้ การติดตั้งโมดูลจะ error ทันที

## 4. Odoo Version ในเอกสารไม่ตรงกับของจริง
FDD §4 เขียนว่า "Odoo 17 Enterprise" แต่ `__manifest__.py` เขียน
`19.0.1.0.0` — FDD ควรถูกอัปเดตเป็น v2.0 ให้ตรงกับของจริง (ยังไม่ได้ทำ)

## 5. ยังไม่เคยรันบน Odoo server จริงเลย
ทุกอย่างผ่านการตรวจสอบด้วย `py_compile` (Python syntax) และ
`xml.etree.ElementTree.parse` (XML well-formed) เท่านั้น **ไม่เคยผ่าน**
`odoo-bin -i fleet_telematics_integration --test-enable` **จริงสักครั้ง**
เนื่องจากเครื่องมือที่ใช้พัฒนาไม่มี Odoo/PostgreSQL ติดตั้งอยู่

## 6. Test Coverage — ยังไม่วัดเป็นตัวเลขจริง
เพิ่ม unit test ให้ `telematics_incentive.py` แล้ว 20 เทส ครอบคลุมทุก
code path หลัก (workflow, immutability, tier boundary, backend/fallback,
audit log) แต่**ไม่มีเครื่องมือรัน coverage.py ในสภาพแวดล้อมนี้** จึงบอกได้
แค่เชิงคุณภาพว่าครอบคลุมมาก ไม่ใช่ตัวเลข % ที่วัดได้จริงตาม FDD §14.2
