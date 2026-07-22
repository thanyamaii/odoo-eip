# Fleet Telematics Integration (Odoo Module)

โมดูล Odoo สำหรับเชื่อมต่อระบบ Fleet Telematics & Driver Behavior Monitoring
กับ Backend API ตาม FDD *Fleet Telematics Proposal v1.4*

> **สถานะ:** ผ่านการติดตั้งและทดสอบบน Odoo 19 instance จริงแล้ว พบและแก้
> บั๊กจริงหลายจุดระหว่างทาง (ดูรายละเอียดทั้งหมดใน `docs/known_risks.md`)
> ยังเหลือ Test Coverage ที่ยังไม่ได้วัดเป็นตัวเลข % อย่างเป็นทางการ และ
> ยังไม่ได้ยืนยันกับ Supervisor ว่า unique constraint ของ Incentive ที่
> เปลี่ยนจาก period_month/period_year เป็น date_from/date_to ให้ใช้ต่อหรือ
> ต้องย้อนกลับตาม FDD เดิม

---

## 1. ภาพรวม

โมดูลนี้ทำหน้าที่:
- ลงทะเบียน GPS Device ผูกกับรถ (`fleet.vehicle`)
- ดึง Trip Log / Harsh Event จาก Backend เข้ามาเก็บใน Odoo อัตโนมัติทุก 5 นาที
- คำนวณคะแนนพฤติกรรมการขับขี่ (Driver Score) และ Incentive/Bonus รายเดือน
- แสดง Dashboard, Live Map, รายงานน้ำมัน/ซ่อมบำรุง
- ให้พนักงานดูคะแนนตนเองผ่าน Odoo Portal
- ลบข้อมูลทริปเก่าอัตโนมัติตามนโยบาย Data Retention (ไม่แตะ Incentive)

## 2. ความต้องการของระบบ (Requirements)

| รายการ | เวอร์ชัน |
|---|---|
| Odoo | 19.0 |
| Python packages | `requests`, `python-dateutil` (มากับ Odoo อยู่แล้วทั้งคู่) |
| Module dependencies | `fleet`, `hr`, `web`, `mail`, `portal` |
| Backend API | ต้องรองรับ endpoint ตาม FDD §11.3 |

**หมายเหตุ:** ไม่มี `hr_contract` อยู่ในรายการ dependencies เพราะ Odoo 19
รวมโมดูลนี้เข้า `hr` core แล้ว (โมเดลเปลี่ยนชื่อเป็น `hr.version`) — ระบบ
ดึงฐานเงินเดือนจาก `hr.version` เป็นหลัก มี fallback รองรับ `hr.contract`
แบบเดิมด้วยเผื่อรันบน Odoo เวอร์ชันเก่ากว่า

## 3. การติดตั้ง (Installation)

```bash
# คัดลอกโฟลเดอร์ fleet_telematics_integration/ ไปไว้ใน addons path
odoo-bin -c odoo.conf -d <your_db> -i fleet_telematics_integration --stop-after-init

# รัน unit test ก่อน deploy จริงเสมอ
odoo-bin -c odoo.conf -d <your_db> --test-enable \
         --test-tags /fleet_telematics_integration --stop-after-init
```

## 4. Admin Guide

### 4.1 ตั้งค่าเชื่อมต่อ Backend (ทำก่อนอย่างอื่นทั้งหมด)
เมนู **Fleet Telematics > Settings** (เห็นเฉพาะกลุ่ม Fleet Manager)
1. กรอก **API URL** ของ Backend
2. กรอก **API Key**
3. กด **บันทึกและทดสอบการเชื่อมต่อ** — ถ้าเชื่อมต่อสำเร็จ badge จะขึ้น 🟢 Ready

### 4.2 ลงทะเบียนรถ + GPS Device (UC-01)
เมนู **Fleet > Vehicles** เปิดฟอร์มรถ > แท็บ Telematics Settings
1. กรอก **GPS Device ID** (รูปแบบ `KTC-XXX`) และ **Device Name**
2. กด **Register Device** — รอสถานะเปลี่ยนเป็น "ลงทะเบียนแล้ว"
3. ใช้ปุ่ม **ตรวจสอบ Device** เพื่อเช็คว่า Odoo กับ Backend ตรงกันได้ทุกเมื่อ

### 4.3 ตั้งค่า Scoring (UC-02)
เมนู **Fleet Telematics > Scoring Config** — ปรับน้ำหนักการหักคะแนนและ
threshold ต่างๆ ได้ที่นี่ (มีได้ config เดียวที่ active พร้อมกัน ต้อง Approve
ก่อนถึงจะ Push ไป Backend ได้)

### 4.4 Sync ข้อมูล (UC-05)
- อัตโนมัติทุก 5 นาที (Cron) — ไม่ต้องทำอะไร
- ถ้าต้องการ sync ทันที: เมนู **Fleet Telematics > Sync Now**

### 4.5 Data Retention
Cron รายเดือนลบ Trip Log (+ Event ที่ผูกอยู่) ที่เก่ากว่า 3 ปีอัตโนมัติ
(ปรับได้ผ่าน System Parameter `fleet_telematics.trip_retention_years`)
**ไม่แตะ Incentive เด็ดขาด** เก็บไว้ตลอดชีพตามที่ FDD กำหนด

## 5. HR Guide

### 5.1 อนุมัติโบนัสประจำเดือน
เมนู **Fleet Telematics > Incentive / Bonus** (เห็นเฉพาะกลุ่ม Fleet Manager
สำหรับดูทุกคน — พนักงานทั่วไปเห็นแค่ของตัวเองผ่าน Record Rule)
1. ตรวจสอบตัวเลข avg_score / bonus_pct / bonus_amount ของแต่ละคน
2. กด **Confirm** → ระบบล็อกตัวเลขและดึง bonus ล่าสุดจาก Backend ครั้งสุดท้าย
3. กด **Approve** → บันทึกชื่อผู้อนุมัติ (เห็นได้ใน Audit Log ใต้ฟอร์ม)
4. กด **Mark as Paid** เมื่อจ่ายเงินจริงแล้ว

> **หลัง Confirm แล้ว ตัวเลขคะแนน/โบนัสจะถูกล็อก แก้ไขไม่ได้** ถ้ากรอกผิด
> ต้องกด **Reset** กลับเป็น Draft ก่อน — การ Reset จะถูกบันทึกใน Audit Log
> เสมอ ถ้าพนักงานได้ Tier D (คะแนนต่ำกว่าเกณฑ์) ระบบจะแจ้งเตือนกลุ่ม
> Fleet Manager อัตโนมัติทั้งทาง chatter และ notification

### 5.2 ตั้งฐานเงินเดือนสำหรับคำนวณโบนัส
ระบบดึงฐานเงินเดือนจากสัญญาจ้างพนักงาน (แท็บ Payroll ในฟอร์มพนักงาน) โดย
อัตโนมัติ — ถ้าไม่มีข้อมูลสัญญาจ้างในระบบเลย ให้กรอกสำรองที่ช่อง
**"🚗 Fleet Telematics — Bonus Calculation"** ในฟอร์มพนักงานแทน (กรอก
ครั้งเดียว ใช้ได้ทุกเดือน)

### 5.3 ดู Dashboard / Scorecard
เมนู **Fleet Telematics > Dashboard** — ดูคะแนนคนขับ, Live Map, รายงานน้ำมัน

### 5.4 พนักงานดูคะแนนตนเอง (UC-11)
พนักงานที่มีบัญชี Portal เข้าที่ `/my/telematics` หรือกดลิงก์ "คะแนนขับขี่
ของฉัน" ในหน้า My Account — เห็นเฉพาะข้อมูลของตัวเองเท่านั้น

## 6. Known Risks / ข้อจำกัดที่ยังเปิดอยู่

รายการเต็มอยู่ที่ `docs/known_risks.md` (มีทั้งที่แก้แล้วและยังไม่แก้)
สรุปเฉพาะที่ยังต้องดำเนินการต่อ:

| ความเสี่ยง | รายละเอียด |
|---|---|
| Test Coverage เป็นตัวเลข % | มี unit test ครอบคลุมทุก UC หลักแล้วเชิงคุณภาพ แต่ยังไม่มีเครื่องมือวัดเป็น % จริง |
| Unique Constraint ของ Incentive | เปลี่ยนจาก `driver_id + period_month + period_year` (ตาม FDD เดิม) เป็น `driver_id + date_from + date_to` เพื่อรองรับรอบตัดวิก — ยังไม่ได้รับการยืนยันจาก Supervisor ว่าให้ใช้ต่อหรือย้อนกลับ |

**ปิดแล้ว:** เอกสาร FDD เคยเขียนอ้างอิง Odoo 17 ไม่ตรงกับโค้ดจริง (Odoo 19)
— บริษัทอนุมัติให้ใช้ Odoo 19 อย่างเป็นทางการแล้ว ไม่ถือเป็นความเสี่ยงอีกต่อไป
(ยังต้องอัปเดตตัวเอกสาร FDD เป็น v2.0 ให้ตรงในภายหลัง แต่ไม่ใช่ blocker)

## 7. โครงสร้างโมดูล

```
fleet_telematics_integration/
├── models/           # Business logic ทั้งหมด
├── wizards/          # Manual Sync Wizard
├── controllers/      # REST endpoints (webhook, live map proxy) + Portal
├── views/            # Form/List/Menu views
├── security/         # Access rights, groups, record rules
├── data/             # Cron jobs (sync ทุก 5 นาที, incentive รายเดือน, data retention รายเดือน)
├── reports/          # QWeb PDF reports
└── tests/            # Unit tests (107 tests ครอบคลุม UC-01, 02, 04, 10, 11, 12,
                       # Maintenance Triggers, Event Lockdown, Trip Wizard,
                       # Vehicle Stats, Data Retention)
```

## 8. การรัน Test

```bash
odoo-bin -c odoo.conf -d <db> --test-enable \
         --test-tags /fleet_telematics_integration --stop-after-init
```

ครอบคลุม: Device Registration, Scoring Config, Trip Sync, Incentive/Bonus
(Workflow + Audit Log + Score Immutability), Portal Data Isolation, Device
Verify/Reconcile, Maintenance Triggers, Event Logs Lockdown, Vehicle Trip
History Wizard, Vehicle Aggregated Stats, Data Retention
