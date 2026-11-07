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

ASN = re.compile(r"AS([0-9]+)$")
SET = re.compile(r"(?:AS[0-9]+:|AS-[A-Z0-9_-]+:)*AS-[A-Z0-9_-]+$")


def validate_set(value):
    if not isinstance(value, str) or not SET.fullmatch(value):
        raise ValueError(f"Invalid AS-SET: {value!r}")
    return value


DEFAULT_MAX_ASNS = 100


def load_config(path):
    cfg = json.loads(Path(path).read_text())
    validate_set(cfg['full_set'])
    validate_set(cfg['customer_set'])
    validate_set(cfg['target_set'])
    default = cfg.get('default_max_asns', DEFAULT_MAX_ASNS)
    # bool is an int subclass, so reject it explicitly before the range check.
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
    """Per-set cap: an explicit exception wins, otherwise the configured default."""
    return cfg.get('exceptions', {}).get(set_name, cfg.get('default_max_asns', DEFAULT_MAX_ASNS))


def direct_members(cfg, set_name):
    # IRRd A<n> counts BYTES, not lines. !! enables multiple commands and has no reply.
    with socket.create_connection((cfg['irr_host'], cfg.get('irr_port', 43)), timeout=30) as sock:
        sock.settimeout(30)
        sock.sendall(('!!\n!i' + set_name + '\n').encode('ascii'))
        with sock.makefile('rb') as stream:
            status = stream.readline().strip()
            if status == b'D':
                raise RuntimeError(f'AS-SET {set_name} missing from IRR')
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
    """Return accepted ASNs plus the excluded AS-SETs and the count of every child set."""
    accepted, excluded, counts = set(), [], []
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
        limit = asn_limit(cfg, member)
        counts.append((member, len(values), limit))
        if len(values) <= limit:
            accepted.update(values)
        else:
            excluded.append(f'{member} ({len(values)} ASNs, maximum {limit})')
    return accepted, excluded, counts


def own_asns(cfg):
    """ASNs of AS-CATHRIX excluding the target set, which only holds flattened customers."""
    asns = set()
    for member in direct_members(cfg, cfg['full_set']):
        match = ASN.fullmatch(member)
        if match:
            number = int(match[1])
            if not 0 < number < 4294967295:
                raise ValueError('Invalid ASN')
            asns.add(number)
        elif member == cfg['target_set']:
            continue
        else:
            # Own sets are expanded fully: their nested sets are ours, not customers.
            values = bgpq(cfg, ['-t'], [member])
            if any(type(n) is not int or not 0 < n < 4294967295 for n in values):
                raise ValueError('Invalid ASN response')
            asns.update(values)
    return asns


def prefixes(cfg, asns, family):
    networks = set()
    # Exact prefixes: deliberately omit -A, which can generate prefix-length ranges.
    asns = sorted(asns)
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


def result_report(generated_at, all_asns, v4, v6, excluded, previous=None):
    """previous holds the last run's counts, or None when there is nothing to compare."""
    previous = previous or {}
    lines = [f'Generated at {generated_at}', '',
             f'ASNs: {len(all_asns)} ({delta(len(all_asns), previous.get("asns"))})',
             f'IPv4 Prefixes: {len(v4)} ({delta(len(v4), previous.get("v4"))})',
             f'IPv6 Prefixes: {len(v6)} ({delta(len(v6), previous.get("v6"))})', '',
             'Excluded AS-SET:']
    lines += [f'  {item}' for item in excluded] if excluded else ['  none']
    return '\n'.join(lines) + '\n'


def previous_counts(path):
    """Read the counts of the last run from an existing result.txt, if any."""
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
    customers, excluded, counts = select_asns(cfg, direct_members(cfg, cfg['customer_set']),
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
    return {'customers': customers, 'own': own, 'all': all_asns}


def arin_request(method, path, key, body=None):
    request = urllib.request.Request('https://reg.arin.net/rest/irr/' + path,
                                     data=body, method=method, headers={'Authorization': 'ApiKey ' + key,
                                                                        'Accept': 'application/rpsl',
                                                                        'Content-Type': 'application/rpsl'})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode()
    except urllib.error.HTTPError as exc:
        if method == 'GET' and exc.code == 404:
            return None
        # Do not print request headers or raw API responses that may contain credentials.
        raise RuntimeError(f'ARIN {method} failed: HTTP {exc.code}') from None


def upload(cfg, customer_asns, own):
    """Publish the customer ASNs to the target set; own ASNs must never appear there."""
    # AS-CR holds only flattened customer ASNs; own ASNs stay in AS-CATHRIX.
    asns = sorted(customer_asns)
    if not asns or any(type(n) is not int or not 0 < n < 4294967295 for n in asns):
        raise ValueError('Refusing empty or invalid ASN set')
    leaked = sorted(set(asns) & set(own))
    if leaked:
        raise ValueError(f'Own ASNs must not be published to {cfg["target_set"]}: '
                         + ', '.join(f'AS{n}' for n in leaked))
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
        if not any(
                line.lower().startswith('as-set:') and line.partition(':')[2].strip().upper() == cfg['target_set'] for
                line in lines):
            raise ValueError('Unexpected ARIN object')
    else:
        arin = cfg['arin']
        fields = {'as-set': cfg['target_set'], 'descr': arin['description'],
                  'admin-c': arin['admin_c'], 'tech-c': arin['tech_c'],
                  'mnt-by': 'MNT-' + arin['org_handle'], 'source': 'ARIN'}
        if any('\n' in v or '\r' in v for v in fields.values()):
            raise ValueError('Invalid RPSL configuration')
        lines = [f'{k}: {v}' for k, v in fields.items()]
    # Members of the target set must be plain ASNs: never another AS-SET, which could
    # pull our own ASNs back in through AS-CATHRIX.
    lines += [f'members: AS{n}' for n in asns]
    payload = ('\n'.join(lines) + '\n').encode()
    path = 'as-set/' + name if current is not None else 'as-set?' + urllib.parse.urlencode(
        {'orgHandle': cfg['arin']['org_handle']})
    arin_request('PUT' if current is not None else 'POST', path, key, payload)
    print(f"Synchronized {cfg['target_set']} with {len(asns)} ASNs")


def sync(cfg, output, publish):
    """One IRR pass: write irr.conf/result.txt, then publish the same ASNs to ARIN."""
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
