#!/usr/bin/env python3
"""Generate bounded IRR customer filters and optionally synchronize ARIN."""
import argparse
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

ASN = re.compile(r"AS([0-9]+)$")
SET = re.compile(r"(?:AS[0-9]+:|AS-[A-Z0-9_-]+:)*AS-[A-Z0-9_-]+$")


def validate_set(value):
    if not isinstance(value, str) or not SET.fullmatch(value):
        raise ValueError(f"Invalid AS-SET: {value!r}")
    return value


def load_config(path):
    cfg = json.loads(Path(path).read_text())
    validate_set(cfg['source_set'])
    validate_set(cfg['target_set'])
    for name, limit in cfg.get('exceptions', {}).items():
        validate_set(name)
        if type(limit) is not int or limit <= 100:
            raise ValueError('Each exception must specify an integer maximum above 100')
    if cfg.get('default_max_asns', 100) != 100:
        raise ValueError('Default maximum must be 100; use explicit exceptions')
    return cfg


def direct_members(cfg):
    # IRRd A<n> counts BYTES, not lines. !! enables multiple commands and has no reply.
    with socket.create_connection((cfg['irr_host'], cfg.get('irr_port', 43)), timeout=30) as sock:
        sock.settimeout(30)
        sock.sendall(('!!\n!i' + cfg['source_set'] + '\n').encode('ascii'))
        with sock.makefile('rb') as stream:
            status = stream.readline().strip()
            if status == b'D':
                raise RuntimeError('Source AS-SET missing from IRR')
            if status == b'C':
                return []
            if not status.startswith(b'A') or not status[1:].isdigit():
                raise RuntimeError('Unexpected IRRd response')
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
    # Treat warnings as failures rather than publishing a potentially partial result.
    if result.stderr.strip():
        raise RuntimeError('bgpq4 reported: ' + result.stderr.strip())
    values = json.loads(result.stdout)['RESULT']
    if not isinstance(values, list):
        raise ValueError('Invalid bgpq4 response')
    return values


def select_asns(cfg, members, expand):
    accepted, rejected, report = set(), [], []
    for member in sorted(set(members)):
        match = ASN.fullmatch(member)
        if match:
            number = int(match[1])
            if not 0 < number < 4294967295:
                raise ValueError('Invalid ASN')
            accepted.add(number)
            continue
        validate_set(member)
        # -L 1 includes only the child SET's direct ASNs, never its nested SETs.
        values = expand(member)
        if any(type(n) is not int or not 0 < n < 4294967295 for n in values):
            raise ValueError('Invalid ASN response')
        values = set(values)
        limit = cfg.get('exceptions', {}).get(member, 100)
        entry = {'set': member, 'asn_count': len(values), 'maximum': limit,
                 'accepted': len(values) <= limit}
        report.append(entry)
        if entry['accepted']:
            accepted.update(values)
        else:
            rejected.append(f"{member}: {len(values)} ASNs exceeds maximum {limit}")
    return sorted(accepted), rejected, report


def prefixes(cfg, asns, family):
    networks = set()
    # Exact prefixes: deliberately omit -A, which can generate prefix-length ranges.
    for start in range(0, len(asns), 200):
        for entry in bgpq(cfg, [f'-{family}'], ['AS' + str(n) for n in asns[start:start+200]]):
            net = ipaddress.ip_network(entry['prefix'], strict=True)
            if net.version != family or entry.get('exact') is not True:
                raise ValueError('Unexpected prefix or prefix range')
            networks.add(net)
    return sorted(networks, key=lambda n: (int(n.network_address), n.prefixlen))


def bird_define(name, networks):
    if not networks:
        return f'define {name} = [];\n'
    return f'define {name} = [\n  ' + ',\n  '.join(map(str, networks)) + '\n];\n'


def generate(cfg, output):
    output.mkdir(parents=True, exist_ok=True)
    asns, rejected, report = select_asns(cfg, direct_members(cfg),
        lambda member: bgpq(cfg, ['-t', '-L', '1'], [member]))
    (output / 'rejected_sets.txt').write_text('\n'.join(rejected) + ('\n' if rejected else ''))
    (output / 'sets.json').write_text(json.dumps(report, indent=2) + '\n')
    if not asns:
        raise RuntimeError('No qualified ASNs; refusing to generate or publish an empty set')
    v4, v6 = prefixes(cfg, asns, 4), prefixes(cfg, asns, 6)
    (output / 'asns.txt').write_text(''.join(f'AS{n}\n' for n in asns))
    (output / 'irr.conf').write_text(bird_define('ALL_PREFIXES_V4', v4) + '\n' + bird_define('ALL_PREFIXES_V6', v6))
    (output / 'manifest.json').write_text(json.dumps({'config': cfg, 'asns': asns}, indent=2) + '\n')
    print(f'Generated {len(asns)} ASNs, {len(v4)} IPv4 and {len(v6)} IPv6 prefixes; rejected {len(rejected)} SETs')


def arin_request(method, path, key, body=None):
    request = urllib.request.Request('https://reg.arin.net/rest/irr/' + path,
        data=body, method=method, headers={'Authorization': 'ApiKey ' + key,
        'Accept': 'application/rpsl', 'Content-Type': 'application/rpsl'})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode()
    except urllib.error.HTTPError as exc:
        if method == 'GET' and exc.code == 404:
            return None
        # Do not print request headers or raw API responses that may contain credentials.
        raise RuntimeError(f'ARIN {method} failed: HTTP {exc.code}') from None


def upload(cfg, output):
    manifest = json.loads((output / 'manifest.json').read_text())
    if manifest['config'] != cfg:
        raise ValueError('Configuration changed since generation')
    asns = manifest['asns']
    if not asns or any(type(n) is not int or not 0 < n < 4294967295 for n in asns):
        raise ValueError('Refusing empty or invalid ASN set')
    key = os.environ.get('ARIN_API_KEY')
    if not key:
        raise ValueError('ARIN_API_KEY is required')
    name = urllib.parse.quote(cfg['target_set'], safe='')
    current = arin_request('GET', 'as-set/' + name, key)
    if current is not None:
        # Preserve existing contacts, description, and other metadata; replace membership.
        lines, drop = [], False
        for line in current.splitlines():
            if line[:1].isspace() or line.startswith('+'):
                if not drop:
                    lines.append(line)
                continue
            field = line.partition(':')[0].lower()
            drop = field in {'members', 'mbrs-by-ref', 'created', 'last-modified'} or line.startswith('%')
            if not drop and line.strip():
                lines.append(line)
        if not any(line.lower().startswith('as-set:') and line.partition(':')[2].strip().upper() == cfg['target_set'] for line in lines):
            raise ValueError('Unexpected ARIN object')
    else:
        arin = cfg['arin']
        fields = {'as-set': cfg['target_set'], 'descr': arin['description'],
                  'admin-c': arin['admin_c'], 'tech-c': arin['tech_c'],
                  'mnt-by': 'MNT-' + arin['org_handle'], 'source': 'ARIN'}
        if any('\n' in v or '\r' in v for v in fields.values()):
            raise ValueError('Invalid RPSL configuration')
        lines = [f'{k}: {v}' for k, v in fields.items()]
    lines += [f'members: AS{n}' for n in asns]
    payload = ('\n'.join(lines) + '\n').encode()
    path = 'as-set/' + name if current is not None else 'as-set?' + urllib.parse.urlencode({'orgHandle': cfg['arin']['org_handle']})
    arin_request('PUT' if current is not None else 'POST', path, key, payload)
    print(f"Synchronized {cfg['target_set']} with {len(asns)} ASNs")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['generate', 'upload'])
    parser.add_argument('--config', default='config/irr.json')
    parser.add_argument('--output', type=Path, default=Path('generated'))
    args = parser.parse_args()
    cfg = load_config(args.config)
    (generate if args.command == 'generate' else upload)(cfg, args.output)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)
