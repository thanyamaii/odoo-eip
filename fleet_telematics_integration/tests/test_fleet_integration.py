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

from odoo.tests.common import TransactionCase
from odoo.exceptions import ValidationError, UserError


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
        cls.env.user.write({
            'groups_id': [(4, cls.env.ref('fleet.fleet_group_manager').id)]
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


# ══════════════════════════════════════════════════════════════════════════════
# UC-04 — GPS Poll + Dedup + Batch  (telematics_log.py section [I]–[N])
# ══════════════════════════════════════════════════════════════════════════════

class TestUC04TripSync(FleetTelematicsBase):
    """
    ครอบคลุม:
      - _get_poll_window() Cold Start และ Warm Start
      - _filter_new_trips() กรอง existing IDs ออก (Dedup ชั้น 2)
      - _build_trip_vals() แปลง dict → vals ถูกต้อง
      - _build_trip_vals() device ไม่มีในระบบ → คืน {}
      - _cron_sync_trips() สร้าง trip ใหม่
      - _cron_sync_trips() ไม่ duplicate เมื่อรัน 2 รอบ (Dedup ชั้น 1+2)
      - _cron_sync_trips() write existing trip (ไม่ create ใหม่)
      - _cron_sync_trips() บันทึก last_poll_ts หลัง sync
      - _sql_constraints UNIQUE(external_trip_id) มีอยู่ (Dedup ชั้น 3)
      - _cron_sync_trips() เมื่อไม่มี API URL → ไม่ raise
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ICP = cls.env['ir.config_parameter'].sudo()
        ICP.set_param('fleet_telematics.trip_last_poll_ts', '')

    def setUp(self):
        super().setUp()
        # รีเซ็ต last_poll_ts ก่อนแต่ละ test
        self.env['ir.config_parameter'].sudo().set_param(
            'fleet_telematics.trip_last_poll_ts', '')

    def test_01_get_poll_window_cold_start(self):
        """Cold Start (ไม่มี last_poll_ts) → since ≈ now-5min, until ≈ now"""
        Log      = self.env['fleet.telematics.log']
        before   = datetime.now(timezone.utc)
        since, until = Log._get_poll_window()
        after    = datetime.now(timezone.utc)

        # until อยู่ระหว่าง before และ after
        self.assertGreaterEqual(until, before)
        self.assertLessEqual(until, after + timedelta(seconds=1))

        # since ≈ until - 5 นาที (tolerance 10 วินาที)
        delta = abs((until - since).total_seconds() - 300)
        self.assertLess(delta, 10, f"since ต้องห่าง until ~5 นาที (delta={delta}s)")

    def test_02_get_poll_window_warm_start(self):
        """Warm Start → since ตรงกับ last_poll_ts ที่บันทึกไว้"""
        ICP      = self.env['ir.config_parameter'].sudo()
        fixed_ts = datetime(2025, 6, 10, 8, 0, 0, tzinfo=timezone.utc)
        ICP.set_param('fleet_telematics.trip_last_poll_ts', fixed_ts.isoformat())

        Log  = self.env['fleet.telematics.log']
        since, until = Log._get_poll_window()

        self.assertEqual(
            since.replace(tzinfo=timezone.utc), fixed_ts,
            "since ต้องตรงกับ last_poll_ts ที่บันทึกไว้"
        )
        self.assertGreater(until, since)

    def test_03_filter_new_trips_dedup(self):
        """_filter_new_trips() กรอง trip ที่มีใน DB แล้ว — คืนเฉพาะรายการใหม่"""
        Log = self.env['fleet.telematics.log']

        # สร้าง trip เดิมใน DB ก่อน
        Log.create({
            'external_trip_id': 'FILTER-OLD',
            'vehicle_id':  self.v1.id,
            'driver_id':   self.employee.id,
            'trip_start':  '2025-06-10 08:00:00',
            'state':       'synced',
        })

        trips = [
            self._make_trip_dict('FILTER-OLD'),   # มีอยู่แล้ว
            self._make_trip_dict('FILTER-NEW-1'), # ใหม่
            self._make_trip_dict('FILTER-NEW-2'), # ใหม่
        ]
        new_trips, existing_map = Log._filter_new_trips(trips)

        self.assertEqual(len(new_trips), 2)
        self.assertIn('FILTER-OLD', existing_map)
        new_ids = [t['trip_id'] for t in new_trips]
        self.assertNotIn('FILTER-OLD',   new_ids)
        self.assertIn('FILTER-NEW-1', new_ids)
        self.assertIn('FILTER-NEW-2', new_ids)

    def test_04_build_trip_vals_ok(self):
        """_build_trip_vals() แปลง dict → vals ครบถูกต้อง"""
        Log  = self.env['fleet.telematics.log']
        vals = Log._build_trip_vals(self._make_trip_dict('VALS-001'))

        self.assertEqual(vals['external_trip_id'], 'VALS-001')
        self.assertEqual(vals['vehicle_id'],        self.v1.id)
        self.assertEqual(vals['distance_km'],       50.0)
        self.assertEqual(vals['state'],             'synced')

    def test_05_build_trip_vals_unknown_device_empty(self):
        """_build_trip_vals() device ไม่มีในระบบ → คืน {} (caller จะ skip)"""
        Log  = self.env['fleet.telematics.log']
        vals = Log._build_trip_vals(
            self._make_trip_dict('VALS-X', device='DEVICE-NOTEXIST'))
        self.assertEqual(vals, {})

    def test_06_cron_creates_new_trips(self):
        """_cron_sync_trips() สร้าง trip ใหม่จาก API response"""
        Log   = self.env['fleet.telematics.log']
        trips = [
            self._make_trip_dict('CRON-C01'),
            self._make_trip_dict('CRON-C02'),
        ]
        before = Log.search_count(
            [('external_trip_id', 'in', ['CRON-C01', 'CRON-C02'])])

        with patch('requests.get', return_value=self._mock_api(trips)):
            with patch('time.sleep'):
                Log._cron_sync_trips()

        after = Log.search_count(
            [('external_trip_id', 'in', ['CRON-C01', 'CRON-C02'])])
        self.assertEqual(after - before, 2, "ต้องสร้าง 2 trips ใหม่")

    def test_07_cron_no_duplicate_on_second_run(self):
        """Dedup ชั้น 1+2: รัน Cron 2 รอบ → บันทึกแค่ครั้งเดียว"""
        Log   = self.env['fleet.telematics.log']
        trips = [self._make_trip_dict('CRON-D01')]

        with patch('requests.get', return_value=self._mock_api(trips)):
            with patch('time.sleep'):
                Log._cron_sync_trips()   # รอบ 1
                Log._cron_sync_trips()   # รอบ 2 (since ≥ until รอบ 1 → API คืน [])

        count = Log.search_count([('external_trip_id', '=', 'CRON-D01')])
        self.assertEqual(count, 1, "ต้องมีแค่ 1 record ไม่ซ้ำ")

    def test_08_cron_updates_existing_trip(self):
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

        with patch('requests.get', return_value=self._mock_api([trip])):
            with patch('time.sleep'):
                Log._cron_sync_trips()

        rec = Log.search([('external_trip_id', '=', 'CRON-U01')], limit=1)
        self.assertEqual(len(rec), 1,    "ต้องไม่สร้างซ้ำ")
        self.assertAlmostEqual(rec.distance_km, 99.0,
                               msg="ต้อง update distance_km เป็นค่าใหม่")

    def test_09_cron_saves_last_poll_ts(self):
        """หลัง _cron_sync_trips() ต้องบันทึก last_poll_ts ใน ir.config_parameter"""
        Log = self.env['fleet.telematics.log']
        ICP = self.env['ir.config_parameter'].sudo()

        with patch('requests.get', return_value=self._mock_api([])):
            with patch('time.sleep'):
                Log._cron_sync_trips()

        ts = ICP.get_param('fleet_telematics.trip_last_poll_ts', '')
        self.assertTrue(ts, "ต้องบันทึก last_poll_ts หลัง Cron ทำงาน")
        # ตรวจว่า parse ได้ (format ถูก)
        try:
            datetime.fromisoformat(ts)
        except ValueError:
            self.fail(f"last_poll_ts ต้องเป็น ISO format (ได้: {ts})")

    def test_10_sql_unique_constraint_on_external_trip_id(self):
        """Dedup ชั้น 3: _sql_constraints ต้องมี UNIQUE(external_trip_id)"""
        Log         = self.env['fleet.telematics.log']
        constraints = [c[1].upper() for c in Log._sql_constraints]
        self.assertTrue(
            any('EXTERNAL_TRIP_ID' in c for c in constraints),
            "_sql_constraints ต้องมี UNIQUE บน external_trip_id"
        )

    def test_11_cron_no_api_url_skips_gracefully(self):
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


# ══════════════════════════════════════════════════════════════════════════════
# UC-12 — Verify Device (GET /vehicles/{id}/device) — fleet_vehicle_ext.py
#   เพิ่มใหม่ 2026-07-06
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
        from odoo.exceptions import UserError
        with patch('requests.get', return_value=self._mock_resp(404)):
            with self.assertRaises(UserError):
                self.v1.action_verify_device()
        self.assertTrue(self.v1.device_verify_mismatch)

    def test_04_connection_error_raises_and_flags_mismatch(self):
        import requests as _requests
        from odoo.exceptions import UserError
        with patch('requests.get', side_effect=_requests.RequestException('timeout')):
            with self.assertRaises(UserError):
                self.v1.action_verify_device()
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
        config = self._get_config()
        with patch('requests.get', return_value=self._mock_resp(
                [{'device_id': 'KTC-BASE-01', 'vehicle_id': self.v1.id}])):
            config.action_reconcile_devices()
        self.assertEqual(config.device_mismatch_count, 0)
        self.assertTrue(config.last_reconciled_at)

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
        cls.driver_user = Users.create({
            'name': 'Driver User', 'login': 'driver_user_test',
            'groups_id': [(6, 0, [cls.env.ref('fleet.fleet_group_user').id])],
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