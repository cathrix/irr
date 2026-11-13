# IRR Config Generation

Generates customer ASNs and BIRD prefix configuration from `AS-CATHRIX-CUST`, and synchronizes them to ARIN's `AS-CR`.

## Algorithm

1. Read the direct members of `AS-CATHRIX-CUST`.
2. Keep only direct ASNs.
3. Read the own members of `AS-CATHRIX`, skip `AS-CR`, and merge them with the customer ASNs.
4. Look up exact prefixes for the merged ASNs.
5. Write `irr.conf` and `result.txt`, then publish the customer ASNs to `AS-CR`.

## Parameters

`config/irr.json`:

| Parameter          | Description                                             |
|--------------------|---------------------------------------------------------|
| `full_set`         | Own AS-SET, default `AS-CATHRIX`                        |
| `customer_set`     | Customer AS-SET, default `AS-CATHRIX-CUST`              |
| `target_set`       | Target AS-SET, default `AS-CR`                          |
| `irr_host`         | IRRd server, default `rr.ntt.net`                       |
| `irr_port`         | IRRd port, default `43`                                 |

Environment variables:

| Variable       | Description                           |
|----------------|---------------------------------------|
| `ARIN_API_KEY` | ARIN API key, required for publishing |

## Commands

```sh
python3 scripts/irr.py sync                  # generate only
python3 scripts/irr.py sync --publish        # generate and publish to ARIN
python3 -m unittest discover -s tests        # run tests
```

## Output

- `irr.conf`: `ALL_ASNS`, `ALL_PREFIXES_V4`, `ALL_PREFIXES_V6`
- `result.txt`: counts and rejected AS-SET members
