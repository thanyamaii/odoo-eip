-- ==============================================================================
-- docs/pre_upgrade_duplicate_check.sql
--
-- รันก่อน Upgrade โมดูลเสมอ เพราะรอบนี้เพิ่ม _sql_constraints ใหม่ 2 ตัว:
--   1) fleet_telematics_incentive: UNIQUE(driver_id, period_month, period_year)
--   2) fleet_telematics_device:    UNIQUE(device_id)
-- ถ้ามีข้อมูลซ้ำอยู่แล้วในฐานข้อมูลปัจจุบัน Odoo จะ Upgrade ไม่ผ่าน
-- (PostgreSQL ปฏิเสธสร้าง constraint ทันทีที่เจอแถวซ้ำ)
--
-- วิธีใช้: รันผ่าน psql หรือ pgAdmin เชื่อมต่อ database ของ Odoo ก่อน
-- Upgrade โมดูล ถ้า query ไหนคืนแถวมา แปลว่าต้องจัดการก่อน
-- ==============================================================================

-- ── เช็คที่ 1: Incentive ซ้ำ (driver_id, period_month, period_year) ──────────
SELECT
    driver_id,
    period_month,
    period_year,
    COUNT(*)            AS duplicate_count,
    array_agg(id ORDER BY id) AS record_ids
FROM fleet_telematics_incentive
GROUP BY driver_id, period_month, period_year
HAVING COUNT(*) > 1
ORDER BY duplicate_count DESC;

-- ถ้าเจอแถว: ต้องตัดสินใจว่าจะเก็บ record ไหนไว้ (โดยทั่วไปเก็บ id ล่าสุด/state
-- ที่ก้าวหน้าที่สุด เช่น paid > approved > confirmed > draft) แล้วลบที่เหลือ
-- ตัวอย่างลบ (ปรับ record_ids ตามผลจริงจาก query ด้านบนก่อนรัน — ห้ามรันเดา):
--
-- DELETE FROM fleet_telematics_incentive WHERE id IN (<id ที่ไม่ต้องการ>);


-- ── เช็คที่ 2: Device ID ซ้ำ ──────────────────────────────────────────────
SELECT
    device_id,
    COUNT(*)                  AS duplicate_count,
    array_agg(id ORDER BY id) AS record_ids
FROM fleet_telematics_device
GROUP BY device_id
HAVING COUNT(*) > 1
ORDER BY duplicate_count DESC;

-- หมายเหตุ: ตาราง fleet_telematics_device เพิ่งถูก "เปิดใช้งานจริง" ในรอบนี้
-- (เดิมโมเดลไม่เคยถูก import เข้าระบบเลย) ถ้าไม่มีใครเคยสร้างข้อมูลผ่าน ORM
-- มาก่อน ตารางนี้น่าจะว่างเปล่าอยู่แล้ว เช็คไว้เผื่อมีคนยัดข้อมูลผ่าน SQL ตรงๆ


-- ── เช็คที่ 3 (เผื่อไว้): external_trip_id ซ้ำใน trip log ──────────────────
-- (constraint นี้มีอยู่แล้วในโมดูลเดิม ไม่ใช่ของใหม่ แต่เช็คคู่กันไปด้วยเลย
--  เผื่อมีข้อมูลทดสอบที่หลุดเข้ามาซ้ำจากตอน dev/debug ก่อนหน้านี้)
SELECT
    external_trip_id,
    COUNT(*) AS duplicate_count,
    array_agg(id ORDER BY id) AS record_ids
FROM fleet_telematics_log
GROUP BY external_trip_id
HAVING COUNT(*) > 1;
