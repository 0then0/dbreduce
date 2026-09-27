"""Structured verdict for a paid order with a negative total after its coupon."""

import json
import os

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
print(json.dumps({"reproduced": bug, "signature": "checkout-negative-total"}))
