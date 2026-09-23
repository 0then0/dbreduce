"""Exit 1 only when a paid order has a negative total after applying its coupon."""

import os
import sys

import psycopg

with psycopg.connect(os.environ["DBREDUCE_DATABASE_URL"]) as conn:
    bug = conn.execute("""
        SELECT EXISTS (
            SELECT o.id FROM orders o
            JOIN users u ON u.id = o.user_id
            JOIN coupons c ON c.id = o.coupon_id
            JOIN order_items i ON i.order_id = o.id
            WHERE EXISTS (SELECT 1 FROM payments p
                          WHERE p.order_id = o.id AND p.status = 'paid')
            GROUP BY o.id, c.discount
            HAVING sum(i.amount) - c.discount < 0
        )
    """).fetchone()[0]
sys.exit(1 if bug else 0)
