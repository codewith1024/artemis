"""Explicit observation mode cannot enter the database path."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import qnh_capture_common as C
import qnh_capture_pipeline as P
import qnh_product_scan as S


class ObservationEntryTests(unittest.TestCase):
    def test_exception_mode_requires_capture_only_and_disallows_commit(self):
        for extra in ([], ['--commit'], ['--capture-only', '--commit']):
            with mock.patch('sys.argv', ['qnh_capture_pipeline.py', '--observe-exceptions', *extra]), \
                 mock.patch.object(P, 'run_capture') as capture, \
                 mock.patch.object(P.loader, '_open_connection') as connect:
                with self.assertRaises(SystemExit) as raised:
                    P.main()
                self.assertEqual(raised.exception.code, 2)
                capture.assert_not_called()
                connect.assert_not_called()

    def test_observation_mode_is_passed_to_scanner(self):
        # The actual CLI rejects the unsafe combinations above; this checks forwarding.
        import argparse
        args = argparse.Namespace(profile='xiniu-changan', profiles_file=C.DEFAULT_PROFILE_FILE,
                                  device='device', out_dir='/tmp', count=0, verify_every=1,
                                  pace='ultra', checkpoint_every=1, tall_screen=True, record=False,
                                  dismiss_order_dialogs=False, require_helper=False, category='A/B',
                                  carry_baseline=None, resume=None, observe_exceptions=True)
        with tempfile.TemporaryDirectory() as directory:
            args.out_dir = directory
            process = mock.Mock()
            process.wait.return_value = 1
            with mock.patch.object(P.subprocess, 'Popen', return_value=process) as popen:
                P.run_capture(args, C.load_profile('xiniu-changan'))
            self.assertIn('--observe-exceptions', popen.call_args.args[0])

    def test_recovery_commands_preserve_capture_only_and_observation_flags(self):
        import argparse
        from tests.unit.test_qnh_pipeline_safety import write_manifest
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_manifest(directory, state='partial')
            args = argparse.Namespace(profile='xiniu-changan', profiles_file=C.DEFAULT_PROFILE_FILE,
                                      device='device', out_dir=directory, count=0, verify_every=1,
                                      pace='ultra', checkpoint_every=1, tall_screen=True, record=False,
                                      dismiss_order_dialogs=False, require_helper=False, category='A/B',
                                      capture_only=True, observe_exceptions=True)
            for command in (P._resume_hint(manifest, args), P._restart_hint(args)):
                self.assertIn('--capture-only', command)
                self.assertIn('--observe-exceptions', command)
                self.assertNotIn('--commit', command)

    def test_resume_hint_of_observed_artifact_keeps_the_safe_flags(self):
        # 观测契约产物的续跑命令必须自带两个安全开关：只看命令行参数的话，
        # 用户没带 --observe-exceptions 时给出的命令会被扫描器直接拒。
        import argparse
        from tests.unit.test_qnh_pipeline_safety import write_manifest
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_manifest(directory, state='partial')
            value = C.read_json(manifest)
            value['capture']['observation_contract'] = 'v1'
            C.atomic_write_json(manifest, value, overwrite=True)
            args = argparse.Namespace(profile='xiniu-changan', profiles_file=C.DEFAULT_PROFILE_FILE,
                                      device='device', out_dir=directory, count=0, verify_every=1,
                                      pace='ultra', checkpoint_every=1, tall_screen=True, record=False,
                                      dismiss_order_dialogs=False, require_helper=False, category='A/B',
                                      capture_only=False, observe_exceptions=False)
            command = P._resume_hint(manifest, args)
            self.assertIn('--capture-only', command)
            self.assertIn('--observe-exceptions', command)
            self.assertNotIn('--commit', command)

    def test_unknown_details_are_rejected_without_explicit_observation_mode(self):
        nodes = S.parse_nodes((Path(__file__).parents[1] / 'fixtures/qnh_multispec_detail.xml').read_text())
        row = {'SKU': '123', '_channel_observations': {
            '京东到家': {'observation': 'row_absent', 'section_status': '已上架', 'reason': 'no row'}}}
        with mock.patch.object(S.CTX, 'observe_exceptions', False), \
             mock.patch.object(S.SKU_CAPTURE, 'parse_sku_pages', return_value=[row]):
            with self.assertRaisesRegex(C.CaptureContractError, 'observe-exceptions'):
                S.collect_sku_detail({}, nodes, 1080, 4560)


if __name__ == '__main__':
    unittest.main()
