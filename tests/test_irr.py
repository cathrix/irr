import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

spec = importlib.util.spec_from_file_location('irr', Path(__file__).resolve().parents[1] / 'scripts/irr.py')
irr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(irr)


def members_by_set(mapping):
    """Stub for direct_members(cfg, set_name)."""
    def stub(cfg, set_name):
        return mapping[set_name]
    return stub


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = irr.load_config(Path(__file__).resolve().parents[1] / 'config/irr.json')

    def test_threshold_and_bounded_exception(self):
        data = {'AS-EXACT': list(range(1, 101)), 'AS-LARGE': list(range(1, 102)),
                'AS-MOEDOVE': list(range(1, 501))}
        asns, excluded, counts = irr.select_asns(self.cfg, list(data), data.__getitem__)
        self.assertEqual(len(asns), 500)
        self.assertEqual(len(excluded), 1)
        self.assertIn('AS-LARGE (101 ASNs, maximum 100)', excluded[0])
        data['AS-MOEDOVE'].append(501)
        asns, excluded, counts = irr.select_asns(self.cfg, list(data), data.__getitem__)
        self.assertEqual(len(asns), 100)
        self.assertEqual(len(excluded), 2)
        self.assertEqual({name: (count, limit) for name, count, limit in counts},
                         {'AS-EXACT': (100, 100), 'AS-LARGE': (101, 100), 'AS-MOEDOVE': (501, 500)})

    def test_customer_depth_is_one_and_own_set_is_excluded_from_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)

            def bgpq(cfg, flags, objects):
                if '-t' in flags:
                    self.assertEqual(flags, ['-t', '-L', '1'])
                    self.assertEqual(objects, ['AS-CHILD'])
                    return [150289]
                self.assertEqual(objects, ['AS112', 'AS62748', 'AS150289'])
                self.assertNotIn('-A', flags)
                return [{'prefix': '192.0.2.0/24', 'exact': True}] if '-4' in flags else []

            stub = members_by_set({
                self.cfg['customer_set']: ['AS112', 'AS-CHILD'],
                self.cfg['full_set']: ['AS62748', self.cfg['target_set']],
            })
            with patch.object(irr, 'direct_members', side_effect=stub), patch.object(irr, 'bgpq', side_effect=bgpq):
                irr.generate(self.cfg, output)

            conf = (output / 'irr.conf').read_text()
            self.assertIn('define ALL_ASNS = [\n  AS112,\n  AS62748,\n  AS150289\n];', conf)
            self.assertIn('define ALL_PREFIXES_V6 = [];', conf)
            self.assertFalse((output / 'asns.txt').exists())

            report = (output / 'result.txt').read_text()
            self.assertIn('ASNs: 3 (+0)', report)
            self.assertIn('IPv4 Prefixes: 1 (+0)', report)
            self.assertIn('IPv6 Prefixes: 0 (+0)', report)
            self.assertIn('Excluded AS-SET:\n  none', report)
            self.assertEqual(sorted(p.name for p in output.iterdir()), ['irr.conf', 'result.txt'])

    def test_result_report_delta_and_round_trip(self):
        previous = {'asns': 320, 'v4': 1500, 'v6': 9231}
        report = irr.result_report('2026-11-06T23:55:11Z', [1] * 324, [1] * 1421, [1] * 9000,
                                   ['AS-BIG (323 ASNs, maximum 100)'], previous)
        self.assertEqual(report.splitlines()[:6], [
            'Generated at 2026-11-06T23:55:11Z', '',
            'ASNs: 324 (+4)',
            'IPv4 Prefixes: 1421 (-79)',
            'IPv6 Prefixes: 9000 (-231)', '',
        ])
        self.assertIn('Excluded AS-SET:\n  AS-BIG (323 ASNs, maximum 100)', report)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'result.txt'
            path.write_text(report)
            self.assertEqual(irr.previous_counts(path), {'asns': 324, 'v4': 1421, 'v6': 9000})
            self.assertIsNone(irr.previous_counts(Path(directory) / 'missing.txt'))
            path.write_text('garbage')
            self.assertIsNone(irr.previous_counts(path))

    def test_irr_conf_layout(self):
        conf = irr.irr_conf([112, 62748], [], [], '2026-11-06T23:25:49Z')
        self.assertEqual(conf.splitlines()[:2], ['# Generated at 2026-11-06T23:25:49Z', ''])
        self.assertIn('define ALL_ASNS = [\n  AS112,\n  AS62748\n];', conf)
        self.assertTrue(conf.index('ALL_ASNS') < conf.index('ALL_PREFIXES_V4') < conf.index('ALL_PREFIXES_V6'))

    def test_empty_result_is_not_published(self):
        with tempfile.TemporaryDirectory() as directory:
            stub = members_by_set({self.cfg['customer_set']: [], self.cfg['full_set']: ['AS62748']})
            with patch.object(irr, 'direct_members', side_effect=stub):
                with self.assertRaises(RuntimeError):
                    irr.generate(self.cfg, Path(directory))

    def test_irrd_byte_framing(self):
        data = b'AS-CHILD AS112\n'
        stream = io.BytesIO(b'A' + str(len(data)).encode() + b'\n' + data + b'C\n')
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.makefile.return_value = stream
        with patch.object(irr.socket, 'create_connection', return_value=connection):
            self.assertEqual(irr.direct_members(self.cfg, 'AS-CATHRIX-CUST'), ['AS-CHILD', 'AS112'])
        connection.sendall.assert_called_once_with(b'!!\n!iAS-CATHRIX-CUST\n')

    def test_upload_replaces_members_and_removes_implicit_membership(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            current = 'as-set: AS-CR\ndescr: Existing description\nmembers: AS-OLD\n AS1\nmbrs-by-ref: ANY\nmnt-by: MNT-CN-578\nsource: ARIN\n'
            with patch.dict(irr.os.environ, {'ARIN_API_KEY': 'test'}), patch.object(irr, 'arin_request', side_effect=[current, 'ok']) as api:
                irr.upload(self.cfg, output, {112})
            method, path, key, payload = api.call_args.args
            self.assertEqual((method, path), ('PUT', 'as-set/AS-CR'))
            self.assertIn(b'descr: Existing description', payload)
            self.assertIn(b'members: AS112', payload)
            self.assertNotIn(b'AS62748', payload)
            self.assertNotIn(b'ANY', payload)
            self.assertNotIn(b'AS-OLD', payload)

    def test_create_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch.dict(irr.os.environ, {'ARIN_API_KEY': 'test'}), patch.object(irr, 'arin_request', side_effect=[None, 'ok']) as api:
                irr.upload(self.cfg, output, {112})
            self.assertEqual(api.call_args.args[:2], ('POST', 'as-set?orgHandle=CN-578'))


if __name__ == '__main__':
    unittest.main()
