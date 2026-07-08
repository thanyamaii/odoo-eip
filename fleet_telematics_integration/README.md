# Fleet Telematics Integration (Odoo Module)

โมดูล Odoo สำหรับเชื่อมต่อระบบ Fleet Telematics & Driver Behavior Monitoring
กับ Backend API ("กวางตุ้ง") ตาม FDD *Fleet Telematics Proposal v1.1*

> ⚠️ **สถานะเอกสารนี้:** README นี้เขียนขึ้นภายหลัง (2026-07) เพื่อปิด gap ใน
> FDD §15 Handover Checklist ที่ระบุว่าต้องมี "Source code บน Git พร้อม
> README ทุก module" และ "Odoo User Manual (Admin Guide + HR Guide)" —
> **ยังไม่เคยผ่านการติดตั้ง/ทดสอบจริงบน Odoo 19 instance จริง** เนื้อหาด้าน
> ล่างอ้างอิงจากโค้ดและ FDD เท่านั้น ควรตรวจสอบซ้ำหลังติดตั้งจริงครั้งแรก

---

## 1. ภาพรวม

โมดูลนี้ทำหน้าที่:
- ลงทะเบียน GPS Device ผูกกับรถ (`fleet.vehicle`)
- ดึง Trip Log / Harsh Event จาก Backend เข้ามาเก็บใน Odoo อัตโนมัติทุก 5 นาที
- คำนวณคะแนนพฤติกรรมการขับขี่ (Driver Score) และ Incentive/Bonus รายเดือน
- แสดง Dashboard, Live Map, รายงานน้ำมัน/ซ่อมบำรุง
- ให้พนักงานดูคะแนนตนเองผ่าน Odoo Portal

## 2. ความต้องการของระบบ (Requirements)

| รายการ | เวอร์ชัน |
|---|---|
| Odoo | 19.0 |
| Python packages | `requests` (มากับ Odoo อยู่แล้ว) |
| Module dependencies | `fleet`, `hr`, `hr_contract`, `web`, `mail`, `portal` |
| Backend API | ต้องรองรับ endpoint ตาม FDD §11.3 + JWT Authentication ตาม §13 |

## 3. การติดตั้ง (Installation)

```bash
# คัดลอกโฟลเดอร์ fleet_telematics_integration/ ไปไว้ใน addons path
odoo-bin -c odoo.conf -d <your_db> -i fleet_telematics_integration --stop-after-init

# รัน unit test ก่อน deploy จริงเสมอ
odoo-bin -c odoo.conf -d <your_db> --test-enable \
         --test-tags /fleet_telematics_integration --stop-after-init
```

**⚠️ ก่อนติดตั้งจริง ต้องตรวจสอบ 2 จุดนี้ก่อน** (ดูรายละเอียดใน `docs/known_risks.md`):
1. Field `hr.employee.work_contact_id` มีอยู่จริงในเวอร์ชัน Odoo ที่ใช้หรือไม่
   (ใช้ใน Portal self-service fallback — มีรายงานว่าถูกลบตั้งแต่ v17)
2. xmlid `fleet.module_category_fleet` มีอยู่จริงหรือไม่ (ใช้สร้างกลุ่ม Executive)

## 4. Admin Guide

### 4.1 ตั้งค่าเชื่อมต่อ Backend (ทำก่อนอย่างอื่นทั้งหมด)
เมนู **Fleet Telematics > Settings**
1. กรอก **API URL** ของ Backend
2. กรอก **Backend Username / Password** (สำหรับ JWT Login ตาม FDD §13 —
   ถ้าไม่กรอก ระบบจะ fallback ไปใช้ "API Key (Legacy)" แทนชั่วคราว)
3. กด **Save and Test** — ถ้าเชื่อมต่อสำเร็จ badge จะขึ้น 🟢 Ready

### 4.2 ลงทะเบียนรถ + GPS Device (UC-01)
เมนู **Fleet > Vehicles** เปิดฟอร์มรถ
1. กรอก **GPS Device ID** (รูปแบบ `KTC-XXX`) และ **Device Name**
2. กด **Register Device** — รอสถานะเปลี่ยนเป็น "ลงทะเบียนแล้ว"
3. ใช้ปุ่ม **🔄 ดึงข้อมูล Device จาก Backend** เพื่อ sync ข้อมูลล่าสุดได้ทุกเมื่อ

### 4.3 ตั้งค่า Scoring (UC-02)
เมนู **Fleet Telematics > Scoring Config** — ปรับน้ำหนักการหักคะแนนและ
threshold ต่างๆ ได้ที่นี่ (มีได้ config เดียวที่ active พร้อมกัน)

### 4.4 Sync ข้อมูล (UC-05)
- อัตโนมัติทุก 5 นาที (Cron) — ไม่ต้องทำอะไร
- ถ้าต้องการ sync ทันที: เมนู **Fleet Telematics > Sync Now**

## 5. HR Guide

### 5.1 อนุมัติโบนัสประจำเดือน (UC-09)
เมนู **Fleet Telematics > Incentive / Bonus** (เห็นเฉพาะกลุ่ม Fleet Manager)
1. ตรวจสอบตัวเลข avg_score / bonus_pct / bonus_amount ของแต่ละคน
2. กด **Confirm** → ระบบล็อกตัวเลขและดึง bonus ล่าสุดจาก Backend ครั้งสุดท้าย
3. กด **Approve** → บันทึกชื่อผู้อนุมัติ (เห็นได้ใน Audit Log ใต้ฟอร์ม)
4. กด **Mark as Paid** เมื่อจ่ายเงินจริงแล้ว

> ⚠️ **หลัง Confirm แล้ว ตัวเลขคะแนน/โบนัสจะถูกล็อก แก้ไขไม่ได้** (ตาม FDD §13
> Score Immutability) ถ้ากรอกผิดต้องกด **Reset** กลับเป็น Draft ก่อน — การ
> Reset จะถูกบันทึกใน Audit Log เสมอ

### 5.2 ดู Dashboard / Scorecard (UC-06, UC-08)
เมนู **Fleet Telematics > Dashboard** — ดูคะแนนคนขับ, Live Map, รายงานน้ำมัน

### 5.3 พนักงานดูคะแนนตนเอง (UC-11)
พนักงานที่มีบัญชี Portal เข้าที่ `/my/driving-score` หรือกดลิงก์ "คะแนน
ขับขี่ของฉัน" ในหน้า My Account

## 6. Known Risks / ข้อจำกัดที่ทราบอยู่แล้ว

| ความเสี่ยง | รายละเอียด |
|---|---|
| Portal fallback field | `work_contact_id` อาจไม่มีใน Odoo 19 — ต้องทดสอบก่อน |
| JWT login schema | `POST /auth/login` request/response schema เป็นการเดา ยังไม่ยืนยัน field name จริงกับ Backend |
| ยังไม่เคยรันจริง | โค้ดผ่านแค่ `py_compile`/XML validation เท่านั้น ไม่เคยติดตั้งบน Odoo server จริง |

## 7. โครงสร้างโมดูล

```
fleet_telematics_integration/
├── models/           # Business logic ทั้งหมด
├── wizards/          # Manual Sync Wizard (เพิ่มใหม่)
├── controllers/      # REST endpoints (webhook, live map proxy) + Portal
├── views/            # Form/List/Menu views
├── security/         # Access rights, groups, record rules
├── data/             # Cron jobs
├── reports/          # QWeb PDF reports
└── tests/            # Unit tests (50 tests: UC-01, 02, 04, 09)
```

## 8. การรัน Test

```bash
odoo-bin -c odoo.conf -d <db> --test-enable \
         --test-tags /fleet_telematics_integration --stop-after-init
```

ครอบคลุม: Device Registration (UC-01), Scoring Config (UC-02), Trip Sync
(UC-04/05), **Incentive/Bonus (UC-09) รวม Score Immutability และ Audit Log
(UC-10)** — เพิ่มใหม่ 2026-07 เพื่อปิด gap FDD §14.2 ที่เดิม coverage = 0%
