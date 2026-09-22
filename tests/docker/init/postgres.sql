CREATE TABLE customers (
  id SERIAL PRIMARY KEY,
  name VARCHAR(120) NOT NULL,
  tier VARCHAR(20) NOT NULL,
  signup_date DATE NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT TRUE
);
COMMENT ON TABLE customers IS 'Customer master data';
COMMENT ON COLUMN customers.tier IS 'gold/silver/bronze';

CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  customer_id INT NOT NULL REFERENCES customers(id),
  region VARCHAR(20) NOT NULL,
  amount NUMERIC(10,2) NOT NULL,
  placed_at TIMESTAMP NOT NULL DEFAULT now(),
  is_paid BOOLEAN NOT NULL DEFAULT FALSE
);
COMMENT ON TABLE orders IS 'Orders placed by customers';
COMMENT ON COLUMN orders.amount IS 'Order total in USD';

INSERT INTO customers (name, tier, signup_date, is_active) VALUES
('Acme Corp', 'gold', '2021-03-01', TRUE),
('Globex', 'silver', '2021-06-15', TRUE),
('Initech', 'bronze', '2022-01-10', TRUE),
('Umbrella LLC', 'gold', '2020-11-20', FALSE),
('Soylent Inc', 'silver', '2022-08-02', TRUE),
('Hooli', 'gold', '2019-05-05', TRUE),
('Vehement Capital', 'bronze', '2023-02-14', TRUE),
('Massive Dynamic', 'silver', '2021-09-09', TRUE),
('Stark Industries', 'gold', '2018-12-01', TRUE),
('Wayne Enterprises', 'gold', '2017-07-04', TRUE);

INSERT INTO orders (customer_id, region, amount, placed_at, is_paid)
SELECT c.id,
      (ARRAY['US','EU','APAC','LATAM'])[1 + (o % 4)],
      ROUND((10 + random() * 990)::numeric, 2),
      TIMESTAMP '2023-01-01' + (o || ' days')::interval,
      (o % 3) != 0
FROM customers c, generate_series(1, 6) o;
