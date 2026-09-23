CREATE TABLE users (id integer PRIMARY KEY, email text NOT NULL);
CREATE TABLE coupons (id integer PRIMARY KEY, discount integer NOT NULL);
CREATE TABLE orders (
    id integer PRIMARY KEY,
    user_id integer NOT NULL REFERENCES users,
    coupon_id integer REFERENCES coupons
);
CREATE TABLE order_items (
    id integer PRIMARY KEY,
    order_id integer NOT NULL REFERENCES orders,
    amount integer NOT NULL
);
CREATE TABLE payments (
    id integer PRIMARY KEY,
    order_id integer NOT NULL REFERENCES orders,
    status text NOT NULL
);
INSERT INTO users SELECT i, 'user' || i || '@example.test' FROM generate_series(1, 3000) AS i;
INSERT INTO orders SELECT i, i, NULL FROM generate_series(1, 3000) AS i;
INSERT INTO order_items SELECT i, i, 100 FROM generate_series(1, 3000) AS i;
INSERT INTO payments SELECT i, i, 'paid' FROM generate_series(1, 3000) AS i;
INSERT INTO users VALUES (3001, 'bug@example.test');
INSERT INTO coupons VALUES (1, 150);
INSERT INTO orders VALUES (3001, 3001, 1);
INSERT INTO order_items VALUES (3001, 3001, 100);
INSERT INTO payments VALUES (3001, 3001, 'paid');
