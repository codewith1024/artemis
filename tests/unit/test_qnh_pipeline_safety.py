"""Safety-boundary tests for the QNH capture orchestrator and strict loader."""

import argparse
import csv
import json
import os
import tempfile
import unittest
import uuid
from decimal import Decimal
from unittest import mock

import qnh_capture_common as C
import qnh_capture_pipeline as pipeline
import qnh_load_db as loader
import qnh_validate_db as validator


def complete_row(prefix):
    row = {column: "" for column in C.FULL_COLUMNS}
    row.update({
        "序号": "1",
        "商品名称": "测试商品",
        "条码": "6900000000001",
        "店内码": "A-1",
        "货号": "SKU-1",
        "状态": "成功",
        "时刻": "10:00:30",
        "采集时间": "2026-10-09T10:00:30",
        "证据前缀": prefix,
        "规格": "100g",
        "月售": "10",
        "库存": "2",
        "售罄": "否",
        "活动品": "否",
        "美团闪购价": "10",
        "饿了么价": "10",
        "京东到家价": "10",
        "采购价": "5/袋",
        "价格原文": "10 | 10 | 10 | 5/袋",
        "开放平台标识": "OPEN-1",
        "SPU": "SPU-1",
        "SKU": "SKU-1",
        "重量": "100克(g)",
        "采购价_详情": "5",
        "采购单位_详情": "袋",
        "美团闪购状态": "已上架",
        "美团闪购零售价": "10",
        "美团闪购库存": "2",
        "饿了么状态": "未上线",
        "京东到家状态": "已下架",
        "可售日期": "不限",
        "礼袋": "0个礼袋",
        "是否力荐": "否",
        "是否标品": "标品",
    })
    return row


def write_manifest(directory, *, state="success", profile=None):
    prefix = os.path.join(directory, "qnh_scan_20261009-100000_test")
    csv_path = prefix + ".csv"
    row = complete_row(prefix)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
        writer.writeheader()
        writer.writerow(row)
    profile = profile or C.load_profile("xiniu-changan")
    manifest = C.build_manifest(
        capture_id=str(uuid.uuid4()),
        batch_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        state=state,
        profile=profile,
        device_id="R28M31LJX0E",
        scope={
            "list_title": "全部商品",
            "category": None,
            "expected_product_count": 1,
            "requested_count": 0,
        },
        capture={
            "method": "app_ui",
            "started_at": "2026-10-09T10:00:00",
            "run_started_at": "2026-10-09T10:00:00",
            "finished_at": "2026-10-09T10:01:00",
            "collector_version": "test",
            "pace": "ultra",
            "tall_screen": True,
            "verify_every": 1,
            "checkpoint_every": 50,
            "record": False,
        },
        artifacts={
            "csv_path": csv_path,
            "evidence_prefix": prefix,
            "video_path": None,
            "csv_sha256": C.sha256_file(csv_path),
        },
        rows=[row],
    )
    manifest_path = prefix + ".manifest.json"
    C.atomic_write_json(manifest_path, manifest, overwrite=False)
    return manifest_path


class ValidatorPrecisionTests(unittest.TestCase):
    def test_decimal_comparison_matches_database_scale(self):
        self.assertTrue(validator._field_equal(
            Decimal("22.6667"), Decimal("22.666667")))
        self.assertFalse(validator._field_equal(
            Decimal("22.6666"), Decimal("22.666667")))
        self.assertFalse(validator._field_equal("22.6667", Decimal("22.666667")))


class StrictLoaderBoundaryTests(unittest.TestCase):
    def test_partial_manifest_is_rejected_before_database_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = write_manifest(directory, state="partial")
            with mock.patch.object(loader, "_open_connection") as open_connection:
                with self.assertRaises(loader.LoadError):
                    loader.load_capture(
                        manifest_path=manifest_path,
                        require_complete=False,
                        commit=True,
                    )
            open_connection.assert_not_called()

    def test_profile_mismatch_is_rejected_before_database_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = write_manifest(directory)
            wrong_profile = C.load_profile("xiniu-changan") | {"tenant_id": "wrong"}
            with mock.patch.object(loader, "_open_connection") as open_connection:
                with self.assertRaises(loader.LoadError):
                    loader.load_capture(
                        manifest_path=manifest_path,
                        commit=True,
                        expected_profile=wrong_profile,
                    )
            open_connection.assert_not_called()
    def test_direct_csv_commit_requires_explicit_legacy_partial_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = write_manifest(directory)
            csv_path = C.read_json(manifest_path)["artifacts"]["csv_path"]
            with mock.patch.object(loader, "_open_connection") as open_connection:
                with self.assertRaises(loader.LoadError):
                    loader.load_capture(
                        csv_path=csv_path,
                        store_name="测试店",
                        warehouse_id="1",
                        tenant_id="2",
                        store_code="qnh_test",
                        status_override="success",
                        commit=True,
                    )
            open_connection.assert_not_called()
    def test_loader_cli_commit_requires_result_json(self):
        argv = ["qnh_load_db.py", "--manifest", "capture.manifest.json", "--commit"]
        with mock.patch("sys.argv", argv), \
                mock.patch.object(loader, "load_capture") as load_capture:
            with self.assertRaises(SystemExit):
                loader.main()
        load_capture.assert_not_called()

    def test_loader_cli_pending_result_survives_final_write_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path = os.path.join(directory, "load.result.json")
            manifest_path = os.path.join(directory, "capture.manifest.json")
            argv = [
                "qnh_load_db.py", "--manifest", manifest_path, "--commit",
                "--result-json", result_path,
            ]
            db_result = {
                "ok": True,
                "snapshot_id": "11111111-1111-4111-8111-111111111111",
                "batch_id": "22222222-2222-4222-8222-222222222222",
                "status": "success",
                "rows": 1,
                "fact_rows": 3,
                "new_matches": 0,
                "committed": True,
                "recovered": False,
                "validation": {"ok": True},
            }
            real_atomic = C.atomic_write_json
            calls = 0

            def fail_final(path, value, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("disk full")
                return real_atomic(path, value, **kwargs)

            with mock.patch("sys.argv", argv), \
                    mock.patch.object(loader, "load_capture", return_value=db_result), \
                    mock.patch.object(loader.C, "atomic_write_json", side_effect=fail_final):
                with self.assertRaises(SystemExit):
                    loader.main()
            with open(result_path, encoding="utf-8") as fh:
                pending = json.load(fh)
            self.assertEqual(pending["state"], "commit_pending")


class OrchestratorBoundaryTests(unittest.TestCase):
    @staticmethod
    def args(directory):
        return argparse.Namespace(
            profile="xiniu-changan",
            profiles_file=C.DEFAULT_PROFILE_FILE,
            device=None,
            out_dir=directory,
            count=0,
            verify_every=1,
            pace="ultra",
            checkpoint_every=50,
            tall_screen=True,
            record=False,
            dismiss_order_dialogs=False,
            category=None,
            resume=None,
            from_manifest=None,
            capture_only=False,
            commit=False,
        )

    def test_direct_resume_inherits_parent_profile_and_device(self):
        with tempfile.TemporaryDirectory() as directory:
            profiles_path = os.path.join(directory, "profiles.json")
            profile_data = {
                "schema_version": 1,
                "profiles": {
                    "custom": {
                        "store_name": "自定义门店",
                        "warehouse_id": "10",
                        "tenant_id": "20",
                        "store_code": "qnh_custom",
                        "role": "competitor",
                        "default_device_serial": "DIFFERENT_DEFAULT",
                    }
                },
            }
            C.atomic_write_json(profiles_path, profile_data)
            profile = C.load_profile("custom", profiles_path)
            manifest_path = write_manifest(directory, state="partial", profile=profile)
            args = self.args(directory)
            args.profile = "custom"
            args.profiles_file = profiles_path
            args.resume = manifest_path
            process = mock.Mock()
            process.wait.return_value = 9
            with mock.patch.object(pipeline.subprocess, "Popen",
                                   return_value=process) as popen:
                _, exit_code = pipeline.run_capture(args, profile)
            self.assertEqual(exit_code, 9)
            command = popen.call_args.args[0]
            serial_index = command.index("--serial")
            self.assertEqual(command[serial_index + 1], "R28M31LJX0E")
            self.assertIn("custom", command)

    def test_run_capture_waits_for_scanner_cleanup_after_interrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.args(directory)
            profile = C.load_profile("xiniu-changan")
            process = mock.Mock()
            process.wait.side_effect = [KeyboardInterrupt(), 0]
            with mock.patch.object(pipeline.subprocess, "Popen", return_value=process):
                with self.assertRaises(C.CaptureContractError):
                    pipeline.run_capture(args, profile)
            self.assertEqual(process.wait.call_count, 2)
            self.assertEqual(process.wait.call_args_list[1].kwargs, {"timeout": 120})

    def test_resume_hint_quotes_paths_and_preserves_contract(self):
        with tempfile.TemporaryDirectory(prefix="qnh space ") as directory:
            args = self.args(directory)
            hint = pipeline._resume_hint(os.path.join(directory, "capture manifest.json"), args)
            self.assertIn("--profiles-file", hint)
            self.assertIn("--out-dir", hint)
            self.assertIn("--verify-every 1", hint)
            self.assertIn("--tall-screen", hint)
            self.assertIn("'", hint)

    def test_resume_hint_uses_manifest_capture_contract(self):
        with tempfile.TemporaryDirectory(prefix="qnh contract ") as directory:
            manifest_path = write_manifest(directory, state="partial")
            args = self.args("/wrong/output")
            hint = pipeline._resume_hint(manifest_path, args)
            self.assertIn(directory, hint)
            self.assertIn("--device R28M31LJX0E", hint)
            self.assertIn("--count 1", hint)

    def test_post_validate_resolves_relative_csv_from_manifest_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = write_manifest(directory)
            manifest = C.read_json(manifest_path)
            manifest["artifacts"]["csv_path"] = os.path.basename(
                manifest["artifacts"]["csv_path"])
            C.atomic_write_json(manifest_path, manifest)
            connection = mock.Mock()
            report = pipeline.validator.ValidationReport()
            with mock.patch.object(pipeline.loader, "_open_connection", return_value=connection), \
                    mock.patch.object(pipeline.validator, "validate_snapshot",
                                      return_value=report) as validate:
                pipeline._post_validate(manifest_path, {"snapshot_id": manifest["capture_id"]})
            csv_arg = validate.call_args.args[1]
            self.assertTrue(os.path.isabs(csv_arg))
            self.assertEqual(csv_arg, os.path.join(directory, os.path.basename(csv_arg)))
            connection.close.assert_called_once()

    def test_commit_pending_survives_final_result_write_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = write_manifest(directory)
            argv = [
                "qnh_capture_pipeline.py",
                "--from-manifest", manifest_path,
                "--commit",
            ]
            db_result = {
                "ok": True,
                "snapshot_id": C.read_json(manifest_path)["capture_id"],
                "batch_id": C.read_json(manifest_path)["batch_id"],
                "status": "success",
                "rows": 1,
                "fact_rows": 3,
                "new_matches": 0,
                "committed": True,
                "recovered": False,
                "validation": {"ok": True},
            }
            post = pipeline.validator.ValidationReport()
            real_atomic = C.atomic_write_json
            calls = 0

            def fail_final(path, value, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("disk full")
                return real_atomic(path, value, **kwargs)

            with mock.patch("sys.argv", argv), \
                    mock.patch.object(pipeline.loader, "load_capture", return_value=db_result), \
                    mock.patch.object(pipeline, "_post_validate", return_value=post), \
                    mock.patch.object(pipeline.C, "atomic_write_json", side_effect=fail_final):
                with self.assertRaises(SystemExit) as raised:
                    pipeline.main()
            self.assertEqual(raised.exception.code, 1)
            with open(pipeline._result_path(manifest_path), encoding="utf-8") as fh:
                pending = json.load(fh)
            self.assertEqual(pending["state"], "commit_pending")
            self.assertIsNone(pending["committed"])

    def test_invalid_profile_name_type_writes_restart_result(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = write_manifest(directory, state="partial")
            manifest = C.read_json(manifest_path)
            manifest["profile"]["name"] = ["bad"]
            C.atomic_write_json(manifest_path, manifest)
            argv = ["qnh_capture_pipeline.py", "--from-manifest", manifest_path]
            with mock.patch("sys.argv", argv), \
                    mock.patch.object(pipeline.loader, "load_capture") as load_capture:
                with self.assertRaises(SystemExit) as raised:
                    pipeline.main()
            self.assertEqual(raised.exception.code, 2)
            load_capture.assert_not_called()
            with open(pipeline._result_path(manifest_path), encoding="utf-8") as fh:
                result = json.load(fh)
            self.assertIn("restart_command", result)
            self.assertNotIn("resume_command", result)

    def test_invalid_capture_pace_writes_restart_result(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = write_manifest(directory, state="partial")
            manifest = C.read_json(manifest_path)
            manifest["capture"]["pace"] = ["bad"]
            C.atomic_write_json(manifest_path, manifest)
            argv = ["qnh_capture_pipeline.py", "--from-manifest", manifest_path]
            with mock.patch("sys.argv", argv):
                with self.assertRaises(SystemExit) as raised:
                    pipeline.main()
            self.assertEqual(raised.exception.code, 2)
            with open(pipeline._result_path(manifest_path), encoding="utf-8") as fh:
                result = json.load(fh)
            self.assertIn("restart_command", result)

    def test_boolean_expected_count_hint_falls_back_to_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = write_manifest(directory, state="failed")
            manifest = C.read_json(manifest_path)
            manifest["scope"]["expected_product_count"] = True
            C.atomic_write_json(manifest_path, manifest)
            hint = pipeline._resume_hint(manifest_path, self.args(directory))
            self.assertIn("--count 0", hint)
            self.assertNotIn("--count True", hint)

    def test_malformed_manifest_writes_gate_failure_result(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = os.path.join(directory, "broken.manifest.json")
            with open(manifest_path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            argv = ["qnh_capture_pipeline.py", "--from-manifest", manifest_path]
            with mock.patch("sys.argv", argv), \
                    mock.patch.object(pipeline.loader, "load_capture") as load_capture:
                with self.assertRaises(SystemExit) as raised:
                    pipeline.main()
            self.assertEqual(raised.exception.code, 2)
            load_capture.assert_not_called()
            with open(pipeline._result_path(manifest_path), encoding="utf-8") as fh:
                result = json.load(fh)
            self.assertEqual(result["state"], "gate_failed")

    def test_resume_hint_falls_back_when_expected_count_is_null(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = write_manifest(directory, state="failed")
            manifest = C.read_json(manifest_path)
            manifest["scope"]["expected_product_count"] = None
            C.atomic_write_json(manifest_path, manifest)
            hint = pipeline._resume_hint(manifest_path, self.args(directory))
            self.assertIn("--count 0", hint)
            self.assertNotIn("--count None", hint)

    def test_nonzero_scanner_exit_never_calls_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = os.path.join(directory, "failed.manifest.json")
            argv = ["qnh_capture_pipeline.py", "--out-dir", directory]
            with mock.patch("sys.argv", argv), \
                    mock.patch.object(pipeline, "run_capture", return_value=(manifest_path, 9)), \
                    mock.patch.object(pipeline.loader, "load_capture") as load_capture:
                with self.assertRaises(SystemExit) as raised:
                    pipeline.main()
            self.assertEqual(raised.exception.code, 1)
            load_capture.assert_not_called()
            result_path = pipeline._result_path(manifest_path)
            with open(result_path, encoding="utf-8") as fh:
                result = json.load(fh)
            self.assertEqual(result["state"], "scan_failed")
            self.assertFalse(result["ok"])

    def test_gate_failure_never_calls_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = write_manifest(directory, state="partial")
            argv = [
                "qnh_capture_pipeline.py",
                "--out-dir", directory,
                "--from-manifest", manifest_path,
            ]
            with mock.patch("sys.argv", argv), \
                    mock.patch.object(pipeline.loader, "load_capture") as load_capture:
                with self.assertRaises(SystemExit) as raised:
                    pipeline.main()
            self.assertEqual(raised.exception.code, 2)
            load_capture.assert_not_called()
            with open(pipeline._result_path(manifest_path), encoding="utf-8") as fh:
                result = json.load(fh)
            self.assertEqual(result["state"], "gate_failed")


if __name__ == "__main__":
    unittest.main()
