#!/usr/bin/env python3
"""Generate bounded IRR customer filters and optionally synchronize ARIN."""
import argparse
import datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ASN = re.compile(r"AS([0-9]+)$")
SET = re.compile(r"(?:AS[0-9]+:|AS-[A-Z0-9_-]+:)*AS-[A-Z0-9_-]+$")
DEFAULT_MAX_ASNS = 100


def validate_set(value):
    if not isinstance(value, str) or not SET.fullmatch(value):
        raise ValueError(f"Invalid AS-SET: {value!r}")
    return value


def validate_asns(values):
    if any(type(n) is not int or not 0 < n < 4294967295 for n in values):
        raise ValueError('Invalid ASN')
    return values


def load_config(path):
    cfg = json.loads(Path(path).read_text())
    validate_set(cfg['full_set'])
    validate_set(cfg['customer_set'])
    validate_set(cfg['target_set'])
    default = cfg.get('default_max_asns', DEFAULT_MAX_ASNS)
    if isinstance(default, bool) or not isinstance(default, int) or default <= 0:
        raise ValueError('default_max_asns must be a positive integer')
    exceptions = cfg.get('exceptions', {})
    if not isinstance(exceptions, dict):
        raise ValueError('exceptions must map AS-SET names to limits')
    for name, limit in exceptions.items():
        validate_set(name)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= default:
            raise ValueError(f'Exception for {name} must exceed default_max_asns ({default})')
    return cfg


def asn_limit(cfg, set_name):
    return cfg.get('exceptions', {}).get(set_name, cfg.get('default_max_asns', DEFAULT_MAX_ASNS))


def direct_members(cfg, set_name):
    with socket.create_connection((cfg['irr_host'], cfg.get('irr_port', 43)), timeout=30) as sock:
        sock.sendall(('!!\n!i' + set_name + '\n').encode('ascii'))
        with sock.makefile('rb') as stream:
            status = stream.readline().strip()
            if status == b'D':
                raise RuntimeError(f'AS-SET {set_name} missing from IRR')
            if status == b'C':
                return []
            if not status.startswith(b'A') or not status[1:].isdigit():
                raise RuntimeError('Unexpected IRRd response')
            # IRRd replies "A<bytes>" followed by exactly that many bytes of data.
            size = int(status[1:])
            if size > 16 * 1024 * 1024:
                raise RuntimeError('IRRd response too large')
            data = stream.read(size)
            if len(data) != size or stream.readline().strip() != b'C':
                raise RuntimeError('Incomplete IRRd response')
            return data.decode('ascii').upper().split()


def bgpq(cfg, flags, objects):
    command = ['bgpq4', '-h', f"{cfg['irr_host']}:{cfg.get('irr_port', 43)}",
               '-j', '-l', 'RESULT', *flags, *objects]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=True)
    if result.stderr.strip():
        raise RuntimeError('bgpq4 reported: ' + result.stderr.strip())
    try:
        values = json.loads(result.stdout)['RESULT']
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f'bgpq4 returned unexpected output for {objects}: {exc}') from None
    if not isinstance(values, list):
        raise ValueError('Invalid bgpq4 response')
    return values


def select_asns(cfg, members, expand):
    accepted, excluded, counts = set(), [], []
    for member in sorted(set(members)):
        match = ASN.fullmatch(member)
        if match:
            accepted.add(validate_asns([int(match[1])])[0])
            continue
        validate_set(member)
        # Only the child SET's own ASNs are counted; nested SETs are not expanded.
        values = set(validate_asns(expand(member)))
        limit = asn_limit(cfg, member)
        counts.append((member, len(values), limit))
        if len(values) <= limit:
            accepted.update(values)
        else:
            excluded.append(f'{member} ({len(values)} ASNs, maximum {limit})')
    return accepted, excluded, counts


def own_asns(cfg):
    asns = set()
    for member in direct_members(cfg, cfg['full_set']):
        match = ASN.fullmatch(member)
        if match:
            asns.add(validate_asns([int(match[1])])[0])
        elif member == cfg['target_set']:
            continue
        else:
            asns.update(validate_asns(bgpq(cfg, ['-t'], [member])))
    return asns


def prefixes(cfg, asns, family):
    networks = set()
    asns = sorted(asns)
    # Exact prefixes only; -A would emit prefix-length ranges.
    for start in range(0, len(asns), 200):
        for entry in bgpq(cfg, [f'-{family}'], ['AS' + str(n) for n in asns[start:start + 200]]):
            net = ipaddress.ip_network(entry['prefix'], strict=True)
            if net.version != family or entry.get('exact') is not True:
                raise ValueError('Unexpected prefix or prefix range')
            networks.add(net)
    return sorted(networks, key=lambda n: (int(n.network_address), n.prefixlen))


def bird_define(name, values):
    if not values:
        return f'define {name} = [];\n'
    return f'define {name} = [\n  ' + ',\n  '.join(map(str, values)) + '\n];\n'


def irr_conf(all_asns, v4, v6, generated_at):
    return (f'# Generated at {generated_at}\n\n'
            + bird_define('ALL_ASNS', [f'AS{n}' for n in all_asns]) + '\n'
            + bird_define('ALL_PREFIXES_V4', v4) + '\n'
            + bird_define('ALL_PREFIXES_V6', v6))


def delta(current, previous):
    if previous is None:
        return '+0'
    change = current - previous
    return f'+{change}' if change >= 0 else str(change)


def result_report(generated_at, all_asns, v4, v6, excluded, previous):
    previous = previous or {}
    lines = [f'Generated at {generated_at}', '',
             f'ASNs: {len(all_asns)} ({delta(len(all_asns), previous.get("asns"))})',
             f'IPv4 Prefixes: {len(v4)} ({delta(len(v4), previous.get("v4"))})',
             f'IPv6 Prefixes: {len(v6)} ({delta(len(v6), previous.get("v6"))})', '',
             'Excluded AS-SET:']
    lines += [f'  {item}' for item in excluded] if excluded else ['  none']
    return '\n'.join(lines) + '\n'


def previous_counts(path):
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    found = {}
    for key, label in (('asns', 'ASNs'), ('v4', 'IPv4 Prefixes'), ('v6', 'IPv6 Prefixes')):
        match = re.search(rf'^{label}: (\d+)', text, re.MULTILINE)
        if not match:
            return None
        found[key] = int(match.group(1))
    return found


def generate(cfg, output):
    output.mkdir(parents=True, exist_ok=True)
    previous = previous_counts(output / 'result.txt')
    customers, excluded, _ = select_asns(cfg, direct_members(cfg, cfg['customer_set']),
                                         lambda member: bgpq(cfg, ['-t', '-L', '1'], [member]))
    if not customers:
        raise RuntimeError('No qualified customer ASNs; refusing to generate or publish an empty set')
    own = own_asns(cfg)
    all_asns = sorted(customers | own)
    v4, v6 = prefixes(cfg, all_asns, 4), prefixes(cfg, all_asns, 6)
    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    (output / 'irr.conf').write_text(irr_conf(all_asns, v4, v6, generated_at))
    (output / 'result.txt').write_text(
        result_report(generated_at, all_asns, v4, v6, excluded, previous))
    print(f'Generated {len(all_asns)} ASNs, {len(v4)} IPv4 and {len(v6)} IPv6 prefixes; '
          f'excluded {len(excluded)} AS-SETs')
    return {'customers': customers, 'own': own}


def arin_error(exc):
    try:
        body = exc.read().decode('utf-8', 'replace')
    except (OSError, ValueError):
        return f'HTTP {exc.code}'
    code = re.search(r'code\s*:?\s*>?\s*([A-Z][A-Z_]+)', body)
    message = re.search(r'message\s*:?\s*>?\s*([^<\n]+)', body)
    parts = [f'HTTP {exc.code}']
    if code:
        parts.append(code.group(1).strip())
    if message:
        parts.append(message.group(1).strip())
    return ' '.join(parts)


ARIN_NS = 'http://www.arin.net/regrws/core/v1'
POC_LABELS = {'AD': 'Admin', 'T': 'Tech', 'R': 'Routing'}


def arin_request(method, path, key, body=None):
    # ARIN only serves application/xml.
    headers = {'Authorization': 'ApiKey ' + key, 'Accept': 'application/xml'}
    if body is not None:
        headers['Content-Type'] = 'application/xml'
    request = urllib.request.Request('https://reg.arin.net/rest/irr/' + path,
                                     data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode()
    except urllib.error.HTTPError as exc:
        if method == 'GET' and exc.code == 404:
            return None
        raise RuntimeError(f'ARIN {method} failed: {arin_error(exc)}') from None


def parse_as_set(xml_text, expected_name):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f'ARIN returned unparsable XML: {exc}') from None

    def tag(child):
        return f'{{{ARIN_NS}}}{child}'

    name = root.findtext(tag('name'))
    if (name or '').strip().upper() != expected_name.upper():
        raise ValueError(f'ARIN returned {name!r} instead of {expected_name!r}')
    return {
        'org_handle': (root.findtext(tag('orgHandle')) or '').strip(),
        'description': [(line.text or '') for line in root.findall(f'{tag("description")}/{tag("line")}')],
        'pocs': [(p.get('function'), p.get('handle')) for p in root.findall(f'{tag("pocLinks")}/{tag("pocLinkRef")}')],
        'remarks': [(line.text or '') for line in root.findall(f'{tag("remarks")}/{tag("line")}')],
    }


def as_set_xml(name, org_handle, description, pocs, remarks, asns):
    def lines(parent, tag_name, values):
        if values:
            block = ET.SubElement(parent, f'{{{ARIN_NS}}}{tag_name}')
            for index, value in enumerate(values):
                ET.SubElement(block, f'{{{ARIN_NS}}}line', {'number': str(index)}).text = value

    ET.register_namespace('', ARIN_NS)
    root = ET.Element(f'{{{ARIN_NS}}}asSet')
    ET.SubElement(root, f'{{{ARIN_NS}}}orgHandle').text = org_handle
    lines(root, 'description', description)
    if pocs:
        links = ET.SubElement(root, f'{{{ARIN_NS}}}pocLinks')
        for function, handle in pocs:
            ET.SubElement(links, f'{{{ARIN_NS}}}pocLinkRef',
                          {'description': POC_LABELS.get(function, function),
                           'function': function, 'handle': handle})
    lines(root, 'remarks', remarks)
    ET.SubElement(root, f'{{{ARIN_NS}}}source').text = 'ARIN'
    members = ET.SubElement(root, f'{{{ARIN_NS}}}members')
    for asn in asns:
        ET.SubElement(members, f'{{{ARIN_NS}}}member', {'name': f'AS{asn}'})
    ET.SubElement(root, f'{{{ARIN_NS}}}name').text = name
    # ARIN's XML payloads are ASCII-only; reject anything else instead of sending escapes.
    body = ET.tostring(root, encoding='unicode').encode('ascii')
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + body


def upload(cfg, customer_asns, own):
    asns = sorted(customer_asns)
    if not asns:
        raise ValueError('Refusing empty ASN set')
    validate_asns(asns)
    leaked = sorted(set(asns) & set(own))
    if leaked:
        raise ValueError(f'Own ASNs must not be published to {cfg["target_set"]}: '
                         + ', '.join(f'AS{n}' for n in leaked))
    key = os.environ.get('ARIN_API_KEY')
    if not key:
        raise ValueError('ARIN_API_KEY is required')
    name = cfg['target_set']
    path = 'as-set/' + urllib.parse.quote(name, safe='')
    current = arin_request('GET', path, key)
    if current is not None:
        meta = parse_as_set(current, name)
        org_handle = meta['org_handle'] or cfg['arin']['org_handle']
        description, pocs, remarks = meta['description'], meta['pocs'], meta['remarks']
    else:
        arin = cfg['arin']
        org_handle = arin['org_handle']
        description = [arin['description']]
        pocs = [('T', arin['tech_c']), ('AD', arin['admin_c'])]
        remarks = []
    payload = as_set_xml(name, org_handle, description, pocs, remarks, asns)
    if current is None:
        path = 'as-set?' + urllib.parse.urlencode({'orgHandle': org_handle})
    arin_request('PUT' if current is not None else 'POST', path, key, payload)
    print(f"Synchronized {name} with {len(asns)} ASNs")


def sync(cfg, output, publish):
    state = generate(cfg, output)
    if publish:
        upload(cfg, state['customers'], state['own'])
    else:
        print('Skipping ARIN upload (not enabled for this run)')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['sync', 'generate'])
    parser.add_argument('--config', default='config/irr.json')
    parser.add_argument('--output', type=Path, default=Path('generated'))
    parser.add_argument('--publish', action='store_true',
                        help='also synchronize the target AS-SET with ARIN')
    args = parser.parse_args()
    cfg = load_config(args.config)
    if args.command == 'generate':
        generate(cfg, args.output)
    else:
        sync(cfg, args.output, args.publish)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)
