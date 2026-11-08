import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location('irr', Path(__file__).resolve().parents[1] / 'scripts/irr.py')
irr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(irr)


def members_by_set(mapping):
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
            self.assertIn('define ALL_ASNS = [\n  112,\n  62748,\n  150289\n];', conf)
            self.assertIn('define ALL_PREFIXES_V6 = [];', conf)

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
        self.assertIn('define ALL_ASNS = [\n  112,\n  62748\n];', conf)
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

    def test_upload_replaces_members_and_preserves_metadata(self):
        current = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<asSet xmlns="http://www.arin.net/regrws/core/v1">'
                   '<description><line number="0">Cathrix Network Inc.</line></description>'
                   '<orgHandle>CN-578</orgHandle>'
                   '<pocLinks><pocLinkRef description="Tech" function="T" handle="CNOC24-ARIN"/>'
                   '<pocLinkRef description="Admin" function="AD" handle="CNOC24-ARIN"/></pocLinks>'
                   '<source>ARIN</source><name>AS-CR</name></asSet>')
        with patch.dict(irr.os.environ, {'ARIN_API_KEY': 'test'}), \
                patch.object(irr, 'arin_request', side_effect=[current, 'ok']) as api:
            irr.upload(self.cfg, {112}, {62748})
        method, path, key, payload = api.call_args.args
        self.assertEqual((method, path), ('PUT', 'as-set/AS-CR'))
        self.assertIn(b'Cathrix Network Inc.', payload)
        self.assertIn(b'CNOC24-ARIN', payload)
        self.assertIn(b'<member name="AS112"', payload)
        self.assertNotIn(b'AS62748', payload)
        self.assertNotIn(b'name="AS-CATHRIX"', payload)

    def test_default_max_asns_is_configurable(self):
        cfg = dict(self.cfg, default_max_asns=5, exceptions={})
        data = {'AS-SMALL': [1, 2, 3, 4, 5], 'AS-BIG': [1, 2, 3, 4, 5, 6]}
        asns, excluded, _ = irr.select_asns(cfg, list(data), data.__getitem__)
        self.assertEqual(len(asns), 5)
        self.assertEqual(excluded, ['AS-BIG (6 ASNs, maximum 5)'])

    def test_exception_must_exceed_default(self):
        cfg = dict(self.cfg, default_max_asns=500, exceptions={'AS-MOEDOVE': 500})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cfg.json'
            path.write_text(json.dumps(cfg))
            with self.assertRaises(ValueError):
                irr.load_config(path)

    def test_invalid_default_max_asns_is_rejected(self):
        for bad in (0, -1, '100', 100.5, True, None):
            cfg = dict(self.cfg, default_max_asns=bad)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'cfg.json'
                path.write_text(json.dumps(cfg))
                with self.assertRaises(ValueError, msg=f'{bad!r} should be rejected'):
                    irr.load_config(path)

    def test_default_max_asns_bounds_every_set(self):
        cfg = dict(self.cfg, default_max_asns=5, exceptions={})
        data = {'AS-FIVE': [1, 2, 3, 4, 5], 'AS-SIX': [1, 2, 3, 4, 5, 6]}
        asns, excluded, counts = irr.select_asns(cfg, list(data), data.__getitem__)
        self.assertEqual(len(asns), 5)
        self.assertEqual(excluded, ['AS-SIX (6 ASNs, maximum 5)'])
        self.assertEqual(irr.asn_limit(cfg, 'AS-UNLISTED'), 5)

    def test_exception_overrides_default(self):
        cfg = dict(self.cfg, default_max_asns=5, exceptions={'AS-BIG': 10})
        data = {'AS-BIG': list(range(1, 11)), 'AS-MID': list(range(1, 7))}
        asns, excluded, _ = irr.select_asns(cfg, list(data), data.__getitem__)
        self.assertEqual(len(asns), 10)
        self.assertEqual(excluded, ['AS-MID (6 ASNs, maximum 5)'])
        self.assertEqual(irr.asn_limit(cfg, 'AS-BIG'), 10)

    def _run_sync(self, publish):
        def bgpq(cfg, flags, objects):
            if '-t' in flags:
                return []
            return [{'prefix': '192.0.2.0/24', 'exact': True}] if '-4' in flags else []

        stub = members_by_set({
            self.cfg['customer_set']: ['AS112'],
            self.cfg['full_set']: ['AS62748', self.cfg['target_set']],
        })
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(irr, 'direct_members', side_effect=stub), \
                patch.object(irr, 'bgpq', side_effect=bgpq), \
                patch.object(irr, 'upload') as up:
            irr.sync(self.cfg, Path(directory), publish=publish)
        return up

    def test_sync_publishes_same_asns_it_generated(self):
        up = self._run_sync(publish=True)
        up.assert_called_once()
        _, customers, own = up.call_args.args
        self.assertEqual(customers, {112})
        self.assertEqual(own, {62748})

    def test_sync_without_publish_skips_arin(self):
        self._run_sync(publish=False).assert_not_called()

    def test_get_omits_content_type_and_parses_errors(self):
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen['headers'] = {k.lower(): v for k, v in request.header_items()}
            raise urllib.error.HTTPError(
                request.full_url, 406, 'Not Acceptable', {},
                io.BytesIO(b'code:  E_AUTHENTICATION\nmessage:  The API key was invalid.\n'))

        with patch.object(irr.urllib.request, 'urlopen', side_effect=fake_urlopen):
            with self.assertRaises(RuntimeError) as caught:
                irr.arin_request('GET', 'as-set/AS-CR', 'secret-key')
        self.assertNotIn('content-type', seen['headers'])
        self.assertEqual(seen['headers']['authorization'], 'ApiKey secret-key')
        text = str(caught.exception)
        self.assertIn('E_AUTHENTICATION', text)
        self.assertNotIn('secret-key', text)

    def test_own_asn_is_never_published_to_target_set(self):
        with patch.dict(irr.os.environ, {'ARIN_API_KEY': 'test'}), \
                patch.object(irr, 'arin_request') as api:
            with self.assertRaises(ValueError) as caught:
                irr.upload(self.cfg, {112, 62748}, {62748})
            api.assert_not_called()
        self.assertIn('AS62748', str(caught.exception))

    def test_create_endpoint(self):
        with patch.dict(irr.os.environ, {'ARIN_API_KEY': 'test'}), \
                patch.object(irr, 'arin_request', side_effect=[None, 'ok']) as api:
            irr.upload(self.cfg, {112}, {62748})
        self.assertEqual(api.call_args.args[:2], ('POST', 'as-set?orgHandle=CN-578'))
        payload = api.call_args.args[3]
        self.assertIn(b'<member name="AS112"', payload)

    def test_arin_uses_xml_media_type(self):
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen['headers'] = {k.lower(): v for k, v in request.header_items()}
            raise urllib.error.HTTPError(request.full_url, 500, 'err', {}, io.BytesIO(b''))

        with patch.object(irr.urllib.request, 'urlopen', side_effect=fake_urlopen):
            with self.assertRaises(RuntimeError):
                irr.arin_request('GET', 'as-set/AS-CR', 'k')
        self.assertEqual(seen['headers']['accept'], 'application/xml')
        self.assertNotIn('content-type', seen['headers'])

    def test_poc_functions_map_to_correct_labels(self):
        payload = irr.as_set_xml('AS-CR', 'CN-578', ['x'],
                                 [('T', 'A-ARIN'), ('R', 'B-ARIN'), ('AD', 'C-ARIN')], [], [112])
        for expected in (b'description="Tech" function="T"',
                         b'description="Routing" function="R"',
                         b'description="Admin" function="AD"'):
            self.assertIn(expected, payload)

    def test_payload_is_ascii_only(self):
        self.assertIn(b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                      irr.as_set_xml('AS-CR', 'CN-578', ['x'], [], [], [112]))
        with self.assertRaises(UnicodeEncodeError):
            irr.as_set_xml('AS-CR', 'CN-578', ['\u6d4b\u8bd5'], [], [], [112])

    def test_parse_as_set_rejects_wrong_object(self):
        xml = ('<asSet xmlns="http://www.arin.net/regrws/core/v1"><name>AS-OTHER</name></asSet>')
        with self.assertRaises(ValueError):
            irr.parse_as_set(xml, 'AS-CR')


if __name__ == '__main__':
    unittest.main()
