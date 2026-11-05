"""Plan or create the Razorpay TEST orders that become the agent's test payments.

    python scripts/razorpay_seed.py                  # dry run: shows the plan and what already exists (read-only)
    python scripts/razorpay_seed.py --create         # creates the missing orders (a write to your TEST account)

Afterwards open results/seed/checkout.html through a local web server and pay each order with the UPI id shown
(see docs/LIVE_DATA.md). Reads RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET from the environment or a gitignored .env.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from acp.assure.razorpay import RazorpayError  # noqa: E402
from acp.assure.razorpay_seed import RazorpaySeeder, render_checkout_page  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--create", action="store_true", help="actually create the missing orders (default is a dry run)")
    args = ap.parse_args(argv)
    load_dotenv(ROOT / ".env")
    key, secret = os.environ.get("RAZORPAY_KEY_ID", ""), os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not key or not secret:
        print("Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET (test-mode keys) first.")
        return 2
    try:
        seeder = RazorpaySeeder(key, secret)
        out = seeder.seed(create=args.create)
    except RazorpayError as e:
        print(f"Razorpay error: {e}")
        return 1
    print(
        f"{'Created' if args.create else 'Would create'}: "
        f"{sum(1 for r in out['rows'] if r['order_id'] is None) if not args.create else out['created']} order(s); "
        f"{out['existing']} already existed.\n"
    )
    for r in out["rows"]:
        o = r["order"]
        state = r["order_id"] or ("(will be created)" if not args.create else "(missing)")
        rupees = f"{o.amount_paise / 100:>10,.2f}"
        print(f"  {o.key:<8} {o.tenant:<7} ₹{rupees}  pay with {o.outcome + '@razorpay':<17} {state}  {o.why}")
    if not args.create:
        print("\nDry run only: nothing was written. Re-run with --create to create the orders.")
        return 0
    page = ROOT / "results" / "seed" / "checkout.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(render_checkout_page(key, out["rows"]))
    print(f"\nWrote {page.relative_to(ROOT)}. Serve it locally:  python -m http.server 8766 -d results/seed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
