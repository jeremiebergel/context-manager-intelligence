"""Tabulate bench/cost_replay.py outputs: one or more JSONL files (one per engine version), side by side.

    python bench/cost_report.py base.jsonl new.jsonl [--mech]
"""
import collections
import json
import sys


def read(p):
    return {d["session"]: d for d in map(json.loads, open(p))}


def bucket(n):
    return "1-10" if n <= 10 else "11-30" if n <= 30 else "31-80" if n <= 80 else "81+"


def main():
    files = [a for a in sys.argv[1:] if not a.startswith("--")]
    runs = [read(f) for f in files]
    sids = [s for s in runs[0] if all(s in r for r in runs)]
    hdr = f"{'session':24} {'prov':6} {'req':>4} {'ttl':>3} {'raw':>9} " + " ".join(
        f"{'v%d' % i:>7} {'>raw':>4}" for i in range(len(runs)))
    print(hdr)
    agg = collections.defaultdict(lambda: [0.0] * (len(runs) + 1))
    for s in sorted(sids, key=lambda s: -runs[0][s]["requests"]):
        d0 = runs[0][s]
        row = f"{s:24} {d0['provider'][:6]:6} {d0['requests']:>4} {d0['ttl_expired']:>3} {d0['cost_raw']:>9} "
        row += " ".join(f"{100 * r[s]['delta']:>+6.1f}% {r[s]['req_over_raw']:>4}" for r in runs)
        print(row)
        for key in ("ALL", bucket(d0["requests"]), d0["provider"] or "?"):
            a = agg[key]
            a[0] += d0["cost_raw"]
            for i, r in enumerate(runs):
                a[i + 1] += r[s]["cost_cmi"]
    print("\naggregate (cost-weighted) delta vs raw")
    for k in sorted(agg):
        a = agg[k]
        print(f"  {k:14} " + "  ".join(f"v{i}:{100 * (a[i + 1] / a[0] - 1):+.1f}%" for i in range(len(runs))))
    for i, r in enumerate(runs):
        ds = [r[s]["delta"] for s in sids]
        print(f"v{i}: sessions={len(ds)} worse_than_raw>2%={sum(d > 0.02 for d in ds)} >0={sum(d > 0.0005 for d in ds)}"
              f" max={100 * max(ds):+.1f}% requests_over_raw={sum(r[s]['req_over_raw'] for s in sids)}")
    if "--mech" in sys.argv:
        for i, r in enumerate(runs):
            m = collections.defaultdict(collections.Counter)
            for s in sids:
                for k, v in r[s]["breaks"].items():
                    m[k].update(v)
            print(f"\nv{i} prefix breaks by trigger (premium/gain in input-token equivalents)")
            for k, v in sorted(m.items()):
                if not k.startswith("change:"):
                    print(f"  {k:20} n={v['n']:>4} rewrite={v['rewrite_tok']:>9.0f} premium={v['premium']:>10.0f}"
                          f" removed={v['removed_tok']:>8.0f} realised_gain={v['gain']:>10.0f} net_negative={v['net_negative']}")
            print("  changes:", {k[7:]: v["n"] for k, v in sorted(m.items()) if k.startswith("change:")})


if __name__ == "__main__":
    main()
