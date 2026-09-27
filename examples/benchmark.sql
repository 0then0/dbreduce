-- psql loader: reuse the complete relational demo and extend its unrelated checkouts.
\ir fixture.sql
INSERT INTO users SELECT i, 'user' || i || '@example.test' FROM generate_series(3002, 30001) AS i;
INSERT INTO orders SELECT i, i, NULL FROM generate_series(3002, 30001) AS i;
INSERT INTO order_items SELECT i, i, 100 FROM generate_series(3002, 30001) AS i;
INSERT INTO payments SELECT i, i, 'paid' FROM generate_series(3002, 30001) AS i;
ANALYZE;
