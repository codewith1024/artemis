"""Tests for the observation-exception contract on the category pipeline.

离线：不连设备、不连库、不入库。观测终态 manifest 按扫描器（qnh_observation）
产出的形状离线合成，并带一份离线合成的逐屏列表证据，让 common 的 relaxed
门禁（例外凭据 + 短收的列表证据）真跑一遍：

    state = observed_with_exceptions
    capture.observation_contract = "v1"
    progress.observation_complete = true
    progress.business_complete = false
    progress.exceptions = [{type: platform_count_mismatch,
                             platform_count: N, visible_card_count: M}]
"""

import csv
import os
import sys
import tempfile
import unittest
import uuid
from unittest import mock

import qnh_capture_common as C
import qnh_capture_pipeline as capture_pipeline
import qnh_category_pipeline as category_pipeline
from tests.unit.test_qnh_manifest_pipeline import complete_row

SEGMENT = {"order": 1, "top": "休闲零食", "leaf": "薯片薯条", "expected_count": 2}
CATEGORY = f"{SEGMENT['top']}/{SEGMENT['leaf']}"
CARD = {"name": "商品A", "spec": "100g", "barcode": "6900000000001"}

PAGE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
  <node text="{leaf}（{count}）" bounds="[0,100][300,160]" class="android.widget.TextView"/>
  <node class="android.widget.ScrollView" bounds="[380,200][1000,1800]">
    <node bounds="[380,200][1000,600]" class="android.view.ViewGroup">
      <node text="{name}" bounds="[380,220][900,260]" class="android.widget.TextView"/>
      <node text="规格：{spec}" bounds="[380,270][900,300]" class="android.widget.TextView"/>
      <node text="条码:{barcode}" bounds="[380,310][900,340]" class="android.widget.TextView"/>
    </node>
    <node text="已加载全部数据" bounds="[0,0][0,0]" class="android.widget.TextView"/>
  </node>
</hierarchy>
"""


def count_exception(platform_count, visible_card_count):
    return {"type": "platform_count_mismatch",
            "platform_count": platform_count,
            "visible_card_count": visible_card_count}


def product_rows(*names):
    return [complete_row(index, name, f"69000000000{index:02d}")
            for index, name in enumerate(names, 1)]


def list_evidence(directory, *, leaf=SEGMENT["leaf"], category=CATEGORY,
                  platform_count=2, cards=(CARD,)):
    """合成逐屏列表证据：两屏同卡 + 末尾标记（证明读尽）。"""
    pages = []
    for index in (1, 2):
        path = os.path.join(directory, f"page_{index:03d}.xml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(PAGE_XML.format(leaf=leaf, count=platform_count, **cards[0]))
        pages.append({"path": path, "sha256": C.sha256_file(path)})
    evidence = {"schema_version": C.LIST_EVIDENCE_SCHEMA_VERSION,
                "category": category, "platform_count": platform_count,
                "cards": [dict(card) for card in cards],
                "list_exhausted": True, "pages": pages}
    path = os.path.join(directory, "inventory.json")
    C.atomic_write_json(path, evidence)
    return path


def observed_manifest(directory, *, rows, platform_count, exceptions,
                      name="qnh_scan_observed",
                      state=category_pipeline.OBSERVED_STATE, contract="v1",
                      observation_complete=True, business_complete=False,
                      evidence_path=None):
    """合成一个观测终态分片产物（CSV + manifest）。"""
    prefix = os.path.join(directory, name)
    csv_path = prefix + ".csv"
    for index, row in enumerate(rows, 1):
        row["序号"] = str(index)
        row["证据前缀"] = prefix
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=C.FULL_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    artifacts = {"csv_path": csv_path, "evidence_prefix": prefix,
                 "video_path": None, "csv_sha256": C.sha256_file(csv_path)}
    if evidence_path:
        artifacts["list_evidence_path"] = evidence_path
        artifacts["list_evidence_sha256"] = C.sha256_file(evidence_path)
    manifest = C.build_manifest(
        capture_id=str(uuid.uuid4()), batch_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()), state=state,
        profile=C.load_profile("xiniu-changan"), device_id="R28M31LJX0E",
        scope={"list_title": SEGMENT["leaf"], "category": CATEGORY,
               "expected_product_count": platform_count, "requested_count": 0},
        capture={"method": "app_ui", "started_at": "2026-10-09T10:00:00",
                 "run_started_at": "2026-10-09T10:00:00",
                 "finished_at": "2026-10-09T10:01:00",
                 "collector_version": "test", "pace": "ultra",
                 "tall_screen": True, "verify_every": 1,
                 "checkpoint_every": 25, "record": False,
                 "dismiss_order_dialogs": False},
        artifacts=artifacts, rows=rows)
    manifest["capture"]["observation_contract"] = contract
    manifest["progress"].update({"observation_complete": observation_complete,
                                 "business_complete": business_complete,
                                 "exceptions": exceptions})
    manifest_path = prefix + ".manifest.json"
    C.atomic_write_json(manifest_path, manifest)
    return manifest_path


def observed_segment(root, **kwargs):
    """目录里放一份观测终态分片产物（带列表证据）。"""
    directory = os.path.join(root, "001")
    os.makedirs(directory, exist_ok=True)
    kwargs.setdefault("rows", product_rows("商品A"))
    kwargs.setdefault("platform_count", 2)
    kwargs.setdefault("exceptions", [count_exception(2, 1)])
    evidence_path = list_evidence(directory, platform_count=kwargs["platform_count"])
    return observed_manifest(directory, evidence_path=evidence_path, **kwargs)


class FakeProcess:
    """假装扫描器进程：立刻退出，不碰设备。"""

    returncode = 0

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


class ObserveExceptionsCliTests(unittest.TestCase):
    """--observe-exceptions 只允许 capture-only，且与 --commit 互斥。"""

    def test_observe_exceptions_can_commit_the_aggregate(self):
        # 聚合是全店并集：观测终态的聚合现在可以入库（未知的渠道格记 unknown，
        # 快照状态 partial，例外清单写进 error_message）。
        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "out")
            argv = ["qnh_category_pipeline.py", "--segments-file",
                    os.path.join(root, "missing-segments.json"),
                    "--out-dir", out_dir, "--observe-exceptions", "--commit"]
            with mock.patch.object(sys, "argv", argv):
                # 清单文件不存在 → 读清单就失败（不是参数互斥）。
                with self.assertRaises(FileNotFoundError):
                    category_pipeline.main()

    def test_single_category_observation_stays_capture_only(self):
        # 单类目的观测产物不是全店快照：入库会冒充门店快照，所以仍只 capture-only。
        with tempfile.TemporaryDirectory() as root:
            out_dir = os.path.join(root, "out")
            argv = ["qnh_capture_pipeline.py", "--observe-exceptions", "--commit"]
            with mock.patch.object(sys, "argv", argv):
                with self.assertRaises(SystemExit) as ctx:
                    capture_pipeline.main()
            self.assertEqual(ctx.exception.code, 2)

    def test_observe_exceptions_passes_allow_observed_and_records_state(self):
        with tempfile.TemporaryDirectory() as root:
            inventory = os.path.join(root, "segments.json")
            C.atomic_write_json(inventory, {"segments": [dict(SEGMENT)]})
            out_dir = os.path.join(root, "out")
            calls = {}

            def fake_run_segment(root_dir, segment, **kwargs):
                calls.update(kwargs)
                directory = os.path.join(root_dir, "001")
                os.makedirs(directory, exist_ok=True)
                path = observed_segment(root_dir)
                report = C.validate_manifest(
                    path, require_complete=False,
                    expected_profile=C.load_profile("xiniu-changan"))
                return path, report

            argv = ["qnh_category_pipeline.py", "--segments-file", inventory,
                    "--out-dir", out_dir, "--observe-exceptions", "--capture-only"]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(category_pipeline, "run_segment",
                                   side_effect=fake_run_segment), \
                 mock.patch.object(category_pipeline, "read_full_store_count",
                                   return_value=1), \
                 mock.patch.object(category_pipeline, "merge_segments",
                                   return_value="merged.manifest.json"), \
                 mock.patch.object(category_pipeline.subprocess, "run"):
                category_pipeline.main()
            self.assertTrue(calls["allow_observed"])
            state = C.read_json(os.path.join(out_dir, "capture.state.json"))
            record = state["completed"]["1"]
            self.assertEqual(record["state"], category_pipeline.OBSERVED_STATE)
            self.assertFalse(record["business_complete"])
            self.assertTrue(record["observation_complete"])
            # 平台计数与实抓行数分开记，差看得见。
            self.assertEqual(record["platform_expected_count"], 2)
            self.assertEqual(record["platform_actual_count"], 1)


class ValidateSegmentObservationTests(unittest.TestCase):
    """validate_segment 的观测分支：严格拒，放行只对有凭据的例外。"""

    def build(self, root, **kwargs):
        return observed_segment(root, **kwargs)

    def test_strict_mode_rejects_observed_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.build(root)
            report = category_pipeline.validate_segment(path, dict(SEGMENT))
            self.assertFalse(report.ok)

    def test_relaxed_mode_accepts_observed_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.build(root)
            report = category_pipeline.validate_segment(path, dict(SEGMENT),
                                                        allow_observed=True)
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.row_count, 1)
            self.assertEqual(report.expected_count, 2)

    def test_relaxed_mode_keeps_original_expected_count(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.build(root)
            segment = {**SEGMENT, "expected_count": 5}
            report = category_pipeline.validate_segment(path, segment,
                                                        allow_observed=True)
            self.assertTrue(report.ok, report.errors)
            # 原目标数保留：既不刷新为平台计数，也不刷新为实抓行数。
            self.assertEqual(segment["expected_count"], 5)

    def test_relaxed_mode_rejects_missing_list_evidence(self):
        # 短收没有逐屏列表证据：拿不出凭据就不许放行。
        with tempfile.TemporaryDirectory() as root:
            directory = os.path.join(root, "001")
            os.makedirs(directory)
            path = observed_manifest(directory, rows=product_rows("商品A"),
                                     platform_count=2,
                                     exceptions=[count_exception(2, 1)])
            report = category_pipeline.validate_segment(path, dict(SEGMENT),
                                                        allow_observed=True)
            self.assertFalse(report.ok)

    def test_relaxed_mode_rejects_missing_count_exception(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.build(root, exceptions=[])
            report = category_pipeline.validate_segment(path, dict(SEGMENT),
                                                        allow_observed=True)
            self.assertFalse(report.ok)

    def test_relaxed_mode_rejects_count_exception_of_another_count(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.build(root, exceptions=[count_exception(99, 1)])
            report = category_pipeline.validate_segment(path, dict(SEGMENT),
                                                        allow_observed=True)
            self.assertFalse(report.ok)

    def test_relaxed_mode_rejects_business_complete_manifest(self):
        # 业务完整就不该是观测终态：business_complete 必须仍是 false。
        with tempfile.TemporaryDirectory() as root:
            path = self.build(root, business_complete=True)
            report = category_pipeline.validate_segment(path, dict(SEGMENT),
                                                        allow_observed=True)
            self.assertFalse(report.ok)

    def test_relaxed_mode_rejects_unknown_exception_type(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.build(root, exceptions=[{"type": "whatever"}])
            report = category_pipeline.validate_segment(path, dict(SEGMENT),
                                                        allow_observed=True)
            self.assertFalse(report.ok)

    def test_relaxed_mode_rejects_wrong_contract_version(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.build(root, contract="v2")
            report = category_pipeline.validate_segment(path, dict(SEGMENT),
                                                        allow_observed=True)
            self.assertFalse(report.ok)

    def test_relaxed_mode_rejects_other_category(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.build(root)
            segment = {**SEGMENT, "top": "别的类目", "leaf": "薯片薯条"}
            report = category_pipeline.validate_segment(path, segment,
                                                        allow_observed=True)
            self.assertFalse(report.ok)
            self.assertTrue(any("类目不一致" in error for error in report.errors),
                            report.errors)


class RunSegmentObservationTests(unittest.TestCase):
    """run_segment：观测终态是本轮终态，不空转 20 attempt。"""

    def test_observed_manifest_is_terminal_in_observation_mode(self):
        with tempfile.TemporaryDirectory() as root:
            path = observed_segment(root)
            with mock.patch.object(category_pipeline.subprocess, "Popen") as popen, \
                 mock.patch.object(category_pipeline.time, "sleep"):
                manifest_path, report = category_pipeline.run_segment(
                    root, dict(SEGMENT), dismiss_order_dialogs=False,
                    max_attempts=20, allow_observed=True)
            self.assertEqual(manifest_path, path)
            self.assertTrue(report.ok, report.errors)
            # 已经收口：不该再起扫描器。
            popen.assert_not_called()

    def test_strict_mode_rejects_observed_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            observed_segment(root)
            with mock.patch.object(category_pipeline.subprocess, "Popen",
                                   return_value=FakeProcess()), \
                 mock.patch.object(category_pipeline.time, "sleep"):
                with self.assertRaises(C.CaptureContractError) as ctx:
                    category_pipeline.run_segment(root, dict(SEGMENT),
                                                 dismiss_order_dialogs=False,
                                                 max_attempts=1)
            self.assertIn("分片重试耗尽", str(ctx.exception))

    def test_observation_mode_passes_flag_to_child(self):
        with tempfile.TemporaryDirectory() as root:
            commands = []

            def fake_popen(command, **kwargs):
                commands.append(command)
                return FakeProcess()

            with mock.patch.object(category_pipeline.subprocess, "Popen",
                                   side_effect=fake_popen), \
                 mock.patch.object(category_pipeline.time, "sleep"):
                with self.assertRaises(C.CaptureContractError):
                    category_pipeline.run_segment(root, dict(SEGMENT),
                                                 dismiss_order_dialogs=False,
                                                 max_attempts=1,
                                                 allow_observed=True)
            self.assertIn("--observe-exceptions", commands[0])
            # 子进程的 --observe-exceptions 只允许配 --capture-only。
            self.assertIn("--capture-only", commands[0])

    def test_strict_mode_does_not_pass_flag_to_child(self):
        with tempfile.TemporaryDirectory() as root:
            commands = []

            def fake_popen(command, **kwargs):
                commands.append(command)
                return FakeProcess()

            with mock.patch.object(category_pipeline.subprocess, "Popen",
                                   side_effect=fake_popen), \
                 mock.patch.object(category_pipeline.time, "sleep"):
                with self.assertRaises(C.CaptureContractError):
                    category_pipeline.run_segment(root, dict(SEGMENT),
                                                 dismiss_order_dialogs=False,
                                                 max_attempts=1)
            self.assertNotIn("--observe-exceptions", commands[0])


class MergeObservationTests(unittest.TestCase):
    """聚合遇观测终态分片：明确拒并列出异常清单，不假聚合成功。"""

    def make_state(self, root, manifest_path):
        state_path = os.path.join(root, "capture.state.json")
        C.atomic_write_json(state_path, {
            "capture_id": str(uuid.uuid4()), "batch_id": str(uuid.uuid4()),
            "dismiss_order_dialogs": False,
            "completed": {"1": {"manifest_path": manifest_path,
                                "manifest_sha256": C.sha256_file(manifest_path),
                                "expected_count": 2}},
        })
        return state_path

    def test_merge_rejects_observed_segment(self):
        with tempfile.TemporaryDirectory() as root:
            path = observed_segment(root)
            state_path = self.make_state(root, path)
            with self.assertRaises(C.CaptureContractError) as ctx:
                category_pipeline.merge_segments(
                    root, {"full_store_count": 2}, [dict(SEGMENT)], state_path,
                    full_count=2)
            message = str(ctx.exception)
            self.assertIn("observed_with_exceptions", message)
            # 异常清单要带出来：哪一片、差在哪、证据是什么。
            self.assertIn("platform_count_mismatch", message)
            self.assertIn("薯片薯条", message)
            # 不产出成功聚合。
            self.assertFalse([name for name in os.listdir(root)
                              if name.startswith("qnh_category_full")])


if __name__ == "__main__":
    unittest.main()
