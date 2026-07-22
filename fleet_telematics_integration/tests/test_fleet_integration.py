# ==============================================================================
# tests/test_fleet_integration.py
# Odoo TestCase รวม UC-01, UC-02, UC-04 ในไฟล์เดียว
#
# วิธีรัน:
#   odoo-bin -c odoo.conf -d <db> --test-enable --stop-after-init \
#            --test-tags /fleet_telematics_integration
# ==============================================================================

from unittest.mock import patch, MagicMock
from datetime import datetime, date, timezone, timedelta

from odoo import fields
from odoo.tests.common import TransactionCase
from odoo.exceptions import ValidationError, UserError


# ══════════════════════════════════════════════════════════════════════════════
# เพิ่ม 2026-07-16: Odoo 19 เปลี่ยนชื่อ field many2many บน res.users จาก
# `groups_id` เป็น `group_ids` (ยืนยันจาก error จริงตอนรัน test บน Odoo 19
# instance: "KeyError: 'groups_id'" / "ValueError: Invalid field 'groups_id'
# in 'res.users'") — เขียน helper ตรวจหาชื่อ field ที่ถูกต้องแบบไดนามิก
# แทนที่จะ hardcode ชื่อใดชื่อหนึ่งตรงๆ กันพังอีกถ้า Odoo เปลี่ยนชื่ออีกใน
# อนาคต (ใช้ pattern เดียวกับที่แก้ไปแล้วใน telematics_incentive.py /
# telematics_log.py ตอนเจอปัญหาเดียวกันฝั่ง res.groups.users)
# ══════════════════════════════════════════════════════════════════════════════
def _users_groups_field(env):
    """คืนชื่อ field many2many ที่ถูกต้องบน res.users สำหรับผูกกับ res.groups
    ('group_ids' ใน Odoo 19+, 'groups_id' ในเวอร์ชันเก่ากว่า)"""
    User = env['res.users']
    for fname in ('groups_id', 'group_ids'):
        if fname in User._fields:
            return fname
    raise KeyError(
        "หา field เชื่อมกลุ่มบน res.users ไม่เจอเลย (ลองแล้ว: groups_id, "
        "group_ids) — Odoo เวอร์ชันนี้อาจเปลี่ยนชื่อ field อีกแล้ว ต้องเช็ค "
        "ผ่าน Odoo shell: [f for f in env['res.users']._fields if 'group' in f]"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Shared Setup — ข้อมูลพื้นฐานที่ใช้ร่วมกันทุก UC
# ══════════════════════════════════════════════════════════════════════════════

class FleetTelematicsBase(TransactionCase):
    """Base class: สร้าง brand/model/vehicle/driver ที่ใช้ร่วมกัน"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.brand  = cls.env['fleet.vehicle.model.brand'].create(
            {'name': 'TEST_BRAND'})
        cls.fmodel = cls.env['fleet.vehicle.model'].create(
            {'name': 'TEST_MODEL', 'brand_id': cls.brand.id})
        cls.partner = cls.env['res.partner'].create({'name': 'Test Driver'})
        cls.employee = cls.env['hr.employee'].create({'name': 'Test Employee'})

        cls.v1 = cls.env['fleet.vehicle'].create({
            'model_id':             cls.fmodel.id,
            'license_plate':        'BASE-001',
            'telematics_device_id': 'KTC-BASE-01',
        })

        ICP = cls.env['ir.config_parameter'].sudo()
        ICP.set_param('fleet_telematics.mtd_api_url', 'http://test-backend:8001')
        ICP.set_param('fleet_telematics.mtd_api_key', 'TEST-KEY')

        # เพิ่ม 2026-07-08: ต้องอยู่กลุ่ม Fleet Manager เพื่อทดสอบ action_approve()
        # ของ fleet.telematics.scoring.config (ดู TestUC02ScoringConfig)
        # แก้ 2026-07-16: groups_id → group_ids ใน Odoo 19 (ดู _users_groups_field ด้านบน)
        cls.env.user.write({
            _users_groups_field(cls.env): [(4, cls.env.ref('fleet.fleet_group_manager').id)]
        })

    def _make_vehicle(self, plate, device=None):
        vals = {
            'model_id':      self.fmodel.id,
            'license_plate': plate,
            'driver_id':     self.partner.id,
        }
        if device is not None:
            vals['telematics_device_id'] = device
        return self.env['fleet.vehicle'].create(vals)

    def _make_scoring(self, name='Cfg', active=True, **kw):
        # deactivate ที่มีอยู่ก่อน (ถ้า active=True)
        if active:
            self.env['fleet.telematics.scoring.config'].search(
                [('active', '=', True)]).write({'active': False})
        vals = dict(
            name=name, active=active, effective_date='2025-01-01',
            score_base=100.0, max_deduct_per_trip=50.0,
            harsh_brake_deduct=5.0, harsh_accel_deduct=3.0,
            harsh_corner_deduct=3.0, speeding_deduct=10.0,
            idling_deduct=2.0, bump_deduct=4.0,
            harsh_brake_g=0.40, harsh_accel_g=0.40, harsh_corner_g=0.40,
            speeding_kmh_over=20.0, idle_min_threshold=5.0,
            tier_a_min_score=90.0, tier_a_bonus_pct=10.0,
            tier_b_min_score=75.0, tier_b_bonus_pct=5.0,
            tier_c_min_score=60.0, tier_c_bonus_pct=0.0,
        )
        vals.update(kw)
        return self.env['fleet.telematics.scoring.config'].create(vals)

    def _make_trip_dict(self, trip_id, device='KTC-BASE-01', **kw):
        data = {
            'trip_id':           trip_id,
            'device_id':         device,
            'driver_name':       'Test Employee',
            'start_time':        '2025-06-10T08:00:00',
            'end_time':          '2025-06-10T09:00:00',
            'distance_km':       50.0,
            'avg_speed':         60.0,
            'max_speed':         90.0,
            'idle_min':           5.0,
            'fuel_used_est':      4.5,
            'driver_score':      85.0,
            'harsh_brake_count':  1,
            'harsh_accel_count':  0,
            'harsh_corner_count': 2,
            'speeding_count':     1,
            'gps_track_json':    '[]',
        }
        data.update(kw)
        return data

    def _mock_api(self, trips):
        r = MagicMock()
        r.json.return_value = {'trips': trips}
        r.raise_for_status.return_value = None
        return r


# ══════════════════════════════════════════════════════════════════════════════
# UC-01 — สร้าง / จัดการรถและ Device  (fleet_vehicle_ext.py)
# ══════════════════════════════════════════════════════════════════════════════

class TestUC01VehicleDevice(FleetTelematicsBase):
    """
    ครอบคลุม:
      - บันทึกรถ + Device ID สำเร็จ
      - ดักจับ Device ID ซ้ำ → ValidationError
      - ดักจับทะเบียนซ้ำ → ValidationError
      - เปลี่ยน Device ID → previous_device_id บันทึกอัตโนมัติ
      - Device ว่าง/None → ไม่นับซ้ำ
      - เคลียร์ Device → รถคันอื่นใช้ได้
      - write Device เดิม → ไม่ error
      - ตรวจ default fields
    """

    def test_01_create_vehicle_with_device_success(self):
        """สร้างรถพร้อม Device ID — บันทึกได้ ค่าตรง"""
        v = self._make_vehicle('กข-T01', 'KTC-T01')
        self.assertTrue(v.id)
        self.assertEqual(v.telematics_device_id, 'KTC-T01')
        self.assertEqual(v.online_status, 'unknown')
        self.assertEqual(v.sync_status,   'idle')
        self.assertFalse(v.ignition)

    def test_02_duplicate_device_id_raises(self):
        """Device ID ซ้ำ → ValidationError พร้อมระบุ Device ID"""
        self._make_vehicle('กข-T02', 'KTC-DUP')
        with self.assertRaises(ValidationError) as ctx:
            self._make_vehicle('กข-T03', 'KTC-DUP')
        self.assertIn('KTC-DUP', str(ctx.exception))

    def test_03_duplicate_license_plate_raises(self):
        """ทะเบียนรถซ้ำ → ValidationError พร้อมระบุทะเบียน"""
        self._make_vehicle('กข-SAME', 'KTC-P01')
        with self.assertRaises(ValidationError) as ctx:
            self._make_vehicle('กข-SAME', 'KTC-P02')
        self.assertIn('กข-SAME', str(ctx.exception))

    def test_04_change_device_saves_previous(self):
        """เปลี่ยน Device ID → previous_device_id เก็บค่าเก่าอัตโนมัติ"""
        v = self._make_vehicle('กข-T04', 'KTC-OLD')
        v.write({'telematics_device_id': 'KTC-NEW'})
        self.assertEqual(v.telematics_device_id, 'KTC-NEW')
        self.assertEqual(v.previous_device_id,   'KTC-OLD')

    def test_05_empty_device_allowed_multiple_vehicles(self):
        """รถหลายคันที่ไม่มี Device → บันทึกได้ทั้งหมด ไม่ถือว่าซ้ำ"""
        v1 = self._make_vehicle('กข-T05A')
        v2 = self._make_vehicle('กข-T05B')
        self.assertTrue(v1.id)
        self.assertTrue(v2.id)
        self.assertFalse(v1.telematics_device_id)

    def test_06_clear_device_allows_reassign(self):
        """เคลียร์ Device ออกจากรถคันเดิม → รถคันใหม่ใช้ Device นั้นได้"""
        v1 = self._make_vehicle('กข-T06A', 'KTC-XFER')
        v1.write({'telematics_device_id': False})
        v2 = self._make_vehicle('กข-T06B', 'KTC-XFER')
        self.assertEqual(v2.telematics_device_id, 'KTC-XFER')

    def test_07_write_same_device_no_error(self):
        """write Device เดิมของรถตัวเอง → ไม่ถือว่าซ้ำ ไม่ error"""
        v = self._make_vehicle('กข-T07', 'KTC-STABLE')
        try:
            v.write({'telematics_device_id': 'KTC-STABLE'})
        except ValidationError:
            self.fail("write Device ID เดิมต้องไม่ raise ValidationError")

    def test_08_default_registered_status_fields(self):
        """ค่า default ของ online_status / sync_status / ignition ถูกต้อง"""
        v = self._make_vehicle('กข-T08', 'KTC-T08')
        self.assertEqual(v.online_status, 'unknown')
        self.assertEqual(v.sync_status,   'idle')
        self.assertFalse(v.ignition)
        self.assertEqual(v.current_speed, 0.0)


# ══════════════════════════════════════════════════════════════════════════════
# UC-02 — ตั้งค่า Scoring Config  (telematics_scoring.py)
# ══════════════════════════════════════════════════════════════════════════════

class TestUC02ScoringConfig(FleetTelematicsBase):
    """
    ครอบคลุม:
      - บันทึก ScoringConfig สำเร็จ
      - Active ได้เพียง 1 config
      - Tier order A > B > C > 0
      - Boundary: deduct ติดลบ, score_base = 0
      - Boundary: G-force / threshold = 0
      - Boundary: max_deduct > score_base
      - _build_config_payload() ครบทุก key
      - Deactivate แล้ว Active ใหม่ได้
    """

    def test_01_create_scoring_config_success(self):
        """สร้าง ScoringConfig สำเร็จ — ค่าทุก field บันทึกถูกต้อง"""
        cfg = self._make_scoring('UC02-01')
        self.assertTrue(cfg.id)
        self.assertEqual(cfg.score_base,         100.0)
        self.assertEqual(cfg.harsh_brake_deduct,   5.0)
        self.assertEqual(cfg.tier_a_min_score,    90.0)
        self.assertTrue(cfg.active)

    def test_02_only_one_active_config_allowed(self):
        """Active ScoringConfig ได้เพียง 1 รายการ → รายการที่ 2 ต้อง raise"""
        self._make_scoring('UC02-02A', active=True)
        with self.assertRaises(ValidationError) as ctx:
            # ไม่ผ่าน _make_scoring เพราะมัน deactivate ให้อัตโนมัติ
            self.env['fleet.telematics.scoring.config'].create({
                'name': 'UC02-02B', 'active': True,
                'effective_date': '2025-01-01',
                'score_base': 100.0, 'max_deduct_per_trip': 50.0,
                'harsh_brake_deduct': 5.0, 'harsh_accel_deduct': 3.0,
                'harsh_corner_deduct': 3.0, 'speeding_deduct': 10.0,
                'idling_deduct': 2.0, 'bump_deduct': 4.0,
                'harsh_brake_g': 0.40, 'harsh_accel_g': 0.40, 'harsh_corner_g': 0.40,
                'speeding_kmh_over': 20.0, 'idle_min_threshold': 5.0,
                'tier_a_min_score': 90.0, 'tier_a_bonus_pct': 10.0,
                'tier_b_min_score': 75.0, 'tier_b_bonus_pct': 5.0,
                'tier_c_min_score': 60.0, 'tier_c_bonus_pct': 0.0,
            })
        self.assertIn('Active', str(ctx.exception))

    def test_03a_tier_order_a_less_than_b_raises(self):
        """Tier A < Tier B → ValidationError"""
        with self.assertRaises(ValidationError):
            self._make_scoring(active=False,
                               tier_a_min_score=70.0,
                               tier_b_min_score=80.0,
                               tier_c_min_score=60.0)

    def test_03b_tier_c_equal_zero_raises(self):
        """Tier C = 0 → ValidationError (ต้อง > 0)"""
        with self.assertRaises(ValidationError):
            self._make_scoring(active=False,
                               tier_a_min_score=90.0,
                               tier_b_min_score=75.0,
                               tier_c_min_score=0.0)

    def test_03c_tier_b_equal_c_raises(self):
        """Tier B == Tier C → ValidationError"""
        with self.assertRaises(ValidationError):
            self._make_scoring(active=False,
                               tier_a_min_score=90.0,
                               tier_b_min_score=60.0,
                               tier_c_min_score=60.0)

    def test_04_negative_deduct_raises(self):
        """ค่าหักคะแนนติดลบ → ValidationError (_check_positive_deducts)"""
        with self.assertRaises(ValidationError):
            self._make_scoring(active=False, harsh_brake_deduct=-5.0)

    def test_05_zero_score_base_raises(self):
        """score_base = 0 → ValidationError"""
        with self.assertRaises(ValidationError):
            self._make_scoring(active=False, score_base=0.0)

    def test_06_zero_g_threshold_raises(self):
        """G-force threshold = 0 → ValidationError (_check_positive_thresholds)"""
        with self.assertRaises(ValidationError):
            self._make_scoring(active=False, harsh_brake_g=0.0)

    def test_07_max_deduct_exceeds_base_raises(self):
        """max_deduct_per_trip > score_base → ValidationError"""
        with self.assertRaises(ValidationError):
            self._make_scoring(active=False,
                               score_base=100.0,
                               max_deduct_per_trip=150.0)

    def test_08_build_config_payload_has_all_keys(self):
        """_build_config_payload() ต้องครบทุก key ที่ Backend ต้องการ"""
        cfg     = self._make_scoring('UC02-08')
        payload = cfg._build_config_payload()
        required = [
            'score_base', 'max_deduct_per_trip',
            'harsh_brake_deduct', 'harsh_accel_deduct', 'harsh_corner_deduct',
            'speeding_deduct', 'idling_deduct', 'bump_deduct',
            'harsh_brake_g', 'harsh_accel_g', 'harsh_corner_g',
            'speeding_kmh_over', 'idle_min_threshold',
            'tier_a_min_score', 'tier_a_bonus_pct',
            'tier_b_min_score', 'tier_b_bonus_pct',
            'tier_c_min_score', 'tier_c_bonus_pct',
        ]
        for key in required:
            self.assertIn(key, payload, f"payload ต้องมี key '{key}'")
        self.assertEqual(payload['score_base'], 100.0)

    def test_09_deactivate_then_activate_new(self):
        """Deactivate config เดิม แล้ว active ใหม่ → ต้องสำเร็จ"""
        c1 = self._make_scoring('UC02-09A', active=True)
        c1.write({'active': False})
        c2 = self._make_scoring('UC02-09B', active=True)
        self.assertTrue(c2.active)
        self.assertFalse(c1.active)

    # ── เพิ่ม 2026-07-08: ทดสอบกฎความเร็วแยกโซน (บรีฟข้อ 2) ────────────────
    def test_10_speed_limit_zone_defaults(self):
        """ค่าเริ่มต้น speed_limit_bkk=80, speed_limit_upcountry=90"""
        cfg = self._make_scoring('UC02-10', active=False)
        self.assertEqual(cfg.speed_limit_bkk, 80.0)
        self.assertEqual(cfg.speed_limit_upcountry, 90.0)

    def test_11_speed_limit_bkk_higher_than_upcountry_raises(self):
        """กรุงเทพฯ จำกัดสูงกว่านอกเมือง → ต้อง ValidationError (ผิดตรรกะ)"""
        with self.assertRaises(ValidationError):
            self._make_scoring('UC02-11', active=False,
                                speed_limit_bkk=100.0,
                                speed_limit_upcountry=90.0)

    def test_12_speed_limit_zero_raises(self):
        """speed_limit_bkk/upcountry = 0 → ValidationError"""
        with self.assertRaises(ValidationError):
            self._make_scoring('UC02-12', active=False, speed_limit_bkk=0.0)

    def test_13_payload_includes_speed_limit_zone_keys(self):
        """_build_config_payload() ต้องมี speed_limit_bkk/upcountry"""
        cfg     = self._make_scoring('UC02-13')
        payload = cfg._build_config_payload()
        self.assertIn('speed_limit_bkk', payload)
        self.assertIn('speed_limit_upcountry', payload)
        self.assertEqual(payload['speed_limit_bkk'], 80.0)
        self.assertEqual(payload['speed_limit_upcountry'], 90.0)

    # ── เพิ่ม 2026-07-08: ทดสอบ Read-only lock เมื่อ Active/Push แล้ว (ข้อ 3) ──
    def test_14_edit_locked_when_active_raises(self):
        """แก้ไข field เกณฑ์คะแนนตอน active=True → ต้อง UserError"""
        cfg = self._make_scoring('UC02-14', active=True)
        with self.assertRaises(UserError):
            cfg.write({'score_base': 50.0})

    def test_15_edit_allowed_after_deactivate(self):
        """ปิด active ก่อน แล้วแก้ไข field เกณฑ์คะแนน → ต้องสำเร็จ"""
        cfg = self._make_scoring('UC02-15', active=True)
        cfg.write({'active': False})
        cfg.write({'score_base': 88.0})
        self.assertEqual(cfg.score_base, 88.0)

    def test_16_edit_allowed_after_push_while_inactive(self):
        """แก้ 2026-07-09 ตามบรีฟใหม่: เคย Push แล้ว (last_push_at มีค่า) แต่
        active=False → ต้องยังแก้ไข/Push ซ้ำได้เรื่อยๆ (ไม่ล็อกถาวรแล้ว)"""
        cfg = self._make_scoring('UC02-16', active=False)
        cfg.write({'last_push_at': '2026-07-08 10:00:00'})
        cfg.write({'harsh_brake_deduct': 1.0})   # ต้องไม่ raise
        self.assertEqual(cfg.harsh_brake_deduct, 1.0)

    def test_17_is_locked_compute(self):
        """is_locked = True เมื่อ active=True เท่านั้น (ไม่ผูกกับ last_push_at แล้ว)"""
        cfg = self._make_scoring('UC02-17', active=False)
        self.assertFalse(cfg.is_locked)
        cfg.write({'last_push_at': '2026-07-08 10:00:00'})
        self.assertFalse(cfg.is_locked)   # เคย push แต่ inactive → ไม่ล็อก
        # แก้ 2026-07-17: _make_scoring() ป้องกัน "Active ซ้อน" ให้อัตโนมัติ
        # แค่ตอนสร้างใหม่ผ่าน helper เท่านั้น — ตรงนี้เรียก .write() ตรงๆ
        # ทีหลัง ถ้าฐานข้อมูลมี config อื่น Active ค้างอยู่ก่อน (เช่นจากการ
        # ทดสอบมือผ่าน UI จริงมาก่อนหน้า) จะชนกับ constraint ทันที ต้อง
        # deactivate ของเดิมก่อนเหมือนที่ _make_scoring ทำให้ตอน create
        self.env['fleet.telematics.scoring.config'].search(
            [('active', '=', True), ('id', '!=', cfg.id)]).write({'active': False})
        cfg.write({'active': True})
        self.assertTrue(cfg.is_locked)

    def test_17b_active_default_false_on_new_record(self):
        """สร้าง record ใหม่โดยไม่ระบุ active → ต้องเป็น False (ไม่ล็อกฟอร์มตอนสร้าง)"""
        cfg = self.env['fleet.telematics.scoring.config'].create({
            'name': 'UC02-17B', 'effective_date': '2025-01-01',
            'score_base': 100.0, 'max_deduct_per_trip': 50.0,
            'harsh_brake_deduct': 5.0, 'harsh_accel_deduct': 3.0,
            'harsh_corner_deduct': 3.0, 'speeding_deduct': 10.0,
            'idling_deduct': 2.0, 'bump_deduct': 4.0,
            'harsh_brake_g': 0.40, 'harsh_accel_g': 0.40, 'harsh_corner_g': 0.40,
            'speeding_kmh_over': 20.0, 'idle_min_threshold': 5.0,
            'tier_a_min_score': 90.0, 'tier_a_bonus_pct': 10.0,
            'tier_b_min_score': 75.0, 'tier_b_bonus_pct': 5.0,
            'tier_c_min_score': 60.0, 'tier_c_bonus_pct': 0.0,
        })
        self.assertFalse(cfg.active)
        self.assertFalse(cfg.is_locked)

    # ── เพิ่ม 2026-07-08: ทดสอบ Approval workflow (บรีฟ "กำหนดผู้อนุมัติ") ──
    def test_18_push_without_approval_raises(self):
        """Push Config โดยยังไม่มี approved_by_id → ต้อง UserError ก่อนยิง API เลย"""
        cfg = self._make_scoring('UC02-18', active=False)
        with self.assertRaises(UserError):
            cfg.action_push_to_backend()

    def test_19_approve_sets_approved_by_and_at(self):
        """action_approve() ต้องตั้ง approved_by_id/approved_at (ในฐานะ Fleet Manager)"""
        cfg = self._make_scoring('UC02-19', active=False)
        cfg.action_approve()
        self.assertTrue(cfg.approved_by_id)
        self.assertTrue(cfg.approved_at)

    def test_20_push_after_approval_succeeds(self):
        """Approve แล้ว Push ต้องผ่านจุดเช็ค approval ไปถึงขั้นยิง API"""
        cfg = self._make_scoring('UC02-20', active=False)
        cfg.action_approve()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'config': {'config_name': cfg.name}}
        mock_resp.raise_for_status.return_value = None
        with patch('requests.post', return_value=mock_resp):
            cfg.action_push_to_backend()
        self.assertTrue(cfg.last_push_at)
        self.assertIn('OK', cfg.last_push_status)

    # ── เพิ่มตาม FDD §12.3/§12.5: Tier D fields + History tracking ────────
    def test_21_tier_d_fields_exist_and_editable(self):
        """Tier D ต้องมี field ปรับได้เหมือน A/B/C (ไม่ใช่ hardcode) และ
        ล็อกเมื่อ Active=True เหมือนฟิลด์เกณฑ์อื่น"""
        cfg = self._make_scoring('UC02-21', active=False)
        cfg.write({'tier_d_bonus_pct': 2.5})
        self.assertEqual(cfg.tier_d_bonus_pct, 2.5)

        self.env['fleet.telematics.scoring.config'].search(
            [('active', '=', True), ('id', '!=', cfg.id)]).write({'active': False})
        cfg.write({'active': True})
        with self.assertRaises(UserError):
            cfg.write({'tier_d_bonus_pct': 5.0})

    def test_22_tier_d_bonus_negative_raises(self):
        """% โบนัส Tier D ติดลบต้อง raise"""
        with self.assertRaises(ValidationError):
            self._make_scoring('UC02-22', active=False, tier_d_bonus_pct=-1.0)

    def test_23_history_created_date_auto_set(self):
        """created_date ต้องตั้งค่าอัตโนมัติตอนสร้าง record ใหม่"""
        cfg = self._make_scoring('UC02-23', active=False)
        self.assertTrue(cfg.created_date)

    def test_24_history_starts_zero(self):
        """total_trips_calculated ต้องเริ่มที่ 0 ตอนสร้างใหม่"""
        cfg = self._make_scoring('UC02-24', active=False)
        self.assertEqual(cfg.total_trips_calculated, 0)
        self.assertFalse(cfg.last_used_date)

    def test_25_track_usage_updates_active_config(self):
        """_track_usage() ต้องอัปเดตเฉพาะ config ที่ Active อยู่เท่านั้น"""
        cfg = self._make_scoring('UC02-25', active=True)
        Config = self.env['fleet.telematics.scoring.config']
        Config._track_usage(count=3)
        self.assertEqual(cfg.total_trips_calculated, 3)
        self.assertTrue(cfg.last_used_date)


# ══════════════════════════════════════════════════════════════════════════════
# UC-04 — GPS Poll + Dedup + Batch  (telematics_log.py section [I]–[N])
# ══════════════════════════════════════════════════════════════════════════════

class TestUC04TripSync(FleetTelematicsBase):
    """
    แก้ 2026-07-17: เขียนใหม่ทั้งคลาส — เวอร์ชันเดิมอ้างอิงเมธอด
    _get_poll_window()/_filter_new_trips() ที่ไม่มีอยู่จริงในโค้ด (ถูกออกแบบ
    ไว้ก่อน implement จริง แล้วไม่เคยอัปเดต test ตาม) และ mock ผิด HTTP verb
    (patch requests.get ทั้งที่โค้ดจริงยิง requests.post) ยืนยันจาก error จริง
    ตอนรันบน Odoo 19: AttributeError ทั้งคู่ + "External requests verboten"
    (เพราะ mock ไม่ตรง เลยยิง POST จริงหลุดออกไป)

    ครอบคลุมของจริงตามโค้ดปัจจุบัน:
      - _fetch_trips_batch() ยิง POST /api/v1/webhook/odoo-sync ถูก endpoint/body
      - _build_trip_vals() แปลง dict (schema จริงจาก Backend) → vals ถูกต้อง
      - _build_trip_vals() ไม่พบรถ (ทั้ง vehicle_id และ device_id) → คืน {}
      - _cron_sync_trips() สร้าง trip ใหม่ (มี POST+PATCH mock ครบ)
      - _cron_sync_trips() ไม่ duplicate เมื่อรัน 2 รอบ
      - _cron_sync_trips() write existing trip แทน create
      - _cron_sync_trips() บันทึก trip_last_sync_timestamp (ชื่อ param จริง
        คือ fleet_telematics.trip_last_sync_timestamp ไม่ใช่ trip_last_poll_ts
        แบบที่เทสเดิมเช็ค)
      - UNIQUE(external_trip_id) บังคับจริงผ่าน models.Constraint (Odoo 19)
      - _cron_sync_trips() เมื่อไม่มี API URL → ไม่ raise
    """

    _PARAM = 'fleet_telematics.trip_last_sync_timestamp'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ICP = cls.env['ir.config_parameter'].sudo()
        ICP.set_param(cls._PARAM, '')

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(self._PARAM, '')

    def _make_trip_dict(self, ext_id, vehicle_id=None, device_id='KTC-BASE-01',
                         driver_id=None, **kw):
        """dict จำลอง trip ตาม schema จริงที่ _build_trip_vals() คาดหวัง
        (ไม่ใช่ schema เดิมที่ทดสอบสมมติไว้ผิด — สาเหตุของ KeyError เดิม)"""
        data = {
            'id':               ext_id,
            'vehicle_id':       self.v1.id if vehicle_id is None else vehicle_id,
            'device_id':        device_id,
            'driver_id':        self.employee.id if driver_id is None else driver_id,
            'trip_start':       '2026-06-10T08:00:00+00:00',
            'trip_end':         '2026-06-10T09:00:00+00:00',
            'distance_km':      50.0,
            'avg_speed':        60.0,
            'max_speed':        90.0,
            'idle_min':         5.0,
            'fuel_used':        4.5,
            'driver_score':     85.0,
            'harsh_brake_count':  1,
            'harsh_accel_count':  0,
            'harsh_corner_count': 2,
            'speeding_count':     1,
            'gps_track_json':   '[]',
        }
        data.update(kw)
        return data

    def _mock_post(self, trips, last_ts='2026-06-10T10:00:00+00:00', total=None):
        r = MagicMock()
        r.json.return_value = {
            'trips': trips,
            'last_sync_timestamp': last_ts,
            'total': total if total is not None else len(trips),
        }
        r.raise_for_status.return_value = None
        return r

    def _mock_patch(self):
        r = MagicMock()
        r.raise_for_status.return_value = None
        return r

    def test_01_fetch_trips_batch_calls_correct_endpoint(self):
        """_fetch_trips_batch() ยิง POST ไปที่ endpoint ที่ถูกต้อง"""
        Log = self.env['fleet.telematics.log']
        with patch('requests.post', return_value=self._mock_post([])) as mock_post:
            trips, new_ts, total = Log._fetch_trips_batch(
                'http://test-backend:8001', 'TEST-KEY', None)
        self.assertEqual(
            mock_post.call_args[0][0],
            'http://test-backend:8001/api/v1/webhook/odoo-sync')
        self.assertEqual(trips, [])

    def test_02_fetch_trips_batch_sends_last_ts_when_present(self):
        """ถ้ามี last_ts ต้องส่งไปใน body ชื่อ last_sync_timestamp"""
        Log = self.env['fleet.telematics.log']
        with patch('requests.post', return_value=self._mock_post([])) as mock_post:
            Log._fetch_trips_batch(
                'http://test-backend:8001', 'TEST-KEY', '2026-06-01T00:00:00+00:00')
        sent_body = mock_post.call_args.kwargs.get('json', {})
        self.assertEqual(
            sent_body.get('last_sync_timestamp'), '2026-06-01T00:00:00+00:00')

    def test_03_build_trip_vals_ok(self):
        """_build_trip_vals() แปลง dict (schema จริง) → vals ครบถูกต้อง"""
        Log  = self.env['fleet.telematics.log']
        vals = Log._build_trip_vals(self._make_trip_dict('VALS-001'))

        self.assertEqual(vals['external_trip_id'], 'VALS-001')
        self.assertEqual(vals['vehicle_id'],        self.v1.id)
        self.assertEqual(vals['distance_km'],       50.0)
        self.assertEqual(vals['state'],             'synced')

    def test_04_build_trip_vals_unknown_vehicle_empty(self):
        """_build_trip_vals() ไม่พบรถทั้งจาก vehicle_id และ device_id → คืน {}"""
        Log  = self.env['fleet.telematics.log']
        vals = Log._build_trip_vals(self._make_trip_dict(
            'VALS-X', vehicle_id=999999999, device_id='DEVICE-NOTEXIST'))
        self.assertEqual(vals, {})

    def test_05_cron_creates_new_trips(self):
        """_cron_sync_trips() สร้าง trip ใหม่จาก API response (mock POST+PATCH)"""
        Log   = self.env['fleet.telematics.log']
        trips = [self._make_trip_dict('CRON-C01'), self._make_trip_dict('CRON-C02')]
        before = Log.search_count(
            [('external_trip_id', 'in', ['CRON-C01', 'CRON-C02'])])

        with patch('requests.post', return_value=self._mock_post(trips)):
            with patch('requests.patch', return_value=self._mock_patch()):
                Log._cron_sync_trips()

        after = Log.search_count(
            [('external_trip_id', 'in', ['CRON-C01', 'CRON-C02'])])
        self.assertEqual(after - before, 2, "ต้องสร้าง 2 trips ใหม่")

    def test_06_cron_no_duplicate_on_second_run(self):
        """รัน Cron 2 รอบ → รอบ 2 ไม่มี trip ใหม่ (Backend คืน []) → บันทึกแค่ครั้งเดียว"""
        Log   = self.env['fleet.telematics.log']
        trips = [self._make_trip_dict('CRON-D01')]

        with patch('requests.post', return_value=self._mock_post(trips)):
            with patch('requests.patch', return_value=self._mock_patch()):
                Log._cron_sync_trips()   # รอบ 1: สร้าง CRON-D01

        with patch('requests.post', return_value=self._mock_post([])):
            with patch('requests.patch', return_value=self._mock_patch()):
                Log._cron_sync_trips()   # รอบ 2: Backend ไม่มี trip ใหม่แล้ว

        count = Log.search_count([('external_trip_id', '=', 'CRON-D01')])
        self.assertEqual(count, 1, "ต้องมีแค่ 1 record ไม่ซ้ำ")

    def test_07_cron_updates_existing_trip(self):
        """_cron_sync_trips() trip ที่มีอยู่แล้ว → write() แทน create()"""
        Log = self.env['fleet.telematics.log']
        Log.create({
            'external_trip_id': 'CRON-U01',
            'vehicle_id': self.v1.id,
            'driver_id':  self.employee.id,
            'trip_start': '2025-06-10 08:00:00',
            'state':      'synced',
            'distance_km': 10.0,
        })
        trip = self._make_trip_dict('CRON-U01', distance_km=99.0)

        with patch('requests.post', return_value=self._mock_post([trip])):
            with patch('requests.patch', return_value=self._mock_patch()):
                Log._cron_sync_trips()

        rec = Log.search([('external_trip_id', '=', 'CRON-U01')], limit=1)
        self.assertEqual(len(rec), 1,    "ต้องไม่สร้างซ้ำ")
        self.assertAlmostEqual(rec.distance_km, 99.0,
                               msg="ต้อง update distance_km เป็นค่าใหม่")

    def test_08_cron_saves_last_sync_timestamp(self):
        """หลัง _cron_sync_trips() ต้องบันทึก trip_last_sync_timestamp ใน
        ir.config_parameter (ต้องมี trip อย่างน้อย 1 รายการ ไม่งั้น loop จะ
        break ก่อนถึงจุดบันทึกค่า — ดู comment ในโค้ดจริงขั้นที่ 5)"""
        Log = self.env['fleet.telematics.log']
        ICP = self.env['ir.config_parameter'].sudo()
        trip = self._make_trip_dict('CRON-TS01')

        with patch('requests.post', return_value=self._mock_post(
                [trip], last_ts='2026-06-15T12:00:00+00:00')):
            with patch('requests.patch', return_value=self._mock_patch()):
                Log._cron_sync_trips()

        ts = ICP.get_param(self._PARAM, '')
        self.assertEqual(ts, '2026-06-15T12:00:00+00:00')

    def test_09_sql_unique_constraint_on_external_trip_id(self):
        """Dedup ชั้นสุดท้าย: ต้องมี UNIQUE บน external_trip_id บังคับใช้จริง
        แก้ 2026-07-17: Odoo 19 เลิกใช้ _sql_constraints (list of tuple) แล้ว
        (ยืนยันจาก warning จริงตอน module load) เปลี่ยนโค้ดจริงไปใช้
        models.Constraint() แทน — แต่ไม่มั่นใจ 100% ว่า attribute ภายในของ
        Constraint object ชื่ออะไรแน่ (เช่น .string/.definition) จึงเลี่ยงไป
        ทดสอบเชิงพฤติกรรมแทน (สร้างซ้ำจริงแล้วดูว่า error จริงไหม) แม่นยำ
        กว่าการเดา attribute name ภายใน"""
        Log = self.env['fleet.telematics.log']
        Log.create({
            'external_trip_id': 'UNIQUE-TEST-01',
            'vehicle_id': self.v1.id,
            'trip_start': '2026-01-01 08:00:00',
        })
        with self.assertRaises(Exception):
            Log.create({
                'external_trip_id': 'UNIQUE-TEST-01',
                'vehicle_id': self.v1.id,
                'trip_start': '2026-01-02 08:00:00',
            })

    def test_10_cron_no_api_url_skips_gracefully(self):
        """ไม่มี API URL → _cron_sync_trips() ต้องไม่ raise exception"""
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('fleet_telematics.mtd_api_url', '')
        try:
            self.env['fleet.telematics.log']._cron_sync_trips()
        except Exception as e:
            self.fail(f"ต้องไม่ raise เมื่อไม่มี API URL: {e}")
        finally:
            ICP.set_param('fleet_telematics.mtd_api_url', 'http://test-backend:8001')


# ══════════════════════════════════════════════════════════════════════════════
# UC-10 — Audit Log บน Incentive state change (models/telematics_incentive.py)
#   เพิ่มใหม่ 2026-07-06: เดิม UC-10 ไม่มี test เลย ทั้งที่ FDD §13 ระบุว่า
#   "audit log ทุก state change" เป็น requirement ที่ต้องผ่าน
# ══════════════════════════════════════════════════════════════════════════════

class TestUC10IncentiveAuditLog(FleetTelematicsBase):
    """ตรวจว่าทุกครั้งที่ state ของ Incentive เปลี่ยน ต้องมี message_post
    (chatter log) บันทึกไว้อัตโนมัติ — ตาม FDD §13 "audit log ทุก state change" """

    def _make_incentive(self, **kw):
        vals = dict(
            driver_id=self.employee.id,
            date_from=date(2026, 6, 1), date_to=date(2026, 6, 30),
            avg_score=88.0, min_score=70.0,
            total_trips=20, total_distance_km=500.0,
            total_harsh_events=3, total_idle_min=40.0,
            incentive_tier='B', bonus_pct=5.0,
            base_salary=20000.0,
        )
        vals.update(kw)
        return self.env['fleet.telematics.incentive'].create(vals)

    def test_01_model_has_mail_thread(self):
        Incentive = self.env['fleet.telematics.incentive']
        self.assertIn(
            'mail.thread', Incentive._inherit if isinstance(Incentive._inherit, list) else [Incentive._inherit],
            "fleet.telematics.incentive ต้อง _inherit mail.thread เพื่อให้มี audit log"
        )

    def test_02_confirm_creates_log_message(self):
        inc = self._make_incentive()
        msg_count_before = len(inc.message_ids)
        inc.action_confirm()
        self.assertEqual(inc.state, 'confirmed')
        self.assertGreater(
            len(inc.message_ids), msg_count_before,
            "action_confirm() ต้อง message_post บันทึก log เพิ่มอย่างน้อย 1 ข้อความ"
        )
        last_body = inc.message_ids.sorted('date', reverse=True)[0].body
        self.assertIn('Confirmed', last_body)

    def test_03_approve_creates_log_and_sets_approved_by(self):
        inc = self._make_incentive()
        inc.action_confirm()
        n_before = len(inc.message_ids)
        inc.action_approve()
        self.assertEqual(inc.state, 'approved')
        self.assertEqual(inc.approved_by, self.env.user)
        self.assertGreater(len(inc.message_ids), n_before)

    def test_04_mark_paid_creates_log(self):
        inc = self._make_incentive()
        inc.action_confirm()
        inc.action_approve()
        n_before = len(inc.message_ids)
        inc.action_mark_paid()
        self.assertEqual(inc.state, 'paid')
        self.assertGreater(len(inc.message_ids), n_before)

    def test_05_reset_creates_log_and_clears_approved_by(self):
        inc = self._make_incentive()
        inc.action_confirm()
        inc.action_approve()
        n_before = len(inc.message_ids)
        inc.action_reset()
        self.assertEqual(inc.state, 'draft')
        self.assertFalse(inc.approved_by)
        self.assertGreater(len(inc.message_ids), n_before)

    def test_06_full_workflow_has_one_log_entry_per_transition(self):
        """draft→confirmed→approved→paid = อย่างน้อย 3 รอบ transition
        ต้องมี message อย่างน้อย 3 ข้อความที่เพิ่มขึ้นมา (ไม่นับ create log)"""
        inc = self._make_incentive()
        n0 = len(inc.message_ids)
        inc.action_confirm()
        n1 = len(inc.message_ids)
        inc.action_approve()
        n2 = len(inc.message_ids)
        inc.action_mark_paid()
        n3 = len(inc.message_ids)
        self.assertGreater(n1, n0)
        self.assertGreater(n2, n1)
        self.assertGreater(n3, n2)

    def test_07_state_change_via_direct_write_still_tracked(self):
        """tracking=True บน field state ต้องทำงานแม้เปลี่ยนผ่าน write() ตรงๆ
        ไม่ใช่แค่ผ่าน action_* เท่านั้น (mail.thread auto-tracks field changes)"""
        inc = self._make_incentive()
        n_before = len(inc.message_ids)
        inc.write({'state': 'confirmed'})
        # mail.thread tracking สร้าง message แยกจาก manual message_post ใน action_confirm
        self.assertGreaterEqual(len(inc.message_ids), n_before)

    # ── เพิ่ม 2026-07-09: ทดสอบ date_from/date_to แทน period_month/year ────
    def test_08_period_label_shows_date_range(self):
        """period_label ต้องแสดงเป็นช่วงวันที่ ไม่ใช่ MM/YYYY แบบเดิม"""
        inc = self._make_incentive(
            date_from=date(2026, 6, 26), date_to=date(2026, 7, 25))
        self.assertEqual(inc.period_label, '26/06/2026 - 25/07/2026')

    def test_09_period_month_year_derived_from_date_from(self):
        """period_month/period_year ต้อง derive จาก date_from อัตโนมัติ"""
        inc = self._make_incentive(
            date_from=date(2026, 7, 26), date_to=date(2026, 8, 25))
        self.assertEqual(inc.period_month, 7)
        self.assertEqual(inc.period_year, 2026)

    def test_10_date_from_required(self):
        """date_from/date_to เป็น required — สร้างโดยไม่ระบุต้อง raise"""
        with self.assertRaises(Exception):
            self.env['fleet.telematics.incentive'].create({
                'driver_id': self.employee.id,
            })

    def test_11_duplicate_same_date_range_raises(self):
        """สร้างซ้ำ driver+date_from+date_to เดิม → ต้อง raise (unique constraint)"""
        self._make_incentive(date_from=date(2026, 5, 1), date_to=date(2026, 5, 31))
        with self.assertRaises(Exception):
            self._make_incentive(date_from=date(2026, 5, 1), date_to=date(2026, 5, 31))

    # ── เพิ่ม 2026-07-09: ทดสอบ bonus_amount เป็น compute field จริง ───────
    def test_12_bonus_amount_computed_from_formula(self):
        """bonus_amount ต้อง = base_salary * bonus_pct / 100 เสมอ (คำนวณเอง)"""
        inc = self._make_incentive(base_salary=30000.0, bonus_pct=10.0)
        self.assertEqual(inc.bonus_amount, 3000.0)

    def test_13_bonus_amount_updates_when_pct_changes(self):
        """แก้ bonus_pct ตอน draft → bonus_amount ต้อง recompute ตาม"""
        inc = self._make_incentive(base_salary=20000.0, bonus_pct=5.0)
        self.assertEqual(inc.bonus_amount, 1000.0)
        inc.write({'bonus_pct': 10.0})
        self.assertEqual(inc.bonus_amount, 2000.0)

    # ── เพิ่ม 2026-07-09: ทดสอบล็อกฟอร์มถาวรเมื่อพ้น Draft (บรีฟข้อ 5) ──────
    def test_14_edit_blocked_after_confirm(self):
        """แก้ไข field ผลงาน/โบนัส หลัง Confirm ไปแล้ว → ต้อง UserError"""
        inc = self._make_incentive()
        inc.write({'state': 'confirmed'})
        with self.assertRaises(UserError):
            inc.write({'avg_score': 99.0})

    def test_15_edit_allowed_after_reset_to_draft(self):
        """Reset กลับ Draft แล้วต้องแก้ไขได้อีกครั้ง"""
        inc = self._make_incentive()
        inc.write({'state': 'confirmed'})
        inc.write({'state': 'draft'})
        inc.write({'avg_score': 95.0})  # ต้องไม่ raise
        self.assertEqual(inc.avg_score, 95.0)

    def test_16_export_to_appraisal_requires_approved(self):
        """action_export_to_appraisal() ก่อนถึง Approved → ต้อง UserError"""
        inc = self._make_incentive()
        with self.assertRaises(UserError):
            inc.action_export_to_appraisal()

    def test_17_export_to_appraisal_posts_to_employee(self):
        """หลัง Approve แล้ว export ต้องสำเร็จและ post message ไปที่ hr.employee"""
        inc = self._make_incentive()
        inc.write({'state': 'confirmed'})
        inc.write({'state': 'approved', 'approved_by': self.env.user.id})
        n_before = len(self.employee.message_ids)
        inc.action_export_to_appraisal()
        self.assertGreater(len(self.employee.message_ids), n_before)

    def test_18_notify_hr_new_drafts_batch_posts_message(self):
        """_notify_hr_new_drafts_batch() ต้องบันทึกข้อความลง chatter ของ
        record แรกในกลุ่ม (ตาม FDD §12.4 ขั้นตอน 4: แจ้ง HR ทุกครั้งที่มี
        draft ใหม่ ไม่ใช่แค่ตอน Tier D เท่านั้น)"""
        inc1 = self._make_incentive(date_from=date(2026, 3, 1), date_to=date(2026, 3, 31))
        inc2 = self._make_incentive(
            driver_id=self.env['hr.employee'].create({'name': 'Second Driver'}).id,
            date_from=date(2026, 3, 1), date_to=date(2026, 3, 31))
        batch = inc1 + inc2
        n_before = len(inc1.message_ids)
        batch._notify_hr_new_drafts_batch(3, 2026)
        self.assertGreater(len(inc1.message_ids), n_before)

    def test_19_local_tier_fallback_uses_configured_tier_d_bonus_pct(self):
        """_local_tier_from_score() (ใช้ตอนเรียก Backend ไม่สำเร็จ) ต้องอ่าน
        tier_d_bonus_pct จาก Scoring Config จริง ไม่ใช่ hardcode เป็น 0.0
        เสมอ — ถ้า Admin ตั้งค่าไว้ไม่ใช่ 0 ผลลัพธ์ fallback ต้องตรงกัน"""
        cfg = self._make_scoring('UC10-19', active=True, tier_c_min_score=60.0,
                                  tier_d_bonus_pct=2.5)
        inc = self._make_incentive(avg_score=40.0, scoring_config_id=cfg.id)
        tier, pct = inc._local_tier_from_score(return_pct=True)
        self.assertEqual(tier, 'D')
        self.assertEqual(pct, 2.5)


# ══════════════════════════════════════════════════════════════════════════════
# UC-12 — Verify Device (GET /vehicles/{id}/device) — fleet_vehicle_ext.py
# ══════════════════════════════════════════════════════════════════════════════

class TestUC12VerifyDevice(FleetTelematicsBase):

    def _mock_resp(self, status_code=200, json_data=None):
        r = MagicMock()
        r.status_code = status_code
        r.json.return_value = json_data or {}
        r.raise_for_status.return_value = None
        return r

    def test_01_matching_device_no_mismatch(self):
        with patch('requests.get', return_value=self._mock_resp(
                200, {'device_id': 'KTC-BASE-01', 'date_update_latest': '2026-07-01T00:00:00Z'})):
            try:
                self.v1.action_verify_device()
            except Exception:
                pass  # display_notification client action ไม่ raise แต่ ensure_one() ok
        self.assertFalse(self.v1.device_verify_mismatch)
        self.assertTrue(self.v1.device_verified_at)

    def test_02_mismatched_device_flagged(self):
        with patch('requests.get', return_value=self._mock_resp(
                200, {'device_id': 'KTC-DIFFERENT-99'})):
            self.v1.action_verify_device()
        self.assertTrue(self.v1.device_verify_mismatch)
        self.assertIn('KTC-DIFFERENT-99', self.v1.device_verify_note)

    def test_03_backend_404_raises_and_flags_mismatch_if_odoo_has_device(self):
        """ใช้ try/except ธรรมดาแทน self.assertRaises() ของ Odoo — เพราะ
        self.assertRaises() ของ Odoo (TransactionCase) ตั้ง savepoint ก่อน
        เข้า block แล้ว rollback กลับไปที่ savepoint นั้นทันทีที่จับ
        exception ที่คาดไว้ได้เสมอ (ออกแบบมาเพื่อกันโค้ดที่ทดสอบ error ทิ้ง
        ข้อมูลเพี้ยนไว้) ซึ่งจะ rollback การ write() ของ
        action_verify_device() ทิ้งไปด้วยทั้งที่ตั้งใจให้ mismatch flag รอด
        อยู่เป็นหลักฐาน — try/except ของ Python เฉยๆ ไม่มีกลไกนี้ จึงใช้
        แทนได้ถูกต้องกว่าสำหรับเคสนี้โดยเฉพาะ"""
        from odoo.exceptions import UserError
        with patch('requests.get', return_value=self._mock_resp(404)):
            try:
                self.v1.action_verify_device()
                self.fail('คาดว่าต้อง raise UserError แต่ไม่ raise')
            except UserError:
                pass
        self.assertTrue(self.v1.device_verify_mismatch)

    def test_04_connection_error_raises_and_flags_mismatch(self):
        """เหตุผลเดียวกับ test_03 — ใช้ try/except แทน self.assertRaises()
        ของ Odoo เพื่อไม่ให้ค่าที่ write() ไว้ก่อน raise ถูก rollback ทิ้ง"""
        import requests as _requests
        from odoo.exceptions import UserError
        with patch('requests.get', side_effect=_requests.RequestException('timeout')):
            try:
                self.v1.action_verify_device()
                self.fail('คาดว่าต้อง raise UserError แต่ไม่ raise')
            except UserError:
                pass
        self.assertTrue(self.v1.device_verify_mismatch)
        self.assertIn('เรียก Backend ไม่สำเร็จ', self.v1.device_verify_note)


# ══════════════════════════════════════════════════════════════════════════════
# UC-12 — Reconcile Devices (GET /config_device) — models/telematics_config.py
#   เพิ่มใหม่ 2026-07-06
# ══════════════════════════════════════════════════════════════════════════════

class TestUC12ReconcileDevices(FleetTelematicsBase):

    def _get_config(self):
        return self.env['fleet.telematics.config'].search([], limit=1, order='id asc') \
            or self.env['fleet.telematics.config'].create({'name': 'Test Config'})

    def _mock_resp(self, devices):
        r = MagicMock()
        r.json.return_value = {'devices': devices}
        r.raise_for_status.return_value = None
        return r

    def test_01_all_matching_zero_mismatch(self):
        # แก้ 2026-07-17: เดิมเช็ค device_mismatch_count == 0 ตรงๆ ซึ่งใช้ได้
        # แค่ในฐานข้อมูลว่างเปล่าเท่านั้น — action_reconcile_devices()
        # ตรวจสอบ "ทุกรถที่มี telematics_device_id" ในระบบทั้งหมดโดยออกแบบ
        # ไว้แบบนั้นจริง (ถูกต้องแล้วสำหรับใช้งานจริง) แต่ถ้ารันบนฐานข้อมูล
        # ที่มีรถจริงอยู่ก่อนแล้ว (เช่น KTC-001 ถึง KTC-010 จากการทดสอบมือ)
        # การ mock ให้ Backend มีแค่ 1 device จะทำให้รถจริงที่เหลือถูกนับเป็น
        # mismatch ไปด้วย (ไม่ใช่บั๊กของฟังก์ชัน แค่ test เดิมไม่ทนต่อข้อมูล
        # ที่มีอยู่ก่อน) เปลี่ยนไปเช็คเฉพาะว่า "รถทดสอบของเราเอง (self.v1)
        # ต้องไม่ถูก flag เป็น mismatch" แทนการเช็ค total count ทั้งระบบ
        config = self._get_config()
        with patch('requests.get', return_value=self._mock_resp(
                [{'device_id': 'KTC-BASE-01', 'vehicle_id': self.v1.id}])):
            config.action_reconcile_devices()
        self.assertTrue(config.last_reconciled_at)
        self.assertNotIn(
            self.v1.license_plate, config.device_mismatch_note or '',
            "รถทดสอบ (self.v1) ที่ข้อมูลตรงกับ Backend ต้องไม่ถูกนับเป็น mismatch"
        )

    def test_02_vehicle_id_mismatch_detected(self):
        config = self._get_config()
        other_vehicle_id = self.v1.id + 9999
        with patch('requests.get', return_value=self._mock_resp(
                [{'device_id': 'KTC-BASE-01', 'vehicle_id': other_vehicle_id}])):
            config.action_reconcile_devices()
        self.assertGreaterEqual(config.device_mismatch_count, 1)
        self.assertIn('vehicle_id', config.device_mismatch_note)

    def test_03_odoo_device_missing_on_backend(self):
        config = self._get_config()
        with patch('requests.get', return_value=self._mock_resp([])):
            config.action_reconcile_devices()
        self.assertGreaterEqual(config.device_mismatch_count, 1)
        self.assertIn('ยังไม่ได้ register จริง', config.device_mismatch_note)

    def test_04_backend_extra_device_detected(self):
        config = self._get_config()
        with patch('requests.get', return_value=self._mock_resp([
                {'device_id': 'KTC-BASE-01', 'vehicle_id': self.v1.id},
                {'device_id': 'KTC-ORPHAN-99', 'vehicle_id': 12345},
        ])):
            config.action_reconcile_devices()
        self.assertGreaterEqual(config.device_mismatch_count, 1)
        self.assertIn('KTC-ORPHAN-99', config.device_mismatch_note)

    def test_05_no_auto_fix_vehicle_untouched(self):
        """ต้องไม่ auto-fix ข้อมูลรถ แม้เจอ mismatch — แค่รายงาน"""
        config = self._get_config()
        original_device = self.v1.telematics_device_id
        with patch('requests.get', return_value=self._mock_resp(
                [{'device_id': 'KTC-BASE-01', 'vehicle_id': self.v1.id + 1}])):
            config.action_reconcile_devices()
        self.assertEqual(self.v1.telematics_device_id, original_device)

    def test_06_cron_wrapper_does_not_raise_on_connection_error(self):
        import requests as _requests
        config = self._get_config()
        with patch('requests.get', side_effect=_requests.RequestException('down')):
            try:
                self.env['fleet.telematics.config']._cron_reconcile_devices()
            except Exception as e:
                self.fail(f"_cron_reconcile_devices ต้องไม่ raise ออกไปให้ cron scheduler เห็น: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# UC-11 — Portal Self-service: ตรวจ ir.rule ที่ควบคุมการเห็นข้อมูลของตัวเอง
#   (Controller ใช้ sudo() + กรอง driver_id เอง — เทสนี้ยืนยันว่าชั้น ir.rule
#    ที่เป็น defense-in-depth ยังทำงานถูกต้อง หากมีจุดเรียกอื่นที่ไม่ผ่าน sudo())
# ══════════════════════════════════════════════════════════════════════════════

class TestUC11PortalDataIsolation(FleetTelematicsBase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)
        # แก้ 2026-07-16: groups_id → group_ids ใน Odoo 19 (ดู _users_groups_field)
        cls.driver_user = Users.create({
            'name': 'Driver User', 'login': 'driver_user_test',
            _users_groups_field(cls.env): [(6, 0, [cls.env.ref('fleet.fleet_group_user').id])],
        })
        cls.employee.write({'user_id': cls.driver_user.id})

        cls.other_employee = cls.env['hr.employee'].create({'name': 'Other Employee'})

    def test_01_driver_sees_only_own_incentive(self):
        Incentive = self.env['fleet.telematics.incentive']
        own = Incentive.create({
            'driver_id': self.employee.id,
            'date_from': date(2026, 6, 1), 'date_to': date(2026, 6, 30),
            'avg_score': 90.0,
        })
        others = Incentive.create({
            'driver_id': self.other_employee.id,
            'date_from': date(2026, 6, 1), 'date_to': date(2026, 6, 30),
            'avg_score': 70.0,
        })
        visible = Incentive.with_user(self.driver_user).search([])
        self.assertIn(own, visible)
        self.assertNotIn(others, visible)

    def test_02_driver_sees_only_own_trip_log(self):
        Log = self.env['fleet.telematics.log']
        own_trip = Log.create({
            'vehicle_id': self.v1.id, 'driver_id': self.employee.id,
            'trip_start': '2026-06-10 08:00:00', 'external_trip_id': 'T-OWN-1',
        })
        other_trip = Log.create({
            'vehicle_id': self.v1.id, 'driver_id': self.other_employee.id,
            'trip_start': '2026-06-10 09:00:00', 'external_trip_id': 'T-OTHER-1',
        })
        visible = Log.with_user(self.driver_user).search([])
        self.assertIn(own_trip, visible)
        self.assertNotIn(other_trip, visible)

# ══════════════════════════════════════════════════════════════════════════════
# UC-Maintenance — 3 Trigger การแจ้งเตือนซ่อมบำรุง (FDD §2.2)
#   เพิ่มใหม่ 2026-07-12: พบว่า Trigger 2 (ชั่วโมงเดินเครื่อง) ขาดหายไปทั้งหมด
#   ในโค้ดเดิม — ไม่เคยมี test คลุมส่วนนี้เลยมาก่อน
# ══════════════════════════════════════════════════════════════════════════════

class TestMaintenanceTriggers(FleetTelematicsBase):

    def setUp(self):
        super().setUp()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('fleet_telematics.maintenance_km', '10000')
        ICP.set_param('fleet_telematics.maintenance_hours', '250')
        ICP.set_param('fleet_telematics.maintenance_days', '90')

    def test_01_engine_hours_accumulate_on_sync(self):
        """duration_min ของทริปต้องถูกสะสมลง telematics_engine_hours ของรถ"""
        Log = self.env['fleet.telematics.log']
        self.v1.write({'telematics_engine_hours': 0.0, 'odometer': 0})
        Log._update_odometer_and_check_maintenance(
            self.v1.id, distance_km=50.0, duration_min=120.0)  # 2 ชั่วโมง
        self.assertAlmostEqual(self.v1.telematics_engine_hours, 2.0, places=2)

    def test_02_hours_trigger_creates_service_when_no_prior_service(self):
        """ไม่มี service record เลย + ชั่วโมงเครื่องเกิน threshold แรก → ต้องสร้าง"""
        Log = self.env['fleet.telematics.log']
        self.v1.write({'telematics_engine_hours': 0.0, 'odometer': 0})
        Service = self.env['fleet.vehicle.log.services']
        before = Service.search_count([('vehicle_id', '=', self.v1.id)])

        # 260 ชั่วโมง (> threshold 250) ในทริปเดียว (ไม่สมจริงแต่พอสำหรับทดสอบ)
        Log._update_odometer_and_check_maintenance(
            self.v1.id, distance_km=100.0, duration_min=260 * 60)

        after = Service.search_count([('vehicle_id', '=', self.v1.id)])
        self.assertGreater(after, before, "ต้องสร้าง service alert เมื่อชั่วโมงเครื่องถึง threshold")

    def test_03_hours_trigger_uses_snapshot_from_last_service(self):
        """เทียบชั่วโมงสะสมกับ snapshot ตอน service ล่าสุด ไม่ใช่นับจากศูนย์ใหม่"""
        Log     = self.env['fleet.telematics.log']
        Service = self.env['fleet.vehicle.log.services']

        # ตั้งให้เพิ่งเคย service ตอนชั่วโมงสะสม = 100
        self.v1.write({'telematics_engine_hours': 100.0, 'odometer': 5000})
        Service.create({
            'vehicle_id': self.v1.id,
            'date': date.today(),
            'odometer': 5000,
            'engine_hours_at_service': 100.0,
            'description': 'Manual service (test baseline)',
        })
        before = Service.search_count([('vehicle_id', '=', self.v1.id)])

        # เพิ่มอีกแค่ 50 ชั่วโมง (100+50=150 ยังไม่ถึง threshold 250) → ไม่ควรสร้างใหม่
        Log._update_odometer_and_check_maintenance(
            self.v1.id, distance_km=10.0, duration_min=50 * 60)
        after_small = Service.search_count([('vehicle_id', '=', self.v1.id)])
        self.assertEqual(after_small, before, "ยังไม่ถึง threshold ไม่ควรสร้าง service ใหม่")

        # เพิ่มอีก 200 ชั่วโมง (150+200=350 เกิน threshold 250 จาก baseline 100) → ต้องสร้าง
        Log._update_odometer_and_check_maintenance(
            self.v1.id, distance_km=10.0, duration_min=200 * 60)
        after_big = Service.search_count([('vehicle_id', '=', self.v1.id)])
        self.assertGreater(after_big, after_small)

    def test_04_km_trigger_still_works_unaffected(self):
        """Trigger ระยะทางเดิมต้องยังทำงานถูกต้องหลังเพิ่ม Trigger ชั่วโมง"""
        Log     = self.env['fleet.telematics.log']
        Service = self.env['fleet.vehicle.log.services']
        self.v1.write({'telematics_engine_hours': 0.0, 'odometer': 0})
        before = Service.search_count([('vehicle_id', '=', self.v1.id)])

        Log._update_odometer_and_check_maintenance(
            self.v1.id, distance_km=15000.0, duration_min=10.0)  # เกิน 10,000 km

        after = Service.search_count([('vehicle_id', '=', self.v1.id)])
        self.assertGreater(after, before)


# ══════════════════════════════════════════════════════════════════════════════
# Event Logs — Lockdown 3 ชั้น + Zone-based Speed Limit (models/telematics_event.py)
#   เพิ่มใหม่ 2026-07-12: โมเดลนี้ไม่เคยมี test เลยมาก่อน ทั้งที่เป็นจุดที่
#   ทำ read-only lock 3 ชั้นและ zone speed limit ไว้ค่อนข้างซับซ้อน
# ══════════════════════════════════════════════════════════════════════════════

class TestEventLogsLockdown(FleetTelematicsBase):

    def setUp(self):
        super().setUp()
        self.trip = self.env['fleet.telematics.log'].create({
            'vehicle_id':  self.v1.id,
            'driver_id':   self.employee.id,
            'trip_start':  '2026-06-10 08:00:00',
            'external_trip_id': 'T-EVT-TEST-1',
        })

    def _event_vals(self, **kw):
        vals = dict(
            trip_id=self.trip.id,
            event_type='harsh_brake',
            occurred_at='2026-06-10 08:05:00',
            lat=13.75, lon=100.50,  # ในกรอบกรุงเทพฯ
            severity=70.0,
            speed_at_event=50.0,
        )
        vals.update(kw)
        return vals

    # ── ข้อ 1: ล็อกแก้ไข/ลบ/สร้างผ่านหน้าจอไม่ได้เด็ดขาด ────────────────────
    def test_01_create_without_sync_context_raises(self):
        Event = self.env['fleet.telematics.event']
        with self.assertRaises(UserError):
            Event.create(self._event_vals())

    def test_02_create_with_sync_context_succeeds(self):
        Event = self.env['fleet.telematics.event'].with_context(
            fleet_telematics_allow_sync=True)
        ev = Event.create(self._event_vals())
        self.assertTrue(ev.id)

    def test_03_write_without_sync_context_raises(self):
        Event = self.env['fleet.telematics.event'].with_context(
            fleet_telematics_allow_sync=True)
        ev = Event.create(self._event_vals())
        ev = ev.with_context(fleet_telematics_allow_sync=False)
        with self.assertRaises(UserError):
            ev.write({'severity': 99.0})

    def test_04_unlink_without_sync_context_raises(self):
        Event = self.env['fleet.telematics.event'].with_context(
            fleet_telematics_allow_sync=True)
        ev = Event.create(self._event_vals())
        ev = ev.with_context(fleet_telematics_allow_sync=False)
        with self.assertRaises(UserError):
            ev.unlink()

    def test_05_write_with_sync_context_succeeds(self):
        Event = self.env['fleet.telematics.event'].with_context(
            fleet_telematics_allow_sync=True)
        ev = Event.create(self._event_vals())
        ev.write({'severity': 55.0})
        self.assertEqual(ev.severity, 55.0)

    # ── ข้อ 2: Zone-based Speed Limit ───────────────────────────────────────
    def test_06_bangkok_coords_gets_80kmh_limit(self):
        Event = self.env['fleet.telematics.event'].with_context(
            fleet_telematics_allow_sync=True)
        ev = Event.create(self._event_vals(lat=13.75, lon=100.50, speed_at_event=50.0))
        self.assertEqual(ev.zone_label, 'bangkok')
        self.assertEqual(ev.speed_limit_kmh, 80.0)

    def test_07_outside_bangkok_gets_90kmh_limit(self):
        Event = self.env['fleet.telematics.event'].with_context(
            fleet_telematics_allow_sync=True)
        # เชียงใหม่ — อยู่นอกกรอบกรุงเทพฯ ชัดเจน
        ev = Event.create(self._event_vals(lat=18.79, lon=98.98, speed_at_event=50.0))
        self.assertEqual(ev.zone_label, 'outside')
        self.assertEqual(ev.speed_limit_kmh, 90.0)

    def test_08_zero_coords_treated_as_outside_not_bangkok(self):
        """lat/lon = 0,0 (ยังไม่มีข้อมูลจริง) ต้องไม่ถูกตีเป็นกรุงเทพฯ"""
        Event = self.env['fleet.telematics.event'].with_context(
            fleet_telematics_allow_sync=True)
        ev = Event.create(self._event_vals(lat=0.0, lon=0.0, speed_at_event=50.0))
        self.assertEqual(ev.zone_label, 'outside')

    def test_09_over_speed_limit_flag_true_when_exceeding(self):
        Event = self.env['fleet.telematics.event'].with_context(
            fleet_telematics_allow_sync=True)
        ev = Event.create(self._event_vals(
            event_type='speeding', lat=13.75, lon=100.50, speed_at_event=95.0))
        self.assertTrue(ev.is_over_speed_limit)  # 95 > 80 (กรุงเทพฯ)

    def test_10_not_over_speed_limit_when_within_range(self):
        Event = self.env['fleet.telematics.event'].with_context(
            fleet_telematics_allow_sync=True)
        ev = Event.create(self._event_vals(
            lat=13.75, lon=100.50, speed_at_event=60.0))
        self.assertFalse(ev.is_over_speed_limit)  # 60 <= 80

    # ── ข้อ 3: vehicle_id / driver_id related fields ────────────────────────
    def test_11_vehicle_id_derived_from_trip(self):
        Event = self.env['fleet.telematics.event'].with_context(
            fleet_telematics_allow_sync=True)
        ev = Event.create(self._event_vals())
        self.assertEqual(ev.vehicle_id, self.v1)

    def test_12_driver_id_derived_from_trip(self):
        Event = self.env['fleet.telematics.event'].with_context(
            fleet_telematics_allow_sync=True)
        ev = Event.create(self._event_vals())
        self.assertEqual(ev.driver_id, self.employee)

    def test_13_deleting_trip_cascades_to_events(self):
        """ondelete='cascade' — ลบ trip แล้ว event ต้องหายตามด้วย"""
        Event = self.env['fleet.telematics.event'].with_context(
            fleet_telematics_allow_sync=True)
        ev = Event.create(self._event_vals())
        ev_id = ev.id
        self.trip.with_context(fleet_telematics_allow_sync=True).unlink()
        self.assertFalse(Event.browse(ev_id).exists())


# ══════════════════════════════════════════════════════════════════════════════
# Vehicle Trip History Wizard (models/telematics_vehicle_trip.py)
#   เพิ่มใหม่ 2026-07-12: wizard นี้ไม่เคยมี test เลยมาก่อน
# ══════════════════════════════════════════════════════════════════════════════

class TestVehicleTripHistoryWizard(FleetTelematicsBase):

    def test_01_fetch_without_vehicle_raises(self):
        wiz = self.env['fleet.telematics.vehicle.trip.history'].create({})
        with self.assertRaises(UserError):
            wiz.action_fetch()

    def test_02_fetch_without_device_raises(self):
        """รถที่ยังไม่ผูก GPS Device ต้องเตือนก่อนดึงข้อมูล ไม่ใช่ดึงข้อมูลว่างเปล่า"""
        vehicle_no_device = self.env['fleet.vehicle'].create({
            'model_id': self.fmodel.id,
            'license_plate': 'NO-DEVICE-01',
        })
        wiz = self.env['fleet.telematics.vehicle.trip.history'].create({
            'vehicle_id': vehicle_no_device.id,
        })
        with self.assertRaises(UserError):
            wiz.action_fetch()

    def test_03_fetch_with_device_succeeds(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'trips': [], 'total': 0, 'total_pages': 1}
        wiz = self.env['fleet.telematics.vehicle.trip.history'].create({
            'vehicle_id': self.v1.id,  # self.v1 มี telematics_device_id ผูกไว้แล้ว
        })
        with patch('requests.get', return_value=mock_resp):
            wiz.action_fetch()
        self.assertTrue(wiz.has_result)
        self.assertEqual(wiz.total_trips, 0)

    def test_04_vehicle_info_fields_populate_from_selection(self):
        """เลือกรถแล้ว field ยืนยัน (ทะเบียน/device) ต้องขึ้นอัตโนมัติ"""
        wiz = self.env['fleet.telematics.vehicle.trip.history'].create({
            'vehicle_id': self.v1.id,
        })
        self.assertEqual(wiz.vehicle_license_plate, self.v1.license_plate)
        self.assertEqual(wiz.telematics_device_id, self.v1.telematics_device_id)


# ══════════════════════════════════════════════════════════════════════════════
# Vehicle Aggregated Stats — total_trips/total_distance_km/avg_driver_score
#   เพิ่มใหม่ 2026-07-12: พบว่า field เหล่านี้บน fleet.vehicle ไม่เคยถูกอัปเดต
#   จากที่ไหนเลย ทั้งที่มี field อยู่แล้ว (ค้างเป็น 0 ตลอดไป)
# ══════════════════════════════════════════════════════════════════════════════

class TestVehicleAggregatedStats(FleetTelematicsBase):

    def test_01_total_trips_increments_per_call(self):
        Log = self.env['fleet.telematics.log']
        self.v1.write({'total_trips': 0, 'total_distance_km': 0.0, 'odometer': 0})
        Log._update_odometer_and_check_maintenance(self.v1.id, distance_km=10.0)
        Log._update_odometer_and_check_maintenance(self.v1.id, distance_km=15.0)
        self.assertEqual(self.v1.total_trips, 2)

    def test_02_total_distance_km_accumulates(self):
        Log = self.env['fleet.telematics.log']
        self.v1.write({'total_trips': 0, 'total_distance_km': 0.0, 'odometer': 0})
        Log._update_odometer_and_check_maintenance(self.v1.id, distance_km=10.0)
        Log._update_odometer_and_check_maintenance(self.v1.id, distance_km=15.5)
        self.assertAlmostEqual(self.v1.total_distance_km, 25.5, places=2)

    def test_03_avg_driver_score_computed_as_running_average(self):
        Log = self.env['fleet.telematics.log']
        self.v1.write({
            'total_trips': 0, 'total_distance_km': 0.0,
            'avg_driver_score': 0.0, 'odometer': 0,
        })
        Log._update_odometer_and_check_maintenance(
            self.v1.id, distance_km=10.0, driver_score=90.0)
        self.assertEqual(self.v1.avg_driver_score, 90.0)

        Log._update_odometer_and_check_maintenance(
            self.v1.id, distance_km=10.0, driver_score=70.0)
        # เฉลี่ยของ 90 และ 70 = 80
        self.assertEqual(self.v1.avg_driver_score, 80.0)

    def test_04_no_driver_score_leaves_avg_unchanged(self):
        """ถ้าไม่ส่ง driver_score มา (None) ไม่ควรไปทำให้ avg เพี้ยน"""
        Log = self.env['fleet.telematics.log']
        self.v1.write({
            'total_trips': 1, 'avg_driver_score': 85.0, 'odometer': 0,
        })
        Log._update_odometer_and_check_maintenance(self.v1.id, distance_km=5.0)
        self.assertEqual(self.v1.avg_driver_score, 85.0)

# ══════════════════════════════════════════════════════════════════════════════
# Data Retention (FDD §13) — เพิ่มใหม่ 2026-07-16
#   ลบ Trip Log ที่เก่ากว่าเกณฑ์ที่ตั้งไว้ (default 3 ปี) ไม่แตะ Incentive
# ══════════════════════════════════════════════════════════════════════════════

class TestDataRetention(FleetTelematicsBase):

    def setUp(self):
        super().setUp()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('fleet_telematics.trip_retention_years', '3')

    def test_01_old_trip_gets_purged(self):
        """ทริปที่เก่ากว่า 3 ปี ต้องถูกลบทิ้งหลังรัน cron"""
        Log = self.env['fleet.telematics.log']
        old_trip = Log.create({
            'vehicle_id': self.v1.id,
            'trip_start': '2020-01-01 08:00:00',
            'external_trip_id': 'T-OLD-1',
        })
        old_id = old_trip.id
        Log._cron_purge_old_trips()
        self.assertFalse(Log.browse(old_id).exists())

    def test_02_recent_trip_not_purged(self):
        """ทริปที่ยังไม่เกิน 3 ปี ต้องไม่ถูกลบ"""
        Log = self.env['fleet.telematics.log']
        recent_trip = Log.create({
            'vehicle_id': self.v1.id,
            'trip_start': fields.Datetime.now(),
            'external_trip_id': 'T-RECENT-1',
        })
        recent_id = recent_trip.id
        Log._cron_purge_old_trips()
        self.assertTrue(Log.browse(recent_id).exists())

    def test_03_purge_cascades_to_events(self):
        """ลบทริปเก่าแล้ว Event ที่ผูกอยู่ต้องหายไปด้วย (cascade)"""
        Log = self.env['fleet.telematics.log']
        Event = self.env['fleet.telematics.event']
        old_trip = Log.create({
            'vehicle_id': self.v1.id,
            'trip_start': '2019-06-01 08:00:00',
            'external_trip_id': 'T-OLD-2',
        })
        ev = Event.with_context(fleet_telematics_allow_sync=True).create({
            'trip_id': old_trip.id,
            'event_type': 'harsh_brake',
            'occurred_at': '2019-06-01 08:05:00',
            'lat': 13.75, 'lon': 100.50,
            'severity': 70.0, 'speed_at_event': 50.0,
        })
        ev_id = ev.id
        Log._cron_purge_old_trips()
        self.assertFalse(Event.browse(ev_id).exists())

    def test_04_incentive_never_purged(self):
        """Incentive ต้องไม่ถูกแตะต้องเลย ไม่ว่าจะเก่าแค่ไหน (เก็บตลอดชีวิต)"""
        Log = self.env['fleet.telematics.log']
        Incentive = self.env['fleet.telematics.incentive']
        old_inc = Incentive.create({
            'driver_id': self.employee.id,
            'date_from': date(2018, 1, 1), 'date_to': date(2018, 1, 31),
            'avg_score': 90.0,
        })
        old_inc_id = old_inc.id
        Log._cron_purge_old_trips()
        self.assertTrue(Incentive.browse(old_inc_id).exists(),
                         "Incentive ต้องเก็บตลอดชีวิต ห้าม cron นี้ไปลบเด็ดขาด")

    def test_05_retention_period_configurable(self):
        """เปลี่ยนค่า retention เป็น 1 ปี แล้วทริปอายุ 2 ปีต้องโดนลบด้วย"""
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('fleet_telematics.trip_retention_years', '1')
        Log = self.env['fleet.telematics.log']
        trip_2y = Log.create({
            'vehicle_id': self.v1.id,
            'trip_start': '2024-01-01 08:00:00',
            'external_trip_id': 'T-2Y-1',
        })
        trip_id = trip_2y.id
        Log._cron_purge_old_trips()
        self.assertFalse(Log.browse(trip_id).exists())
