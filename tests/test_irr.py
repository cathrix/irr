import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

spec = importlib.util.spec_from_file_location('irr', Path(__file__).resolve().parents[1] / 'scripts/irr.py')
irr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(irr)


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = irr.load_config(Path(__file__).resolve().parents[1] / 'config/irr.json')

    def test_threshold_and_bounded_exception(self):
        data = {'AS-EXACT': list(range(1, 101)), 'AS-LARGE': list(range(1, 102)),
                'AS-MOEDOVE': list(range(1, 501))}
        asns, rejected, report = irr.select_asns(self.cfg, list(data), data.__getitem__)
        self.assertEqual(len(asns), 500)
        self.assertEqual(len(rejected), 1)
        data['AS-MOEDOVE'].append(501)
        asns, rejected, report = irr.select_asns(self.cfg, list(data), data.__getitem__)
        self.assertEqual(len(asns), 100)
        self.assertEqual(len(rejected), 2)

    def test_depth_is_explicit_and_prefixes_are_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            def bgpq(cfg, flags, objects):
                if '-t' in flags:
                    self.assertEqual(flags, ['-t', '-L', '1'])
                    self.assertEqual(objects, ['AS-CHILD'])
                    return [150289]
                self.assertEqual(objects, ['AS112', 'AS150289'])
                self.assertNotIn('-A', flags)
                return [{'prefix': '192.0.2.0/24', 'exact': True}] if '-4' in flags else []
            with patch.object(irr, 'direct_members', return_value=['AS112', 'AS-CHILD']), patch.object(irr, 'bgpq', side_effect=bgpq):
                irr.generate(self.cfg, output)
            self.assertEqual((output / 'asns.txt').read_text(), 'AS112\nAS150289\n')
            self.assertIn('define ALL_PREFIXES_V6 = [];', (output / 'irr.conf').read_text())

    def test_empty_result_is_not_published(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(irr, 'direct_members', return_value=[]):
                with self.assertRaises(RuntimeError):
                    irr.generate(self.cfg, Path(directory))

    def test_irrd_byte_framing(self):
        data = b'AS-CHILD AS112\n'
        import io
        stream = io.BytesIO(b'A' + str(len(data)).encode() + b'\n' + data + b'C\n')
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.makefile.return_value = stream
        with patch.object(irr.socket, 'create_connection', return_value=connection):
            self.assertEqual(irr.direct_members(self.cfg), ['AS-CHILD', 'AS112'])
        connection.sendall.assert_called_once_with(b'!!\n!iAS-CATHRIX-CUST\n')

    def test_upload_replaces_members_and_removes_implicit_membership(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / 'manifest.json').write_text(json.dumps({'config': self.cfg, 'asns': [112]}))
            current = 'as-set: AS-CR\ndescr: Existing description\nmembers: AS-OLD\n AS1\nmbrs-by-ref: ANY\nmnt-by: MNT-CN-578\nsource: ARIN\n'
            with patch.dict(irr.os.environ, {'ARIN_API_KEY': 'test'}), patch.object(irr, 'arin_request', side_effect=[current, 'ok']) as api:
                irr.upload(self.cfg, output)
            method, path, key, payload = api.call_args.args
            self.assertEqual((method, path), ('PUT', 'as-set/AS-CR'))
            self.assertIn(b'descr: Existing description', payload)
            self.assertIn(b'members: AS112', payload)
            self.assertNotIn(b'ANY', payload)
            self.assertNotIn(b'AS-OLD', payload)

    def test_create_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / 'manifest.json').write_text(json.dumps({'config': self.cfg, 'asns': [112]}))
            with patch.dict(irr.os.environ, {'ARIN_API_KEY': 'test'}), patch.object(irr, 'arin_request', side_effect=[None, 'ok']) as api:
                irr.upload(self.cfg, output)
            self.assertEqual(api.call_args.args[:2], ('POST', 'as-set?orgHandle=CN-578'))


if __name__ == '__main__':
    unittest.main()
